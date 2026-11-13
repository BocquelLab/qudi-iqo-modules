# -*- coding: utf-8 -*-
"""
Qudi hardware module for tests without hardware: a gated fast counter that counts the APD of the SIMULATED
RFSoC experiment (rfsoc_nv simulator), standing in for the NI gated counter of the lab setup.

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

__all__ = ['RFSoCSimFastCounter']

import time
import numpy as np

from qudi.core.configoption import ConfigOption
from qudi.core.connector import Connector
from qudi.util.mutex import Mutex
from qudi.interface.fast_counter_interface import FastCounterInterface


class RFSoCSimFastCounter(FastCounterInterface):
    """ Gated fast counter on the simulated RFSoC experiment, with the behaviour of the lab's NI gated
    counter (ni_x_series.ni_x_series_counter.NIXSeriesCounter): 10 ns bins; every rising edge of the gate
    marker starts a record of the configured length; record number i since start_measure() is added to
    row i % number_of_gates. Photon shot noise is added. Activation fails with a real board.

    Example config for copy-paste:

    fast_counter_rfsoc_sim:
        module.Class: 'bocquellab.rfsoc_sim_fast_counter.RFSoCSimFastCounter'
        options:
            gate_pin: 'PMOD0_1'  # digital output of the simulated board that carries the gate marker
        connect:
            board: rfsoc_board
    """

    _board = Connector(name='board', interface='RFSoCQickBoard')
    _gate_pin = ConfigOption(name='gate_pin', default='PMOD0_1')

    _BIN_WIDTH = 10e-9  # s, as the NI counter (100 MHz time base)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._lock = Mutex()
        self._pin = 0
        self._status = 0
        self._record_length = 1e-6
        self._gates = 1
        self._counts = None
        self._expected_seen = None
        self._records = 0
        self._records_seen = 0
        self._elapsed = 0.0
        self._started = 0.0
        self._rng = np.random.default_rng()

    def on_activate(self):
        board = self._board()
        if not board.is_simulated:
            raise RuntimeError('This fast counter only works with the simulated RFSoC board (simulate: True). '
                               'With the real board, count with the NI card (fast_counter_ni).')
        names = [pin[3] for pin in board.soccfg['tprocs'][0]['output_pins']]
        if self._gate_pin not in names:
            raise ValueError(f'gate_pin "{self._gate_pin}" is not one of {names}.')
        self._pin = names.index(self._gate_pin)
        self._status = 0
        self._counts = None
        self.log.warning('Fast counter data are SIMULATED (APD of the simulated RFSoC experiment).')

    def on_deactivate(self):
        try:
            self.stop_measure()
        except Exception:
            self.log.exception('Could not stop the simulated fast counter:')

    def get_constraints(self):
        return {'hardware_binwidth_list': [self._BIN_WIDTH]}

    def configure(self, bin_width_s, record_length_s, number_of_gates=0):
        with self._lock:
            if self._status == 2:
                raise RuntimeError('Cannot reconfigure while running. Call stop_measure() first.')
            bins = max(1, int(round(record_length_s / self._BIN_WIDTH)))
            self._record_length = bins * self._BIN_WIDTH
            self._gates = max(1, int(number_of_gates))
            self._counts = np.zeros((self._gates, bins), dtype=np.int64)
            self._records, self._elapsed = 0, 0.0
            self._status = 1
            return self._BIN_WIDTH, self._record_length, self._gates

    def get_status(self):
        return self._status

    def start_measure(self):
        with self._lock:
            if self._status != 1:
                raise RuntimeError('start_measure() needs a configured, stopped counter.')
            self._counts[:] = 0
            self._records, self._elapsed = 0, 0.0
            self._arm()
            return 0

    def stop_measure(self):
        with self._lock:
            if self._status in (2, 3):
                self._update()
                if self._status == 2:
                    self._board().soc.sim_gated_counter_stop()
                self._status = 1
            return 0

    def pause_measure(self):
        with self._lock:
            if self._status == 2:
                self._update()
                self._elapsed += time.monotonic() - self._started
                self._board().soc.sim_gated_counter_stop()
                self._status = 3
            return 0

    def continue_measure(self):
        with self._lock:
            if self._status == 3:
                self._arm()
            return 0

    def is_gated(self):
        return True

    def get_binwidth(self):
        return self._BIN_WIDTH

    def get_data_trace(self):
        """ @return (int64 array (number_of_gates, bins), {'elapsed_sweeps': int, 'elapsed_time': float}) """
        with self._lock:
            if self._counts is None:
                return np.zeros((1, 1), dtype=np.int64), {'elapsed_sweeps': 0, 'elapsed_time': 0.0}
            elapsed = self._elapsed
            if self._status == 2:
                self._update()
                elapsed += time.monotonic() - self._started
            return self._counts.copy(), {'elapsed_sweeps': self._records // self._gates, 'elapsed_time': elapsed}

    # ---------------------------------------------------------------- helpers
    def _arm(self):
        self._board().soc.sim_gated_counter_start(self._pin, self._BIN_WIDTH, self._record_length, self._gates)
        self._expected_seen = np.zeros(self._counts.shape)
        self._records_seen = 0
        self._started = time.monotonic()
        self._status = 2

    def _update(self):
        """ Add the photons of the records made since the last call (Poisson noise on the expected counts). """
        if self._status != 2:
            return
        expected, records = self._board().soc.sim_gated_counter_read()
        new = np.clip(expected - self._expected_seen, 0.0, None)
        self._counts += self._rng.poisson(new)
        self._expected_seen = expected
        self._records += records - self._records_seen
        self._records_seen = records
