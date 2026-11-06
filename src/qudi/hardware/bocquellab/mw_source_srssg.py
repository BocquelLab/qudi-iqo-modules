# -*- coding: utf-8 -*-
"""
BocquelLab extension of qudi's SRS SG380 driver (qudi.hardware.microwave.mw_source_srssg): adds a hard
upper limit on the RF output power and a warning above a lower threshold, to protect the amplifier
that follows the generator (Mini-Circuits ZHL-10M4G21W0+: RF input absolute maximum +5 dBm, compression
starts at about -6 dBm input, see docs/ZHL-10M4G21W0+.pdf). Everything else is inherited unchanged.

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

__all__ = ['BocquelLabMicrowaveSRSSG']

from qudi.core.configoption import ConfigOption
from qudi.interface.microwave_interface import MicrowaveConstraints
from qudi.hardware.microwave.mw_source_srssg import MicrowaveSRSSG


class BocquelLabMicrowaveSRSSG(MicrowaveSRSSG):
    """ qudi's MicrowaveSRSSG with a power limit for the amplifier behind it.

    - max_power (dBm, mandatory): the power constraints reported to the logic end at this value, so
      the ODMR logic clips CW and scan powers to it; set_cw / configure_scan also refuse anything
      higher (ValueError), independently of the inherited assert checks.
    - warning_power (dBm, optional): a power above this is accepted but logged as a warning.
    - after set_cw the front-panel display shows the parameter qudi changed last: frequency or
      Type-N power (power if both changed); unchanged values leave the display as it is.
    - rf_output_active: actual Type-N output state read from the generator (ENBR?).
    - trigger_scan_point(): steps a running frequency scan by one list entry (*TRG).

    Only qudi is limited: the SG384 front panel can still set up to +16.5 dBm.

    Note (inherited): activation resets the generator (*RST: 10 MHz, 0 dBm, all outputs off).

    Example config for copy-paste:

    sg384:
        module.Class: 'bocquellab.mw_source_srssg.BocquelLabMicrowaveSRSSG'
        options:
            visa_address: 'TCPIP0::192.168.0.10::inst0::INSTR'
            comm_timeout: 10  # s
            max_power: 5  # dBm, hard limit (amplifier RF input absolute maximum)
            warning_power: -5  # dBm, optional: warn above this (amplifier compression)
    """

    _max_power = ConfigOption('max_power', missing='error', constructor=float)
    _warning_power = ConfigOption('warning_power', default=None, missing='nothing',
                                  constructor=lambda x: None if x is None else float(x))

    _DISPLAY_FREQUENCY = 2  # DISP codes, manual p. 63
    _DISPLAY_POWER = 6  # RF Type-N amplitude

    def on_activate(self):
        super().on_activate()
        hw = self._constraints
        if self._max_power > hw.max_power:
            self.log.warning(f'max_power {self._max_power} dBm is above what the generator can output '
                             f'({hw.max_power} dBm); the generator limit applies.')
        max_power = min(self._max_power, hw.max_power)
        if max_power < hw.min_power:
            raise ValueError(f'max_power {self._max_power} dBm is below the generator minimum '
                             f'{hw.min_power} dBm.')
        self._constraints = MicrowaveConstraints(
            power_limits=(hw.min_power, max_power),
            frequency_limits=hw.frequency_limits,
            scan_size_limits=hw.scan_size_limits,
            sample_rate_limits=hw.sample_rate_limits,
            scan_modes=tuple(hw.scan_modes)
        )
        msg = f'RF output power limited to {max_power:+.1f} dBm'
        if self._warning_power is not None:
            msg += f', warning above {self._warning_power:+.1f} dBm'
        self.log.info(msg + '.')

    def _check_power(self, power: float, what: str) -> None:
        power = float(power)
        limit = self._constraints.max_power
        if power > limit:
            raise ValueError(f'{what} power {power:+.2f} dBm exceeds the limit of {limit:+.1f} dBm '
                             f'(max_power, protects the amplifier). Nothing was sent.')
        if self._warning_power is not None and power > self._warning_power:
            self.log.warning(f'{what} power {power:+.2f} dBm is above {self._warning_power:+.1f} dBm: '
                             f'the amplifier is in compression; its RF input absolute maximum is '
                             f'+5 dBm (ZHL-10M4G21W0+). Hard limit here: {limit:+.1f} dBm.')

    def set_cw(self, frequency, power):
        self._check_power(power, 'CW')
        # values on the generator before the change (also catches changes made on the front panel)
        with self._thread_lock:
            old_frequency = float(self._device.query('FREQ?'))
            old_power = float(self._device.query('AMPR?'))
        super().set_cw(frequency, power)
        # Front panel shows the parameter changed last (display only, no RF change). The frequency
        # is written before the power, so when both change the power is shown. Nothing changed:
        # the display stays as it is. A frequency scan list sets the display to frequency itself.
        power_changed = abs(float(power) - old_power) > 0.005
        frequency_changed = abs(float(frequency) - old_frequency) > 0.0005
        if power_changed or frequency_changed:
            with self._thread_lock:
                self._write(f'DISP {self._DISPLAY_POWER if power_changed else self._DISPLAY_FREQUENCY:d}')

    def trigger_scan_point(self) -> None:
        """ Step the running frequency scan: *TRG loads the list entry at the current index and
        moves the index to the next entry (manual p. 62, 80). The SG384 has no hardware trigger
        input for list mode, so a scan is stepped by this call (see
        bocquellab.stepped_counting_interfuse). reset_scan() returns to the first entry.
        """
        with self._thread_lock:
            if self.module_state() == 'idle' or self._in_cw_mode:
                raise RuntimeError('Unable to step the frequency scan: no scan running.')
            self._write('*TRG')

    @property
    def rf_output_active(self) -> bool:
        """ True if the Type-N RF output is on, as reported by the generator (ENBR?). """
        with self._thread_lock:
            return self._output_active()

    def configure_scan(self, power, frequencies, mode, sample_rate):
        self._check_power(power, 'Scan')
        super().configure_scan(power, frequencies, mode, sample_rate)
