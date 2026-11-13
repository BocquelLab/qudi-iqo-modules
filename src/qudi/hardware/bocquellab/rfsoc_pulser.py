# -*- coding: utf-8 -*-
"""
Qudi hardware module that uses the BocquelLab RFSoC4x2 (QICK) as pulse generator for the pulsed toolchain:
digital channels on PMOD pins (laser gate, marker for the gate input of the counter) and one "digital"
channel that gates the microwave generator.

This is a thin adapter. The program that plays the pattern (rfsoc_nv.PulseSequencer) lives in the RFSoC-NV
repository, the connection in the board module (bocquellab.rfsoc_qick_board.RFSoCQickBoard).

Copyright (c) 2021, the qudi developers. See the AUTHORS.md file at the top-level directory of this
distribution and on <https://github.com/Ulm-IQO/qudi-iqo-modules/>

This file is part of qudi.

Qudi is free software: you can redistribute it and/or modify it under the terms of
the GNU Lesser General Public License as published by the Free Software Foundation,
either version 3 of the License, or (at your option) any later version.

Qudi is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY;
without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
See the GNU Lesser General Public License for more details.

You should have received a copy of the GNU Lesser General Public License along with qudi.
If not, see <https://www.gnu.org/licenses/>.
"""

__all__ = ['RFSoCPulser']

import numpy as np

from qudi.core.configoption import ConfigOption
from qudi.core.connector import Connector
from qudi.util.mutex import Mutex
from qudi.interface.pulser_interface import PulserInterface, PulserConstraints, SequenceOption


