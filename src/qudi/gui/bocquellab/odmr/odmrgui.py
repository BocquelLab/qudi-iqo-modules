# -*- coding: utf-8 -*-
"""
BocquelLab extension of qudi's ODMR GUI (qudi.gui.odmr.odmrgui): the CW control dock also shows the
frequency, power and RF output state read back from the microwave source after each change made by
qudi (BocquelLabOdmrLogic.sigMicrowaveReadback). Each read-back value sits directly below its
setting; the RF output state is to the right of the four fields. Everything else is inherited
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

__all__ = ['BocquelLabOdmrGui']

from PySide6 import QtCore, QtWidgets

from qudi.util.units import ScaledFloat
from qudi.gui.odmr.odmrgui import OdmrGui

_READBACK_TOOLTIP = ('Read back from the microwave source after the last change made by qudi.\n'
                     'Changes made on the generator front panel appear at the next read-back.')


class BocquelLabOdmrGui(OdmrGui):
    """ qudi's OdmrGui with the microwave read-back in the CW control dock (needs
    BocquelLabOdmrLogic; with qudi's original OdmrLogic the read-back fields stay empty).

    Example config for copy-paste:

    odmr_gui:
        module.Class: 'bocquellab.odmr.odmrgui.BocquelLabOdmrGui'
        connect:
            odmr_logic: 'odmr_logic'
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._readback_power_label = None
        self._readback_frequency_label = None
        self._readback_rf_label = None

    def on_activate(self):
        super().on_activate()
        self._add_readback_to_cw_dock()
        logic = self._odmr_logic()
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

    def _add_readback_to_cw_dock(self) -> None:
        """ Rebuild the CW dock as a grid, re-using qudi's two spinboxes:

            CW Power:  [spinbox]   CW Frequency: [spinbox]  | RF output
            Read back:  value      Read back:     value     |    ON
        """
        dock = self._cw_control_dockwidget
        power_spinbox = dock.cw_power_spinbox
        frequency_spinbox = dock.cw_frequency_spinbox

        def caption(text: str) -> QtWidgets.QLabel:
            label = QtWidgets.QLabel(text)
            label.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
            return label

        def value_label() -> QtWidgets.QLabel:
            label = QtWidgets.QLabel('-')
            label.setToolTip(_READBACK_TOOLTIP)
            label.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
            font = label.font()
            font.setBold(True)
            label.setFont(font)
            return label

        self._readback_power_label = value_label()
        self._readback_frequency_label = value_label()
        self._readback_rf_label = value_label()
        self._readback_rf_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        font = self._readback_rf_label.font()
        font.setPointSize(font.pointSize() + 4)
        self._readback_rf_label.setFont(font)
        rf_caption = QtWidgets.QLabel('RF output')
        rf_caption.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        rf_caption.setToolTip(_READBACK_TOOLTIP)

        widget = QtWidgets.QWidget()
        layout = QtWidgets.QGridLayout()
        layout.setContentsMargins(1, 1, 1, 1)
        widget.setLayout(layout)
        layout.addWidget(caption('CW Power:'), 0, 0)
        layout.addWidget(power_spinbox, 0, 1)  # re-parented from qudi's dock widget
        layout.addWidget(caption('CW Frequency:'), 0, 2)
        layout.addWidget(frequency_spinbox, 0, 3)
        layout.addWidget(caption('Read back:'), 1, 0)
        layout.addWidget(self._readback_power_label, 1, 1)
        layout.addWidget(caption('Read back:'), 1, 2)
        layout.addWidget(self._readback_frequency_label, 1, 3)
        separator = QtWidgets.QFrame()
        separator.setFrameShape(QtWidgets.QFrame.Shape.VLine)
        separator.setFrameShadow(QtWidgets.QFrame.Shadow.Sunken)
        layout.addWidget(separator, 0, 4, 2, 1)
        layout.addWidget(rf_caption, 0, 5)
        layout.addWidget(self._readback_rf_label, 1, 5)
        layout.setColumnStretch(1, 1)
        layout.setColumnStretch(3, 1)
        widget.setSizePolicy(QtWidgets.QSizePolicy.Policy.Preferred, QtWidgets.QSizePolicy.Policy.Fixed)
        old_widget = dock.widget()
        dock.setWidget(widget)
        if old_widget is not None:
            old_widget.deleteLater()  # empty now: its two spinboxes moved to the new grid

    @QtCore.Slot(dict)
    def _update_microwave_readback(self, readback: dict) -> None:
        frequency = readback.get('frequency')
        power = readback.get('power')
        rf_on = readback.get('rf_on')
        self._readback_power_label.setText('?' if power is None else f'{power:.2f} dBm')
        self._readback_frequency_label.setText(
            '?' if frequency is None else f'{ScaledFloat(frequency):.6r}Hz'
        )
        if rf_on is None:
            self._readback_rf_label.setText('?')
            self._readback_rf_label.setStyleSheet('')
        else:
            text = 'ON' if rf_on else 'OFF'
            if not readback.get('rf_state_from_device', False):
                text += '*'
                self._readback_rf_label.setToolTip(_READBACK_TOOLTIP + '\n* qudi state, not read from the device.')
            self._readback_rf_label.setText(text)
            self._readback_rf_label.setStyleSheet('color: #ff5555;' if rf_on else '')
