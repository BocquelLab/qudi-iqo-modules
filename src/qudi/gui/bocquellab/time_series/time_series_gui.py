# -*- coding: utf-8 -*-
"""
BocquelLab copy of qudi's time series GUI (gui/time_series/time_series_gui.py).

Differences to the original:
- channels are shown with display names (e.g. 'APD1 (pfi0)', 'APD1 (pfi0) average') taken from
  the logic option channel_display_names, in the plot legend (new), the current-value channel
  list, the axis labels and the settings dialogs. Internally, channels keep their hardware names.
- count-rate channels (unit counts/s) are shown in kcps on the plot axis, and in cps / kcps with a
  sensible number of digits in the big current-value display.
- the current-value channel falls back to the option default_current_value_channel when no valid
  choice is saved.
The main window and dialogs are reused from qudi unchanged.

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
from PySide6 import QtCore, QtWidgets
from typing import Union, Dict, Tuple, Optional

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

# channel units treated as count rates (shown in cps / kcps)
COUNT_RATE_UNITS = ('counts/s', 'c/s', 'cps')
_AVERAGE_PREFIX = 'average '  # internal key of an averaged channel: 'average <channel>'
_NONE_KEY = 'None'


def format_count_rate(value: float, decimals: Optional[int] = None) -> str:
    """ Format a count rate given in counts/s.

    decimals None (auto): below 1 kcps whole cps ('873 cps'); 1-100 kcps one decimal in kcps
    ('1.2 kcps', '45.6 kcps'); from 100 kcps on whole kcps ('345 kcps', '2345 kcps').
    The resolution is never finer than 1 count/s. With decimals given: kcps with that many decimals.
    """
    if not np.isfinite(value):
        return f'{value} cps'
    if decimals is not None:
        return f'{value / 1e3:.{int(decimals):d}f} kcps'
    if abs(value) < 999.5:
        return f'{value:.0f} cps'
    if abs(value) < 99950:
        return f'{value / 1e3:.1f} kcps'
    return f'{value / 1e3:.0f} kcps'


class BocquelLabTimeSeriesGui(GuiBase):
    """
    BocquelLab GUI for TimeSeriesReaderLogic: display names, legend, count rates in kcps.

    Display names come from the logic option channel_display_names
    (bocquellab.time_series_reader_logic.BocquelLabTimeSeriesReaderLogic). With qudi's original
    logic, hardware channel names are shown.

    Example config for copy-paste:

    time_series_gui:
        module.Class: 'bocquellab.time_series.time_series_gui.BocquelLabTimeSeriesGui'
        options:
            use_antialias: True  # optional, set to False if you encounter performance issues
            default_current_value_channel: 'average pfi0'  # optional, '<channel>' or 'average <channel>'
        connect:
            _time_series_logic_con: <TimeSeriesReaderLogic_name>
    """

    # declare connectors
    _time_series_logic_con = Connector(interface=TimeSeriesReaderLogic)

    # declare ConfigOptions
    _use_antialias = ConfigOption('use_antialias', default=True, constructor=lambda x: bool(x))
    _default_current_value_channel = ConfigOption('default_current_value_channel', default=None,
                                                  missing='nothing')

    sigStartCounter = QtCore.Signal()
    sigStopCounter = QtCore.Signal()
    sigStartRecording = QtCore.Signal()
    sigStopRecording = QtCore.Signal()
    sigTraceSettingsChanged = QtCore.Signal(dict)
    sigChannelSettingsChanged = QtCore.Signal(list, list)

    # Internal key of the current-value channel: 'None', '<channel>' or 'average <channel>'.
    # None (nothing saved yet) selects default_current_value_channel.
    _current_value_channel = StatusVar(name='current_value_channel', default=None)
    _visible_traces = StatusVar(name='visible_traces', default=dict())
    _current_value_channel_precision = StatusVar(name='current_value_channel_precision',
                                                 default=dict())

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._streamer_constraints = None
        self._mw = None
        self._vb = None
        self._legend = None
        self._display_names = dict()
        self.curves = dict()
        self.averaged_curves = dict()

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
        # Legend: entries are managed in _toggle_channel_data_plot (curves on both view boxes)
        self._legend = self._mw.trace_plot_widget.addLegend()

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
        self._mw.current_value_combobox.currentIndexChanged.connect(
            self._current_value_channel_changed
        )

        # Connect the default view and settings actions
        self._mw.restore_default_view_action.triggered.connect(self._restore_default_view)
        self._mw.trace_view_selection_action.triggered.connect(self._exec_trace_view_dialog)
        self._mw.channel_settings_action.triggered.connect(self._exec_channel_settings_dialog)

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
        self._current_value_channel_changed()
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
        self._mw.current_value_combobox.currentIndexChanged.disconnect()
        self._mw.restore_default_view_action.triggered.disconnect()
        self._mw.trace_view_selection_action.triggered.disconnect()
        self._mw.channel_settings_action.triggered.disconnect()
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

    def _axis_title(self, unit: str) -> str:
        return 'Count rate' if self._is_count_rate(unit) else 'Signal'

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

    # ------------------------------------------------------------------ current value channel
    def _select_current_value_channel(self) -> None:
        """ Select the saved channel; if it is not available, default_current_value_channel;
        otherwise 'None'. Signals of the combobox must be blocked by the caller.
        """
        combobox = self._mw.current_value_combobox
        index = -1
        if self._current_value_channel is not None:
            index = combobox.findData(str(self._current_value_channel))
        if index < 0 and self._default_current_value_channel is not None:
            index = combobox.findData(str(self._default_current_value_channel))
        combobox.setCurrentIndex(max(index, 0))

    @QtCore.Slot(list, list)
    def update_channel_settings(self, enabled, averaged):
        # Update combobox: text = display name, item data = internal key
        combobox = self._mw.current_value_combobox
        combobox.blockSignals(True)
        try:
            combobox.clear()
            combobox.addItem('None', _NONE_KEY)
            for ch in averaged:
                if ch in enabled:
                    combobox.addItem(self._name(ch, averaged=True), f'{_AVERAGE_PREFIX}{ch}')
            for ch in enabled:
                combobox.addItem(self._name(ch), ch)
            self._select_current_value_channel()
        finally:
            combobox.blockSignals(False)
        self._current_value_channel_changed()

        # Update plot widget axes
        self._streamer_constraints = self._time_series_logic_con().streamer_constraints
        channel_units = self._streamer_constraints.channel_units
        different_units = list({unit for ch, unit in channel_units.items() if ch in enabled})
        self._channels_per_axis = list()
        if len(different_units) == 2:
            self._mw.trace_plot_widget.showAxis('right')
            self._channels_per_axis = [
                tuple(ch for ch in enabled if channel_units[ch] == different_units[0]),
                tuple(ch for ch in enabled if channel_units[ch] == different_units[1])
            ]
            if len(enabled) == 2:
                self._set_axis_label('left', self._name(self._channels_per_axis[0][0]), different_units[0])
                self._set_axis_label('right', self._name(self._channels_per_axis[1][0]), different_units[1])
            else:
                self._set_axis_label('left', self._axis_title(different_units[0]), different_units[0])
                self._set_axis_label('right', self._axis_title(different_units[1]), different_units[1])
        elif len(different_units) > 2:
            self._mw.trace_plot_widget.hideAxis('right')
            self._channels_per_axis = [tuple(enabled), tuple()]
            self._set_axis_label('left', 'Signal', '')
        elif len(enabled) == 2:
            # same unit on both channels: as in qudi, one channel per y-axis (independent scaling)
            self._mw.trace_plot_widget.hideAxis('right')
            self._channels_per_axis = [(enabled[0],), (enabled[1],)]
            self._set_axis_label('left', self._name(enabled[0]), channel_units[enabled[0]])
            self._set_axis_label('right', self._name(enabled[1]), channel_units[enabled[1]])
        elif len(enabled) > 2:
            self._mw.trace_plot_widget.hideAxis('right')
            self._channels_per_axis = [tuple(enabled), tuple()]
            self._set_axis_label('left', self._axis_title(different_units[0]), different_units[0])
        elif len(enabled) == 1:
            self._mw.trace_plot_widget.hideAxis('right')
            self._channels_per_axis = [tuple(enabled), tuple()]
            self._set_axis_label('left', self._name(enabled[0]), different_units[0])
        else:
            self._mw.trace_plot_widget.hideAxis('right')
            self._channels_per_axis = [tuple(), tuple()]

        for ch in channel_units:
            show_channel = (ch in enabled) and self._visible_traces[ch][0]
            show_average = (ch in enabled) and (ch in averaged) and self._visible_traces[ch][1]
            self._toggle_channel_data_plot(ch, show_channel, show_average)

    def _format_value(self, val, channel: str) -> str:
        constraints = self._streamer_constraints
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

        key = self._mw.current_value_combobox.currentData()
        if key and key != _NONE_KEY:
            try:
                if key.startswith(_AVERAGE_PREFIX):
                    channel = key[len(_AVERAGE_PREFIX):]
                    val = smooth_data[channel][-1]
                else:
                    channel = key
                    val = data[channel][-1]
                self._mw.current_value_label.setText(self._format_value(val, channel))
            except (TypeError, IndexError, KeyError):
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
    def _current_value_channel_changed(self):
        key = self._mw.current_value_combobox.currentData()
        if key is None or key == _NONE_KEY:
            self._mw.current_value_label.setVisible(False)
            self._mw.current_value_label.setText('0')
        else:
            self._mw.current_value_label.setVisible(True)
        if key is not None:
            self._current_value_channel = key

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

    def _remove_channel_from_plot(self, channel: str) -> None:
        data_curve = self.curves[channel]
        average_curve = self.averaged_curves[channel]
        for curve in (data_curve, average_curve):
            if curve in self._vb.addedItems:
                self._vb.removeItem(curve)
            if curve in self._mw.trace_plot_widget.items():
                self._mw.trace_plot_widget.removeItem(curve)
            self._legend.removeItem(curve)

    def _toggle_channel_data_plot(self, channel, show_data: bool, show_average: bool):
        self._remove_channel_from_plot(channel)
        for curve, show, averaged in ((self.curves[channel], show_data, False),
                                      (self.averaged_curves[channel], show_average, True)):
            if not show:
                continue
            if channel in self._channels_per_axis[0]:
                self._mw.trace_plot_widget.addItem(curve)
            else:
                self._vb.addItem(curve)
            self._legend.addItem(curve, self._name(channel, averaged))
