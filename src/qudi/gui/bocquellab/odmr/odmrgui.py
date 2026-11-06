# -*- coding: utf-8 -*-
"""
BocquelLab extension of qudi's ODMR GUI (qudi.gui.odmr.odmrgui): the CW control dock also shows the
frequency, power and RF output state read back from the microwave source after each change made by
qudi (BocquelLabOdmrLogic.sigMicrowaveReadback). Each read-back value sits directly below its
setting; the RF output state is to the right of the four fields. A "Settle time" field in the
scan control dock (BocquelLabOdmrLogic.set_settle_time). Default view: control docks stacked on the
left, plots on the right. Everything else is inherited unchanged.

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

from qudi.core.statusvariable import StatusVar
from qudi.util.units import ScaledFloat
from qudi.util.widgets.scientific_spinbox import ScienDSpinBox
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

    # Bump when the default layout changes: a window layout saved by an older version is then
    # replaced once by the new default view (afterwards qudi keeps restoring the user's layout).
    _DEFAULT_LAYOUT_VERSION = 1
    _layout_version = StatusVar(name='default_layout_version', default=0)

    sigSettleTimeChanged = QtCore.Signal(float)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._readback_power_label = None
        self._readback_frequency_label = None
        self._readback_rf_label = None
        self._settle_time_spinbox = None
        self._applied_settle_time = None  # last value confirmed by the logic

    def on_activate(self):
        super().on_activate()  # ends with show(), which restores the saved window layout
        self._add_readback_to_cw_dock()
        self._add_settle_time_to_scan_dock()
        if self._layout_version != self._DEFAULT_LAYOUT_VERSION:
            self.restore_default_view()
            self._layout_version = self._DEFAULT_LAYOUT_VERSION
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
        for signal, slot in (('sigMicrowaveReadback', self._update_microwave_readback),
                             ('sigSettleTimeUpdated', self._update_settle_time),
                             ('sigScanStateUpdated', self._settle_time_scan_state)):
            if hasattr(logic, signal):
                try:
                    getattr(logic, signal).disconnect(slot)
                except (RuntimeError, TypeError):
                    pass
        try:
            self.sigSettleTimeChanged.disconnect()
        except (RuntimeError, TypeError):
            pass
        super().on_deactivate()

    def _add_settle_time_to_scan_dock(self) -> None:
        """ "Settle time" row in the Runtime Parameters box of the scan control dock. Hidden if the
        logic or its data scanner has no settle time.
        """
        logic = self._odmr_logic()
        settle_time = getattr(logic, 'settle_time', None)
        if settle_time is None:
            return
        grid = self._scan_control_dockwidget.runtime_spinbox.parentWidget().layout()
        label = QtWidgets.QLabel('Settle time:')
        label.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
        spinbox = ScienDSpinBox()
        spinbox.setSuffix('s')
        spinbox.setRange(0.0, 1.0)
        spinbox.setDecimals(3)
        spinbox.setMinimumWidth(self._scan_control_dockwidget.runtime_spinbox.minimumWidth())
        tooltip = ('Wait after each frequency step before counting.\n'
                   'SG384 spec: frequency settles to within 1 ppm in < 8 ms.\n'
                   'Shorter = faster sweeps; too short may broaden or shift the ODMR lines.')
        for widget in (label, spinbox):
            widget.setToolTip(tooltip)
        spinbox.setValue(settle_time)
        self._applied_settle_time = float(settle_time)
        row = grid.rowCount()
        grid.addWidget(label, row, 0)
        grid.addWidget(spinbox, row, 1)
        self._settle_time_spinbox = spinbox

        spinbox.editingFinished.connect(self._settle_time_edited)
        self.sigSettleTimeChanged.connect(logic.set_settle_time, QtCore.Qt.ConnectionType.QueuedConnection)
        logic.sigSettleTimeUpdated.connect(self._update_settle_time, QtCore.Qt.ConnectionType.QueuedConnection)
        logic.sigScanStateUpdated.connect(self._settle_time_scan_state, QtCore.Qt.ConnectionType.QueuedConnection)
        spinbox.setEnabled(logic.module_state() == 'idle')

    @QtCore.Slot(bool)
    def run_stop_odmr(self, is_checked):
        if is_checked:
            self._settle_time_edited()  # apply a typed but not yet confirmed value before the start
        super().run_stop_odmr(is_checked)

    @QtCore.Slot()
    def resume_odmr(self):
        self._settle_time_edited()
        super().resume_odmr()

    @QtCore.Slot()
    def _settle_time_edited(self) -> None:
        # editingFinished also fires when the field loses focus, e.g. when it is disabled at scan
        # start while the cursor is still in it: only send real changes, never from a locked field.
        spinbox = self._settle_time_spinbox
        if spinbox is None or not spinbox.isEnabled():
            return
        value = float(spinbox.value())
        if self._applied_settle_time is not None and abs(value - self._applied_settle_time) < 1e-9:
            return
        self.sigSettleTimeChanged.emit(value)

    @QtCore.Slot(object)
    def _update_settle_time(self, seconds) -> None:
        if self._settle_time_spinbox is not None and seconds is not None:
            self._applied_settle_time = float(seconds)
            self._settle_time_spinbox.blockSignals(True)
            self._settle_time_spinbox.setValue(seconds)
            self._settle_time_spinbox.blockSignals(False)

    @QtCore.Slot(bool)
    def _settle_time_scan_state(self, running: bool) -> None:
        spinbox = self._settle_time_spinbox
        if spinbox is not None:
            spinbox.setEnabled(not running)
            if running and self._applied_settle_time is not None:
                self._update_settle_time(self._applied_settle_time)  # always show the value in use

    def _add_readback_to_cw_dock(self) -> None:
        """ Rebuild the CW dock as a grid, re-using qudi's two spinboxes:

            CW Power:      [spinbox]   CW Frequency:      [spinbox]  | RF output
            Current power:  value      Current frequency:  value     |    ON
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
        layout.addWidget(caption('Current power:'), 1, 0)
        layout.addWidget(self._readback_power_label, 1, 1)
        layout.addWidget(caption('Current frequency:'), 1, 2)
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

    @QtCore.Slot()
    def restore_default_view(self):
        """ Default layout: CW control, scan control and fit stacked top to bottom on the left,
        the plots fill the right side (central widget).
        """
        super().restore_default_view()  # floating state, CW dock visibility and menu check state
        docks = (self._cw_control_dockwidget, self._scan_control_dockwidget, self._fit_dockwidget)
        for dock in docks:
            dock.setFloating(False)
            self._mw.addDockWidget(QtCore.Qt.DockWidgetArea.LeftDockWidgetArea, dock)
        self._mw.splitDockWidget(docks[0], docks[1], QtCore.Qt.Orientation.Vertical)
        self._mw.splitDockWidget(docks[1], docks[2], QtCore.Qt.Orientation.Vertical)

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
