# -*- coding: utf-8 -*-
"""
Qudi hardware module for tests without hardware: the BocquelLab NI stepped ODMR counter with the NI card
replaced by the APD of the SIMULATED RFSoC experiment (rfsoc_nv simulator).

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

__all__ = ['RFSoCSimOdmrCounter']

import time

from qudi.core.configoption import ConfigOption
from qudi.core.connector import Connector
from qudi.interface.finite_sampling_input_interface import FiniteSamplingInputConstraints
from qudi.hardware.bocquellab.ni_stepped_odmr_counter import NISteppedOdmrCounter


class _SimCountTask:
    """ Stands in for an NI count-edges task: read() returns the running total. """

    def __init__(self):
        self.total = 0

    def read(self):
        return self.total

    def close(self):
        pass


class _SimGateTask:
    """ Stands in for the NI gate pulse task: one start / wait_until_done / stop cycle counts the simulated
    APD pulses of one dwell time.
    """

    def __init__(self, soc, dwell, count_tasks):
        self._soc = soc
        self._dwell = dwell
        self._count_tasks = count_tasks
        self._started = 0.0

    def start(self):
        self._started = time.perf_counter()

    def wait_until_done(self, timeout=None):
        for task in self._count_tasks:
            task.total += self._soc.sim_apd_counts(self._dwell)
        remaining = self._dwell - (time.perf_counter() - self._started)
        if remaining > 0:
            time.sleep(remaining)

    def stop(self):
        pass

    def close(self):
        pass


class RFSoCSimOdmrCounter(NISteppedOdmrCounter):
    """ NISteppedOdmrCounter without NI card, for the simulated RFSoC board only.

    The acquisition loop is the unchanged one of NISteppedOdmrCounter (trigger_scan_point(), settle time,
    gated counting); only the NI tasks are replaced: the counts of each point come from the simulated
    experiment of the board module (laser, microwave being played, NV centre, APD), with shot noise.
    Every configured channel sees the same simulated APD. Activation fails with a real board.

    Example config for copy-paste:

    odmr_counter_rfsoc_sim:
        module.Class: 'bocquellab.rfsoc_sim_odmr_counter.RFSoCSimOdmrCounter'
        options:
            channel_units:
                'PFI0': 'c/s'
            settle_time: 0.001  # s
        connect:
            microwave: rfsoc_microwave
            board: rfsoc_board
    """

    _board = Connector(name='board', interface='RFSoCQickBoard')
    _device_name = ConfigOption(name='device_name', default='simulated')  # no NI device needed

    def on_activate(self):
        if self._settle_time < 0:
            raise ValueError(f'settle_time must be >= 0 s, got {self._settle_time}')
        if not hasattr(self._microwave(), 'trigger_scan_point'):
            raise TypeError('Connected microwave source has no trigger_scan_point().')
        if not self._board().is_simulated:
            raise RuntimeError('This counter only works with the simulated RFSoC board (simulate: True). '
                               'With the real board, count with bocquellab.ni_stepped_odmr_counter.')
        units = {str(source).strip('/').rsplit('/', 1)[-1].lower(): str(unit)
                 for source, unit in dict(self._channel_units).items()}
        if not units:
            raise ValueError('No channel configured in channel_units.')
        self._sources = {name: f'simulated/{name}' for name in units}
        self._constraints = FiniteSamplingInputConstraints(
            channel_units=units,
            frame_size_limits=self._frame_size_limits,
            sample_rate_limits=self._sample_rate_limits
        )
        self._active_channels = tuple(units)
        self._sample_rate = float(min(max(self._sample_rate, self._constraints.min_sample_rate),
                                      self._constraints.max_sample_rate))
        self.log.warning('ODMR counts are SIMULATED (APD of the simulated RFSoC experiment).')

    def _create_tasks(self, dwell):
        count_tasks = {ch: _SimCountTask() for ch in self._active_channels}
        gate_task = _SimGateTask(self._board().soc, dwell, tuple(count_tasks.values()))
        return gate_task, 'simulated', count_tasks
