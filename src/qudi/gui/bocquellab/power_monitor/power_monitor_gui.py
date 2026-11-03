# -*- coding: utf-8 -*-
"""
BocquelLab power monitor GUI: large live reading of a power meter, its measurement wavelength, a start/stop button
and a scrolling plot of power vs. time. Works with bocquellab.power_monitor_logic.PowerMonitorLogic.

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

__all__ = ['PowerMonitorGui', 'format_reading']

import math

import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

from qudi.core.connector import Connector
from qudi.core.module import GuiBase
from qudi.util.colordefs import QudiPalettePale as palette
from qudi.util.units import ScaledFloat


def format_reading(value, unit):
    """ '355.62 µW', '12.34 mW' for unit 'W' (SI prefix chosen from the value); '-' for None / NaN. """
    if value is None or not math.isfinite(value):
        return '-'
    return f'{ScaledFloat(float(value)):.2r}{unit}'


class PowerMonitorMainWindow(QtWidgets.QMainWindow):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setWindowTitle('qudi: Power Monitor')
        central = QtWidgets.QWidget()
        layout = QtWidgets.QGridLayout(central)
        self.setCentralWidget(central)

        self.reading_label = QtWidgets.QLabel('-')
        font = QtGui.QFont()
        font.setPointSize(40)
        font.setBold(True)
        self.reading_label.setFont(font)
        self.reading_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.reading_label.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.reading_label, 0, 0, 1, 3)

        self.wavelength_label = QtWidgets.QLabel('Wavelength: -')
        self.channel_label = QtWidgets.QLabel('Channel: -')
        self.start_stop_button = QtWidgets.QPushButton('Start')
        self.start_stop_button.setCheckable(True)
        self.start_stop_button.setMinimumWidth(120)
        layout.addWidget(self.wavelength_label, 1, 0)
        layout.addWidget(self.channel_label, 1, 1)
        layout.addWidget(self.start_stop_button, 1, 2)

        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setLabel('bottom', 'Time', units='s')
        self.plot_widget.showGrid(x=True, y=True, alpha=0.3)
        self.curve = self.plot_widget.plot(pen=pg.mkPen(palette.c1, width=2))
        layout.addWidget(self.plot_widget, 2, 0, 1, 3)
        layout.setRowStretch(2, 1)
        layout.setColumnStretch(1, 1)
        self.resize(700, 500)


class PowerMonitorGui(GuiBase):
    """ Large live reading and scrolling time plot of a power meter.

    Example config for copy-paste:

    power_monitor_gui:
        module.Class: 'bocquellab.power_monitor.power_monitor_gui.PowerMonitorGui'
        connect:
            power_monitor_logic: power_monitor_logic
    """

    _logic_con = Connector(name='power_monitor_logic', interface='PowerMonitorLogic')

    sigStart = QtCore.Signal()
    sigStop = QtCore.Signal()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._mw = None
        self._unit = ''

    def on_activate(self):
        logic = self._logic_con()
        self._mw = PowerMonitorMainWindow()
        self._unit = logic.unit
        self._mw.channel_label.setText(f'Channel: {logic.channel}')
        self._mw.plot_widget.setLabel('left', logic.channel, units=logic.unit)
        self._update_wavelength(logic.wavelength)
        self._update_running(logic.is_running)
        times, values = logic.data
        if len(values):
            self._update_data(times, values, values[-1])

        self._mw.start_stop_button.clicked[bool].connect(self._start_stop_clicked)
        self.sigStart.connect(logic.start_monitoring, QtCore.Qt.ConnectionType.QueuedConnection)
        self.sigStop.connect(logic.stop_monitoring, QtCore.Qt.ConnectionType.QueuedConnection)
        logic.sigDataUpdated.connect(self._update_data, QtCore.Qt.ConnectionType.QueuedConnection)
        logic.sigRunningChanged.connect(self._update_running, QtCore.Qt.ConnectionType.QueuedConnection)
        logic.sigWavelengthChanged.connect(self._update_wavelength, QtCore.Qt.ConnectionType.QueuedConnection)
        self.show()

    def on_deactivate(self):
        logic = self._logic_con()
        logic.sigDataUpdated.disconnect(self._update_data)
        logic.sigRunningChanged.disconnect(self._update_running)
        logic.sigWavelengthChanged.disconnect(self._update_wavelength)
        self.sigStart.disconnect()
        self.sigStop.disconnect()
        self._mw.start_stop_button.clicked[bool].disconnect()
        self._mw.close()
        self._mw = None

    def show(self):
        self._mw.show()
        self._mw.raise_()
        self._mw.activateWindow()

    @QtCore.Slot(bool)
    def _start_stop_clicked(self, checked):
        self._mw.start_stop_button.setEnabled(False)  # re-enabled when the logic reports its state
        if checked:
            self.sigStart.emit()
        else:
            self.sigStop.emit()

    @QtCore.Slot(bool)
    def _update_running(self, running):
        button = self._mw.start_stop_button
        button.setChecked(running)
        button.setText('Stop' if running else 'Start')
        button.setEnabled(True)

    @QtCore.Slot(float)
    def _update_wavelength(self, wavelength):
        text = '-' if not math.isfinite(wavelength) else f'{wavelength:.1f} nm'
        self._mw.wavelength_label.setText(f'Wavelength: {text}')

    @QtCore.Slot(object, object, float)
    def _update_data(self, times, values, latest):
        self._mw.reading_label.setText(format_reading(latest, self._unit))
        self._mw.curve.setData(times, values)