class RFSoCPulser(PulserInterface):
    """ Pulse generator on the RFSoC4x2 / QICK: digital channels only.

    - channels: each qudi digital channel is either a PMOD pin ('PMOD0_0' .. 'PMOD0_7', 3.3 V) or
      'microwave': while that channel is high, the generator plays a rectangular pulse at the microwave
      frequency and gain of this module. The microwave is not an analog channel: in the pulsed GUI, set
      the microwave channel to that digital channel; phases cannot be chosen (the predefined Rabi, Ramsey
      and Hahn echo work with a digital microwave channel).
    - Microwave frequency and gain: set_microwave(frequency, gain), normally through the module
      bocquellab.rfsoc_pulsed_microwave ("ext. microwave" fields of the pulsed GUI, power in dBm). They are
      constant during a sequence. pulser_on() is refused while a loaded sequence has microwave pulses and
      no frequency / gain has been set.
    - max_gain (mandatory): hard limit of the generator gain in DAC units. A higher gain is refused
      (nothing is clipped); the limit of the board module applies as well and is checked on the compiled
      program. WARNING: generator 0 is meant to drive the Mini-Circuits ZHL-10M4G21W0+ amplifier (RF input
      absolute maximum +5 dBm).
    - Timing: sample rate 307.2 MHz, i.e. a time grid of 3.2552 ns (the tProc clock). Two changes of the
      PMOD pins need at least 3 samples (9.8 ns) between them; a microwave pulse is at least 3 samples long.
      The waveform repeats without end and without gap until pulser_off(). The delay between the
      microwave output and the PMOD pins is constant but has not been measured.
    - Memory: the program holds 1024 instructions: about 1 per change of the PMOD pins and 2 to 3 per
      microwave pulse (about 200 Rabi points). A sequence that does not fit, or that changes its outputs
      faster than the processor can follow, is refused when it is loaded.
    - No sequence mode, no analog channels, one waveform loaded at a time.
    - pulser_off() and deactivation switch all outputs off (generators silent, PMOD pins low). While the
      pulser runs, this module holds the board: the RFSoC microwave source (CW ODMR) is refused.

    Example config for copy-paste:

    rfsoc_pulser:
        module.Class: 'bocquellab.rfsoc_pulser.RFSoCPulser'
        options:
            channels:  # qudi channel: 'PMOD0_<n>' or 'microwave'
                d_ch1: 'PMOD0_0'  # laser gate (Cobolt digital modulation input)
                d_ch2: 'microwave'  # generator 0 plays while this channel is high
                d_ch3: 'PMOD0_1'  # marker to the gate input of the NI counter (PFI1)
            generator_channel: 0  # 0 = DAC_B
            nyquist_zone: 2  # 2: 2457.6 .. 4915.2 MHz (default, not verified on the setup yet)
            max_gain: 2000  # DAC units, hard limit (full scale 32766)
        connect:
            board: rfsoc_board
    """

    _board = Connector(name='board', interface='RFSoCQickBoard')

    _channels = ConfigOption(name='channels', missing='error')
    _generator_channel = ConfigOption(name='generator_channel', default=0, constructor=lambda x: int(x))
    _nyquist_zone = ConfigOption(name='nyquist_zone', default=2, constructor=lambda x: int(x))
    _max_gain = ConfigOption(name='max_gain', missing='error', constructor=float)

    _FULL_SCALE_GAIN = 32766
    _HIGH_LEVEL = 3.3  # V, PMOD outputs (LVCMOS33)
    _MAX_EDGES = 8192  # level changes kept per channel and waveform; far more than the board can play
    _MAX_SAMPLES = 2 ** 28  # 0.87 s

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._lock = Mutex()
        self._sequencer = None
        self._gain_limit = 0
        self._pins = dict()  # qudi channel -> index of the PMOD pin
        self._mw_channel = None
        self._waveforms = dict()  # name -> (number of samples, {channel: [(on, off), ...]})
        self._writing = None  # waveform being written in chunks
        self._loaded = ''
        self._status = 0
        self._mw_frequency = None
        self._mw_gain = None
        self._info = dict()

    # ---------------------------------------------------------------- module lifecycle
    def on_activate(self):
        if not 0 < self._max_gain <= self._FULL_SCALE_GAIN:
            raise ValueError(f'max_gain must be in ]0, {self._FULL_SCALE_GAIN}], got {self._max_gain}.')
        board = self._board()
        from rfsoc_nv import PulseSequencer  # lab package: imported here so that qudi starts without it

        self._gain_limit = int(min(self._max_gain, board.max_gain))
        if self._max_gain > board.max_gain:
            self.log.warning(f'max_gain {self._max_gain:.0f} is above the limit of the board module '
                             f'({board.max_gain:.0f}); the board limit applies.')
        self._sequencer = PulseSequencer(board.board, gen_ch=self._generator_channel, nqz=self._nyquist_zone)
        names = self._sequencer.pin_names
        self._pins, self._mw_channel = dict(), None
        for channel, target in dict(self._channels).items():
            channel, target = str(channel), str(target)
            if not (channel.startswith('d_ch') and channel[4:].isdigit()):
                raise ValueError(f'Channel name "{channel}" is not of the form d_ch<number>.')
            if target.lower() == 'microwave':
                if self._mw_channel is not None:
                    raise ValueError('Only one channel can be "microwave".')
                self._mw_channel = channel
            elif target in names:
                if names.index(target) in self._pins.values():
                    raise ValueError(f'{target} is used by two channels.')
                self._pins[channel] = names.index(target)
            else:
                raise ValueError(f'Channel {channel}: "{target}" is neither "microwave" nor one of {names}.')
        if not self._pins and self._mw_channel is None:
            raise ValueError('No channel configured.')
        self._waveforms, self._writing, self._loaded = dict(), None, ''
        self._status = 0
        self._mw_frequency, self._mw_gain = None, None
        self._info = dict()
        wiring = ', '.join(f'{ch} = {names[pin]}' for ch, pin in sorted(self._pins.items()))
        if self._mw_channel is not None:
            wiring += f', {self._mw_channel} = microwave (generator {self._generator_channel}, gain limit ' \
                      f'{self._gain_limit})'
        self.log.info(f'RFSoC pulser: {wiring}; time grid {1e9 / self._sequencer.clock_frequency:.4f} ns.')

    def on_deactivate(self):
        """ Outputs off. Never raises. """
        try:
            self.pulser_off()
        except Exception:
            self.log.exception('Could not switch the RFSoC pulser off:')
        self._sequencer = None

    # ---------------------------------------------------------------- microwave settings
    @property
    def gain_limit(self):
        """ Highest generator gain this module accepts (DAC units). """
        return self._gain_limit

    @property
    def microwave_frequency_limits(self):
        """ (min, max) microwave frequency in Hz for the configured Nyquist zone. """
        return self._sequencer.frequency_limits

    @property
    def microwave_settings(self):
        """ (frequency in Hz as played, gain in DAC units) of the microwave pulses, or (None, None). """
        return self._mw_frequency, self._mw_gain

    def set_microwave(self, frequency, gain):
        """ Frequency (Hz) and gain (DAC units) of the microwave pulses; used at the next pulser_on().
        Raises ValueError for a gain above the limit or a frequency outside the Nyquist zone (nothing is
        changed in that case).

        @return (float, int): frequency as the generator plays it, gain
        """
        with self._lock:
            gain = int(round(gain))
            if not 0 < gain <= self._gain_limit:
                raise ValueError(f'Microwave gain {gain} is outside ]0, {self._gain_limit}] (max_gain, protects '
                                 f'the amplifier). Nothing was changed.')
            gain = self._board().board.check_gain(gain)  # limit of the board module
            frequency = self._sequencer.round_frequency(frequency)
            self._mw_frequency, self._mw_gain = frequency, gain
            return frequency, gain

    def clear_microwave(self):
        """ Forget the microwave settings: sequences with microwave pulses are refused until set again. """
        with self._lock:
            self._mw_frequency, self._mw_gain = None, None

    @property
    def sequence_info(self):
        """ Facts about the sequence compiled last (instructions, outputs per repetition, duration, ...). """
        return dict(self._info)

    def completed_repetitions(self):
        """ Number of repetitions of the waveform played since pulser_on() (read from the board). """
        with self._lock:
            return self._sequencer.passes() if self._status == 1 else 0

    # ---------------------------------------------------------------- PulserInterface
    def get_constraints(self):
        constraints = PulserConstraints()
        rate = self._sequencer.clock_frequency
        constraints.sample_rate.min = rate
        constraints.sample_rate.max = rate
        constraints.sample_rate.step = 0
        constraints.sample_rate.default = rate
        constraints.d_ch_low.min = 0.0
        constraints.d_ch_low.max = 0.0
        constraints.d_ch_low.step = 0.0
        constraints.d_ch_low.default = 0.0
        constraints.d_ch_high.min = self._HIGH_LEVEL
        constraints.d_ch_high.max = self._HIGH_LEVEL
        constraints.d_ch_high.step = 0.0
        constraints.d_ch_high.default = self._HIGH_LEVEL
        constraints.waveform_length.min = 64
        constraints.waveform_length.max = self._MAX_SAMPLES
        constraints.waveform_length.step = 1
        constraints.waveform_length.default = 64
        constraints.activation_config = {'all': frozenset(self._all_channels())}
        constraints.sequence_option = SequenceOption.NON
        return constraints

    def pulser_on(self):
        """ Start the loaded waveform from its beginning; it repeats until pulser_off().

        @return int: error code (0: OK, -1: error, logged)
        """
        with self._lock:
            if self._status == 1:
                return 0
            try:
                if not self._loaded:
                    raise RuntimeError('No waveform loaded.')
                self._compile(self._loaded, need_microwave=True)
                board = self._board()
                board.claim(self.module_name)  # BoardBusyError if another RFSoC module is using the board
                try:
                    self._sequencer.start()
                except Exception:
                    try:
                        self._sequencer.stop()
                    finally:
                        board.release(self.module_name)
                    raise
            except Exception:
                self.log.exception('RFSoC pulser not started:')
                self._status = -1
                return -1
            self._status = 1
            if self.module_state() == 'idle':
                self.module_state.lock()
            info = self._info
            self.log.info(f'RFSoC pulser on: "{self._loaded}", {info["pass_duration"] * 1e6:.3f} us per repetition, '
                          f'{info["instructions"]} of {info["program_memory"]} instructions'
                          + (f', microwave {info["frequency"] / 1e6:.6f} MHz at gain {info["gain"]}.'
                             if info['microwave_pulses'] else ', no microwave pulses.'))
            return 0

    def pulser_off(self):
        """ Stop the waveform and switch all outputs off.

        @return int: error code (0: OK, -1: error, logged)
        """
        with self._lock:
            if self._sequencer is None:
                return 0
            was_running = self._status == 1
            try:
                if was_running or self._sequencer.running:
                    self._sequencer.stop()
            except Exception:
                self.log.exception('Could not switch the RFSoC outputs off:')
                self._status = -1
                return -1
            finally:
                if was_running:
                    self._board().release(self.module_name)
                    if self.module_state() == 'locked':
                        self.module_state.unlock()
            self._status = 0
            return 0

    def load_waveform(self, load_dict):
        """ Make a written waveform the one that pulser_on() plays. It is compiled here, so a waveform the
        board cannot play (too many instructions, outputs too close together) is refused now.

        @return dict: {1: name of the loaded waveform} (empty if nothing is loaded)
        """
        with self._lock:
            if isinstance(load_dict, dict):
                names = set(load_dict.values())
            else:
                names = set(load_dict)
            if len(names) != 1:
                self.log.error(f'The RFSoC pulser plays exactly one waveform, got {sorted(names)}.')
                return self.get_loaded_assets()[0]
            name = names.pop()
            if self._status == 1:
                self.log.error('Unable to load a waveform while the pulser is running.')
                return self.get_loaded_assets()[0]
            if name not in self._waveforms:
                self.log.error(f'No waveform "{name}" has been written to the RFSoC pulser.')
                return self.get_loaded_assets()[0]
            try:
                self._compile(name, need_microwave=False)
            except (ValueError, RuntimeError) as err:
                self.log.error(f'Waveform "{name}" cannot be played by the RFSoC: {err}')
                self._loaded = ''
                return self.get_loaded_assets()[0]
            except Exception:
                self.log.exception(f'Waveform "{name}" cannot be played by the RFSoC:')
                self._loaded = ''
                return self.get_loaded_assets()[0]
            self._loaded = name
            return self.get_loaded_assets()[0]

    def load_sequence(self, sequence_name):
        self.log.error('The RFSoC pulser has no sequence mode.')
        return self.get_loaded_assets()[0]

    def get_loaded_assets(self):
        if self._loaded:
            return {1: self._loaded}, 'waveform'
        return dict(), None

    def clear_all(self):
        self.pulser_off()
        with self._lock:
            self._waveforms, self._writing, self._loaded = dict(), None, ''
        return 0

    def get_status(self):
        status = {-1: 'Failed request or failed communication with the board.',
                  0: 'Pulser has stopped, all outputs off.',
                  1: 'Pulser is running.'}
        return self._status, status

    def get_sample_rate(self):
        return self._sequencer.clock_frequency

    def set_sample_rate(self, sample_rate):
        return self.get_sample_rate()  # fixed: the tProc clock

    def get_analog_level(self, amplitude=None, offset=None):
        return dict(), dict()

    def set_analog_level(self, amplitude=None, offset=None):
        return dict(), dict()

    def get_digital_level(self, low=None, high=None):
        low = self._all_channels() if not low else low
        high = self._all_channels() if not high else high
        return {ch: 0.0 for ch in low}, {ch: self._HIGH_LEVEL for ch in high}

    def set_digital_level(self, low=None, high=None):
        return self.get_digital_level()  # fixed: 0 V / 3.3 V

    def get_active_channels(self, ch=None):
        channels = self._all_channels()
        if ch:
            return {channel: channel in channels for channel in ch}
        return {channel: True for channel in channels}

    def set_active_channels(self, ch=None):
        return self.get_active_channels()  # all configured channels are always active

    def write_waveform(self, name, analog_samples, digital_samples, is_first_chunk, is_last_chunk,
                       total_number_of_samples):
        """ Store a waveform: only the times at which each channel changes are kept.

        @return (int, list): number of samples written (-1 on error), names of the written waveforms
        """
        with self._lock:
            if analog_samples:
                self.log.error('The RFSoC pulser has no analog channels. Use a digital channel for the microwave.')
                return -1, list()
            if set(digital_samples) != set(self._all_channels()):
                self.log.error(f'Sample arrays for channels {sorted(digital_samples)} do not match the channels '
                               f'of the RFSoC pulser {self._all_channels()}.')
                return -1, list()
            waveform = f'{name}_ch1'
            if is_first_chunk:
                self._writing = dict(name=waveform, offset=0, level={ch: False for ch in digital_samples},
                                     edges={ch: [] for ch in digital_samples})
            writing = self._writing
            if writing is None or writing['name'] != waveform:
                self.log.error(f'Waveform "{name}": chunk received without a first chunk.')
                return -1, list()
            chunk_length = 0
            for channel, samples in digital_samples.items():
                samples = np.asarray(samples, dtype=bool)
                chunk_length = len(samples)
                if chunk_length == 0:
                    continue
                offset, level, edges = writing['offset'], writing['level'][channel], writing['edges'][channel]
                flips = np.flatnonzero(samples[1:] != samples[:-1]) + 1 + offset
                if bool(samples[0]) != level:
                    flips = np.concatenate(([offset], flips))
                writing['level'][channel] = bool(samples[-1])
                if edges is not None:
                    if len(edges) + len(flips) > self._MAX_EDGES:
                        writing['edges'][channel] = None  # too many level changes: cannot be played anyway
                    else:
                        edges.extend(flips.tolist())
            writing['offset'] += chunk_length
            if is_last_chunk:
                total = writing['offset']
                if total != total_number_of_samples:
                    self.log.error(f'Waveform "{name}": {total} samples written, {total_number_of_samples} announced.')
                    self._writing = None
                    return -1, list()
                intervals = dict()
                for channel, edges in writing['edges'].items():
                    if edges is None:
                        intervals[channel] = None
                        continue
                    if len(edges) % 2:
                        edges = edges + [total]  # high until the end of the waveform
                    intervals[channel] = list(zip(edges[0::2], edges[1::2]))
                self._waveforms[waveform] = (total, intervals)
                self._writing = None
                if self._loaded == waveform:
                    self._loaded = ''  # rewritten: has to be loaded again
            return chunk_length, [waveform]

    def write_sequence(self, name, sequence_parameters):
        self.log.error('The RFSoC pulser has no sequence mode.')
        return -1

    def get_waveform_names(self):
        return list(self._waveforms)

    def get_sequence_names(self):
        return list()

    def delete_waveform(self, waveform_name):
        with self._lock:
            names = [waveform_name] if isinstance(waveform_name, str) else list(waveform_name)
            deleted = []
            for name in names:
                if name in self._waveforms and not (self._status == 1 and name == self._loaded):
                    del self._waveforms[name]
                    deleted.append(name)
                    if name == self._loaded:
                        self._loaded = ''
            return deleted

    def delete_sequence(self, sequence_name):
        return list()

    def get_interleave(self):
        return False

    def set_interleave(self, state=False):
        return False

    def reset(self):
        """ Stop, switch all outputs off and forget all waveforms. """
        return self.clear_all()

    # ---------------------------------------------------------------- helpers
    def _all_channels(self):
        channels = list(self._pins)
        if self._mw_channel is not None:
            channels.append(self._mw_channel)
        return sorted(channels, key=lambda ch: int(ch[4:]))

    def _compile(self, name, need_microwave):
        """ Compile waveform ``name``. Without microwave settings: an error if ``need_microwave``, otherwise
        the check is done with the lowest gain in the middle of the frequency range.
        """
        length, intervals = self._waveforms[name]
        crowded = [ch for ch, ivs in intervals.items() if ivs is None]
        if crowded:
            raise ValueError(f'more than {self._MAX_EDGES} level changes on {crowded}; the program memory of the '
                             f'board holds 1024 instructions (about 1 per change).')
        microwave = intervals.get(self._mw_channel, []) if self._mw_channel is not None else []
        frequency, gain = self._mw_frequency, self._mw_gain
        if microwave and frequency is None:
            if need_microwave:
                raise RuntimeError(f'Waveform "{name}" has microwave pulses but no microwave frequency and power '
                                   f'are set. In the pulsed GUI: tick "use ext. microwave" and set frequency '
                                   f'and power (module bocquellab.rfsoc_pulsed_microwave); or call '
                                   f'set_microwave(frequency, gain).')
            frequency, gain = sum(self._sequencer.frequency_limits) / 2, 1
        self._info = self._sequencer.compile(length, {pin: intervals[ch] for ch, pin in self._pins.items()},
                                             microwave, frequency=frequency, gain=gain)
