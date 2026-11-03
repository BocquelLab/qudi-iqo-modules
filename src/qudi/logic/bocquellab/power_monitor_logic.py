# -*- coding: utf-8 -*-
"""
BocquelLab logic that monitors one channel of a process value device (e.g. a power meter) over time:
polls it at a fixed interval and keeps a scrolling time window of readings for a GUI.

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

__all__ = ['PowerMonitorLogic']

import math
import time
from collections import deque

import numpy as np
from PySide6 import QtCore

from qudi.core.configoption import ConfigOption
from qudi.core.connector import Connector
from qudi.core.module import LogicBase
from qudi.util.mutex import RecursiveMutex


class PowerMonitorLogic(LogicBase):
    """ Reads one process value channel every query_interval seconds while running and keeps the readings of the
    last time_window seconds. If the hardware has a get_wavelength() method (e.g. BocquelLabThorlabsPowermeter), the
    measurement wavelength is read when monitoring starts.

    Example config for copy-paste:

    power_monitor_logic:
        module.Class: 'bocquellab.power_monitor_logic.PowerMonitorLogic'
        options:
            channel: 'Power'  # optional, default: first process value channel of the hardware
            query_interval: 0.2  # optional, s
            time_window: 120  # optional, s of history shown
        connect:
            process_value: thorlabs_pm100d2
    """

    _process_value = Connector(name='process_value', interface='ProcessValueInterface')

    _channel = ConfigOption(name='channel', default=None)
    _query_interval = ConfigOption(name='query_interval', default=0.2)
    _time_window = ConfigOption(name='time_window', default=120.0)

    sigDataUpdated = QtCore.Signal(object, object, float)  # times (s since start), values, latest value
    sigRunningChanged = QtCore.Signal(bool)
    sigWavelengthChanged = QtCore.Signal(float)  # nm, NaN if unknown

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._lock = RecursiveMutex()
        self._timer = None
        self._times = deque()
        self._values = deque()
        self._start_time = 0.0
        self._channel_name = ''
        self._unit = ''
        self._wavelength = math.nan

    def on_activate(self):
        hardware = self._process_value()
        channels = tuple(hardware.constraints.process_channels)
        if self._channel is None:
            if not channels:
                raise ValueError('The connected hardware has no process value channel.')
            self._channel_name = channels[0]
        elif self._channel in channels:
            self._channel_name = self._channel
        else:
            raise ValueError(f'Channel "{self._channel}" not found. Available channels: {channels}')
        self._unit = hardware.constraints.channel_units.get(self._channel_name, '')
        self._times.clear()
        self._values.clear()
        self._timer = QtCore.QTimer()
        self._timer.setSingleShot(True)
        self._timer.setInterval(round(1000 * float(self._query_interval)))
        self._timer.timeout.connect(self._poll, QtCore.Qt.ConnectionType.QueuedConnection)

    def on_deactivate(self):
        self.stop_monitoring()
        self._timer.timeout.disconnect()
        self._timer = None

    # ---------------------------------------------------------------- properties for the GUI
    @property
    def channel(self):
        return self._channel_name

    @property
    def unit(self):
        return self._unit

    @property
    def time_window(self):
        return float(self._time_window)

    @property
    def wavelength(self):
        """ Last measurement wavelength read from the hardware in nm, NaN if unknown. """
        return self._wavelength

    @property
    def is_running(self):
        return self.module_state() == 'locked'

    @property
    def data(self):
        with self._lock:
            return np.array(self._times), np.array(self._values)

    # ---------------------------------------------------------------- control
    @QtCore.Slot()
    def start_monitoring(self):
        with self._lock:
            if self.module_state() != 'idle':
                return
            hardware = self._process_value()
            try:
                hardware.set_activity_state(self._channel_name, True)
            except Exception:
                self.log.exception(f'Could not activate channel "{self._channel_name}" of the power meter:')
                self.sigRunningChanged.emit(False)
                return
            self._read_wavelength(hardware)
            self._times.clear()
            self._values.clear()
            self._start_time = time.monotonic()
            self.module_state.lock()
            self._timer.start()
        self.sigRunningChanged.emit(True)

    @QtCore.Slot()
    def stop_monitoring(self):
        with self._lock:
            if self.module_state() != 'locked':
                return
            self._timer.stop()
            try:
                self._process_value().set_activity_state(self._channel_name, False)
            except Exception:
                self.log.exception(f'Could not deactivate channel "{self._channel_name}" of the power meter:')
            self.module_state.unlock()
        self.sigRunningChanged.emit(False)

    # ---------------------------------------------------------------- internals
    def _read_wavelength(self, hardware):
        getter = getattr(hardware, 'get_wavelength', None)
        wavelength = math.nan
        if getter is not None:
            try:
                wavelength = float(getter())
            except Exception:
                self.log.exception('Could not read the measurement wavelength:')
        self._wavelength = wavelength
        self.sigWavelengthChanged.emit(wavelength)

    @QtCore.Slot()
    def _poll(self):
        with self._lock:
            if self.module_state() != 'locked':
                return
            try:
                value = float(self._process_value().get_process_value(self._channel_name))
            except Exception:
                self.log.exception('Reading the power meter failed; monitoring stopped:')
                failed = True
            else:
                failed = False
                now = time.monotonic() - self._start_time
                self._times.append(now)
                self._values.append(value)
                while self._times and self._times[0] < now - float(self._time_window):
                    self._times.popleft()
                    self._values.popleft()
                times, values = np.array(self._times), np.array(self._values)
                self._timer.start()
        if failed:
            self.stop_monitoring()
        else:
            self.sigDataUpdated.emit(times, values, value)
