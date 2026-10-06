# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""GPIO, monitor and analog facets of a simulated instrument.

They act on the virtual circuit through the device model (:class:`~.device.SimulatedDriver`):
an output pin drives its net from the device's time plus the profile's latency, a pulse has
exactly its width, PWM is a square wave on the net (an RC filter in the wiring turns it into a
voltage). A watchdog releases every output when no command came for the profile's ``watchdog``
seconds (the firmware does the same). Pins the profile reserves (USB serial, ...) and – while a
capture runs – the capture channels cannot be outputs.
"""

from __future__ import annotations

import threading
import time
from typing import Optional

import numpy as np

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
from .circuit import KEEP_PAST, OutputSource, Source, SyncSource, WaveSource


class DacSource(Source):
    """An analog output: voltages from given times on."""

    kind = "dac"

    def __init__(self) -> None:
        self.changes: list[tuple[float, float]] = []
        self.frequency = None

    def set(self, time_: float, volts: float) -> None:
        self.changes = [change for change in self.changes if change[0] < time_] + [(time_, float(volts))]

    def analog(self, start, rate, count):
        times = start + np.arange(count) / rate
        if not self.changes:
            return np.zeros(count)
        stamps = np.array([when for when, _value in self.changes])
        values = np.array([value for _when, value in self.changes])
        index = np.searchsorted(stamps, times + 1e-12, side="right") - 1
        return np.where(index >= 0, values[np.clip(index, 0, None)], 0.0)

    def value_range(self, analog):
        values = [value for _when, value in self.changes] or [0.0]
        return (min(values), max(values)) if analog else (0.0, 1.0)


def pins_of(profile: dict) -> list[PinInfo]:
    """The pins of a profile (``pins``), or the capture channels as inputs."""
    level = float(profile.get("logic_level", 3.3))
    digital = list(profile.get("digital", []))
    if profile.get("pins"):
        result = []
        for pin in profile["pins"]:
            name = str(pin["name"])
            result.append(PinInfo(
                name=name,
                capabilities=frozenset(pin.get("caps", ["DIN"])),
                channel=digital.index(name) if name in digital else pin.get("channel"),
                reserved=pin.get("reserved"),
                logic_level=float(pin.get("level", level)),
                analog_channel=pin.get("analog_channel"),
                description=str(pin.get("description", "")),
            ))
        return result
    return [PinInfo(name, frozenset({"DIN"}), channel=index, logic_level=level) for index, name in enumerate(digital)]


class _DeviceFacet:
    def __init__(self, device) -> None:
        self.device = device

    def _check(self) -> None:
        if self.device.fault == "disconnect":
            raise InstrumentError("the device does not answer (disconnected)")

    def _now(self) -> float:
        return self.device.clock() + self.device.delay + float(self.device.profile.get("gpio_latency", 0.0))


#: the pin capabilities for people
CAPABILITY_NAMES = {PIN_DIN: "digital input", PIN_DOUT: "digital output", PIN_PULLUP: "input with pull-up",
                    PIN_PULLDOWN: "input with pull-down", PIN_PWM: "PWM output", PIN_ADC: "analog input",
                    PIN_DAC: "analog output", "CLOCK": "clock input", "CLOCK_SW": "clock input"}


class SimulatedGpio(_DeviceFacet, GpioFacet):
    def __init__(self, device) -> None:
        GpioFacet.__init__(self)
        _DeviceFacet.__init__(self, device)
        self._modes: dict[str, str] = {}
        self._outputs: dict[str, OutputSource] = {}
        self.last_contact = 0.0
        self.watchdog = float(device.profile.get("watchdog", 1.0))
        self.watchdog_fired = False
        self._lock = threading.RLock()

    # ----------------------------------------------------------------- pins
    def pins(self) -> list[PinInfo]:
        pins = pins_of(self.device.profile)
        if not self.device.is_capturing:
            return pins
        busy = {channel.channel_number for channel in getattr(self.device, "_capture_channels", [])}
        result = []
        for pin in pins:
            if pin.reserved is None and pin.channel is not None and (not busy or pin.channel in busy):
                pin = PinInfo(pin.name, pin.capabilities, pin.channel, f"LA channel {pin.channel} while capturing",
                              pin.logic_level, pin.analog_channel, pin.description)
            result.append(pin)
        return result

    def _pin(self, name: str, capability: Optional[str] = None) -> PinInfo:
        self._check()
        info = self.pin(name)
        if info.reserved is not None:
            raise InstrumentError(f"{name} is reserved: {info.reserved}")
        if capability is not None and capability not in info.capabilities:
            raise InstrumentError(f"{name} is no {CAPABILITY_NAMES.get(capability, capability)} "
                                  f"(it is: {', '.join(CAPABILITY_NAMES.get(item, item) for item in sorted(info.capabilities))})")
        return info

    def mode(self, pin: str) -> str:
        return self._modes.get(pin, MODE_INPUT)

    def set_mode(self, pin: str, mode: str) -> None:
        needs = {MODE_OUTPUT: PIN_DOUT, MODE_PWM: PIN_PWM, MODE_INPUT_PULLUP: PIN_PULLUP,
                 MODE_INPUT_PULLDOWN: PIN_PULLDOWN, MODE_ANALOG: PIN_ADC}.get(mode)
        info = self._pin(pin, needs)
        with self._lock:
            self._contact()
            now = self._now()
            if mode in (MODE_OUTPUT, MODE_PWM):
                output = self._output(pin, info)
                if mode == MODE_OUTPUT and output.level_at(now) is None:
                    output.set(now, 0)
            else:
                self._release(pin, now)
                if mode == MODE_INPUT_PULLUP:
                    self._pull(pin, info, 1)
                elif pin in self._outputs:
                    self._outputs[pin].idle = 0  # (no pull-up any more: a pull-down or nothing)
            self._modes[pin] = mode
        self.device.outputs_changed()
        self.device.log(f"{pin}: {mode}")

    def _output(self, pin: str, info: PinInfo) -> OutputSource:
        output = self._outputs.get(pin)
        if output is None:
            output = OutputSource(high=info.logic_level)
            current = self.device.circuit.sources.get(pin)
            if current is not None and not isinstance(current, OutputSource):
                # what is connected to the pin shows again once the output lets go of it
                output.below = current
            self.device.circuit.drive(pin, output)
            self._outputs[pin] = output
        return output

    def _pull(self, pin: str, info: PinInfo, level: int) -> None:
        output = self._output(pin, info)
        output.idle = level

    def _release(self, pin: str, at: float) -> None:
        output = self._outputs.get(pin)
        if output is not None:
            output.set(at, None)

    def write(self, pin: str, value: int) -> None:
        info = self._pin(pin, PIN_DOUT)
        with self._lock:
            self._contact()
            if self.mode(pin) != MODE_OUTPUT:
                self._modes[pin] = MODE_OUTPUT
            self._output(pin, info).set(self._now(), 1 if value else 0)
        self.device.outputs_changed()
        self.device.log(f"{pin} = {1 if value else 0}")

    def write_many(self, values: dict[str, int]) -> None:
        # one port write: the same moment for every pin
        infos = {pin: self._pin(pin, PIN_DOUT) for pin in values}
        with self._lock:
            self._contact()
            now = self._now()
            for pin, value in values.items():
                self._modes[pin] = MODE_OUTPUT
                self._output(pin, infos[pin]).set(now, 1 if value else 0)
        self.device.outputs_changed()
        self.device.log(", ".join(f"{pin} = {1 if value else 0}" for pin, value in values.items()))

    def read(self, pin: str) -> int:
        self._check()
        self.pin(pin)  # (a pin the device does not have reads nothing, it is an error)
        with self._lock:
            self._contact()
            return int(self.device.circuit.digital(pin, self._now(), 1e6, 1)[0])

    def pulse(self, pin: str, width: float, level: int = 1, count: int = 1, period: float = 0.0) -> None:
        info = self._pin(pin, PIN_DOUT)
        count = int(count)
        if width <= 0 or count < 1 or (count > 1 and period <= width):
            raise InstrumentError("a pulse needs a width above 0 and, repeated, a period longer than it")
        with self._lock:
            self._contact()
            output = self._output(pin, info)
            now = self._now()
            rest = output.level_at(now)
            rest = (1 - level) if rest is None or rest == 2 else rest
            output.set(now, level)
            # exactly ``width`` later (the device times it), then every ``period``
            output.add(now + float(width), rest)
            for index in range(1, count):
                output.add(now + index * float(period), level)
                output.add(now + index * float(period) + float(width), rest)
            self._modes[pin] = MODE_OUTPUT
        self.device.outputs_changed()
        self.device.log(f"{pin}: pulse {width * 1e6:g} µs" + (f" × {count}" if count > 1 else ""))

    def pwm(self, pin: str, frequency: float, duty: float) -> None:
        info = self._pin(pin, PIN_PWM)
        duty = min(max(float(duty), 0.0), 1.0)
        with self._lock:
            self._contact()
            output = self._output(pin, info)
            if duty <= 0:
                output.set(self._now(), 0)
            elif duty >= 1:
                output.set(self._now(), 1)
            else:
                output.set_pwm(self._now(), frequency, duty)
            self._modes[pin] = MODE_PWM
        self.device.outputs_changed()
        self.device.log(f"{pin}: PWM {frequency:g} Hz, {duty * 100:g} %")
        return float(frequency)

    def safe_all(self) -> None:
        with self._lock:
            now = self._now()
            for pin in list(self._outputs):
                self._release(pin, now)
                self._outputs[pin].idle = 0
                self._modes[pin] = MODE_INPUT
        self.device.release_outputs(now)
        self.device.outputs_changed()
        self.device.log("all outputs safe")

    def heartbeat(self) -> None:
        self._check()
        with self._lock:
            self._contact()

    def _contact(self) -> None:
        self.check_watchdog()
        self.last_contact = self.device.clock()

    def check_watchdog(self) -> bool:
        """Releases the outputs when the watchdog ran out; ``True`` if it did now.

        While :attr:`keepalive` is on, the driver keeps contact (as a real driver sends its
        heartbeat): the watchdog only runs out when the host stops (``keepalive`` off) or the
        device is disconnected (the fault ``disconnect``)."""
        with self._lock:
            if not self.watchdog or not self._outputs:
                return False
            if self.keepalive and self.device.fault != "disconnect":
                self.last_contact = max(self.last_contact, self.device.clock())
                return False
            expiry = self.last_contact + self.watchdog
            if self.device.clock() <= expiry:
                return False
            fired = False
            for pin, output in self._outputs.items():
                level = output.level_at(expiry)
                if level is not None and output.changes and output.changes[-1][0] <= expiry:
                    output.set(expiry, None)
                    self._modes[pin] = MODE_INPUT
                    fired = True
            if fired:
                self.watchdog_fired = True
                self.device.release_outputs(expiry)
                self.device.log("watchdog: outputs released")
            return fired

    def restart(self) -> None:
        """The device restarted: every pin an input again, generators and analog outputs off."""
        with self._lock:
            now = self._now()
            for pin in list(self._outputs):
                self._release(pin, now)
                self._outputs[pin].idle = 0
            self._modes.clear()
        self.device.release_outputs(now)


class SimulatedAnalogIn(_DeviceFacet, AnalogInFacet):
    def __init__(self, device) -> None:
        AnalogInFacet.__init__(self)
        _DeviceFacet.__init__(self, device)

    def read(self, pins: list[str]) -> dict[str, float]:
        self._check()
        names = self.device.analog_channel_names()
        result = {}
        for pin in pins:
            if pin not in names:
                raise InstrumentError(f"{pin} is no analog input")
            # (the circuit's time on the device's sample clock, which sample_analog expects)
            raw = self.device.sample_analog(names.index(pin), self.device.device_time(self._now()), 1e6, 1)[0]
            scale, offset, _bits = self.device.analog_scale()
            result[pin] = float(raw) * scale + offset
        return result


class SimulatedAnalogOut(_DeviceFacet, AnalogOutFacet):
    def __init__(self, device) -> None:
        AnalogOutFacet.__init__(self)
        _DeviceFacet.__init__(self, device)
        self._sources: dict[str, DacSource] = {}

    def voltage_range(self, pin: str) -> tuple[float, float]:
        low, high = self.device.profile.get("dac_range", [0.0, 3.3])
        return float(low), float(high)

    def set_voltage(self, pin: str, volt: float) -> float:
        """Sets the output; returns the voltage it gives (in its range and steps)."""
        self._check()
        info = self.device.gpio.pin(pin) if self.device.gpio else None
        if info is not None and PIN_DAC not in info.capabilities:
            raise InstrumentError(f"{pin} has no DAC")
        low, high = self.voltage_range(pin)
        bits = int(self.device.profile.get("dac_bits", 12))
        volt = min(max(float(volt), low), high)
        volt = low + round((volt - low) / (high - low) * ((1 << bits) - 1)) * (high - low) / ((1 << bits) - 1)
        source = self._sources.get(pin)
        if source is None:
            source = DacSource()
            self._sources[pin] = source
            self.device.circuit.drive(pin, source)
        source.set(self._now(), volt)
        self.device.outputs_changed()
        self.device.log(f"{pin} = {volt:.4g} V")
        return volt

    def release(self, at: Optional[float] = None) -> None:
        """Every analog output to 0 V (*Safe*, a restart)."""
        at = self._now() if at is None else at
        for source in self._sources.values():
            source.set(at, 0.0)
        if self._sources:
            self.device.outputs_changed()


class SimulatedMonitor(_DeviceFacet, MonitorFacet):
    """Reports of the inputs: :meth:`sample` at once, or periodically while :meth:`start` runs."""

    def __init__(self, device) -> None:
        MonitorFacet.__init__(self)
        _DeviceFacet.__init__(self, device)
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.rate = 10.0
        self.watched: list[str] = []
        self.analog: tuple[str, ...] = ()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def sample(self, pins: Optional[list[str]] = None, analog: Optional[tuple[str, ...]] = None) -> MonitorState:
        self._check()
        pins = self.watched if pins is None else pins
        analog = self.analog if analog is None else analog
        now = self._now()
        if self.device.gpio is not None:
            self.device.gpio.check_watchdog()
        digital = {pin: int(self.device.circuit.digital(pin, now, 1e6, 1)[0]) for pin in pins}
        values = {}
        names = self.device.analog_channel_names()
        scale, offset, _bits = self.device.analog_scale()
        for pin in analog:
            if pin in names:
                raw = self.device.sample_analog(names.index(pin), self.device.device_time(now), 1e6, 1)[0]
                values[pin] = float(raw) * scale + offset
        return MonitorState(now, digital, values)

    def start(self, rate: float, pins: list[str], analog: tuple[str, ...] = ()) -> None:
        self._check()
        maximum = float(self.device.profile.get("monitor_rate", 1000))
        self.rate = min(max(float(rate), 0.1), maximum)
        self.watched = list(pins)
        self.analog = tuple(analog)
        self.stop()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="openscilab-sim-monitor", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        period = 1.0 / self.rate
        next_time = time.monotonic()
        while not self._stop.is_set():
            try:
                self._emit(self.sample())
            except InstrumentError:
                pass
            next_time += period
            self._stop.wait(max(next_time - time.monotonic(), 0.0))

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(2)
        self._thread = None

    def close(self) -> None:
        self.stop()


def outputs_of(profile: dict) -> list[OutputInfo]:
    """The generator outputs of a profile (``"outputs"``)."""
    result = []
    for item in profile.get("outputs", []):
        low, high = item.get("voltage_range", [0.0, float(profile.get("logic_level", 3.3))])
        result.append(OutputInfo(
            name=str(item["name"]), kind=str(item.get("kind", "analog")), voltage_range=(float(low), float(high)),
            resolution=int(item.get("resolution", 1)), max_rate=float(item.get("max_rate", 1e6)),
            max_points=int(item.get("max_points", 4096)), max_frequency=float(item.get("max_frequency", 1e6)),
            capabilities=frozenset(item.get("capabilities", [])), pins=tuple(item.get("pins", []))))
    return result


class SimulatedGenerator(_DeviceFacet, GeneratorFacet):
    """Generator outputs as sources of the circuit: an analog output drives its net (``"net"`` of the
    output in the profile, e.g. CH1), a pattern output its pins; the previous sources come back on
    :meth:`stop`. Waveforms beyond the output's limits are refused."""

    def __init__(self, device) -> None:
        GeneratorFacet.__init__(self)
        _DeviceFacet.__init__(self, device)
        #: per playing output: its waveform, its sources by net, its start
        self._running: dict[str, tuple[object, dict[str, Source], float]] = {}
        #: device time when the last transmission (:meth:`transmit`) is through: it runs in the
        #: background, where a real device answers only then
        self.transmit_end: Optional[float] = None
        self._config = {str(item["name"]): item for item in device.profile.get("outputs", [])}

    @staticmethod
    def _below(source: Optional[Source], now: float) -> Optional[Source]:
        """``source`` without the generators that stopped long ago (a capture never looks back that far)."""
        while isinstance(source, (WaveSource, SyncSource)) and source.ended_before(now - KEEP_PAST):
            source = getattr(source, "before", None)
        return source

    def outputs(self) -> list[OutputInfo]:
        return outputs_of(self.device.profile)

    def start(self, output: str, waveform) -> float:
        """Plays ``waveform``; returns the device time it starts at."""
        from ...core import waveform as waves

        self._check()
        info = self.output(output)
        found = waves.problems(waveform, info)
        if found:
            raise InstrumentError("; ".join(found))
        self.stop(output, log=False)
        config = self._config[output]
        circuit = self.device.circuit
        start = self._now()
        driven: dict[str, Source] = {}
        level = float(self.device.profile.get("logic_level", 3.3))
        if waveform.is_pattern:
            pins = {pin: pin for pin in waveform.tracks}
        else:
            pins = {str(config.get("net", output)): None}
        for net, track in pins.items():
            # what drove the net before: what it carries again outside the time the output plays
            driven[net] = WaveSource(waveform, start, pin=track, high=level,
                                     before=self._below(circuit.sources.get(net), start))
            circuit.drive(net, driven[net])
        sync = config.get("sync")
        if sync:
            driven[sync] = SyncSource(start, waveform.period, high=level)
            circuit.drive(sync, driven[sync])
        self._running[output] = (waveform, driven, start)
        self.device.outputs_changed()
        self.device.log(f"{output}: {waveform.describe()}")
        return start

    def rebase(self, table: dict) -> None:
        """The signals of the device changed while a generator runs: what it gives back when it
        stops is what ``table`` (the new sources by net) has there now."""
        for _waveform, driven, _start in self._running.values():
            for net, source in driven.items():
                below = table.get(net)
                if isinstance(source, WaveSource) and not isinstance(below, (WaveSource, SyncSource)):
                    source.before = below

    def stop(self, output: str, log: bool = True, at: Optional[float] = None) -> None:
        running = self._running.pop(output, None)
        if running is None:
            return
        _waveform, driven, _start = running
        now = self._now() if at is None else at
        for source in driven.values():
            # it stays in the circuit with its end: a capture across the stop sees both parts
            source.end = max(now, source.start)
        self.device.outputs_changed()
        if log:
            self.device.log(f"{output}: stopped")

    def stop_all(self, at: Optional[float] = None) -> None:
        """Every output stops (*Safe*, a restart, the watchdog)."""
        for output in list(self._running):
            self.stop(output, at=at)

    def running(self, output: str) -> bool:
        return output in self._running

    def started_at(self, output: str) -> Optional[float]:
        running = self._running.get(output)
        return None if running is None else running[2]

    def sync_out(self) -> Optional[str]:
        for item in self._config.values():
            if item.get("sync"):
                return "SYNC"
        return None

    def sync_net(self) -> Optional[str]:
        for item in self._config.values():
            if item.get("sync"):
                return str(item["sync"])
        return None

    # -------------------------------------------------------- sending protocols
    def transmits(self, protocol: str) -> bool:
        return f"TX_{protocol.upper()}" in self.device.profile.get("capabilities", [])

    def transmit(self, protocol: str, data: bytes, pins: dict[str, str], **settings) -> bytes:
        """The protocol on the pins (as the firmware's hardware UART, SPI or I²C would drive them):
        SPI reads ``miso`` from the circuit, I²C answers from the devices of the profile
        (``i2c_devices``: address → bytes of the answer)."""
        from ...core import waveform as waves

        self._check()
        protocol = protocol.lower()
        if not self.transmits(protocol):
            raise InstrumentError(f"{self.device.profile.get('title', 'the device')} does not send "
                                  f"{protocol.upper()} by itself")
        data = bytes(data)
        try:
            if protocol == "uart":
                baud = float(settings.get("baud", 115200))
                rate = baud * 16
                tracks = waves.uart_tracks(data, baud, rate, pin=self._tx_pin(pins, "tx"))
            elif protocol == "spi":
                frequency = float(settings.get("frequency", 1e6))
                rate = frequency * 8
                names = (self._tx_pin(pins, "cs") if pins.get("cs") else "_cs", self._tx_pin(pins, "sck"),
                         self._tx_pin(pins, "mosi"))
                tracks = waves.spi_tracks(data, frequency, rate, pins=names)
                tracks.pop("_cs", None)
            elif protocol == "i2c":
                frequency = float(settings.get("frequency", 100e3))
                rate = frequency * 8
                address = int(settings.get("address", 0))
                names = (self._tx_pin(pins, "scl"), self._tx_pin(pins, "sda"))
                parts = [waves.i2c_tracks(address, data, frequency, rate, pins=names)] if data else []
                if int(settings.get("read", 0)) or not data:
                    parts.append(waves.i2c_tracks(address, b"", frequency, rate, read=True, pins=names))
                tracks = waves.combine(*parts)
            else:
                raise InstrumentError(f"unknown protocol {protocol!r} (uart, spi, i2c)")
        except waves.WaveformError as error:
            raise InstrumentError(str(error)) from None

        received = b""
        start = self._now()
        if protocol == "spi" and pins.get("miso"):
            sck = tracks[names[1]]
            rising = np.flatnonzero((sck[1:] == 1) & (sck[:-1] == 0)) + 1
            levels = np.array([self.device.circuit.digital(pins["miso"], start + index / rate, 1e9, 1)[0]
                               for index in rising], dtype=np.uint8)
            whole = len(levels) // 8 * 8
            received = np.packbits(levels[:whole]).tobytes()
        elif protocol == "i2c":
            devices = {int(str(key), 0): value for key, value in (self.device.profile.get("i2c_devices") or {}).items()}
            answer = devices.get(address)
            if answer is None:
                raise InstrumentError(f"no device acknowledges at I²C address 0x{address:02X}")
            count = int(settings.get("read", 0))
            answer = bytes.fromhex(answer) if isinstance(answer, str) else bytes(answer)
            received = bytes(answer[index % len(answer)] for index in range(count)) if answer else bytes(count)

        waveform = waves.pattern(tracks, rate, repeat=1)
        circuit = self.device.circuit
        level = float(self.device.profile.get("logic_level", 3.3))
        for net in tracks:
            before = circuit.sources.get(net)
            if isinstance(before, WaveSource) and before.waveform.repeat > 0 and \
                    before.start + before.waveform.duration <= start and getattr(before, "transmission", False):
                before = before.before  # an earlier transmission that ended: not kept underneath
            source = WaveSource(waveform, start, pin=net, high=level, before=before)
            source.transmission = True
            circuit.drive(net, source)
        self.transmit_end = start + waveform.duration
        self.device.outputs_changed()
        self.device.log(f"{protocol.upper()}: sent {len(data)} byte(s)" +
                        (f", received {len(received)}" if received else ""))
        return received

    def _tx_pin(self, pins: dict[str, str], role: str) -> str:
        name = pins.get(role)
        if not name:
            raise InstrumentError(f"no pin for {role.upper()}")
        gpio = self.device.gpio
        if gpio is not None:
            gpio._pin(str(name), PIN_DOUT)
        return str(name)
