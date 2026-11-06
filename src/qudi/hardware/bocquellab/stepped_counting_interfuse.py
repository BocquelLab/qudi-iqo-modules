# -*- coding: utf-8 -*-
"""
BocquelLab interfuse for ODMR with a microwave source that cannot be stepped by a hardware trigger
(SRS SG384: list mode only advances on a *TRG command). It looks like a finite sampling input to
the ODMR logic; for every sample of a frame it steps the microwave scan by one point, waits a
settle time and acquires one sample from the underlying counter (e.g. the NI X-series finite
sampling input counting APD pulses).

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

__all__ = ['SteppedCountingInterfuse']

import time
import threading
import numpy as np

from qudi.core.configoption import ConfigOption
from qudi.core.connector import Connector
from qudi.util.mutex import Mutex
from qudi.interface.finite_sampling_input_interface import FiniteSamplingInputInterface
from qudi.interface.finite_sampling_input_interface import FiniteSamplingInputConstraints


class SteppedCountingInterfuse(FiniteSamplingInputInterface):
    """ Finite sampling input that steps a microwave frequency scan point by point.

    One frame = one ODMR sweep. For each sample i of the frame:
      1. microwave.trigger_scan_point()  (loads list entry i; the ODMR logic resets the list to
         entry 0 after every frame via reset_scan())
      2. wait settle_time
      3. counter.acquire_frame(samples_per_point) at samples_per_point x sample_rate, averaged:
         hardware-timed counting for 1/sample_rate seconds in total (NI-DAQmx refuses 1-sample
         acquisitions, hence default 2)
    Channels, units and frame size limits are those of the counter; sample rate limits are divided
    by samples_per_point. The dwell time per point is 1 / sample_rate (ODMR data rate x
    oversampling); each point additionally takes settle_time plus the communication and task
    set-up overhead (NI X-series finite sampling input: about 50-60 ms per acquisition).

    The connected microwave source must provide trigger_scan_point()
    (bocquellab.mw_source_srssg.BocquelLabMicrowaveSRSSG).

    Example config for copy-paste:

    odmr_stepped_counter:
        module.Class: 'bocquellab.stepped_counting_interfuse.SteppedCountingInterfuse'
        options:
            settle_time: 0.01  # s, wait after each frequency step (SG384 spec: < 8 ms)
            samples_per_point: 2  # optional, counter samples averaged per point (NI needs >= 2)
        connect:
            counter: ni_finite_sampling_input
            microwave: sg384
    """

    _counter = Connector(name='counter', interface='FiniteSamplingInputInterface')
    _microwave = Connector(name='microwave', interface='MicrowaveInterface')

    _settle_time = ConfigOption(name='settle_time', default=0.01, missing='warn', constructor=float)
    # NI-DAQmx needs a buffer of at least 2 samples per acquisition: each point is acquired as
    # samples_per_point samples at samples_per_point x the sample rate and averaged, so the
    # counting time per point stays 1 / sample_rate.
    _samples_per_point = ConfigOption(name='samples_per_point', default=2, missing='nothing',
                                      constructor=lambda x: int(x))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._lock = Mutex()
        self._frame_size = 1
        self._worker = None
        self._abort = threading.Event()
        self._buffer = dict()  # channel -> list of samples not yet returned
        self._samples_acquired = 0
        self._samples_target = 0
        self._error = None
        self._data_ready = threading.Condition()

    def on_activate(self):
        if self._settle_time < 0:
            raise ValueError(f'settle_time must be >= 0 s, got {self._settle_time}')
        if self._samples_per_point < 1:
            raise ValueError(f'samples_per_point must be >= 1, got {self._samples_per_point}')
        if not hasattr(self._microwave(), 'trigger_scan_point'):
            raise TypeError('Connected microwave source has no trigger_scan_point() '
                            '(use bocquellab.mw_source_srssg.BocquelLabMicrowaveSRSSG).')
        self._frame_size = int(self._counter().frame_size or 1)

    def on_deactivate(self):
        self.stop_buffered_acquisition()

    # ------------------------------------------------------------------ pass-through properties
    @property
    def constraints(self):
        """ Counter constraints, with the sample rate limits divided by samples_per_point. """
        c = self._counter().constraints
        k = self._samples_per_point
        return FiniteSamplingInputConstraints(
            channel_units=c.channel_units,
            frame_size_limits=c.frame_size_limits,
            sample_rate_limits=(c.min_sample_rate / k, c.max_sample_rate / k)
        )

    @property
    def active_channels(self):
        return self._counter().active_channels

    @property
    def sample_rate(self):
        return self._counter().sample_rate / self._samples_per_point

    @property
    def frame_size(self):
        return self._frame_size

    @property
    def samples_in_buffer(self):
        with self._data_ready:
            return min(len(samples) for samples in self._buffer.values()) if self._buffer else 0

    def set_sample_rate(self, rate):
        self._assert_idle('change the sample rate')
        self._counter().set_sample_rate(float(rate) * self._samples_per_point)

    def set_active_channels(self, channels):
        self._assert_idle('change the active channels')
        self._counter().set_active_channels(channels)

    def set_frame_size(self, size):
        self._assert_idle('change the frame size')
        size = int(round(size))
        if not self.constraints.frame_size_in_range(size)[0]:
            raise ValueError(f'Frame size {size} out of range {self.constraints.frame_size_limits}')
        self._frame_size = size

    # ------------------------------------------------------------------ acquisition
    def start_buffered_acquisition(self):
        with self._lock:
            if self.module_state() == 'locked':
                raise RuntimeError('Stepped acquisition already running.')
            channels = list(self.active_channels)
            with self._data_ready:
                self._buffer = {ch: list() for ch in channels}
                self._samples_acquired = 0
                self._samples_target = self._frame_size
                self._error = None
            self._abort.clear()
            self.module_state.lock()
            self._worker = threading.Thread(target=self._acquire_loop, name='stepped-odmr-acquisition',
                                            daemon=True)
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
                    raise RuntimeError('Stepped acquisition failed.') from self._error
                if self._worker is None or not self._worker.is_alive():
                    number_of_samples = available  # stopped early: return what is there
                    break
                self._data_ready.wait(timeout=0.5)
            data = {ch: np.asarray(samples[:number_of_samples], dtype=np.float64)
                    for ch, samples in self._buffer.items()}
            for samples in self._buffer.values():
                del samples[:number_of_samples]
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
        counter = self._counter()
        microwave = self._microwave()
        try:
            for _ in range(self._samples_target):
                if self._abort.is_set():
                    break
                microwave.trigger_scan_point()
                if self._settle_time > 0:
                    time.sleep(self._settle_time)
                samples = counter.acquire_frame(self._samples_per_point)
                with self._data_ready:
                    for ch, values in samples.items():
                        if ch in self._buffer:
                            # mean of equal-length sub-samples = rate over the whole point
                            self._buffer[ch].append(float(np.mean(np.asarray(values, dtype=np.float64))))
                    self._samples_acquired += 1
                    self._data_ready.notify_all()
        except Exception as err:
            self.log.exception('Stepped ODMR acquisition failed:')
            with self._data_ready:
                self._error = err
                self._data_ready.notify_all()

    def _assert_idle(self, what: str) -> None:
        if self.module_state() == 'locked':
            raise RuntimeError(f'Unable to {what} while an acquisition is running.')
