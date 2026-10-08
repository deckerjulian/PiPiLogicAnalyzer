# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Instruments and their facets.

An :class:`Instrument` is a device of the lab (a Pico, a DSLogic, a simulated Arduino, an
oscilloscope). What it can do is a set of *facets*; the application asks for a facet, never for
the kind of device:

=================== =================================================== ==========================
Facet               What it does                                        Capabilities (docs/protocols.md)
=================== =================================================== ==========================
``CaptureFacet``    captures (buffer, stream, state) through a driver   –
``GpioFacet``       pin modes, read/write, pulses, PWM                  ``GPIO``, ``PWM``
``MonitorFacet``    periodic reports of the inputs (and analog values)  ``MONITOR``
``AnalogInFacet``   reads voltages                                      ``ANALOG=<n>``
``AnalogOutFacet``  sets voltages (DAC)                                 ``DAC``
``GeneratorFacet``  outputs waveforms and patterns                      ``PATTERN_GEN``, ``GEN_*``
=================== =================================================== ==========================

The existing drivers (:class:`~openscilab.driver.base.AnalyzerDriverBase`) become instruments
with a :class:`CaptureFacet` and no other change (:meth:`Instrument.from_driver`).

The module has no Qt dependency.
"""

from __future__ import annotations

import logging
import math
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable, Optional, TypeVar

log = logging.getLogger(__name__)

if TYPE_CHECKING:  # pragma: no cover
    from ..driver.base import AnalyzerDriverBase

# ----------------------------------------------------------------- pin roles
#: Digital input
PIN_DIN = "DIN"
#: Digital output
PIN_DOUT = "DOUT"
PIN_PULLUP = "PULLUP"
PIN_PULLDOWN = "PULLDOWN"
#: Hardware PWM output
PIN_PWM = "PWM"
#: Analog input (ADC)
PIN_ADC = "ADC"
#: Analog output (DAC)
PIN_DAC = "DAC"
#: Can clock a state capture in hardware
PIN_CLOCK = "CLOCK"
#: Can clock a state capture in software (interrupt), slower
PIN_CLOCK_SW = "CLOCK_SW"
PIN_CAPABILITIES = (PIN_DIN, PIN_DOUT, PIN_PULLUP, PIN_PULLDOWN, PIN_PWM, PIN_ADC, PIN_DAC,
                    PIN_CLOCK, PIN_CLOCK_SW)

#: Pin modes of :meth:`GpioFacet.set_mode`
MODE_INPUT = "input"
MODE_INPUT_PULLUP = "input_pullup"
MODE_INPUT_PULLDOWN = "input_pulldown"
MODE_OUTPUT = "output"
MODE_PWM = "pwm"
MODE_ANALOG = "analog"
PIN_MODES = (MODE_INPUT, MODE_INPUT_PULLUP, MODE_INPUT_PULLDOWN, MODE_OUTPUT, MODE_PWM, MODE_ANALOG)


class InstrumentError(Exception):
    """An instrument cannot do what was asked (pin reserved, facet missing, not connected)."""


@dataclass(frozen=True)
class PinInfo:
    """One pin of an instrument, as its device describes it."""

    name: str
    capabilities: frozenset[str] = frozenset()
    #: Capture channel the pin is wired to (logic analyzer channel), ``None`` for none.
    channel: Optional[int] = None
    #: Why the pin cannot be used (``"USB"``, ``"LA channel 3 while capturing"``), ``None`` if free.
    reserved: Optional[str] = None
    #: Logic level in volts (3.3, 5.0).
    logic_level: float = 3.3
    #: Analog input number, for pins with ``ADC``.
    analog_channel: Optional[int] = None
    description: str = ""

    def can(self, capability: str) -> bool:
        return capability in self.capabilities

    @property
    def usable(self) -> bool:
        return self.reserved is None


def parse_pin_line(line: str) -> Optional[PinInfo]:
    """A pin of the wire format of ``docs/protocols.md`` (*Pins*), ``None`` for another line::

        PIN:<name>,<capabilities separated by />,<channel or ->,<logic level mV>,<analog channel or ->[,<reserved>]
    """
    if not line.startswith("PIN:"):
        return None
    parts = line[len("PIN:"):].split(",", 5)
    if len(parts) < 5 or not parts[0].strip():
        return None

    def number(text: str) -> Optional[int]:
        text = text.strip()
        return None if text in ("", "-") else int(text)

    try:
        channel, level, analog = number(parts[2]), number(parts[3]), number(parts[4])
    except ValueError:
        return None
    reserved = parts[5].strip() if len(parts) > 5 else ""
    if reserved.startswith("reserved:"):
        reserved = reserved[len("reserved:"):].strip()
    return PinInfo(
        parts[0].strip(),
        frozenset(item.strip() for item in parts[1].split("/") if item.strip()),
        channel=channel,
        reserved=reserved or None,
        logic_level=(level or 3300) / 1000.0,
        analog_channel=analog,
    )


class InstrumentStatus(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTED = "connected"
    SIMULATED = "simulated"
    BUSY = "busy"
    ERROR = "error"

    @property
    def token(self) -> str:
        """Colour token of the status (``theme.TOKENS``)."""
        return {
            InstrumentStatus.DISCONNECTED: "device.disconnected",
            InstrumentStatus.CONNECTED: "device.connected",
            InstrumentStatus.SIMULATED: "device.simulated",
            InstrumentStatus.BUSY: "device.busy",
            InstrumentStatus.ERROR: "device.error",
        }[self]


# -------------------------------------------------------------------- facets
class Facet:
    """Base of the facets. ``instrument`` is set when the facet is added to an instrument."""

    #: Name shown in the device card
    title = "Facet"

    def __init__(self) -> None:
        self.instrument: Optional["Instrument"] = None

    def close(self) -> None:
        """Release what the facet holds (called by :meth:`Instrument.close`)."""


class CaptureFacet(Facet):
    """Captures through one of the existing drivers (buffer, stream, state, multi device sets)."""

    title = "Capture"

    def __init__(self, driver: "AnalyzerDriverBase") -> None:
        super().__init__()
        #: The driver, wrapped for software triggers when the device streams.
        self.driver = driver

    @property
    def channel_count(self) -> int:
        return self.driver.channel_count

    def close(self) -> None:
        self.driver.dispose()


class GpioFacet(Facet):
    """Pins as inputs and outputs: modes, levels, pulses, PWM.

    Implementations check :attr:`PinInfo.reserved` and raise :class:`InstrumentError` for pins
    that cannot be used.

    Devices with a watchdog release their outputs when the host has not been in contact for a
    while (the host crashed or the cable came off). While the facet is open the *driver* keeps
    that contact (:class:`KeepAlive`), whoever drives the outputs – the device card, a flow, a
    script: an output stays as it was set until it is changed, :meth:`safe_all` is called or the
    connection is lost. :attr:`keepalive` ``False`` stops that (tests of the watchdog).
    """

    title = "GPIO"
    #: the driver keeps the device's watchdog satisfied while it is connected
    keepalive = True

    def heartbeat(self) -> None:
        """Tell the device the host is there (drivers call it themselves, see :class:`KeepAlive`)."""

    def pins(self) -> list[PinInfo]:
        raise NotImplementedError

    def pin(self, name: str) -> PinInfo:
        for info in self.pins():
            if info.name == name:
                return info
        raise InstrumentError(f"no pin {name!r}")

    def set_mode(self, pin: str, mode: str) -> None:
        """``mode``: one of :data:`PIN_MODES`."""
        raise NotImplementedError

    def mode(self, pin: str) -> str:
        raise NotImplementedError

    def write(self, pin: str, value: int) -> None:
        """Drive an output pin to 0 or 1."""
        raise NotImplementedError

    def write_many(self, values: dict[str, int]) -> None:
        """Several outputs at once (at the same time where the device can)."""
        for pin, value in values.items():
            self.write(pin, value)

    def read(self, pin: str) -> int:
        raise NotImplementedError

    def read_many(self, pins: list[str]) -> dict[str, int]:
        return {pin: self.read(pin) for pin in pins}

    def pulse(self, pin: str, width: float, level: int = 1, count: int = 1, period: float = 0.0) -> None:
        """``count`` pulses of ``width`` seconds at ``level``, one every ``period`` seconds (exact in
        the device where it can). Returns at once; the device times the pulses."""
        raise NotImplementedError

    def pwm(self, pin: str, frequency: float, duty: float) -> Optional[float]:
        """PWM of ``frequency`` Hz and ``duty`` 0..1 (0 stops it); returns the frequency the device
        makes (its dividers), ``None`` when it does not tell."""
        raise NotImplementedError

    def safe_all(self) -> None:
        """Every output back to a safe input (*all outputs safe*, Esc)."""
        raise NotImplementedError


class KeepAlive:
    """Calls ``beat`` every ``interval`` seconds in a thread of its own until :meth:`stop` –
    the heartbeat a driver sends while outputs of its device are driven (see :class:`GpioFacet`).
    ``beat`` may raise: the device is gone then, the next beat tries again."""

    def __init__(self, beat: Callable[[], None], interval: float = 0.3) -> None:
        self.beat = beat
        self.interval = float(interval)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="openscilab-keepalive", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.beat()
            except Exception:  # noqa: BLE001 - a device that does not answer: the next beat tries again
                log.debug("Heartbeat failed", exc_info=True)

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(2)


@dataclass
class MonitorState:
    """One report of a monitor: levels and voltages at ``time`` (seconds of the instrument)."""

    time: float
    digital: dict[str, int] = field(default_factory=dict)
    analog: dict[str, float] = field(default_factory=dict)


MonitorHandler = Callable[[MonitorState], None]


class MonitorFacet(Facet):
    """Periodic reports of the inputs: a slow capture that runs alongside everything else."""

    title = "Monitor"

    def __init__(self) -> None:
        super().__init__()
        self._handlers: list[MonitorHandler] = []
        self._lock = threading.Lock()

    def start(self, rate: float, pins: list[str], analog: tuple[str, ...] = ()) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    @property
    def running(self) -> bool:
        return False

    def on_state(self, handler: MonitorHandler) -> Callable[[], None]:
        """Call ``handler`` for every report; returns a function that removes it."""
        with self._lock:
            self._handlers.append(handler)

        def remove() -> None:
            with self._lock:
                if handler in self._handlers:
                    self._handlers.remove(handler)

        return remove

    def _emit(self, state: MonitorState) -> None:
        with self._lock:
            handlers = list(self._handlers)
        for handler in handlers:
            handler(state)


class AnalogInFacet(Facet):
    title = "Analog inputs"

    def read(self, pins: list[str]) -> dict[str, float]:
        """Voltages of ``pins`` (volts)."""
        raise NotImplementedError


class AnalogOutFacet(Facet):
    title = "Analog outputs"

    def set_voltage(self, pin: str, volt: float) -> None:
        raise NotImplementedError

    def voltage_range(self, pin: str) -> tuple[float, float]:
        raise NotImplementedError


@dataclass(frozen=True)
class OutputInfo:
    """A generator output of an instrument."""

    name: str
    #: "analog" (AFG, DAC), "pattern" (digital pins) or "square" (PWM/timer)
    kind: str
    #: volts for analog outputs
    voltage_range: tuple[float, float] = (0.0, 3.3)
    #: bits of the DAC / number of pins of a pattern output
    resolution: int = 1
    max_rate: float = 1e6
    max_points: int = 4096
    max_frequency: float = 1e6
    #: ``SWEEP``, ``BURST``, ``ARB``, ``SYNC_OUT``
    capabilities: frozenset[str] = frozenset()
    pins: tuple[str, ...] = ()


class GeneratorFacet(Facet):
    title = "Generator"

    def outputs(self) -> list[OutputInfo]:
        raise NotImplementedError

    def output(self, name: str) -> OutputInfo:
        for info in self.outputs():
            if info.name == name:
                return info
        raise InstrumentError(f"no generator output {name!r}")

    def start(self, output: str, waveform: Any) -> None:
        raise NotImplementedError

    def stop(self, output: str) -> None:
        raise NotImplementedError

    def running(self, output: str) -> bool:
        return False

    def sync_out(self) -> Optional[str]:
        """The pin or connector that marks the start of the waveform, ``None`` without one."""
        return None

    # -------------------------------------------------------- sending protocols
    def transmits(self, protocol: str) -> bool:
        """Whether the device sends ``protocol`` (``uart``, ``spi``, ``i2c``) itself, with its
        own hardware (capabilities ``TX_UART``, ``TX_SPI``, ``TX_I2C``). Without it, protocols are
        sent as a pattern on a pattern output (``core.waveform.uart_tracks`` …)."""
        return False

    def transmit(self, protocol: str, data: bytes, pins: dict[str, str], **settings: Any) -> bytes:
        """Send ``data`` as ``protocol`` and return what came back.

        * ``uart`` – pins ``tx``; settings ``baud`` (default 115200). Returns ``b""``.
        * ``spi`` – pins ``sck``, ``mosi``, optional ``miso`` and ``cs``; settings ``frequency``
          (default 1 MHz), ``mode`` (0–3, default 0). Mode 0, MSB first; returns the bytes read
          on ``miso`` while sending (``b""`` without it).
        * ``i2c`` – pins ``sda``, ``scl``; settings ``address`` (7 bit), ``frequency`` (default
          100 kHz), ``read`` (bytes to read after writing ``data``, with a repeated start).
          Returns the bytes read; raises :class:`InstrumentError` when nothing acknowledges.
        """
        raise InstrumentError(f"this device does not send {protocol.upper()} by itself")


@dataclass(frozen=True)
class CacheEntry:
    """A capture kept in the device's own cache (the bridge app of an oscilloscope)."""

    id: str
    #: unix time of the capture
    time: float
    points: int


