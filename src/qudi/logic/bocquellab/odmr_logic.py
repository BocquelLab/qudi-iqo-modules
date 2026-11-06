# -*- coding: utf-8 -*-
"""
BocquelLab extension of qudi's ODMR logic (qudi.logic.odmr_logic): after every action of this logic
that changes the microwave source (CW on/off, scan start/stop) and at activation, the actual
frequency, power and RF output state are read back from the source and sent to the GUI
(sigMicrowaveReadback). There is no periodic polling. Also forwards a settle time chosen in the GUI
to the data scanner. Everything else is inherited unchanged.

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

__all__ = ['BocquelLabOdmrLogic']

import datetime as dt
from typing import Optional, Dict
from PySide6 import QtCore

from qudi.core.statusvariable import StatusVar
from qudi.logic.odmr_logic import OdmrLogic


class BocquelLabOdmrLogic(OdmrLogic):
    """ qudi's OdmrLogic plus a read-back of the microwave source after each change made by qudi.

    Read-back dict (also property last_microwave_readback):
        frequency (Hz), power (dBm): as reported by the source (None if the read failed)
        rf_on (bool or None): actual output state if the source provides rf_output_active
                              (BocquelLab SG384 driver), otherwise qudi's module state
        rf_state_from_device (bool): True if rf_on was read from the device
        time (str): time of the read-back, HH:MM:SS

    Changes made on the generator front panel are only seen at the next read-back.

    Settle time: if the data scanner provides settle_time / set_settle_time (BocquelLab NI stepped
    ODMR counter), set_settle_time() changes it (not during a scan) and the value is saved and
    re-applied at the next activation (sigSettleTimeUpdated).

    Example config for copy-paste:

    odmr_logic:
        module.Class: 'bocquellab.odmr_logic.BocquelLabOdmrLogic'
        connect:
            microwave: <MicrowaveInterface_name>
            data_scanner: <FiniteSamplingInputInterface_name>
    """

    sigMicrowaveReadback = QtCore.Signal(dict)
    sigSettleTimeUpdated = QtCore.Signal(object)  # float (s) or None if the data scanner has none

    # settle time chosen in the GUI (s); None: keep the data scanner's configured value
    _saved_settle_time = StatusVar(name='settle_time', default=None)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._last_readback = None

    def on_activate(self):
        super().on_activate()
        scanner = self._data_scanner()
        if self._saved_settle_time is not None and hasattr(scanner, 'set_settle_time'):
            try:
                scanner.set_settle_time(self._saved_settle_time)
            except Exception:
                self.log.exception('Saved settle time not applied; using the configured value:')
        self.read_back_microwave()

    @property
    def settle_time(self) -> Optional[float]:
        """ Wait after each frequency step before counting (s); None if not supported by the
        data scanner (e.g. a dummy).
        """
        scanner = self._data_scanner()
        return float(scanner.settle_time) if hasattr(scanner, 'settle_time') else None

    @QtCore.Slot(float)
    def set_settle_time(self, seconds: float) -> None:
        with self._threadlock:
            scanner = self._data_scanner()
            if not hasattr(scanner, 'set_settle_time'):
                self.log.error('The connected data scanner has no settle time.')
            elif abs(float(seconds) - float(scanner.settle_time)) < 1e-9:
                pass  # unchanged: nothing to do, also during a scan
            elif self.module_state() != 'idle':
                self.log.error('Unable to change the settle time while an ODMR scan is running.')
            else:
                try:
                    scanner.set_settle_time(seconds)
                    self._saved_settle_time = float(scanner.settle_time)
                except Exception:
                    self.log.exception('Unable to set the settle time:')
            self.sigSettleTimeUpdated.emit(self.settle_time)

    @property
    def last_microwave_readback(self) -> Optional[Dict]:
        return None if self._last_readback is None else dict(self._last_readback)

    @QtCore.Slot()
    def read_back_microwave(self) -> Dict:
        """ Read frequency, power and RF state from the microwave source and emit them. """
        microwave = self._microwave()
        readback = {'frequency': None, 'power': None, 'rf_on': None, 'rf_state_from_device': False,
                    'time': dt.datetime.now().strftime('%H:%M:%S')}
        try:
            readback['frequency'] = float(microwave.cw_frequency)
            readback['power'] = float(microwave.cw_power)
            if hasattr(microwave, 'rf_output_active'):
                readback['rf_on'] = bool(microwave.rf_output_active)
                readback['rf_state_from_device'] = True
            else:
                readback['rf_on'] = microwave.module_state() != 'idle'
        except Exception:
            self.log.exception('Microwave read-back failed:')
        self._last_readback = readback
        self.sigMicrowaveReadback.emit(dict(readback))
        return readback

    @QtCore.Slot(bool)
    def toggle_cw_output(self, enable):
        try:
            super().toggle_cw_output(enable)
        finally:
            self.read_back_microwave()

    @QtCore.Slot()
    def start_odmr_scan(self):
        try:
            super().start_odmr_scan()
        finally:
            self.read_back_microwave()

    @QtCore.Slot()
    def stop_odmr_scan(self):
        try:
            super().stop_odmr_scan()
        finally:
            self.read_back_microwave()
