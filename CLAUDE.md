# CLAUDE.md — qudi project guide

This repo is **qudi-iqo-modules** (fork `BocquelLab/qudi-iqo-modules`, upstream `Ulm-IQO`), installed in
*editable* mode into the conda env `qudi-env` next to **qudi-core 1.8.0** (normal install). qudi is a modular
Python/Qt (PySide6) framework for controlling lab experiments. Read this before changing code.

## Where things are on this machine

| What | Path |
|---|---|
| qudi-core 1.8.0 (installed package, never edit) | `C:\Users\bocj0388\AppData\Local\miniconda3\envs\qudi-env\Lib\site-packages\qudi\` → `core/`, `util/`, `gui/`, `logic/`, `tools/` |
| qudi-iqo-modules (this repo, editable install) | `C:\Software\qudi-iqo-modules\src\qudi\` → `hardware/`, `interface/`, `logic/`, `gui/`, `util/`, `default.cfg` |
| Config files (separate git repo `BocquelLab/qudi-config`) | `C:\Software\qudi-config\dummy.cfg`, `C:\Software\qudi-config\lab_setup.cfg` |
| Python 3.10 (`python` is **not** on PATH) | `C:\Users\bocj0388\AppData\Local\miniconda3\envs\qudi-env\python.exe`; `qudi.exe` in the env's `Scripts\` |
| Logs | `C:\Users\bocj0388\qudi\log\` |
| Status variables (one YAML file per module) | `%APPDATA%\qudi\status-<Class>_<base>_<module_name>.cfg` |
| Last loaded config (re-used at next start) | `%APPDATA%\qudi\load.cfg` |

`qudi`, `qudi.hardware`, `qudi.interface`, `qudi.logic`, `qudi.gui` and `qudi.util` are **namespace packages**:
there is no `__init__.py` anywhere under `src/qudi/` and none must be added. `import qudi.hardware.x` resolves in
this repo, `import qudi.core.x` in site-packages, and `qudi.util` is merged from both (`src/sitecustomize.py`
puts this repo first so that same-named files here win over qudi-core's, e.g. in `qudi.util.fit_models`).

## Config files: dummy vs lab

- **`C:\Software\qudi-config\dummy.cfg`** — development on **any PC**. Dummy hardware only (`hardware/dummy/*`).
  Use it to test logic, GUIs and the module-loading machinery.
- **`C:\Software\qudi-config\lab_setup.cfg`** — **real hardware, lab PC only**. Activating its modules opens
  VISA / NI-DAQ resources; never load it on a development PC. New real drivers get their config block here.
- Today both files are byte-identical copies of upstream `src/qudi/default.cfg` (keep that one as the untouched
  upstream reference). They will diverge as the lab is set up.
- Config blocks live in the config repo, not in this repo. Commit config changes in `C:\Software\qudi-config`.
- This dev PC has pyvisa but **no VISA backend** (no NI-VISA / Keysight IO Libraries): `pyvisa.ResourceManager()`
  raises here. Expected — real drivers can only be activated on the lab PC.

Start qudi (shell with `qudi-env` activated, or use the full python path):

```
qudi -c C:\Software\qudi-config\dummy.cfg                      # GUI; add -d for debug logging
python -m qudi.core -c C:\Software\qudi-config\dummy.cfg -g    # same, headless (-g = no GUI)
qudi-config-editor                                             # GUI editor for .cfg files
```

Without `-c`, qudi reloads the path stored in `%APPDATA%\qudi\load.cfg` (last session), then falls back to
`~\qudi\config\default.cfg`. In the manager window, modules are activated per column; the IPython console dock
(View → Show console) exposes every **active** module under its config name, e.g. `laser_dummy.get_power()`.

## Big picture: three module layers

```
GUI  ──(Qt signals / connectors)──>  LOGIC  ──(connectors, via interfaces)──>  HARDWARE
```

- **Hardware modules** (`src/qudi/hardware/...`): talk to real devices (or dummies). Each implements one or
  more **interfaces**. No experiment logic here.
- **Interfaces** (`src/qudi/interface/...`): abstract classes — `class XInterface(Base)` with `@abstractmethod`
  members — defining what a hardware module must provide. Logic depends on interfaces, never on a concrete
  hardware class.
- **Logic modules** (`src/qudi/logic/...`): experiment control, data processing, measurement sequences.
- **GUI modules** (`src/qudi/gui/...`): Qt windows. Only display / forward user input to logic.

Threads (from `qudi.core.module`): `LogicBase._threaded = True` → every logic module gets its own QThread.
`Base._threaded = False` → a hardware module has **no thread of its own; its methods run in the thread of the
caller** (a logic thread, or the console). GUI modules run in the main thread. Consequences: hardware modules
guard device I/O with a `Mutex`; GUI↔logic communication uses Qt signals/slots, never long blocking calls from
the GUI. (`_threaded = True` on a hardware class gives it a QThread for its own QTimers/slots; direct method
calls still execute in the caller's thread.)

Rule: data and control flow GUI → logic → hardware. Never skip a layer.

## Core API (verified against the qudi-core 1.8.0 source in site-packages)

```python
from qudi.core.module import Base, LogicBase, GuiBase      # hardware / logic / gui base classes
from qudi.core.connector import Connector, ConnectorList
from qudi.core.configoption import ConfigOption
from qudi.core.statusvariable import StatusVar
from qudi.util.mutex import Mutex, RecursiveMutex          # prefer Mutex
from qudi.util.constraints import ScalarConstraint         # qudi.core.interface.ScalarConstraint is deprecated
from PySide6 import QtCore                                 # PySide6 — not PySide2, not qtpy
```

- **`ConfigOption(name=None, default=None, *, missing='nothing', constructor=None, checker=None, converter=None)`**
  with `missing` ∈ `'nothing' | 'info' | 'warn' | 'error'`. Resolved in `Base.__init__`: afterwards the instance
  attribute holds the plain value (deep-copied from YAML, so lists, not tuples). `missing='error'` and no entry
  under `options:` → `ValueError`, the module fails to load. `@_opt.constructor` marks a method that post-processes
  the value (see `dummy/process_control_dummy.py`).
- **`StatusVar(name=None, default=None, *, constructor=None, representer=None)`** — loaded from disk right before
  `on_activate`, written right after `on_deactivate` (even if that raised). Values must be YAML-serialisable; use
  `representer`/`constructor` for numpy arrays, enums, etc.
- **`Connector(interface, name=None, optional=False)`** — `interface` is the class or its name as a string; at
  connect time the target module's MRO must contain a class of that *name*. Call it to get the module:
  `self._laser()`. Optional and unconnected → `None`. `ConnectorList` takes a list of module names in the config.
- If `name` is omitted for ConfigOption / StatusVar / Connector, the attribute name (leading underscore included)
  becomes the config key. Always pass `name=` explicitly.
- `self.log` (logger), `self.module_name` (config key), `self.module_default_data_dir`,
  `self.module_state()` → `'deactivated' | 'idle' | 'locked'`. Convention: `self.module_state.lock()` while the
  device is busy / output is running, `unlock()` when done (see any `microwave/mw_source_*.py`).
- `on_activate()` / `on_deactivate()` are abstract in `Base`. An exception in `on_activate` is logged and the
  module stays deactivated; qudi keeps running. `__init__` must not touch hardware (only
  `super().__init__(*args, **kwargs)` plus attribute defaults); open/close the device in `on_activate` /
  `on_deactivate`.
- A class with any unimplemented `@abstractmethod` cannot be instantiated (`TypeError` at load);
  `Cls.__abstractmethods__` must be empty.
- Two interfaces defining the same attribute name: use `qudi.util.overload.OverloadedAttribute`
  (see `hardware/interface_test_hardware.py`).
- Signals are class attributes `sigSomething = QtCore.Signal(object)`; slots are `@QtCore.Slot()` methods.
  `GuiBase` subclasses must also implement `show()`.

## Configuration file schema

A `.cfg` file is YAML, validated against a JSON schema at load time (unknown keys are rejected):

```yaml
global:                               # every key optional
    startup_modules: []               # module names to activate at start
    stylesheet: 'qdark.qss'
    daily_data_dirs: True
    default_data_dir: null            # fallback: ~\qudi\Data
    extension_paths: []
    remote_modules_server: {address: 'localhost', port: 12345}
    namespace_server_port: 18861
    force_remote_calls_by_value: True
    hide_manager_window: False

hardware:                             # logic: and gui: have the same structure
    my_device:                        # module name, pattern [a-zA-Z_][a-zA-Z0-9_]*
        module.Class: 'temperature.acme_tc200.AcmeTc200'   # dotted path relative to qudi.hardware
        options:                      # ConfigOption name -> value
            visa_address: 'ASRL3::INSTR'
        connect:                      # Connector name -> module name (a list for ConnectorList)
            other: some_module
        allow_remote: False           # optional
```

- qudi imports `module.Class` as `importlib.import_module(f'qudi.{section}.{dotted_module}')` and takes the last
  element as the class name. So a hardware driver must sit under `src/qudi/hardware/` and the dotted path mirrors
  the folders: `src/qudi/hardware/temperature/acme_tc200.py` → `temperature.acme_tc200.AcmeTc200`.
- `connect:` keys must match `Connector` names, `options:` keys must match `ConfigOption` names. A missing
  mandatory connector is an error at activation.
- A hardware module may itself have `connect:` entries (an *interfuse*): `interfuse/switch_combiner_interfuse.py`
  merges two switches, `interfuse/magnet_motor_interfuse.py` turns motors into a `MagnetInterface`.
- Remote modules use `{native_module_name, address, port}` instead of `module.Class`.

## How to add a new instrument

### 1. Choose the interface

| Device | Interface (`src/qudi/interface/`) | Logic / GUI already driving it (`module.Class` values from `dummy.cfg`) |
|---|---|---|
| Read-only sensor: power meter, thermometer, gauge | `ProcessValueInterface` (`process_control_interface.py`) | `software_pid_controller.SoftPIDController` (connectors `process_value`, `setpoint`) → `pid_logic.PIDLogic` → `pidgui.pidgui.PIDGui`; with `ProcessControlSwitchMixin` also `switch_logic.SwitchLogic` → `switch.switch_gui.SwitchGui` |
| Source with setpoint only: voltage/current source, heater | `ProcessSetpointInterface` | same |
| Setpoint + readback: temperature controller, PSU | `ProcessControlInterface` | same |
| Discrete states: shutter, flip mirror, relay, fiber switch | `SwitchInterface` | `switch_logic.SwitchLogic` → `switch.switch_gui.SwitchGui` |
| Fixed-wavelength laser (power / current / shutter) | `SimpleLaserInterface` | `laser_logic.LaserLogic` → `laser.laser_gui.LaserGui` |
| CW / swept microwave source | `MicrowaveInterface` | `odmr_logic.OdmrLogic` → `odmr.odmrgui.OdmrGui` |
| Translation / rotation stage | `MotorInterface` | no generic GUI; `interfuse/magnet_motor_interfuse.py` → `magnet_logic` |
| Spectrometer | `SpectrometerInterface` | `spectrometer_logic.SpectrometerLogic` → `spectrometer.spectrometer_gui.SpectrometerGui` |
| Camera | `CameraInterface` | `camera_logic.CameraLogic` → `camera.cameragui.CameraGui` |
| Continuous data stream (counter, wavemeter) | `DataInStreamInterface` | `time_series_reader_logic.TimeSeriesReaderLogic` → `time_series.time_series_gui.TimeSeriesGui` |
| Triggered finite acquisition / output | `FiniteSamplingInput/Output/IOInterface` | `odmr_logic`, scanning toolchain |
| Pulse generator / AWG, time tagger | `PulserInterface`, `FastCounterInterface` | `pulsed.*` |
| Confocal scanner | `ScanningProbeInterface` | `scanning_probe_logic.ScanningProbeLogic` → `scanning.scannergui.ScannerGui` |
| PID hardware | `PIDControllerInterface` | `pid_logic.PIDLogic` → `pidgui.pidgui.PIDGui` |

Prefer the generic `ProcessValue/Setpoint/ControlInterface` for anything that is "set a number / read a number"
(multi-channel, with units and limits in `ProcessControlConstraints`). Write a new interface only when nothing
fits (step 7).

Read, in this order: the interface file (every `@abstractmethod` and the constraints class), its dummy in
`hardware/dummy/`, and one real driver with the same bus (table at the end of this section).

### 2. Create the file

- Location: `src/qudi/hardware/<folder>/<vendor>_<model>.py`. Use an existing category folder (`laser`,
  `microwave`, `motor`, `switches`, `temperature`, `power_supply`, `powermeter`, `spectrometer`, `camera/<vendor>`,
  `wavemeter`, `awg`, `fast_adc`) or create a new snake_case folder named after the category or the vendor
  (`swabian_instruments`, `picoquant`, `ni_x_series` are vendor folders). **No `__init__.py`.**
- Names: file snake_case; class CamelCase `VendorModel` (existing: `MicrowaveSmiq`, `OSW12`,
  `ThorlabsPowermeter`); one qudi module class per file (helper classes / enums allowed); config module name
  snake_case.
- Dummy counterpart (needed for a new interface, useful for GUI work): `src/qudi/hardware/dummy/<interface>_dummy.py`,
  class `<Interface>Dummy`.
- Keep the LGPL header from any existing file, start the module docstring with one line on what the device is,
  and put an **"Example config for copy-paste:"** block in the class docstring whose `module.Class` path matches
  the real file location (several upstream dummies carry stale paths — don't copy them blindly).

### 3. Write the class (template)

Copy, rename, replace the SCPI strings. This template was import-checked and run through a mocked
activate/deactivate cycle against qudi-core 1.8.0 (every abstract method implemented, missing `visa_address`
rejected, options resolved, status variable collected).

```python
# -*- coding: utf-8 -*-
"""
Qudi hardware module for the (fictional) Acme TC-200 temperature controller, SCPI over VISA.

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

import pyvisa

from qudi.core.configoption import ConfigOption
from qudi.core.statusvariable import StatusVar
from qudi.util.mutex import Mutex
from qudi.interface.process_control_interface import ProcessControlInterface
from qudi.interface.process_control_interface import ProcessControlConstraints


class AcmeTc200(ProcessControlInterface):
    """ Acme TC-200 temperature controller (SCPI over serial/USB/LAN through VISA).

    One setpoint channel ('Setpoint', K) and one read-only process channel ('Temperature', K).

    Example config for copy-paste:

    acme_tc200:
        module.Class: 'temperature.acme_tc200.AcmeTc200'
        options:
            visa_address: 'ASRL3::INSTR'  # or 'USB0::0x1234::0x5678::SN1234::INSTR', 'TCPIP0::192.168.1.10::inst0::INSTR'
            comm_timeout: 2000  # optional, in ms
            baud_rate: 9600  # optional, serial (ASRL) resources only
            setpoint_limits: [4.0, 350.0]  # optional, soft limits in K
            restore_setpoint_on_activate: False  # optional
    """

    # ConfigOption: attribute is private, "name" is the key under "options:" in the .cfg file.
    _visa_address = ConfigOption(name='visa_address', missing='error')
    _comm_timeout = ConfigOption(name='comm_timeout', default=2000, missing='info')
    _baud_rate = ConfigOption(name='baud_rate', default=None)
    _setpoint_limits = ConfigOption(name='setpoint_limits', default=(0.0, 400.0))
    _restore_setpoint = ConfigOption(name='restore_setpoint_on_activate', default=False)

    # StatusVar: saved to %APPDATA%\qudi\status-AcmeTc200_hardware_<module_name>.cfg on deactivation,
    # loaded again right before on_activate(). Must be YAML-serialisable.
    _last_setpoint = StatusVar(name='last_setpoint', default=None)

    _SETPOINT_CH = 'Setpoint'
    _PROCESS_CH = 'Temperature'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._lock = Mutex()  # hardware methods run in the thread of the calling logic module
        self._rm = None
        self._inst = None
        self._constraints = None
        self._active = {self._SETPOINT_CH: False, self._PROCESS_CH: False}

    # ---------------------------------------------------------------- module lifecycle
    def on_activate(self):
        """ Open the connection, identify the device and build the constraints.
        Raise on any failure: qudi logs the exception and leaves the module deactivated.
        """
        self._rm = pyvisa.ResourceManager()
        kwargs = dict(timeout=self._comm_timeout, write_termination='\n', read_termination='\n')
        if self._baud_rate is not None:
            kwargs['baud_rate'] = self._baud_rate
        self._inst = self._rm.open_resource(self._visa_address, **kwargs)
        idn = self._query('*IDN?')
        self.log.info(f'Connected to "{idn}" at {self._visa_address}')

        low, high = sorted(float(x) for x in self._setpoint_limits)
        self._constraints = ProcessControlConstraints(
            setpoint_channels=(self._SETPOINT_CH,),
            process_channels=(self._PROCESS_CH,),
            units={self._SETPOINT_CH: 'K', self._PROCESS_CH: 'K'},
            limits={self._SETPOINT_CH: (low, high), self._PROCESS_CH: (0.0, 1000.0)},
            dtypes={self._SETPOINT_CH: float, self._PROCESS_CH: float}
        )
        if self._restore_setpoint and self._last_setpoint is not None:
            self.set_setpoint(self._SETPOINT_CH, self._last_setpoint)

    def on_deactivate(self):
        """ Release the hardware. Must not raise, even if the device is already gone. """
        try:
            if self._inst is not None:
                self._inst.close()
            if self._rm is not None:
                self._rm.close()
        except pyvisa.VisaIOError:
            self.log.warning('VISA connection was not closed cleanly.')
        finally:
            self._inst = None
            self._rm = None

    # ---------------------------------------------------------------- interface methods
    @property
    def constraints(self):
        """ Read-only ProcessControlConstraints (channels, units, limits, dtypes). """
        return self._constraints

    def set_activity_state(self, channel, active):
        """ Enable/disable a channel (here: the control loop output). """
        self._check_channel(channel)
        with self._lock:
            self._write('OUTP ON' if active else 'OUTP OFF')  # adapt to the real command set
            self._active[channel] = bool(active)

    def get_activity_state(self, channel):
        self._check_channel(channel)
        return self._active[channel]

    def set_setpoint(self, channel, value):
        self._check_channel(channel, setpoint=True)
        in_range, clipped = self._constraints.channel_value_in_range(channel, value)
        if not in_range:
            raise ValueError(f'Setpoint {value} for "{channel}" out of range '
                             f'{self._constraints.channel_limits[channel]}')
        with self._lock:
            self._write(f'SETP {value:.3f}')
            self._last_setpoint = float(value)

    def get_setpoint(self, channel):
        self._check_channel(channel, setpoint=True)
        with self._lock:
            return float(self._query('SETP?'))

    def get_process_value(self, channel):
        self._check_channel(channel)
        with self._lock:
            return float(self._query('TEMP?'))

    # ---------------------------------------------------------------- helpers
    def _check_channel(self, channel, setpoint=False):
        valid = self._constraints.setpoint_channels if setpoint else self._constraints.all_channels
        if channel not in valid:
            raise ValueError(f'Unknown channel "{channel}". Valid channels: {valid}')

    def _write(self, cmd):
        """ Single place for all writes: easy to add logging, retries or error polling. """
        self._inst.write(cmd)

    def _query(self, cmd):
        return self._inst.query(cmd).strip()
```

What the template encodes:

- Communication: all maintained drivers use **pyvisa** for GPIB, USB-TMC, LAN (`TCPIP0::<ip>::inst0::INSTR` or
  `TCPIP::<ip>::<port>::SOCKET`) *and* RS-232 (`ASRL<n>::INSTR` + `baud_rate=`). pyserial is **not** installed
  in `qudi-env`; `motor/motor_newport_conex_agp.py` is the only pyserial driver and is unmaintained. Vendor DLLs
  go through `ctypes` (`powermeter/thorlabs_powermeter.py`); vendor Python packages are imported at module top
  (`zaber_motion`, `nidaqmx` are installed; `seabreeze`, `TimeTagger` are not).
- Set `timeout`, `write_termination`, `read_termination` explicitly in `open_resource`; route every write/query
  through `_write`/`_query` helpers under the `Mutex`.
- Constraints (channels, units, limits) are built in `on_activate`, ideally from `*IDN?` / device queries, with
  optional soft limits from `ConfigOption`s (pattern: `microwave/mw_source_smiq.py`).
- Validate inputs and `raise ValueError` / `RuntimeError`; never return error codes or `-1`. Logic catches and logs.
- SI units everywhere (Hz, W, m, K, V, A, s); human-readable channel names.
- `on_deactivate` leaves the device in a safe state and never raises.

### 4. Wire it in the config

Add the block from your docstring to `C:\Software\qudi-config\lab_setup.cfg` under `hardware:`, then connect a
logic (and GUI) module to it under `logic:` / `gui:` — copy the pair from `dummy.cfg` and swap the hardware name:

```yaml
hardware:
    acme_tc200:
        module.Class: 'temperature.acme_tc200.AcmeTc200'
        options:
            visa_address: 'ASRL3::INSTR'

logic:
    softpid:
        module.Class: 'software_pid_controller.SoftPIDController'
        options: {process_value_channel: 'Temperature', setpoint_channel: 'Setpoint', timestep: 100}
        connect: {process_value: acme_tc200, setpoint: acme_tc200}
```

Keep `dummy.cfg` wired to the dummy of the same interface so the toolchain stays testable on any PC.
Commit the config in the `qudi-config` repo.

### 5. Offline check (any PC, no hardware)

Run with the env's python (`C:\Users\bocj0388\AppData\Local\miniconda3\envs\qudi-env\python.exe`), e.g. pasted
into IPython or saved as a scratch script:

```python
from qudi.hardware.temperature.acme_tc200 import AcmeTc200          # import path == module.Class path
assert not AcmeTc200.__abstractmethods__, AcmeTc200.__abstractmethods__  # every interface method implemented
inst = AcmeTc200(qudi_main_weakref=lambda: None, name='acme_tc200',
                 config={'visa_address': 'ASRL3::INSTR'})            # same dict as the options: block
print(inst.module_base, list(inst._meta['config_options']))          # 'hardware', option attribute names
```

Instantiation does not touch the hardware (that happens in `on_activate`), so this catches typos in option
names, forgotten `missing='error'` options and unimplemented abstract methods. To go further, mock
`pyvisa.ResourceManager` and call `inst.module_state.activate()` / `.deactivate()` (patch
`qudi.core.module.yaml_dump` so no status file is written). Lint with ruff (`pip install -e ".[dev-lint-format]"`,
not installed yet; line length 120 per `pyproject.toml`).

### 6. Test on the lab PC

1. `qudi -c C:\Software\qudi-config\lab_setup.cfg -d`
2. Manager window → hardware column → activate the module. Watch the log dock (also `C:\Users\<user>\qudi\log\`).
3. Console: `acme_tc200.constraints.channel_limits`, `acme_tc200.get_process_value('Temperature')`.
4. Activate logic + GUI and exercise them. Deactivate: the device must be released by `on_deactivate`, and
   `%APPDATA%\qudi\status-AcmeTc200_hardware_acme_tc200.cfg` should now hold the StatusVars.
5. Add a line to `docs/changelog.md` (Pre-Release → New Features); commit on a branch of the fork.

### 7. If no interface fits

- `src/qudi/interface/<thing>_interface.py`: `__all__`, `class <Thing>Interface(Base)` with `@abstractmethod`
  methods / properties (`@property` above `@abstractmethod`), a `<Thing>Constraints` class (use
  `qudi.util.constraints.ScalarConstraint(default, bounds, increment=None, enforce_int=False)` for numbers),
  docstrings with units. Device-agnostic (no vendor commands), SI units, raise on error. Non-abstract convenience
  members are fine (see `SwitchInterface.states`).
- Then `hardware/dummy/<thing>_dummy.py`, a logic module in `src/qudi/logic/`, optionally a GUI; wire all of it
  into `dummy.cfg` so it can be developed without hardware.

### Existing drivers worth copying from

| Bus / style | File | Interface |
|---|---|---|
| VISA GPIB/LAN SCPI, reference quality (Mutex, constraints, `module_state` lock) | `microwave/mw_source_smiq.py` | `MicrowaveInterface` |
| VISA serial (`ASRL`), minimal | `switches/osw12.py` | `SwitchInterface` |
| VISA serial, laser | `laser/coherent_obis_laser.py`, `laser/laserquantum_laser.py` | `SimpleLaserInterface` |
| VISA TCP socket address pattern only (file itself is broken, see below) | `temperature/cryocon.py` | — |
| USB via vendor DLL + ctypes | `powermeter/thorlabs_powermeter.py` | `ProcessValueInterface` |
| USB via vendor Python package | `motor/zaber_motion.py`, `spectrometer/oceanoptics_spectrometer.py` | `MotorInterface`, `SpectrometerInterface` |
| NI-DAQmx | `ni_x_series/ni_x_series_analog_output.py` | `ProcessSetpointInterface` + `ProcessControlSwitchMixin` |
| Dummy with ConfigOption constructor + constraints | `dummy/process_control_dummy.py` | `ProcessControlInterface` |
| Dummy with StatusVar | `dummy/switch_dummy.py` | `SwitchInterface` |
| Hardware → hardware (interfuse) | `interfuse/switch_combiner_interfuse.py` | `SwitchInterface` |
| Two interfaces with a name clash | `interface_test_hardware.py` | test interfaces |

Avoid as models: `power_supply/*`, `temperature/*`, `dig_generator/national_instruments_pulser.py` (still inherit
`(Base, SomeInterface)`, implement the pre-2021 method set, warn "not tested on the new qudi core";
`Cryocon` references an undefined `ProcessInterface`, `CTC100` implements no interface) and
`motor/motor_newport_conex_agp.py` (pyserial, untested).

## Testing

- `pytest` + `pytest-qt` (`pip install -e ".[dev-test]"`, not installed yet). Tests in `tests/` start qudi
  in-process with `tests/test.cfg` (`conftest.py`). No hardware tests exist; new drivers are validated as in
  steps 5–6.
- Ruff config lives in `pyproject.toml` (line length 120).

## Conventions for Claude

- Before writing a new module, find a similar existing one (tables above) and follow its style.
- Prefer testing with dummy hardware; never assume real devices are attached; never load `lab_setup.cfg` on a
  dev PC.
- Don't edit qudi-core (site-packages). Extend in this repo; don't add `__init__.py` files.
- Hardware modules: no experiment logic, no Qt widgets, no `print` (use `self.log`), no hardware access in
  `__init__`.
- Every new hardware class: subclass the interface only (not `Base` again), `Mutex` around I/O, `constraints`
  from the device, docstring with a correct config example, config block in `C:\Software\qudi-config`, and the
  offline check from step 5.
- When adding a module, also show the config snippet needed to load it.
- Old qudi (pre-2022: `core/`, `_modclass`, `_modtype`, `InterfaceMetaclass`, `abstract_interface_method`) is
  a different API; `docs/module_conversion.md` lists the mapping. Don't mix its patterns in.