class CacheFacet(Facet):
    """Captures the device keeps in a cache of its own: list them, delete them, limit the cache."""

    title = "Cache"

    def entries(self) -> list[CacheEntry]:
        raise NotImplementedError

    def delete(self, entry_id: Optional[str] = None) -> None:
        """Delete one entry, or all of them (``None``)."""
        raise NotImplementedError

    def set_limit(self, size: int) -> None:
        """The cache keeps at most ``size`` bytes (the oldest entries go first)."""
        raise NotImplementedError


class SimulationFacet(Facet):
    """What a simulator simulates and how it is wired - the same whether it runs in the application
    or in a device process (``driver/simulated``; the *Signals* tab of its device card)."""

    title = "Simulation"

    def channel_names(self) -> list[str]:
        """The nets of its digital channels (``D0``, ``GP2``, ``P0.0``)."""
        raise NotImplementedError

    def analog_channel_names(self) -> list[str]:
        raise NotImplementedError

    def pin_names(self) -> list[str]:
        """Every pin a wire can start at (outputs, PWM, the channels)."""
        raise NotImplementedError

    def scenarios(self) -> list[tuple[str, str]]:
        """``(scenario key, why it cannot be used - "" when it can)`` of every scenario."""
        raise NotImplementedError

    def signals(self) -> dict:
        """What it simulates now (scenario, its parameters, the signals of single channels)."""
        raise NotImplementedError

    def apply_signals(self, config: dict) -> tuple[dict, dict]:
        """Simulate ``config`` at once; returns it as applied and the names it suggests for the
        channels (``{channel index: name}``). Raises ``ValueError`` when it does not fit."""
        raise NotImplementedError

    def circuit(self) -> dict[str, str]:
        """What each net carries, for people (``square 1 kHz, 50 %``, ``wired to P0.0``)."""
        raise NotImplementedError

    def wires(self) -> list[tuple[str, str]]:
        """``(from, to)`` of every wire of its circuit now (of its profile and added ones)."""
        raise NotImplementedError

    def wiring(self) -> list[dict]:
        """The wires added to its profile's (``[{from: GP16, to: GP17}]``)."""
        raise NotImplementedError

    def profile_wiring(self) -> list[dict]:
        """The wires its profile brings."""
        raise NotImplementedError

    def set_wiring(self, wiring: list[dict]) -> None:
        """Replace the added wires; ``ValueError`` (nothing changed) for one that cannot be."""
        raise NotImplementedError

    def usb(self) -> Optional[dict]:
        """The USB link it emulates (``frame``, ``latency``, ``jitter`` in seconds), ``None``: none."""
        raise NotImplementedError

    def set_usb(self, usb: Optional[dict]) -> None:
        raise NotImplementedError

    def knows_time(self) -> bool:
        """Whether it knows the time of its samples (it emulates no USB link)."""
        raise NotImplementedError

    def drift(self) -> float:
        """How much faster its sample clock runs than the computer's (a share: 30e-6)."""
        raise NotImplementedError

    def set_drift(self, ppm: float) -> None:
        raise NotImplementedError

    def inject(self, fault: str, value: float = 0.0) -> None:
        """A fault: ``disconnect``, ``reconnect``, ``delay`` (seconds), ``overflow``, ``restart``."""
        raise NotImplementedError

    def add_event_listener(self, listener: Callable[[float, str], None]) -> None:
        """``listener(time, text)`` for each thing it does (its log)."""
        raise NotImplementedError

    def remove_event_listener(self, listener: Callable[[float, str], None]) -> None:
        raise NotImplementedError

    # ------------------------------------------- wires to other simulators
    def endpoint(self) -> Any:
        """Where another process reads its circuit (``driver/simulated/nets.Endpoint``: address,
        key, the origin of its clock, its logic level)."""
        raise NotImplementedError

    def follow(self, net: str, endpoint: Any, source_net: str, delay: float = 0.0, label: str = "") -> None:
        """``net`` follows ``source_net`` of the simulator at ``endpoint`` (a wire between two
        simulators, ``delay`` seconds of cable), until :meth:`unfollow`."""
        raise NotImplementedError

    def unfollow(self, net: str) -> None:
        """``net`` is driven again by what drove it before :meth:`follow`."""
        raise NotImplementedError

    def trigger_output_net(self, output: str) -> Optional[str]:
        """The net of the trigger output ``output`` (``SYNC``, ``TRIG OUT``), ``None`` without one."""
        raise NotImplementedError

    def trigger_input_net(self, name: str) -> Optional[str]:
        """The net of the trigger input ``name``, ``None`` without one."""
        raise NotImplementedError


