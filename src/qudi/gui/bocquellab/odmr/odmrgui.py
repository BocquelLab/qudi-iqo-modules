# -*- coding: utf-8 -*-
"""
BocquelLab extension of qudi's ODMR GUI (qudi.gui.odmr.odmrgui): adds a "Microwave readback" dock
showing the frequency, power and RF output state read back from the microwave source after each
change made by qudi (BocquelLabOdmrLogic.sigMicrowaveReadback). Everything else is inherited
unchanged.

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

__all__ = ['BocquelLabOdmrGui', 'MicrowaveReadbackDockWidget']

from PySide6 import QtCore, QtWidgets

from qudi.gui.odmr.odmrgui import OdmrGui


class MicrowaveReadbackDockWidget(QtWidgets.QDockWidget):
    """ Read-only display of the microwave source state as last read back by the logic. """

    def __init__(self, parent=None):
        super().__init__('Microwave readback', parent)
        self.setObjectName('microwave_readback_dockWidget')
        self.setFeatures(QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetMovable |
                         QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetFloatable)
        widget = QtWidgets.QWidget()
        layout = QtWidgets.QGridLayout()
        widget.setLayout(layout)
        self.setWidget(widget)

        self.frequency_label = QtWidgets.QLabel('-')
        self.power_label = QtWidgets.QLabel('-')
        self.rf_label = QtWidgets.QLabel('-')
        self.time_label = QtWidgets.QLabel('-')
        for row, (caption, label) in enumerate((('Frequency:', self.frequency_label),
                                                ('Power:', self.power_label),
                                                ('RF output:', self.rf_label),
                                                ('Read at:', self.time_label))):
            caption_label = QtWidgets.QLabel(caption)
            caption_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight |
                                       QtCore.Qt.AlignmentFlag.AlignVCenter)
            if label is not self.time_label:
                font = label.font()
                font.setBold(True)
                font.setPointSize(font.pointSize() + 4)
                label.setFont(font)
            label.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
            layout.addWidget(caption_label, row, 0)
            layout.addWidget(label, row, 1)
        layout.setColumnStretch(1, 1)
        layout.setRowStretch(4, 1)
        self.setToolTip('Values read back from the microwave source after each change made by qudi.\n'
                        'Changes made on the generator front panel appear at the next read-back.')

    def set_readback(self, readback: dict) -> None:
        frequency = readback.get('frequency')
        power = readback.get('power')
        rf_on = readback.get('rf_on')
        self.frequency_label.setText('?' if frequency is None else f'{frequency / 1e6:.6f} MHz')
        self.power_label.setText('?' if power is None else f'{power:+.2f} dBm')
        if rf_on is None:
            self.rf_label.setText('?')
            self.rf_label.setStyleSheet('')
        else:
            text = 'ON' if rf_on else 'OFF'
            if not readback.get('rf_state_from_device', False):
                text += ' (qudi state)'
            self.rf_label.setText(text)
            self.rf_label.setStyleSheet('color: #ff5555;' if rf_on else '')
        self.time_label.setText(str(readback.get('time', '-')))


class BocquelLabOdmrGui(OdmrGui):
    """ qudi's OdmrGui plus a "Microwave readback" dock (needs BocquelLabOdmrLogic; with qudi's
    original OdmrLogic the dock stays empty).

    Example config for copy-paste:

    odmr_gui:
        module.Class: 'bocquellab.odmr.odmrgui.BocquelLabOdmrGui'
        connect:
            odmr_logic: 'odmr_logic'
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._readback_dockwidget = None

    def on_activate(self):
        super().on_activate()
        logic = self._odmr_logic()
        self._readback_dockwidget = MicrowaveReadbackDockWidget(parent=self._mw)
        self._place_readback_dock()
        if hasattr(logic, 'sigMicrowaveReadback'):
            logic.sigMicrowaveReadback.connect(self._update_microwave_readback,
                                               QtCore.Qt.ConnectionType.QueuedConnection)
            readback = logic.last_microwave_readback
            if readback is not None:
                self._update_microwave_readback(readback)
        else:
            self.log.warning('Connected ODMR logic has no microwave read-back '
                             '(use bocquellab.odmr_logic.BocquelLabOdmrLogic).')

    def on_deactivate(self):
        logic = self._odmr_logic()
        if hasattr(logic, 'sigMicrowaveReadback'):
            try:
                logic.sigMicrowaveReadback.disconnect(self._update_microwave_readback)
            except (RuntimeError, TypeError):
                pass
        super().on_deactivate()

    def _place_readback_dock(self) -> None:
        if self._readback_dockwidget is None:
            return
        self._readback_dockwidget.setFloating(False)
        self._readback_dockwidget.show()
        self._mw.addDockWidget(QtCore.Qt.DockWidgetArea.TopDockWidgetArea, self._readback_dockwidget)
        # next to the CW controls
        self._mw.splitDockWidget(self._cw_control_dockwidget, self._readback_dockwidget,
                                 QtCore.Qt.Orientation.Horizontal)

    @QtCore.Slot()
    def restore_default_view(self):
        super().restore_default_view()
        self._place_readback_dock()

    @QtCore.Slot(dict)
    def _update_microwave_readback(self, readback: dict) -> None:
        self._readback_dockwidget.set_readback(readback)
