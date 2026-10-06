# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The facets of an Arduino with the openSciLab firmware: pins, monitor, analog inputs and
outputs, generator (pattern, square wave, arbitrary waveform on the DAC, protocol transmitters).
Made by :func:`facets_for` after the board's capabilities."""

from __future__ import annotations

import logging
import struct
from typing import TYPE_CHECKING, Any, Optional

import numpy as np

from . import protocol
from .link import ArduinoError, LinkError
from ..base import (
    CAPABILITY_AFG,
    CAPABILITY_ANALOG,
    CAPABILITY_DAC,
    CAPABILITY_GEN_SQUARE,
    CAPABILITY_GPIO,
    CAPABILITY_MONITOR,
    CAPABILITY_PATTERN_GEN,
    CAPABILITY_PWM,
    CAPABILITY_TX_I2C,
    CAPABILITY_TX_SPI,
    CAPABILITY_TX_UART,
    capability_value,
)
from ...core.instrument import (
    MODE_ANALOG,
    MODE_INPUT,
    MODE_INPUT_PULLDOWN,
    MODE_INPUT_PULLUP,
    MODE_OUTPUT,
    MODE_PWM,
    PIN_ADC,
    PIN_DAC,
    PIN_DIN,
    PIN_DOUT,
    PIN_PULLDOWN,
    PIN_PULLUP,
    PIN_PWM,
    AnalogInFacet,
    AnalogOutFacet,
    GeneratorFacet,
    GpioFacet,
    InstrumentError,
    MonitorFacet,
    MonitorState,
    OutputInfo,
    PinInfo,
)

if TYPE_CHECKING:  # pragma: no cover
    from .driver import ArduinoDriver

log = logging.getLogger("openscilab.driver")

#: runs per GEN_LOAD frame (6 bytes each, after the 2 byte offset and the command)
RUNS_PER_FRAME = 40
#: DAC values per ARB_LOAD frame
POINTS_PER_FRAME = 120


class _ArduinoFacet:
    def __init__(self, driver: "ArduinoDriver") -> None:
        self.driver = driver

    def _request(self, command: int, data: bytes = b"", timeout: float = 3.0) -> bytes:
        try:
            return self.driver.connection.request(command, data, timeout=timeout)
        except ArduinoError as error:
            raise InstrumentError(str(error)) from error
        except LinkError as error:
            raise InstrumentError(f"the board does not answer ({error})") from error

    def _index(self, name: str) -> int:
        try:
            return self.driver.pin_index(name)
        except KeyError:
            raise InstrumentError(f"no pin {name!r}") from None

    def _mask(self, names) -> int:
        return sum(1 << self._index(name) for name in names)


class ArduinoGpio(_ArduinoFacet, GpioFacet):
    def __init__(self, driver: "ArduinoDriver") -> None:
        GpioFacet.__init__(self)
        _ArduinoFacet.__init__(self, driver)
        self._modes: dict[str, str] = {}

    def pins(self) -> list[PinInfo]:
        capture = self.driver._capture
        if capture is None:
            return list(self.driver.pins)
        busy = set(capture.pins)
        result = []
        for pin in self.driver.pins:
            if pin.reserved is None and pin.channel in busy:
                pin = PinInfo(pin.name, pin.capabilities, pin.channel, f"LA channel {pin.channel} while capturing",
                              pin.logic_level, pin.analog_channel, pin.description)
            result.append(pin)
        return result

    def _pin(self, name: str, capability: Optional[str] = None) -> int:
        info = self.pin(name)
        if info.reserved is not None:
            raise InstrumentError(f"{name} is reserved: {info.reserved}")
        if capability is not None and capability not in info.capabilities:
            raise InstrumentError(f"{name} cannot {capability.lower()}")
        return self._index(name)

    def mode(self, pin: str) -> str:
        return self._modes.get(pin, MODE_INPUT)

    def set_mode(self, pin: str, mode: str) -> None:
        if mode not in protocol.MODE_CODES:
            raise InstrumentError(f"unknown pin mode {mode!r}")
        needs = {MODE_OUTPUT: PIN_DOUT, MODE_PWM: PIN_PWM, MODE_INPUT_PULLUP: PIN_PULLUP,
                 MODE_INPUT_PULLDOWN: PIN_PULLDOWN, MODE_ANALOG: PIN_ADC}.get(mode, PIN_DIN)
        index = self._pin(pin, needs)
        self._request(protocol.PIN_MODE, struct.pack("<BB", index, protocol.MODE_CODES[mode]))
        self._modes[pin] = mode

    def write(self, pin: str, value: int) -> None:
        self.write_many({pin: value})

    def write_many(self, values: dict[str, int]) -> None:
        mask = levels = 0
        for pin, value in values.items():
            bit = 1 << self._pin(pin, PIN_DOUT)
            mask |= bit
            levels |= bit if value else 0
        self._request(protocol.WRITE, struct.pack("<II", mask, levels))
        for pin in values:
            self._modes[pin] = MODE_OUTPUT

    def read_many(self, pins: list[str]) -> dict[str, int]:
        levels = struct.unpack("<I", self._request(protocol.READ, struct.pack("<I", self._mask(pins)))[:4])[0]
        return {pin: (levels >> self._index(pin)) & 1 for pin in pins}

    def read(self, pin: str) -> int:
        return self.read_many([pin])[pin]

    def pulse(self, pin: str, width: float, level: int = 1, count: int = 1, period: float = 0.0) -> None:
        index = self._pin(pin, PIN_DOUT)
        if width <= 0 or count < 1 or (count > 1 and period <= width):
            raise InstrumentError("a pulse needs a width above 0 and, repeated, a period longer than it")
        if count > 1:
            raise InstrumentError("the Arduino firmware times single pulses only")
        self._request(protocol.PULSE, struct.pack("<BBI", index, 1 if level else 0, max(round(width * 1e6), 1)),
                      timeout=3.0 + width)
        self._modes[pin] = MODE_OUTPUT

    def pwm(self, pin: str, frequency: float, duty: float) -> float:
        index = self._pin(pin, PIN_PWM)
        duty = min(max(float(duty), 0.0), 1.0)
        answer = self._request(protocol.PWM, struct.pack("<BfH", index, float(frequency), round(duty * 65535)))
        self._modes[pin] = MODE_PWM if duty > 0 else MODE_OUTPUT
        return struct.unpack("<f", answer[:4])[0] if len(answer) >= 4 else float(frequency)

    def safe_all(self) -> None:
        self._request(protocol.SAFE)
        self._modes.clear()

    def heartbeat(self) -> None:
        self.driver.heartbeat()


