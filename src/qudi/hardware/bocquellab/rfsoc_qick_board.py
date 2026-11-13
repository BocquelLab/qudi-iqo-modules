# -*- coding: utf-8 -*-
"""
Qudi hardware module for the BocquelLab RFSoC4x2 running QICK (qick-dawg photon counting firmware):
the connection to the board, shared by the RFSoC microwave, counter and pulser modules.

This is a thin adapter. The board client (connection, gain limit, single user) and the simulated board
live in the RFSoC-NV repository (package rfsoc_nv, C:\\Software\\RFSoC-NV), the pulse programs in qick /
qick-dawg (C:\\Software\\qick, C:\\Software\\qick-dawg). Nothing of them is copied here.

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

__all__ = ['RFSoCQickBoard']

from qudi.core.module import Base
from qudi.core.configoption import ConfigOption
from qudi.util.mutex import Mutex


class RFSoCQickBoard(Base):
    """ Connection to the RFSoC4x2 / QICK board (Pyro4 server on the board), or to a simulated board.

    Other RFSoC modules connect to this one (Connector(interface='RFSoCQickBoard')) and use
    ``soc`` / ``soccfg`` to run qick programs; qick-dawg programs find the board by themselves once this
    module is active.

    - max_gain (mandatory): hard limit of the generator gain in DAC units (full scale 32766). Every
      program is inspected when it is loaded on the board; one that sets a higher gain, or changes a
      gain while it runs, is refused (GainLimitError, nothing loaded) - never clipped.
      WARNING: generator 0 (DAC_B) is meant to drive the Mini-Circuits ZHL-10M4G21W0+ amplifier (RF input
      absolute maximum +5 dBm). The DAC power for a given gain has to be measured before the amplifier
      is connected; choose max_gain from that measurement.
    - Activation fails if the board cannot be reached, runs another qick version than this PC or has
      another kind of firmware. Activation and deactivation switch all outputs off (generators silent,
      PMOD outputs low).
    - One user at a time: a measurement module calls claim('name') before it uses the board and
      release('name') afterwards; a second user gets a BoardBusyError. The module is 'locked' meanwhile.
    - simulate: True replaces the board by a simulation that runs the real compiled program and returns
      synthetic APD counts (NV centre with laser, microwave and APD); no network access.
    - edge_counter_thresholds: [high, low] in ADC units for the photon (edge) counters of the readouts,
      for the modules that count with the RFSoC. An APD pulse is counted when the ADC signal rises above
      'high' after having been below 'low'. The default [2000, 500] is NOT VERIFIED on the setup.

    Example config for copy-paste:

    rfsoc_board:
        module.Class: 'bocquellab.rfsoc_qick_board.RFSoCQickBoard'
        options:
            host: '192.168.0.2'  # board on the 'Instruments' network (also 'pynq.local')
            port: 8888  # Pyro4 name server on the board
            proxy_name: 'myqick'
            max_gain: 2000  # DAC units, hard limit (full scale 32766)
            warning_gain: 1000  # optional: gains above this are logged as a warning
            comm_timeout: 10  # s, optional
            edge_counter_thresholds: [2000, 500]  # optional, [high, low] in ADC units; default not verified
            simulate: False  # True: simulated board
            simulation:  # optional, only used with simulate: True (see rfsoc_nv.simulator)
                realtime: True  # simulated programs take as long as real ones
                world:
                    resonances_mhz: [2870.0]
                    count_rate: 200.0e+3  # c/s with the laser on
                    contrast: 0.3
                    rabi_mhz_full_scale: 10.0  # Rabi frequency at gain 32766
    """

    _host = ConfigOption('host', default='192.168.0.2')
    _port = ConfigOption('port', default=8888)
    _proxy_name = ConfigOption('proxy_name', default='myqick')
    _max_gain = ConfigOption('max_gain', missing='error', constructor=float)
    _warning_gain = ConfigOption('warning_gain', default=None)
    _comm_timeout = ConfigOption('comm_timeout', default=10)
    _edge_counter_thresholds = ConfigOption('edge_counter_thresholds', default=(2000, 500))
    _simulate = ConfigOption('simulate', default=False)
    _simulation = ConfigOption('simulation', default=None)

    _FULL_SCALE_GAIN = 32766

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._lock = Mutex()
        self._board = None

    # ---------------------------------------------------------------- module lifecycle
    def on_activate(self):
        """ Connect and switch all outputs off. Raises if the board is not reachable or not as expected. """
        if not 0 < self._max_gain <= self._FULL_SCALE_GAIN:
            raise ValueError(f'max_gain must be in ]0, {self._FULL_SCALE_GAIN}], got {self._max_gain}.')
        if self._warning_gain is not None and not 0 < float(self._warning_gain) <= self._max_gain:
            raise ValueError(f'warning_gain must be in ]0, max_gain], got {self._warning_gain}.')
        high, low = (int(x) for x in self._edge_counter_thresholds)
        if not 0 <= low < high:
            raise ValueError(f'edge_counter_thresholds must be [high, low] with 0 <= low < high, '
                             f'got {list(self._edge_counter_thresholds)}.')
        try:
            from rfsoc_nv import QickBoard  # lab packages: imported here so that qudi starts without them
        except ImportError as err:
            raise ImportError('The packages qick, qickdawg and rfsoc_nv (BocquelLab forks in C:\\Software) are not '
                              'installed in this Python environment, see CLAUDE.md.') from err

        board = QickBoard(max_gain=self._max_gain, host=self._host, port=self._port, proxy_name=self._proxy_name,
                          simulate=bool(self._simulate), sim_options=self._simulation,
                          timeout=float(self._comm_timeout), logger=self.log)
        board.connect()
        try:
            board.outputs_off()
        except Exception:
            board.close()
            raise
        self._board = board
        if self._simulate:
            self.log.warning('RFSoC board is SIMULATED: programs run in software, counts are synthetic.')

    def on_deactivate(self):
        """ Switch all outputs off and release the connection. Never raises. """
        board, self._board = self._board, None
        if board is None:
            return
        try:
            board.outputs_off()
        except Exception:
            self.log.exception('Could not switch the RFSoC outputs off:')
        try:
            board.close()
        except Exception:
            self.log.exception('Could not close the connection to the RFSoC board:')

    # ---------------------------------------------------------------- access for the RFSoC modules
    @property
    def board(self):
        """ The rfsoc_nv.QickBoard client. """
        if self._board is None:
            raise RuntimeError('RFSoC board module is not active.')
        return self._board

    @property
    def soc(self):
        """ Board object to pass to qick programs (prog.acquire(soc, ...)); checks the gain limit. """
        return self.board.soc

    @property
    def soccfg(self):
        """ qick.QickConfig of the board: unit conversions, channel description. """
        return self.board.soccfg

    @property
    def is_simulated(self):
        return bool(self._simulate)

    @property
    def max_gain(self):
        """ Hard generator gain limit in DAC units. """
        return self._max_gain

    @property
    def edge_counter_thresholds(self):
        """ (high, low) thresholds of the photon counters in ADC units, for modules counting with the RFSoC. """
        high, low = self._edge_counter_thresholds
        return int(high), int(low)

    @property
    def owner(self):
        """ Name of the present user of the board, or None. """
        return self.board.owner

    def claim(self, owner):
        """ Reserve the board for ``owner``. Raises rfsoc_nv.BoardBusyError if another user has it. """
        with self._lock:
            self.board.claim(owner)
            if self.module_state() == 'idle':
                self.module_state.lock()

    def release(self, owner):
        """ Give the board back (no effect if ``owner`` is not the present user). """
        with self._lock:
            board = self.board
            board.release(owner)
            if board.owner is None and self.module_state() == 'locked':
                self.module_state.unlock()

    def check_gain(self, gain):
        """ Refuse (GainLimitError) a gain above max_gain; warn above warning_gain. Returns the gain as int. """
        gain = self.board.check_gain(gain)
        if self._warning_gain is not None and abs(gain) > float(self._warning_gain):
            self.log.warning(f'Generator gain {gain} is above the warning level of {float(self._warning_gain):.0f} '
                             f'(limit {self._max_gain:.0f}).')
        return gain

    def outputs_off(self):
        """ Stop the tProc, silence the generators and set all PMOD outputs low. """
        with self._lock:
            self.board.outputs_off()

    def warn_if_zero(self, counts, what='RFSoC measurement'):
        """ Log a warning if a measurement returned zero counts everywhere. Returns True in that case. """
        return self.board.warn_if_zero(counts, what)
