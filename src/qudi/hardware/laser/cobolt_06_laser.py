# -*- coding: utf-8 -*-
"""
Qudi hardware module for Cobolt 06-01 Series lasers (06-MLD / 06-DPL / 06-HPLD, HUBNER Photonics), controlled
over their USB virtual COM port with Cobolt's official "pycobolt" package.

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

__all__ = ['Cobolt06Laser']

import time

from qudi.core.configoption import ConfigOption
from qudi.util.mutex import Mutex

from qudi.interface.simple_laser_interface import ControlMode, LaserState, ShutterState, SimpleLaserInterface

# "pycobolt" (and pyserial underneath it) is imported inside on_activate() only, so that qudi and this module
# still load on PCs where the package is not installed.

_COMMAND_SPACING = 0.1  # s, minimum time between two commands that pycobolt enforces (busy-wait in send_cmd)
_RUN_MODES = {ControlMode.POWER: 'ConstantPower', ControlMode.CURRENT: 'ConstantCurrent'}
_MODE_CHANGE_SETTLE_TIME = 3.0  # s, max. wait for the laser to report on again after a run-mode change while on


class Cobolt06Laser(SimpleLaserInterface):
    """ Cobolt 06-01 Series laser driven through "pycobolt" (https://github.com/cobolt-lasers/pycobolt).

    Written for a Cobolt 06-MLD 520 nm / 80 mW (model 0520-06-01-0080-1100 with CE/CDRH key control box).
    pycobolt must identify the laser as its "Cobolt06" class, i.e. the 06-01 generation that understands the
    "LASer:..." command set of owner's manual D0951 (12 V power supply). Older 06-01 lasers (legacy command set
    only) are rejected at activation. pycobolt methods are used for the power / current setpoints and for on,
    off and clear-fault; status queries that pycobolt does not expose (fault, interlock, run mode, TEC
    temperatures, factory limits) are sent with the syntax of manual D0951-D, section 4.6.

    Interface mapping:
      - power / current: constant power and constant current setpoints in W and A (pycobolt works in mW / mA).
        The power setpoint is limited to min(option max_power, factory limit), the current setpoint to
        min(option max_current, factory limit).
      - control mode: the laser run mode, POWER = ConstantPower, CURRENT = ConstantCurrent. It can be changed while
        the laser is on: the firmware keeps emitting (verified on the laser on 2026-11-03). To avoid a jump of the
        output, the setpoint of the new mode is first set to the measured value (current for POWER -> CURRENT,
        power for CURRENT -> POWER), clamped to the setpoint limits. After the change the driver waits until the
        laser reports on again and logs state, setpoint and readings. If the laser is in
        PowerModulation or CurrentModulation (set e.g. in Cobolt Monitor), the control mode reads UNKNOWN, the
        laser GUI greys out its setpoint controls and the run mode is left untouched; switch back with
        set_control_mode(ControlMode.POWER) from the console or in Cobolt Monitor.
      - laser state: ON / OFF as reported by the laser ("l?"). While a fault is pending the state reads LOCKED
        (interlock open) or UNKNOWN (other faults) and the fault is written to the log once as a clear error.
        Switching on sends "@cob1", which restarts the autostart sequence. On CE/CDRH lasers (key control box,
        model ...-1100) every software start then waits until the key switch is turned OFF and back ON (manual
        D0951-D, section 3.3; logged as a warning), and temperature stabilisation can take up to 3 minutes.
        Nothing is sent when the laser already is on, so a stale GUI cannot restart a running laser.
      - shutter: the laser has none (NO_SHUTTER). With the option emission_pause_as_shutter the shutter button
        pauses / resumes emission instead ("LASer:PAUSed"). DANGER: a paused laser is still armed, this is not a
        fail-safe way of blocking the beam (manual D0951-D, section 3.3).
      - temperatures: the thermo-electric coolers that answer at activation ("TEC1" ... "TEC3", degrees Celsius).

    Notes:
      - The COM port can be opened by one program only: close Cobolt Monitor before activating this module.
      - Every command takes at least 100 ms (pycobolt command spacing) and one polling cycle of laser_logic needs
        about eight commands, so keep its query_interval at 0.5 s or more.
      - With the option serial_number, pycobolt opens every USB serial port of the PC and sends "sn?" to it until
        it finds the laser. Prefer the port option when other serial instruments are connected.
      - Deactivating the module only closes the port; the laser keeps running unless turn_off_on_deactivate is
        set. Console helpers beyond the interface: clear_fault(), get_fault(), get_autostart_state().

    Example config for copy-paste:

    cobolt_laser:
        module.Class: 'laser.cobolt_06_laser.Cobolt06Laser'
        options:
            port: 'COM8'  # USB virtual COM port of the laser (default 'COM8')
            # serial_number: '32011'  # optional alternative to port: search all USB serial ports for this laser
            max_power: 0.080  # W, optional power setpoint limit (default 0.080; the factory limit applies too)
            # max_current: 0.2  # A, optional current setpoint limit (default: factory limit)
            turn_off_on_deactivate: False  # optional, default False: leave the laser running on deactivation
            emission_pause_as_shutter: False  # optional, default False: shutter button pauses the emission
    """

    _port = ConfigOption(name='port', default='COM8', missing='info')
    _serial_number = ConfigOption(name='serial_number', default=None)
    _max_power = ConfigOption(name='max_power', default=0.080, missing='info')
    _max_current = ConfigOption(name='max_current', default=None)
    _turn_off_on_deactivate = ConfigOption(name='turn_off_on_deactivate', default=False)
    _pause_as_shutter = ConfigOption(name='emission_pause_as_shutter', default=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._lock = Mutex()  # all laser I/O runs in the thread of the caller (laser_logic, console)
        self._laser = None  # pycobolt.Cobolt06 instance while the module is active
        self._power_range = (0.0, 0.0)  # W
        self._current_range = (0.0, 0.0)  # A
        self._tec_channels = ()  # temperature channels found at activation, e.g. ('TEC1',)
        self._static_info = ''  # identification text for get_extra_info()
        self._reported_fault = ''  # fault name last written to the log, so each fault is reported once

    # ---------------------------------------------------------------- module lifecycle
    def on_activate(self):
        """ Open the COM port, identify the laser, read the factory limits and report a pending fault.
        Raises on any failure: qudi logs the exception and leaves the module deactivated.
        """
        try:
            import pycobolt
        except ImportError as err:
            raise ImportError('The "pycobolt" package is not installed in this Python environment. Install it '
                              'into qudi-env with "pip install pycobolt" (it depends on pyserial).') from err

        serial_number = None if self._serial_number is None else str(self._serial_number).strip()
        if not serial_number and not self._port:
            raise ValueError('Configure either the "port" or the "serial_number" option.')
        try:
            if serial_number:
                laser = pycobolt.CoboltLaser(serialnumber=serial_number)
            else:
                laser = pycobolt.CoboltLaser(port=self._port)
        except Exception as err:
            msg = self._connection_error_message(err, serial_number)
            self.log.error(msg)
            raise RuntimeError(msg) from err

        if not isinstance(laser, pycobolt.Cobolt06):
            info = (f'serial number {laser.serialnumber}, model {laser.modelnumber}, '
                    f'pycobolt class {type(laser).__name__}')
            port = laser.port
            laser.disconnect()
            raise RuntimeError(f'The laser on {port} ({info}) does not use the 06-01 Series command set this '
                               f'module needs (pycobolt class "Cobolt06"). Older Cobolt lasers are not supported.')

        self._laser = laser
        self._reported_fault = ''
        try:
            self._read_limits()
            self._read_identification()
            with self._lock:
                self._read_fault()  # logs an error if the laser already is in a fault state
                if self._query('INTerlock?') == '0':
                    self.log.warning('The remote interlock circuit of the Cobolt laser is open; the laser can not '
                                     'be started until it is closed.')
        except Exception:
            self._laser.disconnect()
            self._laser = None
            raise
        self.log.info(f'Connected to Cobolt laser S/N {laser.serialnumber}, model {laser.modelnumber} '
                      f'({self._wavelength_text(laser.modelnumber)}) on {laser.port}. Power limit '
                      f'{self._power_range[1] * 1e3:.1f} mW, current limit {self._current_range[1] * 1e3:.1f} mA, '
                      f'temperature channels: {", ".join(self._tec_channels) or "none"}.')

    def on_deactivate(self):
        """ Close the COM port. The laser keeps running unless turn_off_on_deactivate is set. Never raises. """
        if self._laser is None:
            return
        with self._lock:
            try:
                if self._turn_off_on_deactivate:
                    self._check_ok(self._call(self._laser.turn_off), 'turn off')
                    self.log.info('Cobolt laser turned off on deactivation (option turn_off_on_deactivate).')
                else:
                    self.log.info('Disconnecting from the Cobolt laser; it keeps its current state '
                                  '(option turn_off_on_deactivate is False).')
            except Exception:
                self.log.exception('Error while turning the Cobolt laser off on deactivation:')
            finally:
                try:
                    self._laser.disconnect()
                except Exception:
                    self.log.warning('The COM port of the Cobolt laser was not closed cleanly.')
                self._laser = None

    # ---------------------------------------------------------------- SimpleLaserInterface: power
    def get_power_range(self):
        """ @return float[2]: allowed power setpoint range (min, max) in W """
        return self._power_range

    def get_power(self):
        """ @return float: actual laser power in W """
        with self._lock:
            return self._call(self._laser.get_power) / 1e3  # pycobolt: mW

    def get_power_setpoint(self):
        """ @return float: constant power mode setpoint in W """
        with self._lock:
            return self._call(self._laser.get_power_setpoint) / 1e3  # pycobolt: mW

    def set_power(self, power):
        """ Set the constant power mode setpoint.
        @param float power: power in W, within get_power_range()
        """
        power = float(power)
        self._check_range(power, self._power_range, 'Power', 'W')
        with self._lock:
            self._check_ok(self._call(self._laser.set_power, power * 1e3), f'set power to {power} W')

    # ---------------------------------------------------------------- SimpleLaserInterface: current
    def get_current_unit(self):
        """ @return str: unit of the laser diode current """
        return 'A'

    def get_current_range(self):
        """ @return float[2]: allowed current setpoint range (min, max) in A """
        return self._current_range

    def get_current(self):
        """ @return float: measured laser diode current in A """
        with self._lock:
            return self._call(self._laser.get_current) / 1e3  # pycobolt: mA

    def get_current_setpoint(self):
        """ @return float: constant current mode setpoint in A """
        with self._lock:
            return self._call(self._laser.get_current_setpoint) / 1e3  # pycobolt: mA

    def set_current(self, current):
        """ Set the constant current mode setpoint.
        @param float current: current in A, within get_current_range()
        """
        current = float(current)
        self._check_range(current, self._current_range, 'Current', 'A')
        with self._lock:
            self._check_ok(self._call(self._laser.set_current, current * 1e3), f'set current to {current} A')

    # ---------------------------------------------------------------- SimpleLaserInterface: control mode
    def allowed_control_modes(self):
        """ @return frozenset: ControlMode.POWER and ControlMode.CURRENT """
        return frozenset(_RUN_MODES)

    def get_control_mode(self):
        """ @return ControlMode: POWER / CURRENT for the constant power / current run modes, UNKNOWN otherwise
        (PowerModulation, CurrentModulation)
        """
        with self._lock:
            run_mode = self._query('LASer:RUNMode?')
        for control_mode, name in _RUN_MODES.items():
            if run_mode.lower() == name.lower():
                return control_mode
        return ControlMode.UNKNOWN

    def set_control_mode(self, control_mode):
        """ Select the constant power or constant current run mode ("LASer:RUNMode").

        Allowed while the laser is on: this firmware stays on through a run-mode change ("l?" 1, "STATe?"
        AutostartLaserOn right after "LASer:RUNMode ConstantCurrent", checked on the laser on 2026-11-03). The laser
        would then run at the setpoint of the new mode, so while the laser is on that setpoint is first set to the
        measured value (diode current when switching to CURRENT, output power when switching to POWER), clamped to
        get_current_range() / get_power_range(); the laser keeps its operating point. Then the method keeps the
        lock until the laser reports on again ("l?" 1 and "STATe?" Laser On, at most _MODE_CHANGE_SETTLE_TIME),
        so laser_logic never polls a transition, then logs state, setpoint and power / current readings.
        A laser that does not come back is logged as a warning; nothing is restarted.
        Requesting the active run mode sends nothing.
        @param ControlMode control_mode: ControlMode.POWER or ControlMode.CURRENT
        """
        control_mode = ControlMode(control_mode)
        if control_mode not in _RUN_MODES:
            raise ValueError(f'Control mode {control_mode.name} is not supported, use POWER or CURRENT.')
        run_mode = _RUN_MODES[control_mode]
        with self._lock:
            active_run_mode = self._query('LASer:RUNMode?')
            if active_run_mode.lower() == run_mode.lower():
                return
            was_on, _ = self._is_laser_on()
            if was_on:
                self._match_setpoint_to_output(control_mode)
            self._check_ok(self._query(f'LASer:RUNMode {run_mode}'), f'set run mode {run_mode}')
            if not was_on:
                self.log.info(f'Cobolt laser run mode changed from {active_run_mode} to {run_mode} (laser off).')
                return
            on, autostart_state, waited = self._wait_until_on(_MODE_CHANGE_SETTLE_TIME)
            if control_mode == ControlMode.POWER:
                setpoint = f'power setpoint {self._call(self._laser.get_power_setpoint):.1f} mW'
            else:
                setpoint = f'current setpoint {self._call(self._laser.get_current_setpoint):.1f} mA'
            reading = f'{self._call(self._laser.get_power):.1f} mW at {self._call(self._laser.get_current):.1f} mA'
        change = f'run mode changed from {active_run_mode} to {run_mode} during emission'
        if on:
            self.log.info(f'Cobolt laser {change}: laser on (state {autostart_state}, settled after {waited:.1f} s), '
                          f'{setpoint}, output {reading}.')
        else:
            self.log.warning(f'Cobolt laser {change}, but {_MODE_CHANGE_SETTLE_TIME:.0f} s later it does not report '
                             f'on (state {autostart_state}); {setpoint}, output {reading}. No restart was sent.')

    # ---------------------------------------------------------------- SimpleLaserInterface: laser state
    def get_laser_state(self):
        """ @return LaserState: ON / OFF, or LOCKED (interlock open) / UNKNOWN (other fault) while a fault is
        pending. A new fault is reported in the log.
        """
        with self._lock:
            on = self._query('l?')  # pycobolt's is_on() would hide a "Syntax error" reply
            fault, interlock_open = self._read_fault()
        if fault:
            return LaserState.LOCKED if interlock_open else LaserState.UNKNOWN
        if on == '1':
            return LaserState.ON
        if on == '0':
            return LaserState.OFF
        raise RuntimeError(f'Unexpected reply "{on}" from the Cobolt laser to "l?".')

    def set_laser_state(self, state):
        """ Switch the laser on or off.

        OFF sends "l0", also when the laser already is off (this cancels a pending start). ON sends "@cob1", which
        restarts the autostart sequence; on CE/CDRH lasers the sequence then waits until the key switch is turned
        OFF and back ON (logged as a warning). ON sends nothing when the laser already is on, so a stale GUI or a
        repeated request cannot restart a running laser.
        @param LaserState state: LaserState.ON or LaserState.OFF
        """
        state = LaserState(state)
        if state not in (LaserState.ON, LaserState.OFF):
            raise ValueError(f'Can only set LaserState.ON or LaserState.OFF, not {state.name}.')
        with self._lock:
            if state == LaserState.OFF:
                self._check_ok(self._call(self._laser.turn_off), 'turn off')  # "l0"
                autostart_state = self._try_query('STATe?', warn=False)
                self.log.info(f'Cobolt laser switched off (state {autostart_state}).')
                return
            laser_on, autostart_state = self._is_laser_on()
            if laser_on:
                self.log.info(f'Cobolt laser is already on (state {autostart_state}); no start command sent.')
                return
            fault, _ = self._read_fault()
            if fault:
                raise RuntimeError(f'Cannot start the Cobolt laser: fault "{fault}" is pending. Fix its cause, '
                                   f'call clear_fault() and try again.')
            if self._query('INTerlock?') == '0':
                raise RuntimeError('Cannot start the Cobolt laser: the remote interlock circuit is open. Close the '
                                   'interlock and try again.')
            self._check_ok(self._call(self._laser.turn_on), 'turn on')  # "@cob1": (re)start autostart sequence
            autostart_state = self._try_query('STATe?', warn=False)
            if autostart_state and 'waitingforkey' in autostart_state.lower():
                self.log.warning('Cobolt laser start requested, but the laser is waiting for the key switch: turn '
                                 'the key on the control box OFF and back ON to start the emission (CE/CDRH '
                                 'lasers need this after every software start).')
            else:
                self.log.info(f'Cobolt laser start requested (laser state: {autostart_state}).')

    # ---------------------------------------------------------------- SimpleLaserInterface: shutter
    def get_shutter_state(self):
        """ @return ShutterState: NO_SHUTTER, or with option emission_pause_as_shutter CLOSED (emission paused) /
        OPEN (emission resumed)
        """
        if not self._pause_as_shutter:
            return ShutterState.NO_SHUTTER
        with self._lock:
            paused = self._query('LASer:PAUSed?')
        if paused == '1':
            return ShutterState.CLOSED
        if paused == '0':
            return ShutterState.OPEN
        raise RuntimeError(f'Unexpected reply "{paused}" from the Cobolt laser to "LASer:PAUSed?".')

    def set_shutter_state(self, state):
        """ Pause (CLOSED) or resume (OPEN) the emission; only with option emission_pause_as_shutter.
        @param ShutterState state: ShutterState.OPEN or ShutterState.CLOSED
        """
        state = ShutterState(state)
        if not self._pause_as_shutter:
            raise RuntimeError('The Cobolt laser has no shutter. Set the option "emission_pause_as_shutter" to '
                               'use the shutter controls for pausing the emission.')
        if state not in (ShutterState.OPEN, ShutterState.CLOSED):
            raise ValueError(f'Can only set ShutterState.OPEN or ShutterState.CLOSED, not {state.name}.')
        paused = 1 if state == ShutterState.CLOSED else 0
        with self._lock:
            self._check_ok(self._query(f'LASer:PAUSed {paused}'), 'pause / resume emission')

    # ---------------------------------------------------------------- SimpleLaserInterface: misc
    def get_temperatures(self):
        """ @return dict: TEC temperatures in degrees Celsius, keyed 'TEC1' ... 'TEC3' """
        with self._lock:
            return {channel: self._query_float(f'{channel}:TEMPerature?') for channel in self._tec_channels}

    def get_extra_info(self):
        """ @return str: identification, limits, operating hours, state, run mode, interlock and fault status """
        with self._lock:
            hours = self._try_query('LASer:HOURs?', warn=False)
            state = self._try_query('STATe?', warn=False)
            run_mode = self._try_query('LASer:RUNMode?', warn=False)
            interlock = self._try_query('INTerlock?', warn=False)
            fault, _ = self._read_fault()
        interlock_text = {'0': 'open', '1': 'closed'}.get(interlock, interlock)
        return (f'{self._static_info}\n'
                f'Operating hours: {hours}\n'
                f'Laser state: {state}, run mode: {run_mode}\n'
                f'Interlock: {interlock_text}, fault: {fault or "none"}')

    # ---------------------------------------------------------------- console helpers (not in the interface)
    def clear_fault(self):
        """ Clear a pending fault once its cause is fixed ("cf"). The laser is in the Aborted state afterwards
        and has to be started again (set_laser_state(LaserState.ON) or the GUI laser button).
        """
        with self._lock:
            self._check_ok(self._call(self._laser.clear_fault), 'clear fault')
            self._read_fault()

    def get_fault(self):
        """ @return str: name of the pending fault, e.g. 'InterlockOpen' or 'Overtemperature'; '' if none """
        with self._lock:
            return self._read_fault()[0]

    def get_autostart_state(self):
        """ @return str: state of the laser within its startup sequence ("STATe?"), e.g. 'AutostartLaserOn',
        'AutostartWaitingForKeyOn', 'Standby' or 'Fault'
        """
        with self._lock:
            return self._query('STATe?')

    # ---------------------------------------------------------------- activation helpers
    def _read_limits(self):
        """ Setpoint limits: the stricter of the factory limits and the max_power / max_current options. """
        with self._lock:
            factory_power = self._try_query_float('LASer:POWer:SETPoint:MAX?')  # mW
            factory_current = self._try_query_float('LASer:CURRent:SETPoint:MAX?')  # mA

        max_power = float(self._max_power)
        if max_power <= 0:
            raise ValueError(f'ConfigOption "max_power" must be positive (got {self._max_power!r}).')
        if factory_power is not None and factory_power / 1e3 < max_power:
            self.log.info(f'Power limit lowered from the configured {max_power * 1e3:.1f} mW to the factory limit '
                          f'of {factory_power:.1f} mW.')
            max_power = factory_power / 1e3
        self._power_range = (0.0, max_power)

        max_current = None if self._max_current is None else float(self._max_current)
        if max_current is not None and max_current <= 0:
            raise ValueError(f'ConfigOption "max_current" must be positive (got {self._max_current!r}).')
        if factory_current is not None and (max_current is None or factory_current / 1e3 < max_current):
            max_current = factory_current / 1e3
        if max_current is None:
            self.log.warning('The Cobolt laser did not report its maximum current and no "max_current" option is '
                             'configured: the current setpoint range is set to 0 A.')
            max_current = 0.0
        self._current_range = (0.0, max_current)

    def _read_identification(self):
        """ Static information for get_extra_info() and the available temperature channels. """
        laser = self._laser
        with self._lock:
            idn = self._try_query('*IDN?')
            autostart = self._try_query('AUTOstart:ENAbled?')
            nominal_power = self._try_query_float('LASer:POWer:SETPoint:NOMInal?')  # mW
            tec_channels = tuple(
                channel for channel in ('TEC1', 'TEC2', 'TEC3')
                if self._try_query_float(f'{channel}:TEMPerature?', warn=False) is not None
            )
        self._tec_channels = tec_channels
        autostart_text = {'0': 'disabled', '1': 'enabled'}.get(autostart, autostart)
        self._static_info = '\n'.join((
            f'Cobolt 06-01 Series laser on {laser.port} (pycobolt {type(laser).__name__})',
            f'Identification: {idn}',
            (f'Model: {laser.modelnumber} ({self._wavelength_text(laser.modelnumber)}), '
             f'serial number: {laser.serialnumber}'),
            f'Autostart: {autostart_text}, nominal power: {nominal_power} mW',
            (f'Power setpoint limit: {self._power_range[1] * 1e3:.1f} mW, '
             f'current setpoint limit: {self._current_range[1] * 1e3:.1f} mA'),
            f'Temperature channels: {", ".join(tec_channels) or "none"}',
        ))

    @staticmethod
    def _wavelength_text(model_number):
        """ The first four digits of the model number are the wavelength in nm, e.g. '0520-06-01-0080-1100'. """
        try:
            return f'{int(str(model_number)[:4])} nm'
        except ValueError:
            return 'unknown wavelength'

    def _connection_error_message(self, err, serial_number):
        """ Turn the exception raised by pycobolt.CoboltLaser() into one clear sentence for the log. """
        chain = []
        while err is not None and len(chain) < 10:  # pycobolt chains the pyserial error as __cause__
            chain.append(f'{type(err).__name__}: {err}')
            err = err.__cause__ if err.__cause__ is not None else err.__context__
        details = ' <- '.join(chain)
        if serial_number:
            target = f'the Cobolt laser with serial number {serial_number}'
        else:
            target = f'the Cobolt laser on {self._port}'
        # pyserial reports a port held by another process as "PermissionError(13, 'Accès refusé.', None, 5)"
        # on a French Windows ("Access is denied" on an English one, "Permission denied" on Linux).
        if any(key in details for key in ('PermissionError', 'Access is denied', 'Accès refusé',
                                          'Permission denied', 'WinError 5')):
            return (f'Could not open {self._port}: access denied. The port is held by another program, '
                    f'e.g. Cobolt Monitor. Close that program and activate this module again. [{details}]')
        if any(key in details for key in ('FileNotFoundError', 'WinError 2', 'No such file', 'introuvable')):
            return (f'Could not open {self._port}: this port does not exist. Check the COM port number in the '
                    f'Device Manager or use the "serial_number" option. [{details}]')
        if 'No laser found' in details:
            return (f'No Cobolt laser with serial number {serial_number} answered on any USB serial port. Check '
                    f'the USB connection and the serial number, and close other programs using the port '
                    f'(e.g. Cobolt Monitor). [{details}]')
        if 'Not a Cobolt laser' in details:
            return (f'The device on {self._port} does not answer like a Cobolt laser. Check the port number. '
                    f'[{details}]')
        return f'Could not connect to {target}. [{details}]'

    # ---------------------------------------------------------------- fault reporting
    def _read_fault(self):
        """ Query the fault status (lock must be held).
        @return (str, bool): name of the pending fault ('' if none) and whether the interlock circuit is open.
        A new fault is written to the log once as an error, its clearing as info.
        """
        fault = ''
        interlock_open = False
        if self._query('FAULt:STATe?') == '1':
            fault = self._query('FAULt?') or 'unnamed fault'  # e.g. InterlockOpen, NoConvergence, Overtemperature
            # Manual D0951-D: "INTerlock?" returns 0 if the circuit is open, 1 if closed. (The legacy "ilk?" used
            # by pycobolt.interlock() has the opposite polarity: 1 = open.)
            interlock_open = self._query('INTerlock?') == '0'
        if fault != self._reported_fault:
            if not fault:
                self.log.info('Cobolt laser fault cleared.')
            elif interlock_open or 'interlock' in fault.lower():
                self.log.error(f'Cobolt laser fault "{fault}": the remote interlock circuit is open and the laser '
                               f'is off. Close the interlock, clear the fault (clear_fault() in the console or '
                               f'Cobolt Monitor) and start the laser again.')
            else:
                description = self._try_query('FAULt:DESCription?', warn=False)
                description = f' ({description})' if description else ''
                self.log.error(f'Cobolt laser fault "{fault}"{description}: the laser is off. Check heat sinking, '
                               f'ambient temperature and the interlock (manual D0951-D, section 7.3), then clear '
                               f'the fault (clear_fault()) and start the laser again.')
            self._reported_fault = fault
        return fault, interlock_open

    def _is_laser_on(self):
        """ Whether the laser is on, i.e. emitting, paused or armed (lock must be held).
        Decided by "l?" alone: "STATe?" lags behind commands (on the laser it still read AutostartLaserOn right
        after "l0"), so it is only returned for log messages.
        @return (bool, str): True if "l?" returns 1; the "STATe?" reply.
        """
        on = self._query('l?') == '1'
        autostart_state = self._try_query('STATe?', warn=False) or 'unknown'
        return on, autostart_state

    def _match_setpoint_to_output(self, control_mode):
        """ Before a run-mode change during emission: set the setpoint of the new mode to the measured value, clamped
        to the setpoint range, so the output does not jump (lock must be held).
        """
        if control_mode == ControlMode.CURRENT:
            name, unit, value_range = 'current', 'mA', self._current_range
            measured = self._call(self._laser.get_current)  # mA
            setter = self._laser.set_current
        else:
            name, unit, value_range = 'power', 'mW', self._power_range
            measured = self._call(self._laser.get_power)  # mW
            setter = self._laser.set_power
        low, high = value_range[0] * 1e3, value_range[1] * 1e3
        if high <= 0:
            self.log.warning(f'Cobolt laser: no {name} setpoint limit known, the {name} setpoint is not matched to '
                             f'the output before the run-mode change; the output can jump.')
            return
        target = round(min(max(measured, low), high), 3)
        if target != round(measured, 3):
            self.log.warning(f'Cobolt laser: measured {name} {measured:.2f} {unit} is outside the setpoint range '
                             f'[{low:.1f}, {high:.1f}] {unit}; {name} setpoint clamped to {target:.2f} {unit}.')
        self._check_ok(self._call(setter, target), f'set {name} setpoint to {target} {unit}')

    def _wait_until_on(self, timeout):
        """ Poll until "l?" is 1 and "STATe?" is a Laser On state (or unsupported), at most timeout s (lock held).
        @return (bool, str, float): settled, last "STATe?" reply, seconds waited.
        """
        start = time.perf_counter()
        while True:
            on, autostart_state = self._is_laser_on()
            settled = on and (autostart_state == 'unknown' or autostart_state.lower().endswith('laseron'))
            waited = time.perf_counter() - start
            if settled or waited >= timeout:
                return settled, autostart_state, waited

    # ---------------------------------------------------------------- communication helpers (lock must be held)
    def _send(self, cmd):
        """ Send one raw command (manual D0951-D syntax) and return the reply. Raises RuntimeError when the laser
        does not answer.
        """
        self._wait_for_command_spacing()
        try:
            return self._laser.send_cmd(cmd).strip()
        except Exception as err:  # pycobolt raises RuntimeError; pyserial errors if the port vanished
            raise RuntimeError(f'Communication with the Cobolt laser on {self._laser.port} failed for "{cmd}": '
                               f'{err}') from err

    def _query(self, cmd):
        """ Like _send(), but raises RuntimeError if the laser rejects the command. """
        reply = self._send(cmd)
        if reply.lower().startswith('syntax error'):
            raise RuntimeError(f'The Cobolt laser rejected the command "{cmd}" (Syntax error).')
        return reply

    def _query_float(self, cmd):
        reply = self._query(cmd)
        try:
            return float(reply)
        except ValueError as err:
            raise RuntimeError(f'Non-numeric reply "{reply}" from the Cobolt laser to "{cmd}".') from err

    def _try_query(self, cmd, warn=True):
        """ Query optional information: None (plus a warning) if the laser rejects the command. """
        reply = self._send(cmd)
        if reply.lower().startswith('syntax error'):
            if warn:
                self.log.warning(f'The Cobolt laser does not support the command "{cmd}".')
            return None
        return reply

    def _try_query_float(self, cmd, warn=True):
        reply = self._try_query(cmd, warn)
        if reply is None:
            return None
        try:
            return float(reply)
        except ValueError:
            if warn:
                self.log.warning(f'Non-numeric reply "{reply}" from the Cobolt laser to "{cmd}".')
            return None

    def _call(self, method, *args):
        """ Call a pycobolt method with command pacing; its errors become RuntimeError. """
        self._wait_for_command_spacing()
        try:
            return method(*args)
        except Exception as err:
            raise RuntimeError(f'Cobolt laser command {method.__name__}{args} failed: {err}') from err

    def _wait_for_command_spacing(self):
        """ pycobolt enforces 100 ms between two commands with a busy-wait loop; sleep that time here instead so
        the calling thread does not burn CPU.
        """
        last = getattr(self._laser, 'msg_timer', None)
        if last is not None:
            remaining = _COMMAND_SPACING - (time.perf_counter() - last)
            if remaining > 0:
                time.sleep(remaining)

    @staticmethod
    def _check_ok(reply, what):
        if str(reply).strip().upper() != 'OK':
            raise RuntimeError(f'The Cobolt laser did not acknowledge "{what}" (reply: "{reply}").')

    @staticmethod
    def _check_range(value, value_range, name, unit):
        low, high = value_range
        if not low <= value <= high:
            raise ValueError(f'{name} setpoint {value} {unit} is outside the allowed range [{low}, {high}] {unit}.')
