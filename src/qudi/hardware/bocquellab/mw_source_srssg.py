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
        super().set_cw(frequency, power)

    def configure_scan(self, power, frequencies, mode, sample_rate):
        self._check_power(power, 'Scan')
        super().configure_scan(power, frequencies, mode, sample_rate)