class ArduinoMonitor(_ArduinoFacet, MonitorFacet):
    def __init__(self, driver: "ArduinoDriver") -> None:
        MonitorFacet.__init__(self)
        _ArduinoFacet.__init__(self, driver)
        self.watched: list[str] = []
        self.analog: tuple[str, ...] = ()
        self._running = False
        driver.connection.event_handlers[protocol.EVENT_STATE] = self._report

    @property
    def running(self) -> bool:
        return self._running

    def _adc_order(self) -> list[str]:
        names = self.driver.analog_channel_names()
        return sorted(self.analog, key=names.index)

    def start(self, rate: float, pins: list[str], analog: tuple[str, ...] = ()) -> None:
        if rate <= 0:
            raise InstrumentError("the monitor needs a rate above 0")
        names = self.driver.analog_channel_names()
        unknown = [name for name in analog if name not in names]
        if unknown:
            raise InstrumentError(f"no analog input {', '.join(unknown)}")
        self.watched, self.analog = list(pins), tuple(analog)
        adc_mask = sum(1 << names.index(name) for name in analog)
        self._request(protocol.MONITOR, struct.pack("<IIH", round(rate * 1000), self._mask(pins), adc_mask))
        self._running = True

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        try:
            self._request(protocol.MONITOR, struct.pack("<IIH", 0, 0, 0))
        except InstrumentError as error:
            log.debug("Stopping the monitor failed: %s", error)

    def _report(self, frame: protocol.Frame) -> None:
        data = frame.data
        if len(data) < 8:
            return
        stamp, levels = struct.unpack_from("<II", data)
        order = self._adc_order()
        values = struct.unpack_from(f"<{len(order)}H", data, 8) if len(data) >= 8 + 2 * len(order) else ()
        scale = self.driver.adc_scale()
        self._emit(MonitorState(stamp / 1e6, {pin: (levels >> self._index(pin)) & 1 for pin in self.watched},
                                {name: raw * scale for name, raw in zip(order, values)}))

    def close(self) -> None:
        try:
            self.stop()
        except Exception:  # noqa: BLE001 - the board may be gone
            pass


class ArduinoAnalogIn(_ArduinoFacet, AnalogInFacet):
    def read(self, pins: list[str]) -> dict[str, float]:
        names = self.driver.analog_channel_names()
        unknown = [name for name in pins if name not in names]
        if unknown:
            raise InstrumentError(f"no analog input {', '.join(unknown)} (inputs: {', '.join(names)})")
        ordered = sorted(set(pins), key=names.index)
        answer = self._request(protocol.ADC_READ, struct.pack("<H", sum(1 << names.index(name) for name in ordered)))
        values = struct.unpack_from(f"<{len(ordered)}H", answer)
        scale = self.driver.adc_scale()
        return {name: raw * scale for name, raw in zip(ordered, values)}


