# -*- coding: utf-8 -*-
"""
BocquelLab copy of qudi's time series GUI (gui/time_series/time_series_gui.py).

Differences to the original:
- channels are shown with display names (e.g. 'APD1 (pfi0)', 'APD1 (pfi0) average') taken from
  the logic option channel_display_names (axis labels, value displays, settings dialogs).
  Internally, channels keep their hardware names.
- all count-rate channels (unit counts/s) share one y-axis in kcps, so several APDs are plotted
  together. Another unit (e.g. V) gets the right axis.
- instead of qudi's single "current value channel" selector, one big value display per active
  channel, side by side (the moving average when it is enabled, the raw value otherwise). Count
  rates are shown in cps / kcps with a sensible number of digits.
- optional sum of all count-rate channels (e.g. APD1 + APD2) as extra trace and value display,
  switched on with the option show_sum.
The main window and dialogs are reused from qudi unchanged (the selector row is hidden).

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

__all__ = ['BocquelLabTimeSeriesGui', 'format_count_rate']

import pyqtgraph as pg
import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets
from typing import Union, Dict, Tuple, Optional, List

from qudi.core.statusvariable import StatusVar
from qudi.core.connector import Connector
from qudi.core.configoption import ConfigOption
from qudi.util.colordefs import QudiPalettePale as palette
from qudi.util.helpers import is_integer_type
from qudi.util.units import ScaledFloat
from qudi.core.module import GuiBase
from qudi.gui.time_series.main_window import TimeSeriesGuiMainWindow
from qudi.gui.time_series.settings_dialog import TraceViewDialog, ChannelSettingsDialog
from qudi.interface.data_instream_interface import SampleTiming
from qudi.logic.time_series_reader_logic import TimeSeriesReaderLogic

# channel units treated as count rates (shown in cps / kcps, summed by show_sum)
COUNT_RATE_UNITS = ('counts/s', 'c/s', 'cps')
_SUM_KEY = '__sum__'  # key of the sum display / curve (never a hardware channel name)
_SUM_PEN_COLOR = (215, 215, 215)  # light grey: distinct from the channel colours


def format_count_rate(value: float, decimals: Optional[int] = None) -> str:
    """ Format a count rate given in counts/s.

    decimals None (auto): resolution of exactly 1 count/s. Below 1 kcps whole cps ('873 cps'),
    from 1 kcps on kcps with 3 decimals ('1.234 kcps', '45.678 kcps', '2345.678 kcps').
    With decimals given: kcps with that many decimals.
    """
    if not np.isfinite(value):
        return f'{value} cps'
    if decimals is not None:
        return f'{value / 1e3:.{int(decimals):d}f} kcps'
    if abs(value) < 999.5:
        return f'{value:.0f} cps'
    return f'{value / 1e3:.3f} kcps'


class _ValueDisplay(QtWidgets.QWidget):
    """ Caption (channel name) above a big number. """

    def __init__(self, caption: str, point_size: int, parent=None):
        super().__init__(parent)
        self.caption_label = QtWidgets.QLabel(caption)
        self.caption_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        font = self.caption_label.font()
        font.setPointSize(12)
        self.caption_label.setFont(font)
        self.value_label = QtWidgets.QLabel('-')
        self.value_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        font = self.value_label.font()
        font.setBold(True)
        font.setPointSize(point_size)
        self.value_label.setFont(font)
        layout = QtWidgets.QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.caption_label)
        layout.addWidget(self.value_label)
        self.setLayout(layout)

    def set_caption(self, text: str) -> None:
        if self.caption_label.text() != text:
            self.caption_label.setText(text)


class BocquelLabTimeSeriesGui(GuiBase):
    """
    BocquelLab GUI for TimeSeriesReaderLogic: display names, all APDs on one kcps axis, one big
    value display per channel, optional sum of the count-rate channels.

    Display names come from the logic option channel_display_names
    (bocquellab.time_series_reader_logic.BocquelLabTimeSeriesReaderLogic). With qudi's original
    logic, hardware channel names are shown.

    The sum (trace + value display) appears only with show_sum: True and at least two active
    count-rate channels. It is the sum of the moving averages when all summed channels are
    averaged, of the raw values otherwise. It is display only (not saved).

    Example config for copy-paste:

    time_series_gui:
        module.Class: 'bocquellab.time_series.time_series_gui.BocquelLabTimeSeriesGui'
        options:
            use_antialias: True  # optional, set to False if you encounter performance issues
            show_sum: False  # optional, sum of all count-rate channels as extra trace and display
            sum_display_name: 'APD1 + APD2'  # optional, default: channel names joined with ' + '
        connect:
            _time_series_logic_con: <TimeSeriesReaderLogic_name>
    """

    # declare connectors
    _time_series_logic_con = Connector(interface=TimeSeriesReaderLogic)

    # declare ConfigOptions
    _use_antialias = ConfigOption('use_antialias', default=True, constructor=lambda x: bool(x))
    _show_sum = ConfigOption('show_sum', default=False, missing='nothing', constructor=lambda x: bool(x))
    _sum_display_name = ConfigOption('sum_display_name', default=None, missing='nothing')

    sigStartCounter = QtCore.Signal()
    sigStopCounter = QtCore.Signal()
    sigStartRecording = QtCore.Signal()
    sigStopRecording = QtCore.Signal()
    sigTraceSettingsChanged = QtCore.Signal(dict)
    sigChannelSettingsChanged = QtCore.Signal(list, list)

    _visible_traces = StatusVar(name='visible_traces', default=dict())
    _current_value_channel_precision = StatusVar(name='current_value_channel_precision',
                                                 default=dict())
    # value displays show the moving average (True) or the latest raw value (False)
    _show_average_values = StatusVar(name='show_average_values', default=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._streamer_constraints = None
        self._mw = None
        self._vb = None
        self._display_names = dict()
        self.curves = dict()
        self.averaged_curves = dict()
        self.sum_curve = None
        self._value_displays = dict()
        self._displays_widget = None
        self._average_values_action = None
        self._sum_channels = list()
        self._sum_on_left_axis = True

        self._channels_per_axis = [set(), set()]

    def on_activate(self):
        """ Initialisation of the GUI """
        self._mw = TimeSeriesGuiMainWindow()
        # Get hardware constraints
        logic = self._time_series_logic_con()
        self._streamer_constraints = logic.streamer_constraints
        all_channels = list(self._streamer_constraints.channel_units)
        names = getattr(logic, 'channel_display_names', None) or dict()
        self._display_names = {ch: names.get(ch, ch) for ch in all_channels}

        # Refine ConfigOptions
        self._visible_traces = {
            ch: self._visible_traces.get(ch, (True, True)) for ch in all_channels
        }
        self._current_value_channel_precision = {
            ch: self._current_value_channel_precision.get(ch, None) for ch in all_channels
        }

        self._replace_current_value_selector()

        # Configure PlotWidget
        if self._streamer_constraints.sample_timing == SampleTiming.RANDOM:
            self._mw.trace_plot_widget.setLabel('bottom', 'Sample')
        else:
            self._mw.trace_plot_widget.setLabel('bottom', 'Time', units='s')
        self._mw.trace_plot_widget.setMouseEnabled(x=False, y=False)
        self._mw.trace_plot_widget.setMouseTracking(False)
        self._mw.trace_plot_widget.setMenuEnabled(False)
        self._mw.trace_plot_widget.hideButtons()
        # Create second ViewBox to plot with two independent y-axes
        self._vb = pg.ViewBox()
        self._mw.trace_plot_widget.scene().addItem(self._vb)
        self._mw.trace_plot_widget.getAxis('right').linkToView(self._vb)
        self._vb.setXLink(self._mw.trace_plot_widget)
        self._vb.setMouseEnabled(x=False, y=False)
        self._vb.setMenuEnabled(False)
        # Sync resize events
        self._mw.trace_plot_widget.plotItem.vb.sigResized.connect(self.__update_viewbox_sync)

        self._mw.trace_plot_widget.disableAutoRange(axis='x')

        self.curves = dict()
        self.averaged_curves = dict()
        for i, ch in enumerate(all_channels):
            # Determine pen style
            # FIXME: Choosing a pen width != 1px (not cosmetic) causes massive performance drops
            # For mixed signals each signal type (digital or analog) has the same color
            # If just a single signal type is present, alternate the colors accordingly
            if i % 3 == 0:
                pen1 = pg.mkPen(palette.c2, cosmetic=True)
                pen2 = pg.mkPen(palette.c1, cosmetic=True)
            elif i % 3 == 1:
                pen1 = pg.mkPen(palette.c3, cosmetic=True)
                pen2 = pg.mkPen(palette.c4, cosmetic=True)
            else:
                pen1 = pg.mkPen(palette.c5, cosmetic=True)
                pen2 = pg.mkPen(palette.c6, cosmetic=True)
            self.averaged_curves[ch] = pg.PlotCurveItem(pen=pen1,
                                                        clipToView=True,
                                                        downsampleMethod='subsample',
                                                        autoDownsample=True,
                                                        antialias=self._use_antialias)
            self.curves[ch] = pg.PlotCurveItem(pen=pen2,
                                               clipToView=True,
                                               downsampleMethod='subsample',
                                               autoDownsample=True,
                                               antialias=self._use_antialias)
        self.sum_curve = pg.PlotCurveItem(pen=pg.mkPen(_SUM_PEN_COLOR, cosmetic=True),
                                          clipToView=True,
                                          downsampleMethod='subsample',
                                          autoDownsample=True,
                                          antialias=self._use_antialias)

        # Connecting user interactions
        self._mw.toggle_trace_action.triggered[bool].connect(self._trace_toggled)
        self._mw.record_trace_action.triggered[bool].connect(self._record_toggled)
        self._mw.snapshot_trace_action.triggered.connect(logic.save_trace_snapshot,
                                                         QtCore.Qt.ConnectionType.QueuedConnection)
        self._mw.settings_dockwidget.trace_length_spinbox.editingFinished.connect(
            self._trace_settings_changed
        )
        self._mw.settings_dockwidget.data_rate_spinbox.editingFinished.connect(
            self._trace_settings_changed
        )
        self._mw.settings_dockwidget.oversampling_spinbox.editingFinished.connect(
            self._trace_settings_changed
        )
        self._mw.settings_dockwidget.moving_average_spinbox.editingFinished.connect(
            self._trace_settings_changed
        )

        # Connect the default view and settings actions
        self._mw.restore_default_view_action.triggered.connect(self._restore_default_view)
        self._mw.trace_view_selection_action.triggered.connect(self._exec_trace_view_dialog)
        self._mw.channel_settings_action.triggered.connect(self._exec_channel_settings_dialog)
        self._average_values_action.toggled[bool].connect(self._average_values_toggled)

        # Connect signals to/from logic
        self.sigStartCounter.connect(logic.start_reading, QtCore.Qt.ConnectionType.QueuedConnection)
        self.sigStopCounter.connect(logic.stop_reading, QtCore.Qt.ConnectionType.QueuedConnection)
        self.sigStartRecording.connect(logic.start_recording, QtCore.Qt.ConnectionType.QueuedConnection)
        self.sigStopRecording.connect(logic.stop_recording, QtCore.Qt.ConnectionType.QueuedConnection)
        self.sigTraceSettingsChanged.connect(logic.set_trace_settings, QtCore.Qt.ConnectionType.QueuedConnection)
        self.sigChannelSettingsChanged.connect(logic.set_channel_settings,
                                               QtCore.Qt.ConnectionType.QueuedConnection)

        logic.sigDataChanged.connect(self.update_data, QtCore.Qt.ConnectionType.QueuedConnection)
        logic.sigTraceSettingsChanged.connect(self.update_trace_settings,
                                              QtCore.Qt.ConnectionType.QueuedConnection)
        logic.sigChannelSettingsChanged.connect(self.update_channel_settings,
                                                QtCore.Qt.ConnectionType.QueuedConnection)
        logic.sigStatusChanged.connect(self.update_status, QtCore.Qt.ConnectionType.QueuedConnection)

        self.update_status(running=logic.module_state() == 'locked',
                           recording=logic.data_recording_active)
        self.update_channel_settings(logic.active_channel_names, logic.averaged_channel_names)
        self.update_trace_settings(logic.trace_settings)
        self.update_data(*logic.trace_data, *logic.averaged_trace_data)
        self._apply_trace_view_settings(self.trace_view_settings)
        self.show()

    def show(self):
        """Make window visible and put it above all other windows.
        """
        self._restore_window_geometry(self._mw)
        self._mw.show()
        self._mw.raise_()
        self._mw.activateWindow()

    def on_deactivate(self):
        """ Deactivate the module
        """
        logic = self._time_series_logic_con()

        # disconnect signals
        self._mw.trace_plot_widget.plotItem.vb.sigResized.disconnect()
        self._mw.toggle_trace_action.triggered.disconnect()
        self._mw.record_trace_action.triggered.disconnect()
        self._mw.snapshot_trace_action.triggered.disconnect()
        self._mw.settings_dockwidget.trace_length_spinbox.editingFinished.disconnect()
        self._mw.settings_dockwidget.data_rate_spinbox.editingFinished.disconnect()
        self._mw.settings_dockwidget.oversampling_spinbox.editingFinished.disconnect()
        self._mw.settings_dockwidget.moving_average_spinbox.editingFinished.disconnect()
        self._mw.restore_default_view_action.triggered.disconnect()
        self._mw.trace_view_selection_action.triggered.disconnect()
        self._mw.channel_settings_action.triggered.disconnect()
        self._average_values_action.toggled.disconnect()
        self.sigStartCounter.disconnect()
        self.sigStopCounter.disconnect()
        self.sigStartRecording.disconnect()
        self.sigStopRecording.disconnect()
        self.sigTraceSettingsChanged.disconnect()
        self.sigChannelSettingsChanged.disconnect()
        logic.sigDataChanged.disconnect(self.update_data)
        logic.sigTraceSettingsChanged.disconnect(self.update_trace_settings)
        logic.sigChannelSettingsChanged.disconnect(self.update_channel_settings)
        logic.sigStatusChanged.disconnect(self.update_status)
        self._save_window_geometry(self._mw)
        self._mw.close()

    # ------------------------------------------------------------------ names and units
    def _name(self, channel: str, averaged: bool = False) -> str:
        """ Display name of a channel or of its moving average. """
        name = self._display_names.get(channel, channel)
        return f'{name} average' if averaged else name

    def _sum_name(self, averaged: bool = False) -> str:
        if self._sum_display_name:
            name = str(self._sum_display_name)
        else:
            name = ' + '.join(self._name(ch) for ch in self._sum_channels)
        return f'{name} average' if averaged else name

    @staticmethod
    def _is_count_rate(unit: str) -> bool:
        return unit in COUNT_RATE_UNITS

    def _set_axis_label(self, axis: str, text: str, unit: str) -> None:
        """ Axis label with unit. Count rates use a fixed kcps scale, other units qudi's
        automatic SI prefix.
        """
        axis_item = self._mw.trace_plot_widget.getAxis(axis)
        if self._is_count_rate(unit):
            axis_item.enableAutoSIPrefix(False)
            axis_item.setScale(1e-3)
            self._mw.trace_plot_widget.setLabel(axis, text, units='kcps')
        else:
            axis_item.setScale(1.0)
            axis_item.enableAutoSIPrefix(True)
            self._mw.trace_plot_widget.setLabel(axis, text, units=unit)

    def _axis_title(self, channels, unit: str) -> str:
        if len(channels) == 1:
            return self._name(channels[0])
        return 'Count rate' if self._is_count_rate(unit) else 'Signal'

    # ------------------------------------------------------------------ value displays
    def _replace_current_value_selector(self) -> None:
        """ Hide qudi's selector row and big label; put a row of value displays in their place. """
        self._value_displays = dict()  # widgets of a previous activation died with its window
        layout = self._mw.centralWidget().layout()
        selector_caption = layout.itemAtPosition(0, 0)
        for widget in (selector_caption.widget() if selector_caption else None,
                       self._mw.current_value_combobox,
                       self._mw.current_value_label):
            if widget is not None:
                layout.removeWidget(widget)
                widget.hide()
        self._displays_widget = QtWidgets.QWidget()
        displays_layout = QtWidgets.QHBoxLayout()
        displays_layout.setContentsMargins(0, 0, 0, 0)
        self._displays_widget.setLayout(displays_layout)
        layout.addWidget(self._displays_widget, 1, 0, 1, 2)

        # On/off switch: average or raw value in the displays (toolbar and View menu)
        self._average_values_action = QtGui.QAction('Average values', self._mw)
        self._average_values_action.setCheckable(True)
        self._average_values_action.setChecked(bool(self._show_average_values))
        self._average_values_action.setToolTip(
            'Value displays: moving average (on) or latest raw value (off)'
        )
        self._mw.toolbar.addSeparator()
        self._mw.toolbar.addAction(self._average_values_action)
        for menu_action in self._mw.menuBar().actions():
            if menu_action.text() == 'View':
                menu_action.menu().addSeparator()
                menu_action.menu().addAction(self._average_values_action)
                break

    @QtCore.Slot(bool)
    def _average_values_toggled(self, checked: bool) -> None:
        self._show_average_values = bool(checked)
        logic = self._time_series_logic_con()
        self.update_data(*logic.trace_data, *logic.averaged_trace_data)

    def _rebuild_value_displays(self, keys: List[str]) -> None:
        """ One display per key (channel name or _SUM_KEY), side by side. """
        layout = self._displays_widget.layout()
        for display in self._value_displays.values():
            layout.removeWidget(display)
            display.deleteLater()
        self._value_displays = dict()
        point_size = {0: 60, 1: 60, 2: 44}.get(len(keys), 34)  # 3 displays fit the default window
        layout.setSpacing(30)
        for key in keys:
            caption = self._sum_name() if key == _SUM_KEY else self._name(key)
            display = _ValueDisplay(caption, point_size)
            layout.addWidget(display, 1)
            self._value_displays[key] = display

    def _format_value(self, val, channel: Optional[str]) -> str:
        """ channel None: sum of count-rate channels. """
        constraints = self._streamer_constraints
        if channel is None:
            return format_count_rate(float(val))
        ch_unit = constraints.channel_units[channel]
        precision = self._current_value_channel_precision[channel]
        if self._is_count_rate(ch_unit):
            return format_count_rate(float(val), precision)
        if np.isnan(val):
            return f'{val} {ch_unit}'
        if is_integer_type(constraints.data_type):
            return f'{val:,d} {ch_unit}'
        if precision is None:
            return f'{ScaledFloat(val):.5r}{ch_unit}'
        return f'{val:,.{precision:d}f} {ch_unit}'

    # ------------------------------------------------------------------ dialogs
    @property
    def trace_view_settings(self) -> Dict[str, Tuple[bool, bool, Union[int, None]]]:
        """ Read-only """
        return {
            ch: [*flags, self._current_value_channel_precision[ch]] for ch, flags in
            self._visible_traces.items()
        }

    def _exec_trace_view_dialog(self):
        current_settings = self.trace_view_settings
        # the dialog is keyed by the labels it shows: pass display names and map back
        by_name = {self._name(ch): ch for ch in current_settings}
        dialog = TraceViewDialog(list(by_name), parent=self._mw)
        dialog.set_channel_states({self._name(ch): st for ch, st in current_settings.items()})
        # Show modal dialog and update logic if necessary
        if dialog.exec_() == QtWidgets.QDialog.DialogCode.Accepted:
            self._apply_trace_view_settings(
                {by_name[name]: st for name, st in dialog.get_channel_states().items()}
            )

    def _exec_channel_settings_dialog(self):
        logic = self._time_series_logic_con()
        active_channels, averaged_channels = logic.channel_settings
        channels = list(self._streamer_constraints.channel_units)
        channel_states = {ch: (ch in active_channels, ch in averaged_channels) for ch in channels}
        by_name = {self._name(ch): ch for ch in channels}
        dialog = ChannelSettingsDialog(list(by_name), parent=self._mw)
        dialog.set_channel_states({self._name(ch): st for ch, st in channel_states.items()})
        # Show modal dialog and update logic if necessary
        if dialog.exec_() == QtWidgets.QDialog.DialogCode.Accepted:
            self._apply_channel_settings(
                {by_name[name]: st for name, st in dialog.get_channel_states().items()}
            )

    @QtCore.Slot()
    def __update_viewbox_sync(self):
        """ Helper method to sync plots for both y-axes """
        self._vb.setGeometry(self._mw.trace_plot_widget.plotItem.vb.sceneBoundingRect())
        self._vb.linkedViewChanged(self._mw.trace_plot_widget.plotItem.vb, self._vb.XAxis)

    def _apply_trace_view_settings(self, setting):
        active_channels, averaged_channels = self._time_series_logic_con().channel_settings
        for chnl, (show_data, show_average, precision) in setting.items():
            chnl_active = chnl in active_channels
            data_visible = show_data and chnl_active
            average_visible = show_average and chnl_active and (chnl in averaged_channels)
            self._toggle_channel_data_plot(chnl, data_visible, average_visible)
            self._visible_traces[chnl] = (show_data, show_average)
            self._current_value_channel_precision[chnl] = precision

    def _apply_channel_settings(self, setting):
        self.sigChannelSettingsChanged.emit(
            [ch for ch, (enabled, _) in setting.items() if enabled],
            [ch for ch, (_, averaged) in setting.items() if averaged]
        )

    # ------------------------------------------------------------------ channels, axes, data
    @QtCore.Slot(list, list)
    def update_channel_settings(self, enabled, averaged):
        self._streamer_constraints = self._time_series_logic_con().streamer_constraints
        channel_units = self._streamer_constraints.channel_units
        enabled = list(enabled)

        # Sum of all active count-rate channels (only with show_sum and at least 2 of them)
        count_channels = [ch for ch in enabled if self._is_count_rate(channel_units[ch])]
        self._sum_channels = count_channels if (self._show_sum and len(count_channels) > 1) else list()

        # Value displays: one per active channel, then the sum
        self._rebuild_value_displays(enabled + ([_SUM_KEY] if self._sum_channels else []))

        # Axes: channels with the same unit share an axis (all APDs plotted together)
        units = list(dict.fromkeys(channel_units[ch] for ch in enabled))
        if any(self._is_count_rate(u) for u in units):
            # count rates always on the left axis
            units.sort(key=lambda u: not self._is_count_rate(u))
        if len(units) == 2:
            self._mw.trace_plot_widget.showAxis('right')
            self._channels_per_axis = [
                tuple(ch for ch in enabled if channel_units[ch] == units[0]),
                tuple(ch for ch in enabled if channel_units[ch] == units[1])
            ]
            self._set_axis_label('left', self._axis_title(self._channels_per_axis[0], units[0]), units[0])
            self._set_axis_label('right', self._axis_title(self._channels_per_axis[1], units[1]), units[1])
        elif len(units) > 2:
            self._mw.trace_plot_widget.hideAxis('right')
            self._channels_per_axis = [tuple(enabled), tuple()]
            self._set_axis_label('left', 'Signal', '')
        elif len(units) == 1:
            self._mw.trace_plot_widget.hideAxis('right')
            self._channels_per_axis = [tuple(enabled), tuple()]
            self._set_axis_label('left', self._axis_title(enabled, units[0]), units[0])
        else:
            self._mw.trace_plot_widget.hideAxis('right')
            self._channels_per_axis = [tuple(), tuple()]
        self._sum_on_left_axis = bool(self._sum_channels) and self._sum_channels[0] in self._channels_per_axis[0]

        for ch in channel_units:
            show_channel = (ch in enabled) and self._visible_traces[ch][0]
            show_average = (ch in enabled) and (ch in averaged) and self._visible_traces[ch][1]
            self._toggle_channel_data_plot(ch, show_channel, show_average)
        self._toggle_sum_plot()

    @QtCore.Slot(object, object, object, object)
    def update_data(self, data_time, data, smooth_time, smooth_data):
        """ The function that grabs the data and sends it to the plot """
        shift_time = data_time[0] != 0
        if data is not None:
            if shift_time:
                data_time = data_time - data_time[0]
            for channel, y_arr in data.items():
                self.curves[channel].setData(y=y_arr, x=data_time)
        if smooth_data is not None:
            if shift_time:
                smooth_time = smooth_time + (
                    data_time[data_time.shape[0] - smooth_time.shape[0]] - smooth_time[0]
                )
            for channel, y_arr in smooth_data.items():
                self.averaged_curves[channel].setData(y=y_arr, x=smooth_time)

        # Value displays: moving average when switched on and available, raw value otherwise
        show_average = bool(self._show_average_values) and smooth_data is not None
        for key, display in self._value_displays.items():
            try:
                if key == _SUM_KEY:
                    continue
                use_average = show_average and key in smooth_data
                val = smooth_data[key][-1] if use_average else data[key][-1]
                display.set_caption(self._name(key, averaged=use_average))
                display.value_label.setText(self._format_value(val, key))
            except (TypeError, IndexError, KeyError):
                pass

        # Sum of the count-rate channels: averages if all of them are averaged, raw otherwise
        if self._sum_channels and data is not None:
            try:
                # trace: sum of the averages when all summed channels are averaged
                avg_available = smooth_data is not None and all(ch in smooth_data for ch in self._sum_channels)
                if avg_available:
                    x, source = smooth_time, smooth_data
                else:
                    x, source = data_time, data
                y_sum = np.sum([source[ch] for ch in self._sum_channels], axis=0)
                self.sum_curve.setData(y=y_sum, x=x)
                # display: follows the "Average values" switch like the channel displays
                display = self._value_displays.get(_SUM_KEY)
                if display is not None:
                    use_average = avg_available and bool(self._show_average_values)
                    source = smooth_data if use_average else data
                    value = sum(source[ch][-1] for ch in self._sum_channels)
                    display.set_caption(self._sum_name(averaged=use_average))
                    display.value_label.setText(self._format_value(value, None))
            except (TypeError, IndexError, KeyError, ValueError):
                pass

    @QtCore.Slot(bool)
    def _trace_toggled(self, enabled: bool) -> None:
        """ Handling the toggle button to stop and start the stream """
        self._mw.toggle_trace_action.setEnabled(False)
        self._mw.record_trace_action.setEnabled(False)
        self._mw.settings_dockwidget.setEnabled(False)
        self._mw.channel_settings_action.setEnabled(False)
        if enabled:
            self._trace_settings_changed()
            self.sigStartCounter.emit()
        else:
            self.sigStopCounter.emit()

    @QtCore.Slot(bool)
    def _record_toggled(self, enabled: bool) -> None:
        """ Handling the save button to save the data into a file """
        self._mw.toggle_trace_action.setEnabled(False)
        self._mw.record_trace_action.setEnabled(False)
        if enabled:
            self.sigStartRecording.emit()
        else:
            self.sigStopRecording.emit()

    @QtCore.Slot(bool, bool)
    def update_status(self, running: bool, recording: bool) -> None:
        """ Function to ensure that the GUI represents the current measurement status """
        # Update toolbutton states
        self._mw.toggle_trace_action.setChecked(running)
        self._mw.toggle_trace_action.setText('Stop trace' if running else 'Start trace')
        self._mw.record_trace_action.setChecked(recording)
        self._mw.record_trace_action.setText('Save recorded' if recording else 'Start recording')
        # Enable/Disable widgets and actions
        self._mw.settings_dockwidget.setEnabled(True)
        self._mw.channel_settings_action.setEnabled(not running)
        self._mw.toggle_trace_action.setEnabled(True)
        self._mw.record_trace_action.setEnabled(running)

    @QtCore.Slot()
    def _trace_settings_changed(self):
        """ Handling the change of the count_length and sending it to the measurement.
        """
        settings = {
            'trace_window_size': self._mw.settings_dockwidget.trace_length_spinbox.value(),
            'data_rate': self._mw.settings_dockwidget.data_rate_spinbox.value(),
            'oversampling_factor': self._mw.settings_dockwidget.oversampling_spinbox.value(),
            'moving_average_width': self._mw.settings_dockwidget.moving_average_spinbox.value()
        }
        self.sigTraceSettingsChanged.emit(settings)

    @QtCore.Slot()
    def _restore_default_view(self):
        """ Restore the arrangement of DockWidgets to the default
        """
        # Show hidden dock widget and re-dock
        self._mw.settings_dockwidget.show()
        self._mw.settings_dockwidget.setFloating(False)
        self._mw.addDockWidget(QtCore.Qt.DockWidgetArea.BottomDockWidgetArea, self._mw.settings_dockwidget)
        # Set the toolbar to its initial top area
        self._mw.toolbar.show()
        self._mw.addToolBar(QtCore.Qt.ToolBarArea.TopToolBarArea, self._mw.toolbar)
        # Restore status if something went wrong
        self.update_status(running=self._time_series_logic_con().module_state() == 'locked',
                           recording=self._time_series_logic_con().data_recording_active)

    @QtCore.Slot(dict)
    def update_trace_settings(self, settings_dict):
        if settings_dict['oversampling_factor'] != self._mw.settings_dockwidget.oversampling_spinbox.value():
            self._mw.settings_dockwidget.oversampling_spinbox.blockSignals(True)
            self._mw.settings_dockwidget.oversampling_spinbox.setValue(
                settings_dict['oversampling_factor']
            )
            self._mw.settings_dockwidget.oversampling_spinbox.blockSignals(False)
        if settings_dict['trace_window_size'] != self._mw.settings_dockwidget.trace_length_spinbox.value():
            self._mw.settings_dockwidget.trace_length_spinbox.blockSignals(True)
            self._mw.settings_dockwidget.trace_length_spinbox.setValue(
                settings_dict['trace_window_size']
            )
            self._mw.settings_dockwidget.trace_length_spinbox.blockSignals(False)
        if settings_dict['data_rate'] != self._mw.settings_dockwidget.data_rate_spinbox.value():
            self._mw.settings_dockwidget.data_rate_spinbox.blockSignals(True)
            self._mw.settings_dockwidget.data_rate_spinbox.setValue(settings_dict['data_rate'])
            self._mw.settings_dockwidget.data_rate_spinbox.blockSignals(False)
        if settings_dict['moving_average_width'] != self._mw.settings_dockwidget.moving_average_spinbox.value():
            self._mw.settings_dockwidget.moving_average_spinbox.blockSignals(True)
            self._mw.settings_dockwidget.moving_average_spinbox.setValue(
                settings_dict['moving_average_width']
            )
            self._mw.settings_dockwidget.moving_average_spinbox.blockSignals(False)
        self._streamer_constraints = self._time_series_logic_con().streamer_constraints
        if self._streamer_constraints.sample_timing == SampleTiming.RANDOM:
            self._mw.trace_plot_widget.setRange(
                xRange=[0, settings_dict['trace_window_size'] * settings_dict['data_rate']],
                disableAutoRange=False
            )
        else:
            self._mw.trace_plot_widget.setRange(
                xRange=[0, settings_dict['trace_window_size']],
                disableAutoRange=False
            )

    def _remove_curve(self, curve) -> None:
        if curve in self._vb.addedItems:
            self._vb.removeItem(curve)
        if curve in self._mw.trace_plot_widget.items():
            self._mw.trace_plot_widget.removeItem(curve)

    def _toggle_channel_data_plot(self, channel, show_data: bool, show_average: bool):
        for curve, show in ((self.curves[channel], show_data),
                            (self.averaged_curves[channel], show_average)):
            self._remove_curve(curve)
            if show:
                if channel in self._channels_per_axis[0]:
                    self._mw.trace_plot_widget.addItem(curve)
                else:
                    self._vb.addItem(curve)

    def _toggle_sum_plot(self) -> None:
        self._remove_curve(self.sum_curve)
        if self._sum_channels:
            if self._sum_on_left_axis:
                self._mw.trace_plot_widget.addItem(self.sum_curve)
            else:
                self._vb.addItem(self.sum_curve)
