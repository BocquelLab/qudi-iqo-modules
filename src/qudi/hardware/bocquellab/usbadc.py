# -*- coding: utf-8 -*-
"""
Qudi hardware module for the BocquelLab USBADC board (STM32U073, USB CDC, https://github.com/BocquelLab/USBADC),
which reads the Mini-Circuits ZX47-40LN-S+ microwave power detector: detector output voltage, power in dBm from a
calibration table, detector temperature.

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

__all__ = ['BocquelLabUsbAdc']

import os
import struct
import time
import zlib

import numpy as np

from qudi.core.configoption import ConfigOption
from qudi.util.mutex import Mutex
from qudi.interface.process_control_interface import ProcessValueInterface, ProcessControlConstraints


# Typical ZX47-40LN+ response at 2000 MHz, read off the datasheet graph (REV. D): [detector output in V, input
# power in dBm]. Accurate to about 1-2 dB; replace it by a measurement at the frequency used.
DATASHEET_CALIBRATION = [[0.53, 20.0], [0.60, 15.0], [0.72, 10.0], [0.85, 5.0], [0.97, 0.0], [1.10, -5.0],
                         [1.22, -10.0], [1.35, -15.0], [1.47, -20.0], [1.60, -25.0], [1.73, -30.0], [1.87, -35.0],
                         [2.00, -40.0]]


class BocquelLabUsbAdc(ProcessValueInterface):
    """ USBADC board reading a ZX47-40LN-S+ power detector (read only: no output pin is ever written).

    Process channels:
    - power channel (name set by 'power_channel', default 'Microwave power'), dBm: the detector input power from the
      calibration table plus 'attenuation' (the attenuator between the measured point and the detector). Outside the
      table the value is extrapolated from the end segments and is not accurate (the detector itself covers about
      -40 to +15 dBm at 1-5 GHz).
    - 'Detector voltage', V: detector DC output.
    - 'Detector temperature', °C: from the detector's temperature output (datasheet: 0.60 V at 20 °C, 2.0 mV/°C).

    The firmware converts one 12-bit ADC sample every 100 ms and scales it with 3.000 V, while the ADC reference
    (VDDA/VREF+) is the 3.3 V rail of the board: the reported voltages are 3.0/3.3 too low. 'voltage_scale'
    (default 1.1) corrects that; measure the 3.3 V test point TP18 with a multimeter and set voltage_scale to
    V(TP18) / 3.0. Readings change at most every 100 ms.

    The board is a USB CDC device (USB ID FFFF:0001). It does not answer through NI-VISA, so this module uses
    pyserial (installed in qudi-env as a dependency of pycobolt). Only one program can open the port: close the
    USBADC web app before activating this module.

    Example config for copy-paste:

    usbadc:
        module.Class: 'bocquellab.usbadc.BocquelLabUsbAdc'
        options:
            port: 'COM7'  # optional, default: the port with USB ID FFFF:0001
            voltage_scale: 1.1  # optional, V(3.3 V rail) / 3.0
            attenuation: 30.0  # optional, dB between the measured point and the detector input
            calibration: [[0.53, 20.0], [0.97, 0.0], [2.00, -40.0]]  # optional, [V, dBm] at the detector input
            calibration_measured: False  # optional, True once the table was measured
            power_channel: 'Microwave power'  # optional, name of the dBm channel
            timeout: 1.0  # optional, s per request
    """

    _port = ConfigOption(name='port', default=None)
    _voltage_scale = ConfigOption(name='voltage_scale', default=3.3 / 3.0, constructor=float)
    _attenuation = ConfigOption(name='attenuation', default=0.0, constructor=float)
    _calibration = ConfigOption(name='calibration', default=DATASHEET_CALIBRATION)
    _calibration_measured = ConfigOption(name='calibration_measured', default=False, constructor=lambda x: bool(x))
    _power_channel = ConfigOption(name='power_channel', default='Microwave power')
    _timeout = ConfigOption(name='timeout', default=1.0, constructor=float)

    USB_VID = 0xFFFF
    USB_PID = 0x0001
    _MAGIC = b'\xAA\x55\xAA\x55'
    _PING, _READ_ADC, _VERSION = 0x00, 0x01, 0x04
    _PONG, _ADC_VALUE, _STATUS, _VERSION_VALUE = 0x80, 0x81, 0x82, 0x83
    _STATUS_NAMES = {0: 'OK', 1: 'NOT_AVAILABLE', 2: 'INVALID_DATA', 3: 'TIMEOUT', 4: 'UNKNOWN'}
    _ADC_POWER_DETECTOR, _ADC_TEMPERATURE = 0, 2  # firmware channels: PA0 (SMA J7), PA2 (J10 pin 3)

    _VOLTAGE_CHANNEL = 'Detector voltage'
    _TEMPERATURE_CHANNEL = 'Detector temperature'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._lock = Mutex()
        self._serial = None
        self._message_id = 0
        self._constraints = None
        self._active = dict()
        self._cal_volts = None
        self._cal_power = None

    # ---------------------------------------------------------------- module lifecycle
    def on_activate(self):
        import serial  # pyserial, installed in qudi-env on the lab PC
        import serial.tools.list_ports

        self._cal_volts, self._cal_power = self._check_calibration(self._calibration)
        if not 0.5 <= self._voltage_scale <= 2.0:
            raise ValueError(f'voltage_scale {self._voltage_scale} is not plausible (expected about 1.1).')
        port = self._port
        if port is None:
            ports = [p.device for p in serial.tools.list_ports.comports()
                     if p.vid == self.USB_VID and p.pid == self.USB_PID]
            if not ports:
                raise RuntimeError(f'No USBADC found (USB ID {self.USB_VID:04X}:{self.USB_PID:04X}). '
                                   f'Is it plugged in and powered?')
            if len(ports) > 1:
                raise RuntimeError(f'Several devices with the USBADC USB ID: {ports}. Set the "port" option.')
            port = ports[0]
        self._serial = serial.Serial(port, baudrate=115200, timeout=0.05, write_timeout=self._timeout)
        try:
            self._serial.reset_input_buffer()
            payload = os.urandom(4)
            msg_type, data = self._request(self._PING, payload)
            if msg_type != self._PONG or data != payload:
                raise RuntimeError(f'The device on {port} does not answer like a USBADC.')
            msg_type, data = self._request(self._VERSION)
            version = tuple(data) if msg_type == self._VERSION_VALUE and len(data) == 3 else None
            if version is None or version[:2] != (0, 0):
                self.log.warning(f'USBADC protocol version {version}; this module was written for 0.0.1.')
        except Exception:
            self._close()
            raise

        power = self._power_channel
        self._constraints = ProcessControlConstraints(
            process_channels=(power, self._VOLTAGE_CHANNEL, self._TEMPERATURE_CHANNEL),
            units={power: 'dBm', self._VOLTAGE_CHANNEL: 'V', self._TEMPERATURE_CHANNEL: '°C'},
            limits={power: tuple(sorted(self._power_for_voltage(v) for v in (0.0, 3.3 * self._voltage_scale))),
                    self._VOLTAGE_CHANNEL: (0.0, 3.3 * self._voltage_scale),
                    self._TEMPERATURE_CHANNEL: (-300.0, 1000.0)},
            dtypes={power: float, self._VOLTAGE_CHANNEL: float, self._TEMPERATURE_CHANNEL: float}
        )
        self._active = {ch: True for ch in self._constraints.process_channels}
        volts = self.get_process_value(self._VOLTAGE_CHANNEL)
        self.log.info(f'USBADC on {port}, protocol {version}: detector {volts:.3f} V = '
                      f'{self._power_for_voltage(volts):.1f} dBm (attenuation {self._attenuation:g} dB included), '
                      f'{self.get_process_value(self._TEMPERATURE_CHANNEL):.1f} °C.')
        if volts < 0.3:
            self.log.warning(f'Detector output {volts:.3f} V is below its range (0.5-2.1 V): is the detector '
                             f'connected and powered?')
        if not self._calibration_measured:
            self.log.warning('USBADC: the dBm values use an unmeasured calibration (typical datasheet curve, '
                             '1-2 dB). Measure the detector at the microwave frequency and set calibration_measured.')

    def on_deactivate(self):
        self._close()

    # ---------------------------------------------------------------- interface methods
    @property
    def constraints(self):
        return self._constraints

    def set_activity_state(self, channel, active):
        """ The board measures all the time; the state is only remembered. """
        self._check_channel(channel)
        self._active[channel] = bool(active)

    def get_activity_state(self, channel):
        self._check_channel(channel)
        return self._active[channel]

    def get_process_value(self, channel):
        self._check_channel(channel)
        if channel == self._TEMPERATURE_CHANNEL:
            volts = self._read_volts(self._ADC_TEMPERATURE)
            return 20.0 + (volts - 0.60) / 0.002
        volts = self._read_volts(self._ADC_POWER_DETECTOR)
        if channel == self._VOLTAGE_CHANNEL:
            return volts
        return self._power_for_voltage(volts)

    # ---------------------------------------------------------------- helpers
    def _power_for_voltage(self, volts):
        """ Power in dBm at the measured point: calibration table (linear, extrapolated from the end segments)
        plus attenuation. """
        x, y = self._cal_volts, self._cal_power
        if volts <= x[0]:
            i = 0
        elif volts >= x[-1]:
            i = len(x) - 2
        else:
            i = int(np.searchsorted(x, volts)) - 1
        power = y[i] + (volts - x[i]) * (y[i + 1] - y[i]) / (x[i + 1] - x[i])
        return float(power) + self._attenuation

    @staticmethod
    def _check_calibration(table):
        try:
            points = sorted((float(v), float(p)) for v, p in table)
        except (TypeError, ValueError):
            raise ValueError(f'calibration must be a list of [volts, dBm] pairs, got {table!r}') from None
        volts = np.array([v for v, _ in points])
        power = np.array([p for _, p in points])
        if len(points) < 2 or np.any(np.diff(volts) <= 0):
            raise ValueError('calibration needs at least 2 points with different voltages.')
        if np.any(np.diff(power) >= 0):
            raise ValueError('calibration: the detector voltage must fall when the power rises (ZX47-40LN+ '
                             'slope about -25 mV/dB).')
        return volts, power

    def _check_channel(self, channel):
        if self._constraints is None:
            raise RuntimeError('USBADC module is not active.')
        if channel not in self._constraints.process_channels:
            raise ValueError(f'Unknown channel "{channel}". Valid channels: {self._constraints.process_channels}')

    def _read_volts(self, adc_channel):
        msg_type, data = self._request(self._READ_ADC, bytes([adc_channel]))
        if msg_type == self._ADC_VALUE and len(data) == 2:
            return struct.unpack('>H', data)[0] * 1e-3 * self._voltage_scale
        if msg_type == self._STATUS and len(data) == 1:
            raise RuntimeError(f'USBADC refused to read ADC channel {adc_channel}: '
                               f'{self._STATUS_NAMES.get(data[0], data[0])}')
        raise RuntimeError(f'Unexpected USBADC answer type 0x{msg_type:02X} to an ADC read.')

    def _request(self, msg_type, data=b''):
        """ Sends one packet and returns (type, data) of the answer with the same message ID. """
        with self._lock:
            if self._serial is None:
                raise RuntimeError('USBADC port is closed.')
            self._message_id = (self._message_id + 1) % 65536
            msg_id = self._message_id
            head = self._MAGIC + struct.pack('>HBB', msg_id, msg_type, len(data)) + data
            self._serial.write(head + struct.pack('>I', zlib.crc32(head)))
            buffer = b''
            t_end = time.perf_counter() + self._timeout
            while time.perf_counter() < t_end:
                buffer += self._serial.read(self._serial.in_waiting or 1)
                while True:
                    start = buffer.find(self._MAGIC)
                    if start < 0:
                        buffer = buffer[-3:]
                        break
                    buffer = buffer[start:]
                    if len(buffer) < 12 or len(buffer) < 12 + buffer[7]:
                        break
                    n = buffer[7]
                    packet, buffer = buffer[:12 + n], buffer[12 + n:]
                    if struct.unpack('>I', packet[8 + n:])[0] != zlib.crc32(packet[:8 + n]):
                        self.log.debug('USBADC: packet with a wrong checksum discarded.')
                        continue
                    rx_id, rx_type = struct.unpack('>HB', packet[4:7])
                    if rx_id == msg_id:
                        return rx_type, packet[8:8 + n]
            raise TimeoutError(f'USBADC did not answer within {self._timeout} s.')

    def _close(self):
        with self._lock:
            if self._serial is not None:
                try:
                    self._serial.close()
                except Exception:
                    self.log.warning('USBADC port was not closed cleanly.')
                self._serial = None
        self._constraints = None