class ArduinoAnalogOut(_ArduinoFacet, AnalogOutFacet):
    def _dac_pins(self) -> list[PinInfo]:
        return [pin for pin in self.driver.pins if PIN_DAC in pin.capabilities and pin.reserved is None]

    def voltage_range(self, pin: str) -> tuple[float, float]:
        if pin not in [info.name for info in self._dac_pins()]:
            raise InstrumentError(f"{pin} has no DAC")
        return 0.0, self.driver._info_int("vref_mv", 3300) / 1000.0

    def set_voltage(self, pin: str, volt: float) -> None:
        low, high = self.voltage_range(pin)
        if not low <= volt <= high:
            raise InstrumentError(f"{volt:g} V is outside {low:g} to {high:g} V")
        bits = self.driver._info_int("dac_bits", 12)
        raw = round(volt / high * ((1 << bits) - 1)) if high else 0
        self._request(protocol.DAC, struct.pack("<BH", self._index(pin), raw))


class ArduinoGenerator(_ArduinoFacet, GeneratorFacet):
    def __init__(self, driver: "ArduinoDriver") -> None:
        GeneratorFacet.__init__(self)
        _ArduinoFacet.__init__(self, driver)
        self._running: set[str] = set()

    def _capabilities(self) -> frozenset[str]:
        return self.driver.capabilities()

    def outputs(self) -> list[OutputInfo]:
        result = []
        level = max((pin.logic_level for pin in self.driver.pins), default=5.0)
        value = capability_value(self._capabilities(), CAPABILITY_PATTERN_GEN)
        if value:
            rate, _, count = value.partition(",")
            pins = tuple(pin.name for pin in self.driver.pins if PIN_DOUT in pin.capabilities and pin.reserved is None
                         and pin.channel is not None)[:int(count or 8)]
            result.append(OutputInfo("PATTERN", "pattern", (0.0, level), resolution=len(pins), max_rate=float(rate),
                                     max_points=max(self.driver.buffer_size // 6, 1) * 0xFFFF, pins=pins))
        if CAPABILITY_GEN_SQUARE in self._capabilities():
            pins = tuple(pin.name for pin in self.driver.pins if PIN_PWM in pin.capabilities and pin.reserved is None)
            if pins:
                result.append(OutputInfo("SQUARE", "square", (0.0, level), max_frequency=4e6, pins=pins[-1:]))
        dac = [pin for pin in self.driver.pins if PIN_DAC in pin.capabilities and pin.reserved is None]
        if CAPABILITY_AFG in self._capabilities() and dac:
            high = self.driver._info_int("vref_mv", 3300) / 1000.0
            result.append(OutputInfo("DAC", "analog", (0.0, high), resolution=self.driver._info_int("dac_bits", 12),
                                     max_rate=50_000, max_points=max(self.driver.buffer_size // 2, 1),
                                     max_frequency=5_000, pins=(dac[0].name,)))
        return result

    def start(self, output: str, waveform: Any) -> float:
        from ...core import waveform as waves

        info = self.output(output)
        found = waves.problems(waveform, info)
        if found:
            raise InstrumentError("; ".join(found))
        self.stop(output)
        if info.kind == "square":
            answer = self._request(protocol.SQUARE, struct.pack("<Bf", self._index(info.pins[0]), waveform.frequency))
            self._running.add(output)
            return struct.unpack("<f", answer[:4])[0] if len(answer) >= 4 else waveform.frequency
        if info.kind == "pattern":
            return self._start_pattern(output, waveform)
        return self._start_arbitrary(output, info, waveform)

    def _start_pattern(self, output: str, waveform) -> float:
        length = waveform.pattern_length
        levels = np.zeros(length, dtype=np.uint32)
        mask = 0
        for pin, track in waveform.tracks.items():
            bit = self._index(pin)
            mask |= 1 << bit
            padded = np.zeros(length, dtype=np.uint32)
            padded[:len(track)] = track
            if 0 < len(track) < length:
                padded[len(track):] = track[-1]
            levels |= (padded & 1) << np.uint32(bit)
        runs = protocol.runs(levels.tolist())
        for start in range(0, len(runs), RUNS_PER_FRAME):
            self._request(protocol.GEN_LOAD, struct.pack("<H", start) + protocol.pack_runs(runs[start:start + RUNS_PER_FRAME]))
        answer = self._request(protocol.GEN_START, struct.pack("<fIHB", float(waveform.rate), mask,
                                                               max(int(waveform.repeat), 0), 0))
        self._running.add(output)
        return struct.unpack("<f", answer[:4])[0] if len(answer) >= 4 else waveform.rate

    def _start_arbitrary(self, output: str, info: OutputInfo, waveform) -> float:
        points = min(info.max_points, 1000)
        times = np.arange(points) / points / waveform.frequency
        volts = np.asarray(waveform.analog(times), dtype=np.float64)
        high = info.voltage_range[1]
        raw = np.clip(np.round(volts / high * ((1 << info.resolution) - 1)), 0, (1 << info.resolution) - 1).astype("<u2")
        for start in range(0, len(raw), POINTS_PER_FRAME):
            self._request(protocol.ARB_LOAD, struct.pack("<H", start) + raw[start:start + POINTS_PER_FRAME].tobytes())
        rate = points * waveform.frequency
        answer = self._request(protocol.ARB_START, struct.pack("<fHB", float(rate), points,
                                                               min(max(int(waveform.repeat), 0), 255)))
        self._running.add(output)
        return (struct.unpack("<f", answer[:4])[0] / points) if len(answer) >= 4 else waveform.frequency

    def stop(self, output: str) -> None:
        info = self.output(output)
        if info.kind == "square":
            self._request(protocol.SQUARE, struct.pack("<Bf", self._index(info.pins[0]), 0.0))
        else:
            self._request(protocol.GEN_STOP)
        self._running.discard(output)

    def running(self, output: str) -> bool:
        if output in self._running and self.output(output).kind != "square":
            try:
                running = self._request(protocol.GEN_STATUS)[:1]
            except InstrumentError:
                return True
            if running == b"\x00":
                self._running.discard(output)
        return output in self._running

    # ------------------------------------------------------- sending protocols
    def transmits(self, protocol_name: str) -> bool:
        wanted = {"uart": CAPABILITY_TX_UART, "spi": CAPABILITY_TX_SPI, "i2c": CAPABILITY_TX_I2C}.get(
            protocol_name.lower())
        return wanted is not None and wanted in self._capabilities()

    def transmit(self, protocol_name: str, data: bytes, pins: dict[str, str], **settings: Any) -> bytes:
        name = protocol_name.lower()
        if not self.transmits(name):
            return super().transmit(protocol_name, data, pins, **settings)
        data = bytes(data)

        def pin(role: str, optional: bool = False) -> int:
            value = pins.get(role)
            if not value:
                if optional:
                    return 0xFF
                raise InstrumentError(f"{name.upper()} needs the pin {role}")
            return self._index(value)

        size = protocol.MAX_PAYLOAD - 16
        if name == "uart":
            header = struct.pack("<BI", pin("tx"), int(settings.get("baud", 115200)))
            for start in range(0, len(data), size):
                self._request(protocol.TX_UART, header + data[start:start + size], timeout=10.0)
            return b""
        if name == "spi":
            header = struct.pack("<BBBBI", pin("sck"), pin("mosi"), pin("miso", True), pin("cs", True),
                                 int(settings.get("frequency", 1_000_000)))
            received = b""
            for start in range(0, max(len(data), 1), size):
                received += self._request(protocol.TX_SPI, header + data[start:start + size], timeout=10.0)
            return received
        if len(data) > size:
            raise InstrumentError(f"an I²C transaction sends at most {size} bytes")
        address = int(settings.get("address", 0)) & 0x7F
        header = struct.pack("<BBBIB", pin("sda"), pin("scl"), address, int(settings.get("frequency", 100_000)),
                             int(settings.get("read", 0)))
        try:
            return self.driver.connection.request(protocol.TX_I2C, header + data, timeout=10.0)
        except ArduinoError as error:
            if error.code == protocol.ERR_NACK:
                raise InstrumentError(f"no I²C device acknowledges address 0x{address:02X}") from error
            raise InstrumentError(str(error)) from error
        except LinkError as error:
            raise InstrumentError(f"the board does not answer ({error})") from error


def facets_for(driver: "ArduinoDriver") -> list:
    capabilities = driver.capabilities()
    facets: list = []
    if CAPABILITY_GPIO in capabilities or CAPABILITY_PWM in capabilities:
        facets.append(ArduinoGpio(driver))
    if CAPABILITY_MONITOR in capabilities:
        facets.append(ArduinoMonitor(driver))
    if capability_value(capabilities, CAPABILITY_ANALOG):
        facets.append(ArduinoAnalogIn(driver))
    if CAPABILITY_DAC in capabilities:
        facets.append(ArduinoAnalogOut(driver))
    if capability_value(capabilities, CAPABILITY_PATTERN_GEN) or any(
            item in capabilities for item in (CAPABILITY_GEN_SQUARE, CAPABILITY_AFG, CAPABILITY_TX_UART,
                                              CAPABILITY_TX_SPI, CAPABILITY_TX_I2C)):
        facets.append(ArduinoGenerator(driver))
    return facets
