# -*- coding: utf-8 -*-

"""
BocquelLab copy of qudi's laser control dock widget (qudi.gui.laser.laser_control_dockwidget):
no power / current sliders, the measured power and current shown as labelled text, and fixed display units
(power in mW, current in mA) instead of automatic SI prefixes.

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

__all__ = ('BocquelLabLaserControlDockWidget', 'FixedUnitSpinBox')

from importlib import resources

from PySide6 import QtCore, QtWidgets, QtGui

import qudi.artwork.icons
from qudi.util.widgets.advanced_dockwidget import AdvancedDockWidget
from qudi.interface.simple_laser_interface import ControlMode

# display unit for each SI unit the logic reports: (factor SI -> display, display unit)
_DISPLAY_UNITS = {'W': (1e3, 'mW'), 'A': (1e3, 'mA')}


class FixedUnitSpinBox(QtWidgets.QDoubleSpinBox):
    """ Spin box that shows and edits a value in a fixed unit (e.g. mW), while value(), setValue() and setRange()
    use the SI unit (e.g. W) like qudi's ScienDSpinBox, so the laser GUI code can use it unchanged.
    """

    def __init__(self, si_unit, decimals=2, parent=None):
        super().__init__(parent)
        self._scale = 1.0
        self._si_range = (0.0, 0.0)
        self.setDecimals(decimals)
        self.setKeyboardTracking(False)
        self.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
        self.set_si_unit(si_unit)

    def set_si_unit(self, si_unit):
        """ Unit of value() (e.g. 'W', 'A'); the display unit follows from _DISPLAY_UNITS (unknown: shown as is). """
        value = self.value()
        self._scale, unit = _DISPLAY_UNITS.get(si_unit, (1.0, si_unit))
        self.setSuffix(f' {unit}' if unit else '')
        self.setRange(*self._si_range)
        self.setValue(value)

    def value(self):
        return super().value() / self._scale

    def setValue(self, value):
        super().setValue(float(value) * self._scale)

    def setRange(self, minimum, maximum):
        self._si_range = (float(minimum), float(maximum))
        super().setRange(self._si_range[0] * self._scale, self._si_range[1] * self._scale)

    def setMinimum(self, minimum):
        self.setRange(minimum, max(minimum, self._si_range[1]))

    def setMaximum(self, maximum):
        self.setRange(min(maximum, self._si_range[0]), maximum)


def format_fixed_unit(value, si_unit, decimals=2):
    """ '40.00 mW' for (0.04, 'W'); '-' for None. """
    if value is None:
        return '-'
    scale, unit = _DISPLAY_UNITS.get(si_unit, (1.0, si_unit))
    return f'{float(value) * scale:.{decimals}f} {unit}'.rstrip()


class BocquelLabLaserControlDockWidget(AdvancedDockWidget):
    """ Laser controls: polling, laser and shutter buttons, control mode, and per quantity the measured value
    (read-only text) and the setpoint (spin box).

    The measured values are QLabels on purpose: in qudi's original dock they are read-only ScienDSpinBoxes without
    buttons, whose text field collapses to 1 px under the qdark stylesheet, so the values were never visible.
    """
    sigControlModeChanged = QtCore.Signal(object)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._current_unit = 'A'

        main_widget = QtWidgets.QWidget()
        main_layout = QtWidgets.QGridLayout()
        main_layout.setContentsMargins(1, 1, 1, 1)
        main_widget.setLayout(main_layout)
        self.setWidget(main_widget)

        self.polling_button = QtWidgets.QPushButton(
            QtGui.QIcon(str(resources.files(qudi.artwork.icons) / "start-counter.svg")), "Start Laser Polling"
        )
        self.polling_button.setCheckable(True)
        main_layout.addWidget(self.polling_button, 0, 0, 1, 2)
        self.laser_button = QtWidgets.QPushButton('Laser')
        self.laser_button.setCheckable(True)
        main_layout.addWidget(self.laser_button, 1, 0)
        self.shutter_button = QtWidgets.QPushButton('Shutter')
        self.shutter_button.setCheckable(True)
        main_layout.addWidget(self.shutter_button, 1, 1)

        group_box = QtWidgets.QGroupBox('Control Mode')
        layout = QtWidgets.QHBoxLayout()
        group_box.setLayout(layout)
        button_group = QtWidgets.QButtonGroup(self)
        self.control_power_radio_button = QtWidgets.QRadioButton('Power')
        self.control_current_radio_button = QtWidgets.QRadioButton('Current')
        button_group.addButton(self.control_power_radio_button)
        button_group.addButton(self.control_current_radio_button)
        layout.addWidget(self.control_power_radio_button)
        layout.addWidget(self.control_current_radio_button)
        self.control_power_radio_button.clicked.connect(
            lambda: self.sigControlModeChanged.emit(ControlMode.POWER)
        )
        self.control_current_radio_button.clicked.connect(
            lambda: self.sigControlModeChanged.emit(ControlMode.CURRENT)
        )
        main_layout.addWidget(group_box, 2, 0, 1, 2)

        self.measured_power_label, self.power_setpoint_spinbox = self._add_quantity_box(main_layout, 3, 'Power', 'W')
        self.measured_current_label, self.current_setpoint_spinbox = self._add_quantity_box(
            main_layout, 4, 'Current', self._current_unit
        )
        main_layout.setRowStretch(5, 1)
        main_widget.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed, QtWidgets.QSizePolicy.Policy.Expanding)

    @staticmethod
    def _add_quantity_box(main_layout, row, title, si_unit):
        group_box = QtWidgets.QGroupBox(title)
        layout = QtWidgets.QGridLayout()
        group_box.setLayout(layout)
        layout.addWidget(QtWidgets.QLabel('Measured:'), 0, 0)
        measured_label = QtWidgets.QLabel('-')
        measured_label.setMinimumWidth(90)
        measured_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
        measured_label.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        font = measured_label.font()
        font.setBold(True)
        measured_label.setFont(font)
        layout.addWidget(measured_label, 0, 1)
        layout.addWidget(QtWidgets.QLabel('Setpoint:'), 1, 0)
        setpoint_spinbox = FixedUnitSpinBox(si_unit)
        setpoint_spinbox.setMinimumWidth(90)
        layout.addWidget(setpoint_spinbox, 1, 1)
        main_layout.addWidget(group_box, row, 0, 1, 2)
        return measured_label, setpoint_spinbox

    def set_current_unit(self, unit):
        """ @param str unit: current unit of the laser ('A' is shown in mA, anything else as it is, e.g. '%') """
        self._current_unit = unit
        self.current_setpoint_spinbox.set_si_unit(unit)

    def set_measured_power(self, value):
        """ @param float value: measured power in W, or None if unknown; shown in mW """
        self.measured_power_label.setText(format_fixed_unit(value, 'W'))

    def set_measured_current(self, value):
        """ @param float value: measured current in the laser's current unit, or None if unknown; A shown in mA """
        self.measured_current_label.setText(format_fixed_unit(value, self._current_unit))
