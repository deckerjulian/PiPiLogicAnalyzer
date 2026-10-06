# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The facets of a Pico with firmware 8: pins, monitor, analog inputs, generator.

They talk to the board through :meth:`PicoDriver.command` (protocol 8 of
``docs/protocols.md``) and are made by :func:`facets_for` after what the board reports
(capabilities ``GPIO``, ``PWM``, ``MONITOR``, ``ANALOG=``, ``PATTERN_GEN=``, ``GEN_SQUARE``,
``TX_*``). Pins are GPIOs named ``GP<n>``.
"""

from __future__ import annotations

import logging
import struct
import threading
from typing import TYPE_CHECKING, Any, Optional

import numpy as np

from . import protocol
from ..base import (
    CAPABILITY_ANALOG,
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
    MODE_INPUT,
    MODE_OUTPUT,
    MODE_PWM,
    PIN_ADC,
    PIN_DIN,
    PIN_DOUT,
    PIN_PULLDOWN,
    PIN_PULLUP,
    PIN_PWM,
    MODE_ANALOG,
    MODE_INPUT_PULLDOWN,
    MODE_INPUT_PULLUP,
    AnalogInFacet,
    GeneratorFacet,
    GpioFacet,
    InstrumentError,
    MonitorFacet,
    MonitorState,
    OutputInfo,
    PinInfo,
    parse_pin_line,
)

if TYPE_CHECKING:  # pragma: no cover
    from .analyzer import PicoDriver

log = logging.getLogger("openscilab.driver")

#: the pin of the square wave output (``GEN_SQUARE``: PWM of GP22)
SQUARE_PIN = "GP22"
#: samples the pattern generator holds at least (64 KiB on the RP2040, 1 byte per sample);
#: beyond what the board holds it answers ``ERR:FULL``
PATTERN_MIN_BUFFER = 65536
#: the ADC: 12 bit, 3.3 V reference
ADC_VOLTS = 3.3 / 4096


def gpio_number(pin: str) -> int:
    """``GP7`` -> 7."""
    if not pin.upper().startswith("GP") or not pin[2:].isdigit():
        raise InstrumentError(f"no pin {pin!r} (pins are GP0, GP1, …)")
    return int(pin[2:])


def mask_of(pins) -> int:
    return sum(1 << gpio_number(pin) for pin in pins)


class _PicoFacet:
    def __init__(self, driver: "PicoDriver") -> None:
        self.driver = driver

    def _command(self, command: int, payload: bytes = b"", answer: bool = True, timeout: float = 5.0) -> str:
        return self.driver.command(command, payload, timeout=timeout, answer=answer)

    def _expect(self, line: str, prefix: str) -> str:
        if not line.startswith(prefix):
            raise InstrumentError(f"unexpected answer of the board: {line!r}")
        return line[len(prefix):]


# ---------------------------------------------------------------------- GPIO
class PicoGpio(_PicoFacet, GpioFacet):
    """Pins of the pin table (command 11); levels, pulses and PWM in the board."""

    def __init__(self, driver: "PicoDriver") -> None:
        GpioFacet.__init__(self)
        _PicoFacet.__init__(self, driver)
        self._pins: Optional[list[PinInfo]] = None
        self._modes: dict[str, str] = {}
        self._lock = threading.Lock()

    def _table(self) -> list[PinInfo]:
        if self._pins is None:
            with self._lock:
                if self._pins is None:
                    self._pins = self.driver.read_pins()
        return self._pins

    def pins(self) -> list[PinInfo]:
        pins = self._table()
        busy = set(self.driver.capture_numbers) if self.driver.is_capturing else set()
        if not busy:
            return list(pins)
        result = []
        for pin in pins:
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
        return gpio_number(name)

    def mode(self, pin: str) -> str:
        return self._modes.get(pin, MODE_INPUT)

    def set_mode(self, pin: str, mode: str) -> None:
        needs = {MODE_OUTPUT: PIN_DOUT, MODE_PWM: PIN_PWM, MODE_INPUT_PULLUP: PIN_PULLUP,
                 MODE_INPUT_PULLDOWN: PIN_PULLDOWN, MODE_ANALOG: PIN_ADC}.get(mode, PIN_DIN)
        if mode not in protocol.PIN_MODE_CODES:
            raise InstrumentError(f"unknown pin mode {mode!r}")
        number = self._pin(pin, needs)
        self._command(protocol.CMD_PIN_MODE, struct.pack("<BB", number, protocol.PIN_MODE_CODES[mode]))
        self._modes[pin] = mode

    def write(self, pin: str, value: int) -> None:
        self.write_many({pin: value})

    def write_many(self, values: dict[str, int]) -> None:
        # one command: the pins change at the same moment
        mask = levels = 0
        for pin, value in values.items():
            bit = 1 << self._pin(pin, PIN_DOUT)
            mask |= bit
            if value:
                levels |= bit
        self._command(protocol.CMD_WRITE, struct.pack("<II", mask, levels))
        for pin in values:
            self._modes[pin] = MODE_OUTPUT

    def read_many(self, pins: list[str]) -> dict[str, int]:
        mask = mask_of(pins)
        levels = int(self._expect(self._command(protocol.CMD_READ, struct.pack("<I", mask)), "LEVELS:"), 16)
        return {pin: (levels >> gpio_number(pin)) & 1 for pin in pins}

    def read(self, pin: str) -> int:
        self.pin(pin)
        return self.read_many([pin])[pin]

    def pulse(self, pin: str, width: float, level: int = 1, count: int = 1, period: float = 0.0) -> None:
        """``count`` pulses of ``width`` seconds every ``period`` seconds, timed in the board."""
        number = self._pin(pin, PIN_DOUT)
        if width <= 0 or count < 1 or (count > 1 and period <= width):
            raise InstrumentError("a pulse needs a width above 0 and, repeated, a period longer than it")
        self._command(protocol.CMD_PULSE, protocol.pack_pulse(number, level, round(width * 1e9), count,
                                                              round(period * 1e9)))
        self._modes[pin] = MODE_OUTPUT

    def pwm(self, pin: str, frequency: float, duty: float) -> float:
        """Returns the frequency the board makes (its dividers)."""
        number = self._pin(pin, PIN_PWM)
        answer = self._expect(self._command(protocol.CMD_PWM, protocol.pack_pwm(number, frequency, duty)), "PWM:")
        self._modes[pin] = MODE_PWM if duty > 0 else MODE_OUTPUT
        return float(answer)

    def safe_all(self) -> None:
        self._command(protocol.CMD_SAFE)
        self._modes.clear()

    def heartbeat(self) -> None:
        self.driver.heartbeat()


# ------------------------------------------------------------------- monitor
class PicoMonitor(_PicoFacet, MonitorFacet):
    """Command 17: the board reports ``STATE:<µs>,<levels>[,<adc>…]`` lines at the rate."""

    def __init__(self, driver: "PicoDriver") -> None:
        MonitorFacet.__init__(self)
        _PicoFacet.__init__(self, driver)
        self.watched: list[str] = []
        self.analog: tuple[str, ...] = ()
        self._running = False
        driver.line_handlers["STATE:"] = self._report

    @property
    def running(self) -> bool:
        return self._running

    def start(self, rate: float, pins: list[str], analog: tuple[str, ...] = ()) -> None:
        if rate <= 0:
            raise InstrumentError("the monitor needs a rate above 0")
        adc_mask = 0
        for name in analog:
            if not (name.upper().startswith("A") and name[1:].isdigit()):
                raise InstrumentError(f"no analog input {name!r} (A0, A1, …)")
            adc_mask |= 1 << int(name[1:])
        self.watched, self.analog = list(pins), tuple(analog)
        self._command(protocol.CMD_MONITOR, protocol.pack_monitor(rate, mask_of(pins), adc_mask))
        self._running = True

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        try:
            self._command(protocol.CMD_MONITOR, protocol.pack_monitor(0, 0, 0))
        except InstrumentError as error:
            log.debug("Stopping the monitor failed: %s", error)

    def parse(self, line: str) -> MonitorState:
        parts = line[len("STATE:"):].split(",")
        levels = int(parts[1], 16)
        analog_order = sorted(self.analog, key=lambda name: int(name[1:]))
        values = {name: int(raw) * ADC_VOLTS for name, raw in zip(analog_order, parts[2:])}
        return MonitorState(int(parts[0]) / 1e6, {pin: (levels >> gpio_number(pin)) & 1 for pin in self.watched},
                            values)

    def _report(self, line: str) -> None:
        try:
            state = self.parse(line)
        except (ValueError, IndexError):
            log.debug("Unreadable monitor report %r", line)
            return
        self._emit(state)

    def close(self) -> None:
        try:
            self.stop()
        except Exception:  # noqa: BLE001 - the board may be gone
            pass
        self.driver.line_handlers.pop("STATE:", None)


# ------------------------------------------------------------- analog inputs
class PicoAnalogIn(_PicoFacet, AnalogInFacet):
    """Command 19: single ADC readings of A0… (``ANALOG=<n>``)."""

    def read(self, pins: list[str]) -> dict[str, float]:
        names = self.driver.analog_channel_names()
        mask = 0
        for name in pins:
            if name not in names:
                raise InstrumentError(f"no analog input {name!r} (inputs: {', '.join(names)})")
            mask |= 1 << names.index(name)
        values = self._expect(self._command(protocol.CMD_ADC_READ, struct.pack("<B", mask)), "ADC:").split(",")
        ordered = sorted(pins, key=names.index)
        return {name: int(raw) * ADC_VOLTS for name, raw in zip(ordered, values)}


# ----------------------------------------------------------------- generator
class PicoGenerator(_PicoFacet, GeneratorFacet):
    """The pattern generator (commands 21–24), the square wave of GP22 (PWM) and the protocol
    transmitters (commands 26–28)."""

    def __init__(self, driver: "PicoDriver") -> None:
        GeneratorFacet.__init__(self)
        _PicoFacet.__init__(self, driver)
        self._running: set[str] = set()

    def _capabilities(self) -> frozenset[str]:
        return self.driver.capabilities()

    def _pattern_limits(self) -> Optional[tuple[float, int]]:
        value = capability_value(self._capabilities(), CAPABILITY_PATTERN_GEN)
        if not value:
            return None
        rate, _, pins = value.partition(",")
        try:
            return float(rate), int(pins or 8)
        except ValueError:
            return None

    def _output_pins(self, count: int) -> tuple[str, ...]:
        gpio = self.driver.read_pins()
        names = [pin.name for pin in gpio if pin.reserved is None and PIN_DOUT in pin.capabilities]
        return tuple(names[:count]) if count < len(names) else tuple(names)

    def outputs(self) -> list[OutputInfo]:
        result = []
        limits = self._pattern_limits()
        if limits is not None:
            rate, count = limits
            result.append(OutputInfo("PATTERN", "pattern", resolution=count, max_rate=rate,
                                     max_points=PATTERN_MIN_BUFFER, pins=self._output_pins(count)))
        if CAPABILITY_GEN_SQUARE in self._capabilities():
            result.append(OutputInfo("SQUARE", "square", max_frequency=62.5e6, pins=(SQUARE_PIN,)))
        return result

    def start(self, output: str, waveform: Any) -> float:
        from ...core import waveform as waves

        info = self.output(output)
        found = waves.problems(waveform, info)
        if found:
            raise InstrumentError("; ".join(found))
        if info.kind == "square":
            frequency = self.driver.command(protocol.CMD_PWM, protocol.pack_pwm(gpio_number(SQUARE_PIN),
                                                                                waveform.frequency, waveform.duty))
            self._running.add(output)
            return float(frequency[len("PWM:"):]) if frequency.startswith("PWM:") else waveform.frequency
        numbers = sorted(gpio_number(pin) for pin in waveform.tracks)
        first, count = numbers[0], numbers[-1] - numbers[0] + 1
        if count > info.resolution:
            raise InstrumentError(f"the pattern covers {count} GPIOs; the generator drives {info.resolution} "
                                  "consecutive ones")
        length = waveform.pattern_length
        samples = np.zeros(length, dtype=np.uint32)
        for pin, levels in waveform.tracks.items():
            bit = gpio_number(pin) - first
            padded = np.zeros(length, dtype=np.uint32)
            padded[:len(levels)] = levels
            if len(levels) < length and len(levels):
                padded[len(levels):] = levels[-1]  # a shorter track holds its last level
            samples |= (padded & 1) << np.uint32(bit)
        self.stop(output)
        flags = protocol.gen_load_flags(count)
        self.driver.pattern_bytes = 0
        for offset, block in protocol.pattern_blocks(samples, count):
            answer = self.driver.command(protocol.CMD_GEN_LOAD, struct.pack("<IB", offset, flags) + block)
            self._expect(answer, "GEN_LOADED:")
        # the pattern lives in the capture memory: captures get smaller while it is loaded
        self.driver.pattern_bytes = (length * protocol.sample_bytes(count) + 3) // 4 * 4
        answer = self.driver.command(protocol.CMD_GEN_START, protocol.pack_gen_start(
            waveform.rate, first, count, length, max(int(waveform.repeat), 0)))
        self._running.add(output)
        return float(self._expect(answer, "GEN_STARTED:"))

    def stop(self, output: str) -> None:
        info = self.output(output)
        if info.kind == "square":
            self.driver.command(protocol.CMD_PWM, protocol.pack_pwm(gpio_number(SQUARE_PIN), 1000.0, 0.0))
        else:
            self.driver.command(protocol.CMD_GEN_STOP)
        self._running.discard(output)

    def running(self, output: str) -> bool:
        if output == "PATTERN" and output in self._running:
            try:
                running, _passes = self._expect(self.driver.command(protocol.CMD_GEN_STATUS), "GEN:").split(",")
            except (InstrumentError, ValueError):
                return True
            if running.strip() != "1":
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
        size = protocol.MAX_DATA_PER_FRAME

        def pin(role: str, optional: bool = False) -> int:
            value = pins.get(role)
            if not value:
                if optional:
                    return protocol.NO_PIN
                raise InstrumentError(f"{name.upper()} needs the pin {role}")
            return gpio_number(value)

        if name == "uart":
            header = struct.pack("<BI", pin("tx"), int(settings.get("baud", 115200)))
            for start in range(0, len(data), size):
                self._command(protocol.CMD_TX_UART, header + data[start:start + size], timeout=10.0)
            return b""
        if name == "spi":
            header = struct.pack("<BBBBIB", pin("sck"), pin("mosi"), pin("miso", True), pin("cs", True),
                                 int(settings.get("frequency", 1_000_000)), int(settings.get("mode", 0)) & 3)
            received = b""
            for start in range(0, max(len(data), 1), size):
                answer = self._command(protocol.CMD_TX_SPI, header + data[start:start + size], timeout=10.0)
                received += bytes.fromhex(self._expect(answer, "RX:"))
            return received
        # i2c: one transaction, one frame
        if len(data) > size:
            raise InstrumentError(f"an I²C transaction sends at most {size} bytes")
        read = int(settings.get("read", 0))
        header = struct.pack("<BBBIH", pin("sda"), pin("scl"), int(settings.get("address", 0)) & 0x7F,
                             int(settings.get("frequency", 100_000)), read)
        try:
            answer = self._command(protocol.CMD_TX_I2C, header + data, timeout=10.0)
        except InstrumentError as error:
            if "NACK" in str(error).upper():
                raise InstrumentError(f"no I²C device acknowledges address 0x{int(settings.get('address', 0)):02X}")
            raise
        return bytes.fromhex(self._expect(answer, "RX:"))


# -------------------------------------------------------------------- factory
def facets_for(driver: "PicoDriver") -> list:
    """The facets the board's capabilities unlock (none for firmware without them)."""
    capabilities = driver.capabilities()
    facets: list = []
    if CAPABILITY_GPIO in capabilities or CAPABILITY_PWM in capabilities:
        facets.append(PicoGpio(driver))
    if CAPABILITY_MONITOR in capabilities:
        facets.append(PicoMonitor(driver))
    if capability_value(capabilities, CAPABILITY_ANALOG):
        facets.append(PicoAnalogIn(driver))
    if any(item in capabilities for item in (CAPABILITY_GEN_SQUARE, CAPABILITY_TX_UART, CAPABILITY_TX_SPI,
                                             CAPABILITY_TX_I2C)) or capability_value(capabilities,
                                                                                    CAPABILITY_PATTERN_GEN):
        facets.append(PicoGenerator(driver))
    return facets


def parse_pins(lines: list[str]) -> list[PinInfo]:
    pins = []
    for line in lines:
        info = parse_pin_line(line)
        if info is not None:
            pins.append(info)
    return pins
