# -*- coding: utf-8 -*-
"""
Qudi hardware module that uses a generator of the BocquelLab RFSoC4x2 (QICK) as a continuous microwave
source: CW output and frequency scans stepped point by point (CW ODMR with counting on the NI card).

This is a thin adapter. The on-board program (rfsoc_nv.SteppedCW) lives in the RFSoC-NV repository, the
connection in the board module (bocquellab.rfsoc_qick_board.RFSoCQickBoard).

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

__all__ = ['RFSoCMicrowave']

import math
import numpy as np

from qudi.core.configoption import ConfigOption
from qudi.core.connector import Connector
from qudi.util.mutex import Mutex
from qudi.util.enums import SamplingOutputMode
from qudi.interface.microwave_interface import MicrowaveInterface, MicrowaveConstraints


class RFSoCMicrowave(MicrowaveInterface):
    """ Continuous microwave output of an RFSoC4x2 / QICK generator (generator 0 = DAC_B).

    - CW: constant frequency and power. Scan: a frequency list (jump list) at constant power; the output
      is continuous and the frequency is stepped by trigger_scan_point(), one call per point, exactly as
      with the BocquelLab SG384 driver. The NI stepped ODMR counter (bocquellab.ni_stepped_odmr_counter)
      calls it before each point. A step is one network call; the frequency changes about 1 us after the
      board has received it, so the counter's settle time can be short.
      reset_scan() goes back to the first frequency and checks that the board has done every step that
      was asked for (RuntimeError otherwise).
    - Power: qudi works in dBm, the generator in DAC gain units (linear in voltage, full scale 32766):
          power = calibration_power + 20 log10(gain / calibration_gain)
      The calibration is one point (calibration_gain, calibration_power), valid near one frequency: the
      DAC output drops with frequency inside a Nyquist zone. With calibration_measured: False the dBm
      values are NOMINAL (a warning is logged); the gain is what protects the amplifier.
    - max_gain (mandatory): hard limit in DAC units. A power that needs a higher gain is refused
      (ValueError, nothing is sent), never clipped; the power range reported to the logic ends there.
      The limit of the board module applies as well (the lower one wins) and is checked again on the
      compiled program when it is loaded.
      WARNING: generator 0 is meant to drive the Mini-Circuits ZHL-10M4G21W0+ amplifier (RF input
      absolute maximum +5 dBm). Measure the DAC power for a given gain before the amplifier is connected.
    - The output is switched off (all generators silent, all PMOD outputs low) by off(), i.e. when a scan
      or the CW output is stopped, and on deactivation. While the output is on, this module holds the
      board: other RFSoC modules (pulsed) are refused until it is off.
    - nyquist_zone: 1 for 0 .. 2457.6 MHz, 2 for 2457.6 .. 4915.2 MHz (DAC at 4915.2 MS/s). A scan cannot
      cross the limit between the zones.
    - laser_gate_pins: PMOD0 pins held high while the microwave output is on (CW and scan), for a laser
      whose digital modulation input is connected to the RFSoC. Empty (default): all pins stay low, the
      laser has to be on by itself (Cobolt in constant power mode).

    Example config for copy-paste:

    rfsoc_microwave:
        module.Class: 'bocquellab.rfsoc_microwave.RFSoCMicrowave'
        options:
            generator_channel: 0  # 0 = DAC_B (microwave output), 1 = DAC_A
            nyquist_zone: 2  # 2: 2457.6 .. 4915.2 MHz (default, not verified on the setup yet)
            max_gain: 2000  # DAC units, hard limit (full scale 32766)
            calibration_gain: 32766  # DAC units
            calibration_power: 0.0  # dBm at calibration_gain. NOT MEASURED: placeholder
            calibration_measured: False  # set True once calibration_power has been measured
            laser_gate_pins: []  # optional, e.g. [0] = PMOD0_0 high while the microwave is on
        connect:
            board: rfsoc_board
    """

    _board = Connector(name='board', interface='RFSoCQickBoard')

    _generator_channel = ConfigOption(name='generator_channel', default=0, constructor=lambda x: int(x))
    _nyquist_zone = ConfigOption(name='nyquist_zone', default=2, constructor=lambda x: int(x))
    _max_gain = ConfigOption(name='max_gain', missing='error', constructor=float)
    _calibration_gain = ConfigOption(name='calibration_gain', default=32766, constructor=float)
    _calibration_power = ConfigOption(name='calibration_power', missing='error', constructor=float)
    _calibration_measured = ConfigOption(name='calibration_measured', default=False, constructor=lambda x: bool(x))
    _laser_gate_pins = ConfigOption(name='laser_gate_pins', default=None)

    _FULL_SCALE_GAIN = 32766
    _FREQUENCY_MARGIN = 1e6  # Hz, distance kept from the edges of the Nyquist zone

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._lock = Mutex()
        self._constraints = None
        self._stepper = None
        self._gain_limit = 0
        self._in_cw_mode = True
        self._cw_frequency = 2.87e9
        self._cw_gain = 1
        self._scan_frequencies = None
        self._scan_gain = 1
        self._scan_sample_rate = 0.0

    # ---------------------------------------------------------------- module lifecycle
    def on_activate(self):
        if not 0 < self._max_gain <= self._FULL_SCALE_GAIN:
            raise ValueError(f'max_gain must be in ]0, {self._FULL_SCALE_GAIN}], got {self._max_gain}.')
        if not 0 < self._calibration_gain <= self._FULL_SCALE_GAIN:
            raise ValueError(f'calibration_gain must be in ]0, {self._FULL_SCALE_GAIN}], '
                             f'got {self._calibration_gain}.')
        board = self._board()
        from rfsoc_nv import SteppedCW  # lab package: imported here so that qudi starts without it

        self._gain_limit = int(min(self._max_gain, board.max_gain))
        if self._max_gain > board.max_gain:
            self.log.warning(f'max_gain {self._max_gain:.0f} is above the limit of the board module '
                             f'({board.max_gain:.0f}); the board limit applies.')
        self._stepper = SteppedCW(board.board, gen_ch=self._generator_channel, nqz=self._nyquist_zone,
                                  gate_pins=tuple(self._laser_gate_pins or ()))
        low, high = self._stepper.frequency_limits
        self._constraints = MicrowaveConstraints(
            power_limits=(self.power_for_gain(1), self.power_for_gain(self._gain_limit)),
            frequency_limits=(low + self._FREQUENCY_MARGIN, high - self._FREQUENCY_MARGIN),
            scan_size_limits=(2, self._stepper.max_points),
            sample_rate_limits=(1e-6, 50e3),
            scan_modes=(SamplingOutputMode.JUMP_LIST,)
        )
        self._in_cw_mode = True
        self._cw_frequency = self._stepper.round_frequency(self._constraints.frequency_in_range(2.87e9)[1])
        self._cw_gain = 1
        self._scan_frequencies = None
        self._scan_gain = 1
        self._scan_sample_rate = self._constraints.max_sample_rate

        self.log.info(f'RFSoC generator {self._generator_channel}, Nyquist zone {self._nyquist_zone}: '
                      f'{self._constraints.min_frequency / 1e6:.1f} .. {self._constraints.max_frequency / 1e6:.1f} '
                      f'MHz, gain limit {self._gain_limit} = {self._constraints.max_power:+.1f} dBm.')
        if not self._calibration_measured:
            self.log.warning(f'The microwave power calibration of the RFSoC is NOT MEASURED: dBm values are '
                             f'nominal (gain {self._calibration_gain:.0f} taken as '
                             f'{self._calibration_power:+.1f} dBm). The gain limit of {self._gain_limit} is what '
                             f'protects the amplifier.')
        if self._stepper.gate_names:
            self.log.info(f'{", ".join(self._stepper.gate_names)} high while the microwave output is on.')

    def on_deactivate(self):
        """ Output off. Never raises. """
        try:
            self.off()
        except Exception:
            self.log.exception('Could not switch the RFSoC microwave output off:')
        self._stepper = None

    # ---------------------------------------------------------------- power <-> gain
    def power_for_gain(self, gain):
        """ Nominal output power in dBm for a generator gain in DAC units. """
        return self._calibration_power + 20 * math.log10(gain / self._calibration_gain)

    def gain_for_power(self, power):
        """ Generator gain (DAC units, integer) for a power in dBm. Raises ValueError if the power is
        outside what the gain limit allows; nothing is clipped.
        """
        gain = int(round(self._calibration_gain * 10 ** ((float(power) - self._calibration_power) / 20)))
        if gain > self._gain_limit:
            raise ValueError(f'Power {float(power):+.2f} dBm needs gain {gain}, above the limit of '
                             f'{self._gain_limit} ({self.power_for_gain(self._gain_limit):+.2f} dBm; max_gain, '
                             f'protects the amplifier). Nothing was sent.')
        if gain < 1:
            raise ValueError(f'Power {float(power):+.2f} dBm is below the smallest output of the generator '
                             f'({self.power_for_gain(1):+.2f} dBm at gain 1).')
        return self._board().check_gain(gain)  # board limit; logs a warning above its warning_gain

    # ---------------------------------------------------------------- interface: properties
    @property
    def constraints(self):
        return self._constraints

    @property
    def is_scanning(self):
        with self._lock:
            return self.module_state() != 'idle' and not self._in_cw_mode

    @property
    def cw_power(self):
        """ CW power in dBm as the generator plays it (the gain is an integer). """
        with self._lock:
            return self.power_for_gain(self._cw_gain)

    @property
    def cw_frequency(self):
        """ CW frequency in Hz as the generator plays it (resolution about 1 Hz). """
        with self._lock:
            return self._cw_frequency

    @property
    def cw_gain(self):
        """ Generator gain (DAC units) of the CW output. """
        with self._lock:
            return self._cw_gain

    @property
    def scan_power(self):
        with self._lock:
            return self.power_for_gain(self._scan_gain)

    @property
    def scan_gain(self):
        """ Generator gain (DAC units) of the frequency scan. """
        with self._lock:
            return self._scan_gain

    @property
    def scan_frequencies(self):
        with self._lock:
            return None if self._scan_frequencies is None else self._scan_frequencies.copy()

    @property
    def scan_mode(self):
        return SamplingOutputMode.JUMP_LIST

    @property
    def scan_sample_rate(self):
        with self._lock:
            return self._scan_sample_rate

    # ---------------------------------------------------------------- interface: control
    def off(self):
        """ Switch the output off: all generators silent, all PMOD outputs low. Returns after the board
        has confirmed it.
        """
        with self._lock:
            if self.module_state() == 'idle':
                return
            try:
                self._stepper.stop()
            finally:
                self._board().release(self.module_name)
                self.module_state.unlock()

    def set_cw(self, frequency, power):
        with self._lock:
            if self.module_state() != 'idle':
                raise RuntimeError('Unable to set CW parameters. Microwave output active.')
            gain = self.gain_for_power(power)
            self._assert_cw_parameters_args(frequency, self.power_for_gain(gain))
            self._cw_frequency = self._stepper.round_frequency(frequency)
            self._cw_gain = gain

    def cw_on(self):
        with self._lock:
            if self.module_state() != 'idle':
                if self._in_cw_mode:
                    return
                raise RuntimeError('Unable to start CW microwave output. Frequency scan is running.')
            self._switch_on([self._cw_frequency], self._cw_gain)
            self._in_cw_mode = True
            self.log.info(f'RFSoC microwave on: {self._cw_frequency / 1e6:.6f} MHz, gain {self._cw_gain} '
                          f'({self._nominal()}{self.power_for_gain(self._cw_gain):+.2f} dBm).')

    def configure_scan(self, power, frequencies, mode, sample_rate):
        with self._lock:
            if self.module_state() != 'idle':
                raise RuntimeError('Unable to configure frequency scan. Microwave output active.')
            gain = self.gain_for_power(power)
            self._assert_scan_configuration_args(self.power_for_gain(gain), frequencies, mode, sample_rate)
            self._scan_frequencies = np.array([self._stepper.round_frequency(f) for f in frequencies],
                                              dtype=np.float64)
            self._scan_gain = gain
            self._scan_sample_rate = float(sample_rate)

    def start_scan(self):
        with self._lock:
            if self.module_state() != 'idle':
                if not self._in_cw_mode:
                    return
                raise RuntimeError('Unable to start frequency scan. CW microwave output is active.')
            if self._scan_frequencies is None:
                raise RuntimeError('No scan frequencies set. Unable to start scan.')
            self._switch_on(self._scan_frequencies.tolist(), self._scan_gain)
            self._in_cw_mode = False
            self.log.info(f'RFSoC microwave scan on: {len(self._scan_frequencies)} points, '
                          f'{self._scan_frequencies.min() / 1e6:.6f} .. {self._scan_frequencies.max() / 1e6:.6f} '
                          f'MHz, gain {self._scan_gain} '
                          f'({self._nominal()}{self.power_for_gain(self._scan_gain):+.2f} dBm).')

    def reset_scan(self):
        """ Back to the first frequency of the list. Raises RuntimeError if the board has not done every
        step asked for since the scan was started (wrong frequency axis).
        """
        with self._lock:
            if self.module_state() == 'idle':
                return
            if self._in_cw_mode:
                raise RuntimeError('Can not reset frequency scan. CW microwave output active.')
            self._stepper.reset()

    def trigger_scan_point(self):
        """ Step the running frequency scan: plays the list entry at the present index and moves the index
        to the next entry (after the last one: back to the first). start_scan() and reset_scan() set the
        index to the first entry. Same behaviour as the SG384 driver.
        """
        with self._lock:
            if self.module_state() == 'idle' or self._in_cw_mode:
                raise RuntimeError('Unable to step the frequency scan: no scan running.')
            self._stepper.step()

    # ---------------------------------------------------------------- helpers
    def _switch_on(self, frequencies, gain):
        board = self._board()
        board.claim(self.module_name)  # BoardBusyError if another RFSoC module is using the board
        try:
            self._stepper.configure(frequencies, gain)
            self._stepper.start()
        except Exception:
            try:
                self._stepper.stop()
            except Exception:
                self.log.exception('Could not switch the RFSoC outputs off:')
            board.release(self.module_name)
            raise
        self.module_state.lock()

    def _nominal(self):
        return '' if self._calibration_measured else 'nominal '
