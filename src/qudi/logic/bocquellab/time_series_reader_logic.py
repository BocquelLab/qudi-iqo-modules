# -*- coding: utf-8 -*-
"""
BocquelLab version of qudi's time series reader logic: adds human-readable channel names (e.g.
'APD1 (pfi0)') that are used in the column headers of saved data, and saves the moving average
next to the raw data in trace snapshots. Acquisition is unchanged (inherited from qudi).

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

__all__ = ['BocquelLabTimeSeriesReaderLogic']

import datetime as dt
import numpy as np
from typing import Dict, Optional
from PySide6 import QtCore

from qudi.core.configoption import ConfigOption
from qudi.util.datastorage import TextDataStorage
from qudi.interface.data_instream_interface import SampleTiming
from qudi.logic.time_series_reader_logic import TimeSeriesReaderLogic


class BocquelLabTimeSeriesReaderLogic(TimeSeriesReaderLogic):
    """ qudi's TimeSeriesReaderLogic with display names for the streamer channels.

    Channels keep their hardware names internally (e.g. 'pfi0'), so hardware settings and status
    variables are unaffected. Display names appear in saved column headers and, through the
    property channel_display_names, in the BocquelLab time series GUI. Channels without an entry
    keep their hardware name. The average of a channel is called '<display name> average'.

    Saved data stays in the streamer units (counts/s for the NI counters).

    Example config for copy-paste:

    time_series_reader_logic:
        module.Class: 'bocquellab.time_series_reader_logic.BocquelLabTimeSeriesReaderLogic'
        options:
            max_frame_rate: 20  # optional (default: 20Hz)
            channel_buffer_size: 1048576  # optional (default: 1MSample)
            max_raw_data_bytes: 1073741824  # optional (default: 1GB)
            channel_display_names:  # optional, hardware channel name -> displayed name
                pfi0: 'APD1 (pfi0)'
                pfi8: 'APD2 (pfi8)'
        connect:
            streamer: <DataInStreamInterface_name>
    """

    _channel_display_names = ConfigOption(name='channel_display_names', default=dict(), missing='nothing')

    def on_activate(self):
        super().on_activate()
        names = {str(ch): str(name) for ch, name in (self._channel_display_names or dict()).items()}
        channels = list(self.streamer_constraints.channel_units)
        unknown = [ch for ch in names if ch not in channels]
        if unknown:
            self.log.warning(f'channel_display_names: unknown channels {unknown} ignored. '
                             f'Streamer channels are {channels}.')
        names = {ch: names.get(ch, ch) for ch in channels}
        if len(set(names.values())) < len(names):
            self.log.error(f'channel_display_names are not unique ({names}). Using hardware channel names.')
            names = {ch: ch for ch in channels}
        self._channel_display_names = names

    @property
    def channel_display_names(self) -> Dict[str, str]:
        """ Read-only: hardware channel name -> displayed name, for every streamer channel. """
        return self._channel_display_names.copy()

    def channel_display_name(self, channel: str, averaged: bool = False) -> str:
        name = self._channel_display_names.get(channel, channel)
        return f'{name} average' if averaged else name

    def _column_header(self, channel: str, averaged: bool = False) -> str:
        unit = self.streamer_constraints.channel_units[channel]
        return f'{self.channel_display_name(channel, averaged)} ({unit})'

    def _save_recorded_data(self, name_tag='', save_figure=True):
        """ Same as qudi's version, with display names in the column headers. Recorded data is raw
        (full sample rate, no moving average), so there is no average column here.
        """
        try:
            constraints = self.streamer_constraints
            metadata = {
                'Start recoding time': self._record_start_time.strftime('%d.%m.%Y, %H:%M:%S.%f'),
                'Sample rate (Hz)': self.sampling_rate,
                'Sample timing': constraints.sample_timing.name
            }
            column_headers = [self._column_header(ch) for ch in self.active_channel_names]
            channel_count = len(column_headers)
            nametag = f'data_trace_{name_tag}' if name_tag else 'data_trace'

            data = self._recorded_raw_data[:channel_count * self._recorded_sample_count].reshape(
                [self._recorded_sample_count, channel_count]
            )
            if self._recorded_raw_times is not None:
                data = np.column_stack([self._recorded_raw_times[:self._recorded_sample_count], data])
                column_headers.insert(0, 'Time (s)')
            try:
                fig = self._draw_raw_data_thumbnail(data) if save_figure else None
            finally:
                storage = TextDataStorage(root_dir=self.module_default_data_dir)
                filepath, _, _ = storage.save_data(data,
                                                   metadata=metadata,
                                                   nametag=nametag,
                                                   column_headers=column_headers)
            if fig is not None:
                storage.save_thumbnail(mpl_figure=fig, file_path=filepath)
        except:
            self.log.exception('Something went wrong while saving raw data:')
            raise

    @QtCore.Slot()
    def save_trace_snapshot(self, name_tag: Optional[str] = '', save_figure: Optional[bool] = True):
        """ Same as qudi's version, with display names in the column headers and one extra column
        '<name> average (<unit>)' per averaged channel. The moving average is centred, so its first
        moving_average_width // 2 rows are NaN (same alignment as the averaged curve in the GUI).
        """
        try:
            timestamp = dt.datetime.now()
            constraints = self.streamer_constraints
            metadata = {
                'Timestamp': timestamp.strftime('%d.%m.%Y, %H:%M:%S.%f'),
                'Data rate (Hz)': self.data_rate,
                'Oversampling factor (samples)': self.oversampling_factor,
                'Sampling rate (Hz)': self.sampling_rate,
                'Moving average width (samples)': self.moving_average_width
            }
            column_headers = [self._column_header(ch) for ch in self.active_channel_names]
            nametag = f'trace_snapshot_{name_tag}' if name_tag else 'trace_snapshot'

            data_offset = self._trace_data.shape[0] - self._moving_average_width // 2
            data = self._trace_data[:data_offset, :]
            x = self._trace_times
            try:
                # thumbnail from the raw data only, as in qudi
                fig = self._draw_trace_snapshot_thumbnail(x, data) if save_figure else None
            finally:
                averaged = self.averaged_channel_names if self.moving_average_width > 1 else list()
                if averaged:
                    avg = np.full((data.shape[0], len(averaged)), np.nan, dtype=data.dtype)
                    n_avg = min(self._trace_data_averaged.shape[0], data.shape[0])
                    avg[data.shape[0] - n_avg:, :] = self._trace_data_averaged[-n_avg:, :len(averaged)]
                    data = np.column_stack([data, avg])
                    column_headers.extend(self._column_header(ch, averaged=True) for ch in averaged)
                if constraints.sample_timing != SampleTiming.RANDOM:
                    data = np.column_stack([x, data])
                    column_headers.insert(0, 'Time (s)')

                storage = TextDataStorage(root_dir=self.module_default_data_dir)
                filepath, _, _ = storage.save_data(data,
                                                   timestamp=timestamp,
                                                   metadata=metadata,
                                                   nametag=nametag,
                                                   column_headers=column_headers)
            if fig is not None:
                storage.save_thumbnail(mpl_figure=fig, file_path=filepath)
        except:
            self.log.exception('Something went wrong while saving trace snapshot:')
            raise
