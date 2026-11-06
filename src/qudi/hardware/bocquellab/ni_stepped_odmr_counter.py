# -*- coding: utf-8 -*-
"""
BocquelLab ODMR counter for an NI X-series card and a microwave source that is stepped by command
(SRS SG384: list mode only advances on *TRG, there is no hardware trigger input).

For each frequency point it steps the microwave scan, waits a settle time and counts APD pulses
during a hardware-timed gate of exactly the dwell time (a single counter-output pulse used as pause
trigger of the counting tasks). The NI tasks are set up once per frame (= one ODMR sweep), so the
overhead per point is well below 1 ms (measured on a PCIe-6363: about 0.3-0.4 ms), plus the
microwave command and the settle time.

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

__all__ = ['NISteppedOdmrCounter']

import time
import threading
import numpy as np
import nidaqmx as ni
from nidaqmx.constants import Edge, CountDirection, TriggerType, Level, AcquisitionType, TaskMode

from qudi.core.configoption import ConfigOption
from qudi.core.connector import Connector
from qudi.util.mutex import Mutex
from qudi.interface.finite_sampling_input_interface import FiniteSamplingInputInterface
from qudi.interface.finite_sampling_input_interface import FiniteSamplingInputConstraints


class NISteppedOdmrCounter(FiniteSamplingInputInterface):
    """ Finite sampling input for ODMR: one frame = one sweep, one sample = one frequency point.

    For each sample i of a frame:
      1. microwave.trigger_scan_point()  (loads list entry i; the ODMR logic resets the list to
         entry 0 after every frame via reset_scan())
      2. wait settle_time
      3. count rising edges on every configured PFI during a hardware gate of 1/sample_rate s
         -> counts / gate time (c/s)
    The sample rate is the ODMR data rate x oversampling, so the dwell time per point is 1/rate.

    Counters: one for the gate plus one per channel, reserved at the start of each sweep and
    released at its end (stop the time series before an ODMR scan). The gate counter's output also
    appears on its default PFI (12-15), as with qudi's other NI counter modules.

    The connected microwave source must provide trigger_scan_point()
    (bocquellab.mw_source_srssg.BocquelLabMicrowaveSRSSG).

    Example config for copy-paste:

    odmr_counter:
        module.Class: 'bocquellab.ni_stepped_odmr_counter.NISteppedOdmrCounter'
        options:
            device_name: 'Dev1'
            channel_units:  # PFI inputs to count; qudi channel names are lower case ('pfi0')
                'PFI0': 'c/s'
            settle_time: 0.01  # s, start value of the wait after each frequency step (SG384 spec:
                               # < 8 ms); can be changed at run time (set_settle_time, ODMR GUI)
            sample_rate_limits: [1, 50000]  # optional, Hz (dwell 1 s ... 20 us)
            read_write_timeout: 10  # optional, s
        connect:
            microwave: sg384
    """

    _microwave = Connector(name='microwave', interface='MicrowaveInterface')

    _device_name = ConfigOption(name='device_name', default='Dev1', missing='warn')
    _channel_units = ConfigOption(name='channel_units', missing='error')
    _settle_time = ConfigOption(name='settle_time', default=0.01, missing='warn', constructor=float)
    _sample_rate_limits = ConfigOption(name='sample_rate_limits', default=(1, 50000))
    _frame_size_limits = ConfigOption(name='frame_size_limits', default=(1, 1000000))
    _rw_timeout = ConfigOption(name='read_write_timeout', default=10, constructor=float)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._lock = Mutex()
        self._constraints = None
        self._sources = dict()  # qudi channel name ('pfi0') -> terminal ('/Dev1/PFI0')
        self._counters = tuple()
        self._sample_rate = 100.0
        self._frame_size = 1
        self._active_channels = tuple()
        # acquisition
        self._worker = None
        self._abort = threading.Event()
        self._buffer = dict()
        self._samples_target = 0
        self._samples_acquired = 0
        self._error = None
        self._data_ready = threading.Condition()

    # ------------------------------------------------------------------ module lifecycle
    def on_activate(self):
        if self._settle_time < 0:
            raise ValueError(f'settle_time must be >= 0 s, got {self._settle_time}')
        if not hasattr(self._microwave(), 'trigger_scan_point'):
            raise TypeError('Connected microwave source has no trigger_scan_point() '
                            '(use bocquellab.mw_source_srssg.BocquelLabMicrowaveSRSSG).')
        devices = {name.lower(): name for name in ni.system.System.local().devices.device_names}
        if self._device_name.lower() not in devices:
            raise ValueError(f'NI device "{self._device_name}" not found; available: {list(devices.values())}')
        self._device_name = devices[self._device_name.lower()]
        device = ni.system.Device(self._device_name)
        self._counters = tuple(ch.rsplit('/', 1)[-1] for ch in device.co_physical_chans.channel_names
                               if 'ctr' in ch.lower())
        terminals = {t.rsplit('/', 1)[-1].lower(): t for t in device.terminals if 'PFI' in t}
        units = dict()
        self._sources = dict()
        for source, unit in dict(self._channel_units).items():
            name = str(source).strip('/').rsplit('/', 1)[-1].lower()
            if name not in terminals:
                raise ValueError(f'Unknown PFI terminal "{source}" for {self._device_name}.')
            self._sources[name] = terminals[name]
            units[name] = str(unit)
        if not units:
            raise ValueError('No channel configured in channel_units.')
        if len(units) + 1 > len(self._counters):
            raise ValueError(f'{len(units)} channels need {len(units) + 1} counters, the device has '
                             f'{len(self._counters)}.')
        self._constraints = FiniteSamplingInputConstraints(
            channel_units=units,
            frame_size_limits=self._frame_size_limits,
            sample_rate_limits=self._sample_rate_limits
        )
        self._active_channels = tuple(units)
        self._sample_rate = float(self._constraints.max_sample_rate
                                  if self._sample_rate > self._constraints.max_sample_rate
                                  else max(self._sample_rate, self._constraints.min_sample_rate))

    def on_deactivate(self):
        self.stop_buffered_acquisition()

    # ------------------------------------------------------------------ properties / settings
    @property
    def constraints(self):
        return self._constraints

    @property
    def active_channels(self):
        return self._active_channels

    @property
    def sample_rate(self):
        return self._sample_rate

    @property
    def frame_size(self):
        return self._frame_size

    @property
    def settle_time(self) -> float:
        """ Wait after each frequency step before counting, in s. """
        return self._settle_time

    _MAX_SETTLE_TIME = 1.0  # s

    def set_settle_time(self, seconds: float) -> None:
        self._assert_idle('change the settle time')
        seconds = float(seconds)
        if not 0 <= seconds <= self._MAX_SETTLE_TIME:
            raise ValueError(f'Settle time {seconds} s out of range [0, {self._MAX_SETTLE_TIME}] s')
        self._settle_time = seconds

    @property
    def samples_in_buffer(self):
        with self._data_ready:
            return min(len(s) for s in self._buffer.values()) if self._buffer else 0

    def set_sample_rate(self, rate):
        self._assert_idle('change the sample rate')
        if not self._constraints.sample_rate_in_range(rate)[0]:
            raise ValueError(f'Sample rate {rate} Hz out of range {self._constraints.sample_rate_limits}')
        self._sample_rate = float(rate)

    def set_active_channels(self, channels):
        self._assert_idle('change the active channels')
        channels = tuple(channels)
        invalid = [ch for ch in channels if ch not in self._constraints.channel_names]
        if invalid:
            raise ValueError(f'Unknown channels {invalid}; valid: {self._constraints.channel_names}')
        self._active_channels = channels

    def set_frame_size(self, size):
        self._assert_idle('change the frame size')
        size = int(round(size))
        if not self._constraints.frame_size_in_range(size)[0]:
            raise ValueError(f'Frame size {size} out of range {self._constraints.frame_size_limits}')
        self._frame_size = size

    # ------------------------------------------------------------------ acquisition
    def start_buffered_acquisition(self):
        with self._lock:
            if self.module_state() == 'locked':
                raise RuntimeError('ODMR counting already running.')
            with self._data_ready:
                self._buffer = {ch: list() for ch in self._active_channels}
                self._samples_target = self._frame_size
                self._samples_acquired = 0
                self._error = None
            self._abort.clear()
            self.module_state.lock()
            self._worker = threading.Thread(target=self._acquire_loop, name='ni-stepped-odmr', daemon=True)
            self._worker.start()

    def stop_buffered_acquisition(self):
        worker = self._worker
        if worker is not None:
            self._abort.set()
            worker.join()
            self._worker = None
        if self.module_state() == 'locked':
            self.module_state.unlock()

    def get_buffered_samples(self, number_of_samples=None):
        with self._data_ready:
            available = min(len(s) for s in self._buffer.values()) if self._buffer else 0
            if number_of_samples is None:
                number_of_samples = available
            pending = self._samples_target - (self._samples_acquired - available)
            if number_of_samples > pending:
                raise ValueError(f'{number_of_samples} samples requested, but only {pending} are left '
                                 f'in this frame.')
            while True:
                available = min(len(s) for s in self._buffer.values()) if self._buffer else 0
                if available >= number_of_samples:
                    break
                if self._error is not None:
                    raise RuntimeError('ODMR counting failed.') from self._error
                if self._worker is None or not self._worker.is_alive():
                    number_of_samples = available  # stopped early: return what is there
                    break
                self._data_ready.wait(timeout=0.5)
            data = {ch: np.asarray(s[:number_of_samples], dtype=np.float64) for ch, s in self._buffer.items()}
            for s in self._buffer.values():
                del s[:number_of_samples]
            return data

    def acquire_frame(self, frame_size=None):
        previous = None
        if frame_size is not None:
            previous = self._frame_size
            self.set_frame_size(frame_size)
        try:
            self.start_buffered_acquisition()
            try:
                return self.get_buffered_samples(self._frame_size)
            finally:
                self.stop_buffered_acquisition()
        finally:
            if previous is not None:
                self._frame_size = previous

    def _acquire_loop(self):
        gate_task, count_tasks = None, dict()
        microwave = self._microwave()
        dwell = 1.0 / self._sample_rate
        settle_time = self._settle_time  # fixed for this sweep
        try:
            gate_task, gate_counter, count_tasks = self._create_tasks(dwell)
            for _ in range(self._samples_target):
                if self._abort.is_set():
                    break
                microwave.trigger_scan_point()
                self._wait(settle_time)
                start = {ch: task.read() for ch, task in count_tasks.items()}
                gate_task.start()
                gate_task.wait_until_done(timeout=dwell + self._rw_timeout)
                gate_task.stop()
                with self._data_ready:
                    for ch, task in count_tasks.items():
                        counts = (int(task.read()) - int(start[ch])) % 2 ** 32  # 32-bit counter roll-over
                        self._buffer[ch].append(counts / dwell)
                    self._samples_acquired += 1
                    self._data_ready.notify_all()
        except Exception as err:
            self.log.exception('ODMR counting failed:')
            with self._data_ready:
                self._error = err
                self._data_ready.notify_all()
        finally:
            for task in [gate_task, *count_tasks.values()]:
                if task is not None:
                    try:
                        task.close()
                    except ni.DaqError:
                        pass

    def _create_tasks(self, dwell: float):
        """ Gate: single counter-output pulse of length dwell. Counting: one count-edges task per
        channel, paused while the gate is low. Tries the counters in order and skips busy ones.
        """
        free = list(self._counters)
        gate_task, gate_counter = None, None
        for ctr in list(free):
            task = ni.Task(f'odmr_gate_{id(self)}')
            try:
                task.co_channels.add_co_pulse_chan_time(f'/{self._device_name}/{ctr}', low_time=1e-6,
                                                        high_time=dwell, initial_delay=1e-6,
                                                        idle_state=Level.LOW)
                task.timing.cfg_implicit_timing(sample_mode=AcquisitionType.FINITE, samps_per_chan=1)
                task.control(TaskMode.TASK_COMMIT)
            except ni.DaqError:
                task.close()
                continue
            gate_task, gate_counter = task, ctr
            free.remove(ctr)
            break
        if gate_task is None:
            raise RuntimeError('No free NI counter for the ODMR gate (is the time series running?).')
        count_tasks = dict()
        try:
            for ch in self._active_channels:
                for ctr in list(free):
                    task = ni.Task(f'odmr_count_{ch}_{id(self)}')
                    try:
                        channel = task.ci_channels.add_ci_count_edges_chan(
                            f'/{self._device_name}/{ctr}', edge=Edge.RISING, initial_count=0,
                            count_direction=CountDirection.COUNT_UP)
                        channel.ci_count_edges_term = self._sources[ch]
                        task.triggers.pause_trigger.trig_type = TriggerType.DIGITAL_LEVEL
                        task.triggers.pause_trigger.dig_lvl_src = \
                            f'/{self._device_name}/{gate_counter}InternalOutput'
                        task.triggers.pause_trigger.dig_lvl_when = Level.LOW  # count while gate high
                        task.start()
                    except ni.DaqError:
                        task.close()
                        continue
                    count_tasks[ch] = task
                    free.remove(ctr)
                    break
                else:
                    raise RuntimeError(f'No free NI counter for channel "{ch}" '
                                       f'(is the time series running?).')
        except Exception:
            for task in [gate_task, *count_tasks.values()]:
                task.close()
            raise
        return gate_task, gate_counter, count_tasks

    @staticmethod
    def _wait(seconds: float) -> None:
        """ Wait accurately. time.sleep on Windows can overshoot by up to ~16 ms, so short waits
        (< 50 ms) are spun on perf_counter; longer ones sleep and spin the last 20 ms.
        """
        if seconds <= 0:
            return
        end = time.perf_counter() + seconds
        if seconds >= 0.05:
            time.sleep(seconds - 0.02)
        while time.perf_counter() < end:
            pass

    def _assert_idle(self, what: str) -> None:
        if self.module_state() == 'locked':
            raise RuntimeError(f'Unable to {what} while an acquisition is running.')
