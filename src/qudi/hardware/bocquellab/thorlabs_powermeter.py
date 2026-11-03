# -*- coding: utf-8 -*-
"""
BocquelLab extension of qudi's Thorlabs power meter driver (qudi.hardware.powermeter.thorlabs_powermeter):
adds a public get_wavelength() so a GUI can show the wavelength the meter corrects for. Everything else, including
the TLPM library calls, is inherited unchanged. Tested with a PM100D2 (USB 1313:8099), which works with TLPM.

Copyright (c) 2022, the qudi developers. See the AUTHORS.md file at the top-level directory of this
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

__all__ = ['BocquelLabThorlabsPowermeter']

from qudi.hardware.powermeter.thorlabs_powermeter import ThorlabsPowermeter


class BocquelLabThorlabsPowermeter(ThorlabsPowermeter):
    """ qudi's ThorlabsPowermeter plus get_wavelength().

    Note (inherited): activation resets the meter (TLPM_init with reset) and then applies the wavelength option.

    Example config for copy-paste:

    thorlabs_pm100d2:
        module.Class: 'bocquellab.thorlabs_powermeter.BocquelLabThorlabsPowermeter'
        options:
            address: 'USB0::0x1313::0x8099::M01337844::INSTR'  # optional, default: first meter found
            wavelength: 520.0  # nm, optional; omitted: the meter keeps its own setting
    """

    def get_wavelength(self):
        """ @return float: measurement (correction) wavelength of the meter in nm """
        if self.get_activity_state(self._channel_name):
            return self._get_wavelength()
        self._init_powermeter()  # the session is only open while the channel is active
        try:
            return self._get_wavelength()
        finally:
            self._close_powermeter()
