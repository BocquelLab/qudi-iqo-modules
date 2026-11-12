# -*- coding: utf-8 -*-
"""
BocquelLab extension of qudi's Keysight M8190A AWG driver (qudi.hardware.awg.keysight_m819x): channel 1 drives
the Mini-Circuits ZHL-10M4G21W0+ amplifier (RF input absolute maximum +5 dBm, compression from about -6 dBm, see
docs/ZHL-10M4G21W0+.pdf). This module adds a hard amplitude limit, locks the outputs to the direct DAC path
(at most 0.7 Vpp, about +0.9 dBm for a full-scale sine into 50 Ohm; the optional AC amplifier path reaches
+10 dBm) and turns the outputs off on deactivation. Everything else is inherited unchanged.

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

__all__ = ['BocquelLabAWGM8190A']

import math

from qudi.core.configoption import ConfigOption
from qudi.hardware.awg.keysight_m819x import AWGM8190A


class BocquelLabAWGM8190A(AWGM8190A):
    """ qudi's AWGM8190A (PulserInterface, for the pulsed toolchain) with an output limit for the amplifier.

    - max_amplitude (Vpp, mandatory): analog amplitude limit of both channels, at most 0.7 Vpp (direct DAC
      output). The constraints reported to the pulsed logic end at this value; set_analog_level refuses
      anything higher (ValueError, nothing sent) instead of the inherited silent clipping.
    - Output path: only the direct DAC path ('DAC') is allowed. It is set on activation and read back;
      activation and pulser_on() fail (RuntimeError) if the AWG reports another path or an amplitude above
      the limit, e.g. after a change in the Keysight soft front panel.
    - Activation fails if the AWG cannot be reached (the inherited driver only logs that and stays active).
    - Deactivation stops the AWG and turns both outputs off.

    The power at the amplifier input is at most that of a full-scale sine: P = Vpp^2 / (8 * 50 Ohm),
    i.e. 0.7 Vpp -> +0.9 dBm, 0.5 Vpp -> -2.0 dBm, 0.25 Vpp -> -8.1 dBm (less at GHz: DAC sinc roll-off).

    Marker levels are set into 50 Ohm; into a high-impedance input (Cobolt digital modulation 2 kOhm,
    NI PFI) the voltage is about twice the set value.

    Inherited behaviour: activation resets the AWG (*RST). The Keysight M8190 firmware (AgM8190Firmware.exe)
    must be running: it provides the SCPI server (HiSLIP) on localhost.

    Example config for copy-paste:

    m8190a:
        module.Class: 'bocquellab.awg_keysight_m8190a.BocquelLabAWGM8190A'
        options:
            awg_visa_address: 'TCPIP0::localhost::hislip0::INSTR'
            awg_timeout: 20  # s
            pulsed_file_dir: 'C:/Software/pulsed_files'
            assets_storage_path: 'C:/Software/saved_pulsed_assets'  # same as in sequence_generator_logic
            sample_rate_div: 1
            dac_resolution_bits: 12  # 12 (up to 12 GSa/s, option -12G) or 14 (up to 8 GSa/s, option -14B)
            max_amplitude: 0.7  # Vpp, hard limit (direct DAC path maximum)
            d_ch_level_low_high: [[0.0, 1.5], [0.0, 1.5], [0.0, 1.5], [0.0, 1.5]]  # V into 50 Ohm
    """

    _max_amplitude = ConfigOption('max_amplitude', missing='error', constructor=float)

    _DAC_MAX_AMPLITUDE = 0.7  # Vpp, direct DAC output, single ended (manual / soft front panel)
    _AMPLITUDE_TOLERANCE = 1e-4  # V, read-back rounding

    _activated_analog = None  # analog channels of the last set_active_channels(), e.g. ['a_ch1']

    @property
    def _amplitude_limit(self) -> float:
        return min(self._max_amplitude, self._DAC_MAX_AMPLITUDE)

    def on_activate(self):
        if not self._max_amplitude > 0:
            raise ValueError(f'max_amplitude must be > 0 Vpp, got {self._max_amplitude}.')
        if self._max_amplitude > self._DAC_MAX_AMPLITUDE:
            self.log.warning(f'max_amplitude {self._max_amplitude} Vpp is above the direct DAC output maximum '
                             f'({self._DAC_MAX_AMPLITUDE} Vpp); that maximum applies.')
        if self._dac_amp_mode != 'direct':
            raise RuntimeError('Only the direct DAC output path is allowed (amplifier protection).')
        self.awg = None
        self._activated_analog = None
        super().on_activate()
        if self.awg is None:
            raise RuntimeError(f'No connection to the M8190A at "{self._visa_address}". Is the Keysight M8190 '
                               f'firmware (AgM8190Firmware.exe) running?')
        self._check_output_safe()
        limit = self._amplitude_limit
        self.log.info(f'Outputs on the direct DAC path, amplitude limited to {limit:.3f} Vpp '
                      f'(full-scale sine {self._sine_power_dbm(limit):+.1f} dBm into 50 Ohm).')

    def on_deactivate(self):
        """ Stop the AWG and switch both outputs off. Never raises. """
        try:
            if getattr(self, 'awg', None) is not None:
                self.write(':ABOR')
                self.write(':OUTP1:NORM OFF')
                self.write(':OUTP2:NORM OFF')
        except Exception:
            self.log.exception('Could not stop the M8190A and switch its outputs off:')
        super().on_deactivate()

    def get_constraints(self):
        constraints = super().get_constraints()
        limit = self._amplitude_limit
        constraints.a_ch_amplitude.max = limit
        constraints.a_ch_amplitude.default = min(constraints.a_ch_amplitude.default, limit)
        return constraints

    def set_analog_level(self, amplitude=None, offset=None):
        if amplitude:
            limit = self._amplitude_limit
            too_high = {ch: float(v) for ch, v in amplitude.items() if float(v) > limit + self._AMPLITUDE_TOLERANCE}
            if too_high:
                raise ValueError(f'Amplitude {too_high} Vpp exceeds the limit of {limit:.3f} Vpp (max_amplitude, '
                                 f'protects the amplifier). Nothing was sent.')
        return super().set_analog_level(amplitude=amplitude, offset=offset)

    def pulser_on(self):
        self._check_output_safe()
        return super().pulser_on()

    def _set_active_ch(self, new_channels_state):
        """ As inherited (an analog channel is active when its output is on), and remembers the analog channels
        of the selected activation config for _write_output_on().
        """
        super()._set_active_ch(new_channels_state)
        self._activated_analog = [ch for ch in self._get_all_analog_channels() if new_channels_state.get(ch)]

    def _write_output_on(self):
        """ Switch on only the outputs of the selected activation config, the others off. The inherited version
        switched on both outputs, so after the first pulser_on() the AWG reported every channel as active and the
        next write_waveform() for e.g. 'ch1_2mrk' failed with a channel mismatch.
        """
        channels = self._activated_analog
        if channels is None:  # no activation config set since activation: keep the outputs that are on
            channels = [ch for ch, on in self.get_active_channels().items() if on and ch.startswith('a')]
        if not channels:
            raise RuntimeError('No analog channel is active: select an activation config (Pulse Generator '
                               'Settings) before switching the pulser on.')
        for ch in self._get_all_analog_channels():
            state = 'ON' if ch in channels else 'OFF'
            self.write(f':OUTP{self.chstr_2_chnum(ch)}:NORM {state}')

    def write_waveform(self, name, analog_samples, digital_samples, is_first_chunk, is_last_chunk,
                       total_number_of_samples):
        """ As inherited, but the channel check compares the sample arrays with the channels that are actually
        active: the inherited check used every key of get_active_channels() (also the inactive ones), so only the
        'all' activation config could be written. On the M8190A an analog channel is active when its output is
        on (:OUTP<n>:NORM ON); its markers follow it.
        """
        waveforms = []
        if len(analog_samples) == 0:
            self.log.error('No analog samples passed to write_waveform method in M8190A.')
            return -1, waveforms
        min_samples = self.get_constraints().waveform_length.min
        if total_number_of_samples < min_samples:
            self.log.error(f'Unable to write waveform: {total_number_of_samples:d} samples is less than the '
                           f'minimum waveform length ({min_samples:d}).')
            return -1, waveforms
        active_channels = {ch for ch, on in self.get_active_channels().items() if on}
        sample_channels = set(analog_samples).union(digital_samples)
        if active_channels != sample_channels:
            self.log.error(f'Mismatch of channel activation and sample arrays for waveform creation.\n'
                           f'Active channels: {sorted(active_channels)}\nSample arrays: {sorted(sample_channels)}\n'
                           f'Select the matching channel activation in Pulse Generator Settings.')
            return -1, waveforms
        active_analog = sorted(ch for ch in active_channels if ch.startswith('a'))
        to_segment_id = -1 if self._wave_mem_mode == 'awg_segments' else 1  # 1: pc_hdd mode
        waveforms = self._write_wave_to_memory(name, analog_samples, digital_samples, active_analog,
                                               to_segment_id=to_segment_id)
        self.check_dev_error()
        return total_number_of_samples, waveforms

    def load_waveform(self, load_dict, to_nextfree_segment=False):
        """ As inherited, but returns the dict of loaded waveforms per channel required by PulserInterface
        (the inherited method returns (dict, asset type)).
        """
        return self._loaded_dict(super().load_waveform(load_dict, to_nextfree_segment=to_nextfree_segment))

    def load_sequence(self, sequence_name):
        return self._loaded_dict(super().load_sequence(sequence_name))

    @staticmethod
    def _loaded_dict(result):
        if isinstance(result, tuple):
            return result[0]
        return result

    def _fname_2_wavename(self, fname, incl_ch_postfix=True):
        """ As inherited, but only the last '_ch<n>' is the channel postfix: the inherited version cuts at the first
        '_ch', so for names like 'qudi_benchmark_chunk_ch1.bin' the old file was not found, not deleted, and
        rewriting it failed (AWG error -257 "file already exists").
        """
        stem = fname.rsplit('.', 1)[0]
        if incl_ch_postfix:
            return stem
        base, sep, ch = stem.rpartition('_ch')
        return base if sep and ch.isdigit() else stem

    def _set_ref_clock(self):
        """ Without ext_ref_clock_freq, use the AXIe backplane reference: this M8190A has no internal one
        (':ROSC:SOUR INT' of the inherited driver gives error -300 "Clock Reference Source INTernal invalid").
        """
        if self._ext_ref_clock_freq is None:
            self.write(':ROSC:SOUR AXI')
        else:
            super()._set_ref_clock()

    def _check_output_safe(self) -> None:
        """ Raise RuntimeError unless both outputs are on the direct DAC path with an amplitude within the limit
        (read from the AWG, so changes made in the soft front panel are caught).
        """
        limit = self._amplitude_limit
        for ch in (1, 2):
            route = self.query(f':OUTP{ch}:ROUT?').strip().upper()
            if route != 'DAC':
                raise RuntimeError(f'Output {ch} is on the "{route}" path, only "DAC" is allowed (the AC amplifier '
                                   f'path reaches +10 dBm, amplifier absolute maximum +5 dBm). Not started.')
            amplitude = float(self.query(f':VOLT{ch}?'))
            if amplitude > limit + self._AMPLITUDE_TOLERANCE:
                raise RuntimeError(f'Output {ch} amplitude {amplitude:.4f} Vpp is above the limit of {limit:.3f} '
                                   f'Vpp (max_amplitude). Not started.')

    @staticmethod
    def _sine_power_dbm(vpp: float) -> float:
        return 10 * math.log10((vpp / (2 * math.sqrt(2))) ** 2 / 50 / 1e-3)
