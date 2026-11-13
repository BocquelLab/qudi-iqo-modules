# -*- coding: utf-8 -*-
"""
Qudi hardware module that gives the pulsed toolchain its "external microwave" for the RFSoC pulser:
frequency and power (dBm) of the microwave pulses that the pulser's microwave channel gates.

It plays nothing by itself: it converts the power to a generator gain with the calibration of the RFSoC
microwave source module and hands frequency and gain to the RFSoC pulser.

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

__all__ = ['RFSoCPulsedMicrowave']

from qudi.core.connector import Connector
from qudi.util.mutex import Mutex
from qudi.util.enums import SamplingOutputMode
from qudi.interface.microwave_interface import MicrowaveInterface, MicrowaveConstraints


class RFSoCPulsedMicrowave(MicrowaveInterface):
    """ "External microwave" of the pulsed measurement logic for the RFSoC pulser.

    In the pulsed GUI, tick "use ext. microwave" and set frequency and power: the pulsed logic then calls
    set_cw() / cw_on() before each measurement and off() after it. Here that means:
    - set_cw(frequency, power): the power in dBm is converted to a generator gain with the calibration of
      the connected bocquellab.rfsoc_microwave.RFSoCMicrowave module (calibration_gain / calibration_power;
      nominal values while calibration_measured is False). A power that needs more than the gain limit of
      that module or of the pulser is refused (ValueError), never clipped.
    - cw_on(): hands frequency and gain to the pulser. No signal is played by this module: the generator
      only plays while the microwave channel of the running pulse sequence is high.
    - off(): the pulser forgets the settings, so a sequence with microwave pulses cannot start without
      "use ext. microwave".
    No frequency scans.

    Example config for copy-paste:

    rfsoc_pulsed_microwave:
        module.Class: 'bocquellab.rfsoc_pulsed_microwave.RFSoCPulsedMicrowave'
        connect:
            pulser: rfsoc_pulser
            calibration: rfsoc_microwave  # power <-> gain calibration and its gain limit
    """

    _pulser = Connector(name='pulser', interface='RFSoCPulser')
    _calibration = Connector(name='calibration', interface='RFSoCMicrowave')

    _FREQUENCY_MARGIN = 1e6  # Hz, distance kept from the edges of the Nyquist zone

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._lock = Mutex()
        self._constraints = None
        self._gain_limit = 0
        self._frequency = None
        self._gain = None

    def on_activate(self):
        pulser, calibration = self._pulser(), self._calibration()
        self._gain_limit = int(min(pulser.gain_limit, calibration.gain_limit))
        low, high = pulser.microwave_frequency_limits
        self._constraints = MicrowaveConstraints(
            power_limits=(calibration.power_for_gain(1), calibration.power_for_gain(self._gain_limit)),
            frequency_limits=(low + self._FREQUENCY_MARGIN, high - self._FREQUENCY_MARGIN),
            scan_size_limits=(2, 2),
            sample_rate_limits=(1.0, 1.0),
            scan_modes=(SamplingOutputMode.JUMP_LIST,)
        )
        self._frequency, self._gain = None, None

    def on_deactivate(self):
        try:
            self.off()
        except Exception:
            self.log.exception('Could not clear the microwave settings of the RFSoC pulser:')

    @property
    def constraints(self):
        return self._constraints

    @property
    def is_scanning(self):
        return False

    @property
    def cw_power(self):
        """ Power in dBm of the microwave pulses (from the integer gain; nominal if not calibrated). """
        with self._lock:
            gain = 1 if self._gain is None else self._gain
            return self._calibration().power_for_gain(gain)

    @property
    def cw_frequency(self):
        with self._lock:
            return self._constraints.min_frequency if self._frequency is None else self._frequency

    @property
    def cw_gain(self):
        """ Generator gain (DAC units) of the microwave pulses, None if not set. """
        return self._gain

    @property
    def scan_power(self):
        return self.cw_power

    @property
    def scan_frequencies(self):
        return None

    @property
    def scan_mode(self):
        return SamplingOutputMode.JUMP_LIST

    @property
    def scan_sample_rate(self):
        return 1.0

    def set_cw(self, frequency, power):
        with self._lock:
            if self.module_state() != 'idle':
                raise RuntimeError('Unable to change the microwave settings while a pulsed measurement runs.')
            self._frequency, self._gain = None, None  # nothing stale is used if the new values are refused
            gain = self._calibration().gain_for_power(power)  # ValueError above the limit of that module
            if gain > self._gain_limit:
                raise ValueError(f'Power {float(power):+.2f} dBm needs gain {gain}, above the limit of the pulser '
                                 f'({self._gain_limit}; max_gain, protects the amplifier). Nothing was set.')
            self._assert_cw_parameters_args(frequency, self._calibration().power_for_gain(gain))
            self._frequency, self._gain = self._pulser().set_microwave(frequency, gain)

    def cw_on(self):
        """ Hand the settings to the pulser. Plays nothing: the pulse sequence gates the generator. """
        with self._lock:
            if self.module_state() != 'idle':
                return
            if self._frequency is None:
                self.log.error('No valid microwave frequency / power set for the RFSoC pulser: the pulse sequence '
                               'will be refused if it has microwave pulses.')
                return
            self._pulser().set_microwave(self._frequency, self._gain)
            self.module_state.lock()

    def off(self):
        with self._lock:
            self._pulser().clear_microwave()
            if self.module_state() != 'idle':
                self.module_state.unlock()

    def configure_scan(self, power, frequencies, mode, sample_rate):
        raise RuntimeError('The microwave of the RFSoC pulser cannot scan. For ODMR use '
                           'bocquellab.rfsoc_microwave.RFSoCMicrowave.')

    def start_scan(self):
        raise RuntimeError('The microwave of the RFSoC pulser cannot scan.')

    def reset_scan(self):
        pass