F = TypeVar("F", bound=Facet)


# ----------------------------------------------------------------- instrument
def timing_rows(state) -> list[tuple[str, str]]:
    """How well the time of the last capture or stream of an instrument is known (device card)."""
    from . import units

    def seconds(value: float) -> str:
        return units.format_quantity(value, "s", 3) if math.isfinite(value) else "unknown (an upper bound)"

    rows = [("Method", state.text), ("Accuracy", f"± {seconds(state.uncertainty)}")]
    if state.reference:
        rows.append(("Reference", state.reference))
    if state.latency is not None:
        rows.append(("Latency", f"{seconds(state.latency.value)} ± {seconds(state.latency.uncertainty)} "
                                f"({state.latency.source})"))
    if state.jitter:
        rows.append(("Jitter of the arrivals", seconds(state.jitter)))
    if state.drift:
        rows.append(("Drift of the sample clock", f"{state.drift * 1e6:+.1f} ppm"))
    return rows


class Instrument:
    """A device of the lab with its facets."""

    def __init__(self, name: str, kind: str = "", uri: str = "",
                 status: InstrumentStatus = InstrumentStatus.CONNECTED,
                 trigger_outputs: tuple[str, ...] = (), trigger_inputs: tuple[str, ...] = ()) -> None:
        self.name = name
        #: kind of device for people ("Pico", "DSLogic", "Simulation: Arduino Uno")
        self.kind = kind
        #: how it was opened (``pico:/dev/cu.usbmodem1``, ``sim:uno``)
        self.uri = uri
        self.status = status
        #: connectors that emit a trigger / take one (for trigger routes of the hub)
        self.trigger_outputs = tuple(trigger_outputs)
        self.trigger_inputs = tuple(trigger_inputs)
        #: The data view its captures go to (one at a time).
        self.owner: object = None
        #: when its samples were taken (:class:`~openscilab.core.timing.InstrumentTiming`, made by the
        #: first capture or stream of a flow)
        self.timing = None
        self._facets: dict[type, Facet] = {}

    def __repr__(self) -> str:
        return f"<Instrument {self.name} ({self.kind}) {self.status.value}>"

    # -------------------------------------------------------------- facets
    def add_facet(self, facet: Facet) -> Facet:
        facet.instrument = self
        for cls in type(facet).__mro__:
            if cls in (Facet, object):
                break
            self._facets.setdefault(cls, facet)
        return facet

    def facet(self, cls: type[F]) -> Optional[F]:
        """The facet of class ``cls`` (or a subclass), ``None`` when the instrument has none."""
        return self._facets.get(cls)  # type: ignore[return-value]

    def has(self, cls: type[Facet]) -> bool:
        return cls in self._facets

    def require(self, cls: type[F]) -> F:
        found = self.facet(cls)
        if found is None:
            raise InstrumentError(f"{self.name} has no {cls.title.lower()} ({cls.__name__})")
        return found

    def facets(self) -> list[Facet]:
        unique: list[Facet] = []
        for facet in self._facets.values():
            if facet not in unique:
                unique.append(facet)
        return unique

    @property
    def capture(self) -> Optional[CaptureFacet]:
        return self.facet(CaptureFacet)

    @property
    def gpio(self) -> Optional[GpioFacet]:
        return self.facet(GpioFacet)

    @property
    def monitor(self) -> Optional[MonitorFacet]:
        return self.facet(MonitorFacet)

    @property
    def generator(self) -> Optional[GeneratorFacet]:
        return self.facet(GeneratorFacet)

    @property
    def simulation(self) -> Optional[SimulationFacet]:
        return self.facet(SimulationFacet)

    # -------------------------------------------------------- description
    def capabilities(self) -> frozenset[str]:
        """The capability strings of the device (``CAPABILITY_*``)."""
        capture = self.capture
        if capture is not None:
            try:
                return frozenset(capture.driver.capabilities())
            except Exception:  # noqa: BLE001 - a device that cannot answer has none
                log.debug("%s does not tell its capabilities", self.name, exc_info=True)
                return frozenset()
        return frozenset()

    def pins(self) -> list[PinInfo]:
        """The pins: from the GPIO facet, or the capture channels of a pure analyzer."""
        gpio = self.gpio
        if gpio is not None:
            return gpio.pins()
        capture = self.capture
        if capture is None:
            return []
        try:
            count = capture.channel_count
        except Exception:  # noqa: BLE001
            log.debug("%s does not tell its channels", self.name, exc_info=True)
            return []
        return [PinInfo(f"CH{index + 1}", frozenset({PIN_DIN}), channel=index) for index in range(count)]

    def details(self) -> list[tuple[str, list[tuple[str, str]]]]:
        """Sections of ``(property, value)`` rows for the device card."""
        rows = [("Name", self.name), ("Kind", self.kind or "-"), ("Address", self.uri or "-"),
                ("Status", self.status.value)]
        sections = [("Instrument", rows)]
        capture = self.capture
        if capture is not None:
            try:
                sections += list(capture.driver.describe())
            except Exception as error:  # noqa: BLE001 - shown instead of the details
                sections.append(("Capture", [("Error", str(error))]))
        timing = self.timing.state() if self.timing is not None else None
        if timing is not None:
            sections.append(("Timing", timing_rows(timing)))
        return sections

    @property
    def is_simulated(self) -> bool:
        """A simulator (``sim:...``) or a simulated device that speaks a real protocol
        (``arduino-sim:...``, ``rigol-sim:...``, ``remote-sim:...``)."""
        kind = self.uri.partition(":")[0]
        return self.status == InstrumentStatus.SIMULATED or kind == "sim" or kind.endswith("-sim")

    def close(self) -> None:
        for facet in self.facets():
            try:
                facet.close()
            except Exception:  # noqa: BLE001 - close every facet even if one fails
                log.exception("Closing %s of %s failed", type(facet).__name__, self.name)
        self.status = InstrumentStatus.DISCONNECTED

    # -------------------------------------------------------------- drivers
    @staticmethod
    def from_driver(driver: "AnalyzerDriverBase", name: Optional[str] = None, uri: str = "",
                    wrap_software_trigger: bool = True) -> "Instrument":
        """An instrument with a :class:`CaptureFacet` for one of the existing drivers.

        Devices that stream get the software trigger wrapper the analyzer has always used.
        """
        from ..driver.base import CAPABILITY_EDGE_TRIGGER_OUT, AnalyzerDriverType
        from ..driver.software_trigger import SoftwareTriggerDriver, supports_software_trigger

        if wrap_software_trigger and not isinstance(driver, SoftwareTriggerDriver) and supports_software_trigger(driver) \
                and not getattr(driver, "software_trigger_inside", False):
            driver = SoftwareTriggerDriver(driver)
        try:
            driver_type = driver.driver_type
        except Exception:  # noqa: BLE001
            log.debug("driver_type = driver.driver_type failed (ignored)", exc_info=True)
            driver_type = AnalyzerDriverType.OTHER
        kind = {
            AnalyzerDriverType.SERIAL: "Pico",
            AnalyzerDriverType.NETWORK: "Pico (network)",
            AnalyzerDriverType.MULTI: "Pico multi device set",
            AnalyzerDriverType.EMULATED: "File",
            AnalyzerDriverType.DSLOGIC: "DSLogic",
        }.get(driver_type, getattr(driver, "driver_id", "") or "Device")
        try:
            capabilities = driver.capabilities()
        except Exception:  # noqa: BLE001
            log.debug("capabilities = driver.capabilities() failed (ignored)", exc_info=True)
            capabilities = frozenset()
        is_hardware = getattr(driver, "is_hardware", True)
        instrument = Instrument(
            name or (driver.device_version or kind),
            kind=kind,
            uri=uri,
            status=InstrumentStatus.CONNECTED if is_hardware else InstrumentStatus.SIMULATED,
            trigger_outputs=("TRIG OUT",) if CAPABILITY_EDGE_TRIGGER_OUT in capabilities else (),
            trigger_inputs=("TRIG IN",) if _has_external_trigger(driver) else (),
        )
        instrument.add_facet(CaptureFacet(driver))
        # The facets beyond the captures (GPIO, monitor, analog inputs, generator) come from the
        # driver, after what the device reports it can do: one place for the device list, flows,
        # the command line and scripts, which all open devices through here.
        extra = getattr(driver, "instrument_facets", None)
        if callable(extra):
            for facet in extra():
                instrument.add_facet(facet)
        return instrument


def _has_external_trigger(driver) -> bool:
    try:
        return bool(driver.has_external_trigger())
    except Exception:  # noqa: BLE001 - a device that cannot answer has none
        log.debug("return bool(driver.has_external_trigger()) failed: a device that cannot answer has none", exc_info=True)
        return False
