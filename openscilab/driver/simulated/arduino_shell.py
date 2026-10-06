# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""A simulated Arduino that speaks the protocol of the openSciLab Arduino firmware.

:class:`ArduinoShell` plays the firmware: it reads request frames from one end of a
:func:`~openscilab.driver.arduino.link.pipe` and answers them with the device model of a
simulator profile (``uno``, ``uno_r4``). The real :class:`~openscilab.driver.arduino.driver.ArduinoDriver`
talks to the other end, so the driver, its facets and the device card are tested without a board.
Address: ``arduino-sim:<profile>`` (``sim:<profile>`` stays the device model without protocol).
"""

from __future__ import annotations

import logging
import struct
import threading
from typing import Optional

import numpy as np

from ..arduino import protocol
from ..arduino.link import LinkError, pipe
from ..base import ACQUISITION_STREAM, CaptureCompletedArgs, CaptureError, CaptureProgressArgs
from ..models import AnalogChannel, AnalyzerChannel, CaptureSession, EdgeKind, TriggerType
from ...core.instrument import InstrumentError, MonitorState
from .facets import pins_of

log = logging.getLogger("openscilab.driver")

#: digital runs, analog values per DATA frame (within 250 payload bytes)
RUNS_PER_FRAME = 40
VALUES_PER_FRAME = 120


class ArduinoShell:
    """The firmware side of a simulated Arduino."""

    def __init__(self, profile: str = "uno", clock=None) -> None:
        from . import open_simulated

        self.instrument = open_simulated(profile, clock=clock)
        self.device = self.instrument.simulated_driver
        self.profile = self.device.profile
        self.pins = pins_of(self.profile)
        self.digital = list(self.profile["digital"])
        self.analog_names = list(self.profile.get("analog", []))
        if self.instrument.gpio is not None:
            self.instrument.gpio.keepalive = False  # the heartbeats of the host count, as on a board
        self.host, self._board = pipe()
        self._decoder = protocol.Decoder()
        self._event_sequence = 0
        self._send_lock = threading.Lock()
        self._stop = threading.Event()
        self._setup: Optional[tuple] = None
        self._capture: Optional[CaptureSession] = None
        self._sent = 0
        self._sent_analog = 0
        self._sent_states = 0
        self._aborted = False
        self._pattern: list[tuple[int, int]] = []
        self._arbitrary: list[int] = []
        self._monitor_remove = None
        self._thread = threading.Thread(target=self._run, name="openscilab-arduino-shell", daemon=True)
        self._thread.start()
        self._watch = threading.Thread(target=self._watchdog, name="openscilab-arduino-watchdog", daemon=True)
        self._watch.start()

    # ------------------------------------------------------------ the line
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                data = self._board.read(0.1)
            except LinkError:
                break
            for frame in self._decoder.feed(data):
                if frame.type == protocol.REQUEST:
                    self._handle(frame)
        self.close()

    def _send(self, frame_type: int, sequence: int, payload: bytes) -> None:
        with self._send_lock:
            try:
                self._board.write(protocol.encode(frame_type, sequence, payload))
            except LinkError:
                pass

    def _event(self, code: int, data: bytes = b"") -> None:
        self._event_sequence = (self._event_sequence + 1) & 0xFF
        self._send(protocol.EVENT, self._event_sequence, bytes([code]) + data)

    def _handle(self, frame: protocol.Frame) -> None:
        command = frame.command
        if self.device.fault == "disconnect":
            return  # a board that is gone answers nothing
        try:
            answer = self._answer(command, frame.data)
        except _Refused as refused:
            self._send(protocol.ERROR, frame.sequence, bytes([command, refused.code]) + refused.text.encode())
            return
        except InstrumentError as error:
            text = str(error)
            code = protocol.ERR_RESERVED if "reserved" in text else protocol.ERR_ARGUMENTS
            self._send(protocol.ERROR, frame.sequence, bytes([command, code]) + text.encode()[:200])
            return
        except (struct.error, IndexError, ValueError) as error:
            self._send(protocol.ERROR, frame.sequence,
                       bytes([command, protocol.ERR_ARGUMENTS]) + str(error).encode()[:200])
            return
        self._send(protocol.ANSWER, frame.sequence, bytes([command]) + answer)

    # -------------------------------------------------------------- helpers
    def _pin(self, index: int) -> str:
        if not 0 <= index < len(self.pins):
            raise _Refused(protocol.ERR_ARGUMENTS, f"no pin {index}")
        return self.pins[index].name

    def _pins(self, mask: int) -> list[str]:
        return [pin.name for index, pin in enumerate(self.pins) if mask >> index & 1]

    def _vref(self) -> float:
        return float(self.profile.get("analog_range", [0.0, 5.0])[1])

    def _adc_counts(self, volts: float) -> int:
        bits = int(self.profile.get("adc_bits", 10))
        return int(min(max(round(volts / self._vref() * (1 << bits)), 0), (1 << bits) - 1))

    def _busy_pins(self) -> set[str]:
        session = self._capture
        if session is None:
            return set()
        return {self.digital[channel.channel_number] for channel in session.capture_channels}

    def _check_free(self, names) -> None:
        busy = self._busy_pins() & set(names)
        if busy:
            raise _Refused(protocol.ERR_BUSY, f"{', '.join(sorted(busy))} belongs to the capture")

    def _capabilities(self) -> list[str]:
        items = list(self.profile.get("capabilities", []))
        if self.analog_names:
            items.append(f"ANALOG={len(self.analog_names)}")
        items.append(f"STREAM={int(self.profile.get('stream_bandwidth', 0))}")
        return items

    # ------------------------------------------------------------- commands
    def _answer(self, command: int, data: bytes) -> bytes:
        gpio = self.instrument.gpio
        if command == protocol.HELLO:
            # (as the firmware: the capture buffer in bytes, one byte a sample of up to 8 channels)
            text = (f"board={self.profile['name']};version=1.0.0;protocol={protocol.PROTOCOL};"
                    f"rate={int(self.profile['max_rate'])};clock={int(self.profile.get('clock_hz', 16_000_000))};"
                    f"buffer={int(self.profile['memory_depth'])};adc_bits={int(self.profile.get('adc_bits', 10))};"
                    f"dac_bits={int(self.profile.get('dac_bits', 0))};vref_mv={int(self._vref() * 1000)}")
            return text.encode()
        if command == protocol.PINS:
            index = data[0]
            if index >= len(self.pins):
                return b""
            pin = self.pins[index]
            line = (f"PIN:{pin.name},{'/'.join(sorted(pin.capabilities))},"
                    f"{'-' if pin.channel is None else pin.channel},{int(pin.logic_level * 1000)},"
                    f"{'-' if pin.analog_channel is None else pin.analog_channel}")
            if pin.reserved:
                line += f",reserved: {pin.reserved}"
            return line.encode()
        if command == protocol.CAPS:
            return ",".join(self._capabilities()).encode()
        if command == protocol.HEARTBEAT:
            if gpio is not None:
                gpio.heartbeat()
            return b""
        if command in (protocol.PIN_MODE, protocol.WRITE, protocol.READ, protocol.PWM, protocol.PULSE,
                       protocol.SAFE, protocol.DAC) and gpio is None:
            raise _Refused(protocol.ERR_UNSUPPORTED, "no GPIO")
        if command == protocol.PIN_MODE:
            index, mode = struct.unpack("<BB", data[:2])
            name = self._pin(index)
            self._check_free([name])
            modes = {code: text for text, code in protocol.MODE_CODES.items()}
            gpio.set_mode(name, modes[mode])
            return b""
        if command == protocol.WRITE:
            mask, levels = struct.unpack("<II", data[:8])
            names = self._pins(mask)
            self._check_free(names)
            gpio.write_many({name: levels >> self._index(name) & 1 for name in names})
            return b""
        if command == protocol.READ:
            (mask,) = struct.unpack("<I", data[:4])
            names = self._pins(mask)
            values = gpio.read_many(names)
            return struct.pack("<I", sum(values[name] << self._index(name) for name in names))
        if command == protocol.PWM:
            index, frequency, duty = struct.unpack("<BfH", data[:7])
            name = self._pin(index)
            self._check_free([name])
            gpio.pwm(name, frequency, duty / 65535)
            return struct.pack("<f", frequency)
        if command == protocol.PULSE:
            index, level, width = struct.unpack("<BBI", data[:6])
            name = self._pin(index)
            self._check_free([name])
            gpio.pulse(name, width / 1e6, level)
            return b""
        if command == protocol.SAFE:
            gpio.safe_all()
            self._stop_generators()
            return b""
        if command == protocol.DAC:
            index, raw = struct.unpack("<BH", data[:3])
            facet = self._facet("AnalogOutFacet")
            bits = int(self.profile.get("dac_bits", 12))
            facet.set_voltage(self._pin(index), raw / ((1 << bits) - 1) * self._vref())
            return b""
        if command == protocol.ADC_READ:
            (mask,) = struct.unpack("<H", data[:2])
            names = [name for number, name in enumerate(self.analog_names) if mask >> number & 1]
            values = self._facet("AnalogInFacet").read(names)
            return b"".join(struct.pack("<H", self._adc_counts(values[name])) for name in names)
        if command == protocol.MONITOR:
            rate, mask, adc_mask = struct.unpack("<IIH", data[:10])
            return self._monitor(rate / 1000.0, mask, adc_mask)
        if command == protocol.CAPTURE_SETUP:
            return self._capture_setup(data)
        if command == protocol.CAPTURE_START:
            return self._capture_start()
        if command == protocol.CAPTURE_ABORT:
            session = self._capture
            if session is not None:
                self._aborted = True
                self.device.stop_capture()
                if session.acquisition_mode != ACQUISITION_STREAM:
                    # a stopped capture ends without its samples (and without completing): the
                    # firmware still says DONE, once the device is free for the next one
                    thread = getattr(self.device, "_thread", None)
                    if thread is not None and thread is not threading.current_thread():
                        thread.join(2.0)
                    if self._capture is session:
                        self.device.remove_capture_progress_handler(self._progress)
                        self._capture = None
                        self._event(protocol.EVENT_DONE, struct.pack("<BII", protocol.DONE_STOPPED, self._sent, 0))
            return b""
        if command in (protocol.GEN_LOAD, protocol.GEN_START, protocol.GEN_STOP, protocol.GEN_STATUS,
                       protocol.SQUARE, protocol.ARB_LOAD, protocol.ARB_START):
            return self._generator(command, data)
        if command in (protocol.TX_UART, protocol.TX_SPI, protocol.TX_I2C):
            return self._transmit(command, data)
        raise _Refused(protocol.ERR_UNKNOWN, f"unknown command 0x{command:02X}")

    def _index(self, name: str) -> int:
        return next(index for index, pin in enumerate(self.pins) if pin.name == name)

    def _facet(self, name: str):
        for facet in self.instrument.facets():
            if any(cls.__name__ == name for cls in type(facet).__mro__):
                return facet
        raise _Refused(protocol.ERR_UNSUPPORTED, "not supported by this board")

    # -------------------------------------------------------------- monitor
    def _monitor(self, rate: float, mask: int, adc_mask: int) -> bytes:
        monitor = self._facet("MonitorFacet")
        if self._monitor_remove is not None:
            self._monitor_remove()
            self._monitor_remove = None
        monitor.stop()
        if rate <= 0:
            return b""
        names = self._pins(mask)
        analog = tuple(name for number, name in enumerate(self.analog_names) if adc_mask >> number & 1)

        def report(state: MonitorState) -> None:
            levels = sum(state.digital.get(name, 0) << self._index(name) for name in names)
            values = b"".join(struct.pack("<H", self._adc_counts(state.analog.get(name, 0.0))) for name in analog)
            self._event(protocol.EVENT_STATE, struct.pack("<II", int(state.time * 1e6) & 0xFFFFFFFF, levels) + values)

        self._monitor_remove = monitor.on_state(report)
        monitor.start(rate, names, analog)
        return b""

    # -------------------------------------------------------------- capture
    def _capture_setup(self, data: bytes) -> bytes:
        if self._capture is not None:
            raise _Refused(protocol.ERR_BUSY, "a capture runs")
        values = struct.unpack("<BIIHIIBIIBB", data[:struct.calcsize("<BIIHIIBIIBB")])
        mode, rate, mask, adc_mask, samples, pre, trigger, trigger_mask, trigger_levels, clock, edge = values
        names = self._pins(mask)
        if not names or any(name not in self.digital for name in names):
            raise _Refused(protocol.ERR_ARGUMENTS, "the pins are no capture channels")
        if mode != protocol.MODE_STATE and not 1 <= rate <= int(self.profile["max_rate"]):
            raise _Refused(protocol.ERR_ARGUMENTS, "rate")
        if mode == protocol.MODE_BUFFER and adc_mask and self.profile.get("buffer_analog") is False:
            raise _Refused(protocol.ERR_UNSUPPORTED, "buffer captures read the pins only")
        self._setup = values
        return struct.pack("<I", rate)

    def _capture_start(self) -> bytes:
        if self._setup is None:
            raise _Refused(protocol.ERR_ARGUMENTS, "no capture set up")
        mode, rate, mask, adc_mask, samples, pre, trigger, trigger_mask, trigger_levels, clock, edge = self._setup
        session = CaptureSession(frequency=max(rate, 1), pre_trigger_samples=pre,
                                 post_trigger_samples=max(samples - pre, 1))
        session.capture_channels = [AnalyzerChannel(channel_number=self.digital.index(name))
                                    for name in self._pins(mask)]
        session.analog_channels = [AnalogChannel(channel_number=number)
                                   for number in range(len(self.analog_names)) if adc_mask >> number & 1]
        session.trigger_type = TriggerType.IMMEDIATE
        if mode in (protocol.MODE_STREAM, protocol.MODE_STATE):
            session.acquisition_mode = ACQUISITION_STREAM
            session.continuous = samples == 0
            session.post_trigger_samples = samples or 10_000_000
            session.pre_trigger_samples = 0
        if mode == protocol.MODE_STATE:
            session.clock_channel = self.digital.index(self._pin(clock))
            session.clock_edge = EdgeKind.FALLING if edge else EdgeKind.RISING
        elif mode == protocol.MODE_BUFFER and trigger:
            pins = self._pins(trigger_mask)
            channels = [self.digital.index(name) for name in pins]
            if trigger in (1, 2):
                session.trigger_type = TriggerType.EDGE
                session.trigger_channel = channels[0]
                session.trigger_inverted = trigger == 2
            else:
                session.trigger_type = TriggerType.COMPLEX
                session.trigger_channel = min(channels)
                session.trigger_bit_count = max(channels) - min(channels) + 1
                session.trigger_pattern = sum(
                    (trigger_levels >> self._index(self.digital[channel]) & 1) << (channel - min(channels))
                    for channel in channels)
                session.trigger_mask = sum(1 << (channel - min(channels)) for channel in channels)
        self._capture = session
        self._sent = self._sent_analog = self._sent_states = 0
        self._aborted = False
        self.device.add_capture_progress_handler(self._progress)
        error = self.device.start_capture(session, self._completed)
        if error != CaptureError.NONE:
            self.device.remove_capture_progress_handler(self._progress)
            self._capture = None
            raise _Refused(protocol.ERR_BUSY if error == CaptureError.BUSY else protocol.ERR_ARGUMENTS, error.value)
        return b""

    def _levels(self, samples: dict[int, np.ndarray], start: int, end: int) -> np.ndarray:
        levels = np.zeros(end - start, dtype=np.uint32)
        for number, values in samples.items():
            levels |= values[start:end].astype(np.uint32) << np.uint32(self._index(self.digital[number]))
        return levels

    def _send_digital(self, levels: np.ndarray, first: int) -> None:
        runs = protocol.runs(levels.tolist())
        for start in range(0, len(runs), RUNS_PER_FRAME):
            self._event(protocol.EVENT_DATA, struct.pack("<BI", protocol.DATA_DIGITAL, first)
                        + protocol.pack_runs(runs[start:start + RUNS_PER_FRAME]))
            first += sum(count for count, _level in runs[start:start + RUNS_PER_FRAME])

    def _send_analog(self, analog: dict[int, np.ndarray], start: int, end: int, first: int) -> None:
        if not analog:
            return
        scale, offset, _bits = self.device.analog_scale()
        numbers = sorted(analog)
        columns = [np.asarray([self._adc_counts(value) for value in (analog[n][start:end] * scale + offset)])
                   for n in numbers]
        values = np.stack(columns, axis=1).reshape(-1).astype("<u2") if columns else np.zeros(0, "<u2")
        step = VALUES_PER_FRAME - VALUES_PER_FRAME % len(numbers)
        for index in range(0, len(values), step):
            self._event(protocol.EVENT_DATA, struct.pack("<BI", protocol.DATA_ANALOG, first + index // len(numbers))
                        + values[index:index + step].tobytes())

    def _send_states(self, levels: np.ndarray, times: np.ndarray, analog: dict[int, np.ndarray], first: int) -> None:
        scale, offset, _bits = self.device.analog_scale()
        numbers = sorted(analog)
        size = 8 + 2 * len(numbers)
        per_frame = max(240 // size, 1)
        for start in range(0, len(levels), per_frame):
            body = b""
            for index in range(start, min(start + per_frame, len(levels))):
                body += struct.pack("<II", int(times[index]) & 0xFFFFFFFF, int(levels[index]))
                body += b"".join(struct.pack("<H", self._adc_counts(analog[n][index] * scale + offset)) for n in numbers)
            self._event(protocol.EVENT_DATA, struct.pack("<BI", protocol.DATA_STATE, first + start) + body)

    def _forward(self, samples: dict[int, np.ndarray], analog: dict[int, np.ndarray],
                 times: Optional[np.ndarray], first_sample: int = 0) -> None:
        count = min((len(values) for values in samples.values()), default=0)
        start = max(self._sent - first_sample, 0)
        if count <= start:
            return
        if times is not None:
            self._send_states(self._levels(samples, start, count), times[start:count],
                              {n: values[start:count] for n, values in analog.items()}, self._sent)
        else:
            self._send_digital(self._levels(samples, start, count), self._sent)
            self._send_analog(analog, start, count, self._sent)
        self._sent += count - start

    def _progress(self, args: CaptureProgressArgs) -> None:
        if args.session is not self._capture:
            return
        times = args.state_times if self._capture.clock_channel is not None else None
        self._forward(args.samples, args.analog, times, args.first_sample)

    def _completed(self, args: CaptureCompletedArgs) -> None:
        session = self._capture
        self.device.remove_capture_progress_handler(self._progress)
        if session is None:
            return
        if args.success:
            samples = {channel.channel_number: channel.samples for channel in session.capture_channels}
            analog = {channel.channel_number: channel.raw for channel in session.analog_channels
                      if channel.raw is not None}
            times = session.state_times if session.clock_channel is not None else None
            self._forward(samples, analog, times, args.first_sample)
        status = protocol.DONE_STOPPED if self._aborted else (
            protocol.DONE_OVERFLOW if args.error and "overflow" in (args.error or "").lower()
            or args.error and "could not keep up" in args.error else protocol.DONE_COMPLETE)
        self._capture = None
        self._event(protocol.EVENT_DONE, struct.pack("<BII", status, self._sent, 0))

    # ------------------------------------------------------------ generator
    def _stop_generators(self) -> None:
        generator = self.instrument.generator
        if generator is None:
            return
        for output in generator.outputs():
            generator.stop(output.name, log=False)

    def _generator(self, command: int, data: bytes) -> bytes:
        from ...core import waveform as waves

        generator = self.instrument.generator
        if generator is None:
            raise _Refused(protocol.ERR_UNSUPPORTED, "no generator")
        if command == protocol.GEN_LOAD:
            (offset,) = struct.unpack("<H", data[:2])
            del self._pattern[offset:]
            self._pattern += protocol.unpack_runs(data[2:])
            return struct.pack("<H", len(self._pattern))
        if command == protocol.GEN_START:
            rate, mask, passes, _flags = struct.unpack("<fIHB", data[:11])
            levels = np.repeat([level for _count, level in self._pattern],
                               [count for count, _level in self._pattern]).astype(np.uint32)
            names = self._pins(mask)
            self._check_free(names)
            tracks = {name: ((levels >> self._index(name)) & 1).astype(np.uint8) for name in names}
            try:
                generator.start("PATTERN", waves.pattern(tracks, rate=rate, repeat=passes))
            except InstrumentError as error:
                raise _Refused(protocol.ERR_ARGUMENTS, str(error)) from None
            return struct.pack("<f", rate)
        if command == protocol.GEN_STOP:
            for name in ("PATTERN", "DAC"):
                if generator.running(name):
                    generator.stop(name)
            return b""
        if command == protocol.GEN_STATUS:
            running = generator.running("PATTERN") or generator.running("DAC")
            return struct.pack("<BH", 1 if running else 0, 0)
        if command == protocol.SQUARE:
            index, frequency = struct.unpack("<Bf", data[:5])
            name = self._pin(index)
            square = next((output for output in generator.outputs() if output.kind == "square"), None)
            if frequency <= 0:
                if square is not None and generator.running(square.name):
                    generator.stop(square.name)
                elif self.instrument.gpio is not None:
                    self.instrument.gpio.pwm(name, 1000.0, 0.0)
                return struct.pack("<f", 0.0)
            if square is not None and name in square.pins:
                level = self.pins[index].logic_level
                generator.start(square.name, waves.standard("square", frequency, amplitude=level / 2,
                                                            offset=level / 2))
            elif self.instrument.gpio is not None:
                self.instrument.gpio.pwm(name, frequency, 0.5)
            else:
                raise _Refused(protocol.ERR_UNSUPPORTED, "no square wave output")
            return struct.pack("<f", frequency)
        if command == protocol.ARB_LOAD:
            (offset,) = struct.unpack("<H", data[:2])
            del self._arbitrary[offset:]
            self._arbitrary += list(struct.unpack(f"<{(len(data) - 2) // 2}H", data[2:2 + (len(data) - 2) // 2 * 2]))
            return struct.pack("<H", len(self._arbitrary))
        if command == protocol.ARB_START:
            rate, points, passes = struct.unpack("<fHB", data[:7])
            bits = int(self.profile.get("dac_bits", 12))
            volts = np.asarray(self._arbitrary[:points], dtype=np.float64) / ((1 << bits) - 1) * self._vref()
            try:
                generator.start("DAC", waves.from_points(volts, frequency=rate / max(points, 1), repeat=passes))
            except InstrumentError as error:
                raise _Refused(protocol.ERR_ARGUMENTS, str(error)) from None
            return struct.pack("<f", rate)
        raise _Refused(protocol.ERR_UNKNOWN, "unknown command")

    def _transmit(self, command: int, data: bytes) -> bytes:
        generator = self.instrument.generator
        name = {protocol.TX_UART: "uart", protocol.TX_SPI: "spi", protocol.TX_I2C: "i2c"}[command]
        if generator is None or not generator.transmits(name):
            raise _Refused(protocol.ERR_UNSUPPORTED, f"no {name.upper()}")

        def pin(index: int) -> Optional[str]:
            return None if index == 0xFF else self._pin(index)

        if command == protocol.TX_UART:
            index, baud = struct.unpack("<BI", data[:5])
            generator.transmit("uart", data[5:], {"tx": pin(index)}, baud=baud)
            return b""
        if command == protocol.TX_SPI:
            sck, mosi, miso, cs, frequency = struct.unpack("<BBBBI", data[:8])
            pins = {"sck": pin(sck), "mosi": pin(mosi)}
            pins.update({role: pin(value) for role, value in (("miso", miso), ("cs", cs)) if value != 0xFF})
            return generator.transmit("spi", data[8:], pins, frequency=frequency)
        sda, scl, address, frequency, read = struct.unpack("<BBBIB", data[:8])
        try:
            return generator.transmit("i2c", data[8:], {"sda": pin(sda), "scl": pin(scl)}, address=address,
                                      frequency=frequency, read=read)
        except InstrumentError as error:
            if "acknowledge" in str(error):
                raise _Refused(protocol.ERR_NACK, str(error)) from None
            raise

    # ------------------------------------------------------------- watchdog
    def _watchdog(self) -> None:
        gpio = self.instrument.gpio
        while gpio is not None and not self._stop.wait(0.1):
            if gpio.check_watchdog():
                self._event(protocol.EVENT_WATCHDOG)

    def close(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        try:
            self.device.stop_capture()
        except Exception:  # noqa: BLE001 - closing
            pass
        self._board.close()
        self.instrument.close()


class _Refused(Exception):
    def __init__(self, code: int, text: str) -> None:
        super().__init__(text)
        self.code = code
        self.text = text


def open_shell(profile: str = "uno", clock=None):
    """An :class:`~openscilab.driver.arduino.driver.ArduinoDriver` connected to a simulated Arduino
    of ``profile``; disposing the driver ends the simulation."""
    from ..arduino.driver import ArduinoDriver

    shell = ArduinoShell(profile, clock=clock)
    driver = ArduinoDriver(shell.host, address=f"arduino-sim:{profile}",
                           name=f"simulated Arduino ({profile})")
    driver.shell = shell
    return driver
