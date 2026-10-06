# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Scripting interface: capture, load, save and decode without the user interface.

Capture eight channels at 10 MHz after a rising edge on channel 0 and save them::

    from openscilab import api

    print(api.devices())                       # [DeviceInfo(id='pico:/dev/cu.usbmodem1', ...)]
    with api.open() as device:                 # the first device found
        capture = device.capture(
            channels=range(8), rate="10M", duration="5ms", pre_trigger=1000,
            trigger=api.Edge(0, rising=True),
        )
    capture.save("capture.sr")                 # .lac, .lac.gz, .sr, .csv or .vcd

Work with the samples (``numpy`` arrays of 0 and 1, one per channel)::

    clock = capture.samples(0)                 # by channel number, or by name: samples("SCL")
    rising = ((clock[1:] == 1) & (clock[:-1] == 0)).sum()
    print(rising / capture.duration, "Hz")

Decode a protocol with the sigrok decoders (no PulseView or libsigrokdecode needed)::

    capture = api.load("i2c.sr")
    for annotation in capture.decode("i2c", channels={"scl": 0, "sda": 1}):
        print(f"{annotation.start_time * 1e6:9.2f} µs  {annotation.row:12} {annotation.value}")

Decoders that work on the output of another decoder get it stacked below them automatically
(``capture.decode("eeprom24xx", channels={"scl": 0, "sda": 1})`` runs ``i2c`` first).

Run flows (``*.flow.yaml`` or DSL scripts; the lab itself is :mod:`openscilab.lab`)::

    result = api.run_flow("examples/flows/counter.flow.yaml", sim=True, fast=True)
    print(result.state, result.time)

Simulated instruments are devices like any other: ``api.open("sim:free")``.
"""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping, Optional, Union
from typing import Sequence as SequenceType

import numpy as np

from .core import capture_io, sigrok_session
from .core.annotation_export import AnnotationRecord, annotation_records, export_annotations
from .core.regions import SampleRegion
from .driver.base import (
    ACQUISITION_BUFFER,
    ACQUISITION_STREAM,
    CAPABILITY_IMMEDIATE_TRIGGER,
    CAPABILITY_STREAM_IMMEDIATE_ONLY,
    CAPABILITY_THRESHOLD,
    CAPABILITY_TRIGGER_CONDITIONS,
    CAPABILITY_TRIGGER_SEQUENCE,
    AnalyzerDriverBase,
    CaptureCompletedArgs,
    CaptureError,
    DeviceSection,
    pattern_fits,
    pattern_max_bits,
)
from .driver.discovery import DeviceInfo, list_devices, open_device
from .driver.models import (
    AnalyzerChannel,
    CaptureSession,
    TriggerSequence,
    TriggerStage,
    TriggerType,
)
from .sigrok.engine import DecoderInfo, DecoderRegistry, OptionType
from .sigrok.provider import AnnotationGroup, DecoderInstance, SigrokProvider

if TYPE_CHECKING:  # pragma: no cover - at run time ``lab`` is loaded on demand (see __getattr__)
    from . import lab


def __getattr__(name: str):
    # ``api.lab``: the lab as part of the scripting API, imported when it is first used (a script
    # that only captures and decodes does not pay for the flow engine)
    if name == "lab":
        from . import lab

        return lab
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "lab",
    "run_flow",
    "AnnotationRecord",
    "Capture",
    "CaptureFailed",
    "Device",
    "DeviceInfo",
    "Edge",
    "Immediate",
    "Pattern",
    "Sequence",
    "Simulation",
    "decoders",
    "devices",
    "load",
    "open",
    "parse_duration",
    "parse_rate",
]

#: Extensions :func:`load` reads and :meth:`Capture.save` writes
READ_FORMATS = (".lac", ".lac.gz", ".sr")
WRITE_FORMATS = (".lac", ".lac.gz", ".sr", ".csv", ".vcd")


class CaptureFailed(RuntimeError):
    """The device refused the capture or reported an error."""


# --------------------------------------------------------------------- triggers
@dataclass
class Edge:
    """Trigger on an edge of ``channel`` (the device's external trigger input is
    ``channel_count``)."""

    channel: int = 0
    rising: bool = True


@dataclass
class Pattern:
    """Trigger when consecutive channels show ``levels``: ``Pattern({0: 1, 1: 0})`` triggers
    when channel 0 is high and channel 1 low. ``fast``: the fast trigger of the Pico (up to 5
    channels, reacts within one sample)."""

    levels: Mapping[int, int] = field(default_factory=dict)
    fast: bool = False


@dataclass
class Immediate:
    """Start at once, without a trigger."""


@dataclass
class Sequence:
    """Trigger sequence: stages of patterns, edges, pulse widths and gaps
    (:class:`~openscilab.driver.models.TriggerStage`), for devices that evaluate them::

        from openscilab.driver.models import ConditionKind, TriggerCondition, TriggerStage

        api.Sequence([
            TriggerStage(TriggerCondition(ConditionKind.EDGE, channel=2)),
            TriggerStage(TriggerCondition(ConditionKind.PULSE, channel=3, min_ns=500), within_ns=10_000),
        ])
    """

    stages: SequenceType[TriggerStage] = field(default_factory=list)


@dataclass
class Simulation:
    """Test signals instead of a trigger (the emulated device, boards with ``SIMULATION``)."""

    pattern: int = 0


Trigger = Union[Edge, Pattern, Immediate, Sequence, Simulation]


# ---------------------------------------------------------------------- parsing
_NUMBER_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)\s*([a-zA-Zµ]*)\s*$")
_RATE_UNITS = {"": 1, "hz": 1, "k": 1e3, "khz": 1e3, "m": 1e6, "mhz": 1e6, "g": 1e9, "ghz": 1e9}

def parse_rate(value: Union[int, float, str]) -> int:
    """Sampling rate in Hz: ``10_000_000``, ``"10M"``, ``"100k"``, ``"1e6"``, ``"24 MHz"``."""
    if isinstance(value, (int, float)):
        return int(round(value))
    match = _NUMBER_RE.match(str(value))
    unit = match.group(2).lower() if match else None
    if match is None or unit not in _RATE_UNITS:
        raise ValueError(f"Invalid sampling rate '{value}': use e.g. 10M, 100k, 1e6 or 24MHz.")
    return int(round(float(match.group(1)) * _RATE_UNITS[unit]))


def parse_duration(value: Union[int, float, str]) -> float:
    """Duration in seconds: ``0.005``, ``"5ms"``, ``"250us"``, ``"2 s"``, ``"2 min"``.

    Read like every quantity of the application (:func:`openscilab.core.units.parse`): ``m`` is
    milli, minutes are ``min``.
    """
    from .core import units

    try:
        return units.parse(value, "s")
    except units.UnitError:
        raise ValueError(f"Invalid duration '{value}': use e.g. 5ms, 250us, 2s or 2 min.") from None


# ---------------------------------------------------------------------- devices
def devices() -> list[DeviceInfo]:
    """The connected devices (Pico boards over USB, DSLogic analyzers)::

        for device in api.devices():
            print(device.id, device.label)
    """
    return list_devices()


def open(device_id: Optional[str] = None, download_bitstream: bool = False,  # noqa: A001
         process: bool = False) -> "Device":
    """Connect to a device: an identifier of :func:`devices` (``"pico:/dev/cu.usbmodem1"``,
    ``"pico-net:192.168.1.5:4045"``, ``"dslogic:1:4"``, ``"emulated"``), a serial port,
    ``host:port`` or a comma separated list of ports (multi device set). ``None``: the first
    device found.

    ``download_bitstream`` lets a DSLogic fetch its missing FPGA bitstream from the DSView
    repository. ``process``: read the device in a process of its own, as the application does (a
    script then needs the ``if __name__ == "__main__":`` guard of ``multiprocessing``). Use the
    device in a ``with`` block, or call :meth:`Device.close`::

        with api.open("pico:/dev/ttyACM0") as device:
            print(device.name, device.channel_count, device.max_frequency)
    """
    if device_id is None:
        found = list_devices()
        if not found:
            from .driver.base import DeviceConnectionError

            raise DeviceConnectionError("No device is connected.")
        device_id = found[0].id
    if process:
        from .driver.process import open_instrument as open_in_process

        return Device(open_in_process(device_id, download_bitstream=download_bitstream).capture.driver)
    return Device(open_device(device_id, download_bitstream=download_bitstream))


def instrument(address: str, seed: int = 1):
    """The instrument at ``address`` with all its facets (GPIO, monitor, generator, ...), e.g. a
    simulator ``"sim:uno"``::

        uno = api.instrument("sim:uno")
        uno.gpio.write("D7", 1)
        print(uno.monitor.sample(["D3"]).digital)
    """
    from .lab.engine.devices import open_instrument

    return open_instrument(address, seed=seed)


class Device:
    """A connected device (wraps its driver, :attr:`driver`)."""

    def __init__(self, driver: AnalyzerDriverBase) -> None:
        from .driver.software_trigger import SoftwareTriggerDriver, supports_software_trigger

        # Captures with ``software_trigger=True`` look for the trigger in a stream (a device process
        # looks for it itself, next to the device)
        wrap = supports_software_trigger(driver) and not getattr(driver, "software_trigger_inside", False)
        self.driver = SoftwareTriggerDriver(driver) if wrap else driver

    # ------------------------------------------------------------- properties
    @property
    def name(self) -> str:
        return self.driver.device_version or "Unknown"

    @property
    def channel_count(self) -> int:
        return self.driver.channel_count

    @property
    def max_frequency(self) -> int:
        return self.driver.max_frequency

    @property
    def capabilities(self) -> frozenset[str]:
        return self.driver.capabilities()

    def acquisition_modes(self) -> tuple[str, ...]:
        """``"buffer"`` and/or ``"stream"``."""
        return self.driver.acquisition_modes() or (ACQUISITION_BUFFER,)

    def describe(self) -> list[DeviceSection]:
        """Board, firmware and connection as ``(title, [(property, value), ...])`` sections."""
        return self.driver.describe()

    def close(self) -> None:
        self.driver.dispose()

    def inject(self, fault: str, value: float = 0.0) -> None:
        """Fault injection of a simulated device (``sim:<profile>``): ``"disconnect"``,
        ``"reconnect"``, ``"delay"`` (seconds), ``"overflow"`` (the next stream), ``"restart"``."""
        model = getattr(self.driver, "inner", self.driver)
        if not hasattr(model, "inject"):
            raise ValueError("only simulated devices take faults")
        model.inject(fault, value)

    def __enter__(self) -> "Device":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"<Device {self.name}>"

    # ---------------------------------------------------------------- capture
    def build_session(
        self,
        channels: Iterable[int] = (0, 1),
        rate: Union[int, float, str, None] = None,
        samples: Optional[int] = None,
        duration: Union[float, str, None] = None,
        pre_trigger: Optional[int] = None,
        trigger: Optional[Trigger] = None,
        mode: str = ACQUISITION_BUFFER,
        names: Union[Mapping[int, str], SequenceType[str], None] = None,
        threshold: Optional[float] = None,
        to_disk: bool = False,
        software_trigger: bool = False,
    ) -> CaptureSession:
        """The session :meth:`capture` would start; raises ``ValueError`` for invalid settings."""
        driver = self.driver
        if software_trigger and mode != ACQUISITION_STREAM:
            raise ValueError("A software trigger looks for the trigger in a stream: use mode='stream'.")
        numbers = [int(channel) for channel in channels]
        if not numbers:
            raise ValueError("Select at least one channel to capture.")
        if len(set(numbers)) != len(numbers):
            raise ValueError("A channel is selected twice.")
        invalid = [n for n in numbers if not 0 <= n < driver.channel_count]
        if invalid:
            raise ValueError(
                f"Channel {invalid[0]} does not exist: the device has channels 0 to "
                f"{driver.channel_count - 1}."
            )

        modes = self.acquisition_modes()
        if mode not in modes:
            raise ValueError(f"The device does not capture in {mode} mode (modes: {', '.join(modes)}).")
        acquisition = mode if driver.acquisition_modes() else None

        frequency = self._frequency(numbers, rate, acquisition)
        capabilities = driver.capabilities()

        if trigger is None:
            trigger = self._default_trigger(mode, capabilities)
        immediate = isinstance(trigger, (Immediate, Simulation))

        limits = driver.get_limits(numbers, acquisition, to_disk=to_disk)
        if software_trigger:
            from .driver.software_trigger import software_trigger_limits

            limits = software_trigger_limits(limits)
        if samples is not None and duration is not None:
            raise ValueError("Give either the number of samples or the duration, not both.")
        if duration is not None:
            seconds = parse_duration(duration)
            if seconds <= 0:
                raise ValueError("The duration must be positive.")
            total = max(int(round(seconds * frequency)), 1)
        elif samples is not None:
            total = int(samples)
        else:
            total = min(10_000, limits.max_total_samples)

        if immediate:
            pre = 0
        else:
            pre = limits.min_pre_samples if pre_trigger is None else int(pre_trigger)
            if not limits.min_pre_samples <= pre <= limits.max_pre_samples:
                raise ValueError(
                    f"{pre} samples before the trigger are not possible: use "
                    f"{limits.min_pre_samples} to {limits.max_pre_samples}."
                )
        post = total - pre
        if post < limits.min_post_samples:
            raise ValueError(
                f"Too few samples: at least {pre + limits.min_post_samples} with {pre} before the trigger."
            )
        if post > limits.max_post_samples or pre + post > limits.max_total_samples:
            maximum = min(limits.max_total_samples, pre + limits.max_post_samples)
            raise ValueError(
                f"Too many samples ({total}): at most {maximum} fit with these channels "
                f"({maximum / frequency:.6g} s at {frequency} Hz)."
            )

        session = CaptureSession(frequency=frequency, pre_trigger_samples=pre, post_trigger_samples=post)
        session.acquisition_mode = mode
        session.to_disk = bool(to_disk)
        session.capture_channels = [
            AnalyzerChannel(channel_number=number, channel_name=self._name(names, index, number))
            for index, number in enumerate(numbers)
        ]
        if threshold is not None:
            if CAPABILITY_THRESHOLD not in capabilities:
                raise ValueError("The device has no adjustable input threshold.")
            session.threshold_voltage = float(threshold)

        if software_trigger:
            from .core.trigger_engine import session_trigger_sequence, validate_sequence

            if isinstance(trigger, Sequence):
                session.trigger_type = TriggerType.SEQUENCE
                session.trigger_sequence = TriggerSequence(stages=list(trigger.stages))
            else:
                self._apply_trigger(session, trigger, ACQUISITION_BUFFER, capabilities | {CAPABILITY_IMMEDIATE_TRIGGER}, software=True)
            sequence = session_trigger_sequence(session)
            if sequence is not None:
                problem = validate_sequence(sequence, numbers, frequency)
                if problem:
                    raise ValueError(problem)
            session.software_trigger = True
            return session
        self._apply_trigger(session, trigger, mode, capabilities)
        return session

    def capture(self, *args: Any, timeout: Optional[float] = None, **kwargs: Any) -> "Capture":
        """Capture and wait for the samples; the arguments are those of :meth:`build_session`:

        ``channels``
            Channel numbers, counted from 0.
        ``rate``
            Samples per second (``10_000_000``, ``"10M"``, ``"100k"``); default: the highest.
        ``samples`` or ``duration``
            Total samples (including ``pre_trigger``), or the time to capture (``"5ms"``, 0.005).
        ``pre_trigger``
            Samples kept before the trigger.
        ``trigger``
            :class:`Edge`, :class:`Pattern`, :class:`Immediate`, :class:`Sequence` or
            :class:`Simulation`; default: immediate when the device can start without a trigger.
        ``mode``
            ``"buffer"`` (into the memory of the device) or ``"stream"`` (over USB while capturing).
        ``software_trigger``
            Look for the trigger in a stream (``mode="stream"``) instead of in the device: any
            trigger on any captured channel, also :class:`Sequence` on devices without one.
        ``timeout``
            Seconds to wait for the trigger; the capture is stopped and ``TimeoutError`` raised.

        Raises ``ValueError`` for settings the device cannot capture and :class:`CaptureFailed`
        when the device reports an error::

            capture = device.capture(channels=[0, 1], rate="1M", samples=50_000,
                                     pre_trigger=5_000, trigger=api.Pattern({0: 1, 1: 0}))
        """
        session = self.build_session(*args, **kwargs)
        return self.run(session, timeout=timeout)

    def run(self, session: CaptureSession, timeout: Optional[float] = None) -> "Capture":
        """Start a prepared session (:meth:`build_session`) and wait for it."""
        done = threading.Event()
        results: list[CaptureCompletedArgs] = []

        def completed(args: CaptureCompletedArgs) -> None:
            results.append(args)
            done.set()

        error = self.driver.start_capture(session, completed)
        if error != CaptureError.NONE:
            raise CaptureFailed(error.message or f"The capture could not be started ({error.value}).")

        try:
            waited = 0.0
            while not done.wait(0.1):  # short waits keep Ctrl+C working
                waited += 0.1
                if timeout is not None and waited >= timeout:
                    self.driver.stop_capture()
                    done.wait(5.0)
                    raise TimeoutError(f"No trigger within {timeout:g} s: the capture was stopped.")
        except KeyboardInterrupt:
            self.driver.stop_capture()
            raise

        result = results[0]
        if not result.success:
            raise CaptureFailed(result.error or "The capture failed.")
        self._wait_for_transfer(result.session)
        return Capture(result.session)

    def _wait_for_transfer(self, session: CaptureSession) -> None:
        """A device that hands over a large capture piece by piece (an oscilloscope over the
        network) reports it complete with an overview: wait for the samples themselves, so that
        saving or decoding the result does not see zeros."""
        progressive = getattr(session, "progressive", None)
        if progressive is None or progressive.complete:
            return
        arrived = threading.Event()
        errors: list[Optional[str]] = []

        def tile(args) -> None:
            if args.session is session and (args.complete or args.error):
                errors.append(args.error)
                arrived.set()

        self.driver.add_capture_tile_handler(tile)
        try:
            while not progressive.complete and not arrived.wait(0.1):  # short waits keep Ctrl+C working
                pass
        except KeyboardInterrupt:
            stop = getattr(self.driver, "stop_transfer", None)
            if stop is not None:
                stop()
            raise
        finally:
            self.driver.remove_capture_tile_handler(tile)
        if errors and errors[0] and not progressive.complete:
            raise CaptureFailed(f"The capture did not arrive completely: {errors[0]}.")

    # --------------------------------------------------------------- helpers
    def _frequency(self, channels: list[int], rate: Any, acquisition: Optional[str]) -> int:
        driver = self.driver
        fixed = driver.sample_rates(channels, acquisition)
        maximum = driver.max_frequency_for(channels, acquisition)
        if rate is None:
            return max(fixed) if fixed else maximum
        frequency = parse_rate(rate)
        if fixed is not None:
            if frequency not in fixed:
                rates = ", ".join(_rate_text(value) for value in sorted(fixed))
                raise ValueError(f"The device cannot sample these channels at {_rate_text(frequency)}: use {rates}.")
            return frequency
        minimum = max(driver.min_frequency, 1)
        if not minimum <= frequency <= maximum:
            raise ValueError(
                f"Sampling rate {_rate_text(frequency)} out of range: the device samples these channels "
                f"at {_rate_text(minimum)} to {_rate_text(maximum)}."
            )
        return frequency

    def _default_trigger(self, mode: str, capabilities: frozenset[str]) -> Trigger:
        if not self.driver.is_hardware:
            return Simulation()
        if CAPABILITY_IMMEDIATE_TRIGGER in capabilities or (
            mode == ACQUISITION_STREAM and CAPABILITY_STREAM_IMMEDIATE_ONLY in capabilities
        ):
            return Immediate()
        raise ValueError("This device cannot start without a trigger: give an Edge or Pattern trigger.")

    @staticmethod
    def _name(names: Any, index: int, number: int) -> str:
        if names is None:
            return ""
        if isinstance(names, Mapping):
            return str(names.get(number, ""))
        return str(names[index]) if index < len(names) else ""

    def _apply_trigger(
        self, session: CaptureSession, trigger: Trigger, mode: str, capabilities: frozenset[str],
        software: bool = False,
    ) -> None:
        """``software``: the application evaluates the trigger, any captured channel can trigger."""
        driver = self.driver
        if isinstance(trigger, Simulation):
            session.trigger_type = TriggerType.SIMULATION
            session.trigger_pattern = int(trigger.pattern)
            return
        if not driver.is_hardware:
            raise ValueError("The emulated device only generates test signals: use trigger=Simulation().")
        if (
            mode == ACQUISITION_STREAM
            and CAPABILITY_STREAM_IMMEDIATE_ONLY in capabilities
            and not isinstance(trigger, Immediate)
        ):
            raise ValueError("Stream captures of this device start at once: use trigger=Immediate().")

        if isinstance(trigger, Immediate):
            if CAPABILITY_IMMEDIATE_TRIGGER not in capabilities and not (
                mode == ACQUISITION_STREAM and CAPABILITY_STREAM_IMMEDIATE_ONLY in capabilities
            ):
                raise ValueError("This device cannot start without a trigger.")
            session.trigger_type = TriggerType.IMMEDIATE
            session.pre_trigger_samples = 0
        elif isinstance(trigger, Edge):
            channel = int(trigger.channel)
            usable = set(driver.edge_trigger_channels())
            if driver.has_external_trigger():
                usable.add(driver.channel_count)
            if channel not in usable and not (software and channel in session.channel_numbers):
                raise ValueError(f"Channel {channel} cannot trigger on an edge.")
            session.trigger_type = TriggerType.EDGE
            session.trigger_channel = channel
            session.trigger_inverted = not trigger.rising
        elif isinstance(trigger, Pattern):
            self._apply_pattern(session, trigger, software)
        elif isinstance(trigger, Sequence):
            self._apply_sequence(session, trigger, capabilities)
        else:
            raise ValueError(f"Unknown trigger {trigger!r}.")

    def _apply_pattern(self, session: CaptureSession, trigger: Pattern, software: bool = False) -> None:
        levels = {int(channel): int(level) for channel, level in trigger.levels.items()}
        if not levels:
            raise ValueError("A pattern trigger needs at least one channel.")
        if any(level not in (0, 1) for level in levels.values()):
            raise ValueError("The levels of a pattern trigger are 0 or 1.")
        first = min(levels)
        count = max(levels) - first + 1
        if sorted(levels) != list(range(first, first + count)):
            raise ValueError("A pattern trigger covers consecutive channels: give a level for each of them.")
        trigger_type = TriggerType.FAST if trigger.fast else TriggerType.COMPLEX
        max_bits = pattern_max_bits(trigger_type)
        if not software and count > max_bits:
            raise ValueError(f"A {'fast' if trigger.fast else 'pattern'} trigger compares at most {max_bits} channels.")
        if not software and not pattern_fits(self.driver.pattern_trigger_groups(), first, count):
            groups = ", ".join(f"{start}-{start + size - 1}" for start, size in self.driver.pattern_trigger_groups())
            raise ValueError(
                f"Channels {first} to {first + count - 1} are not consecutive trigger inputs of one "
                f"board (usable: {groups})."
            )
        session.trigger_type = trigger_type
        session.trigger_channel = first
        session.trigger_bit_count = count
        session.trigger_pattern = sum(level << (channel - first) for channel, level in levels.items())

    @staticmethod
    def _apply_sequence(session: CaptureSession, trigger: Sequence, capabilities: frozenset[str]) -> None:
        stages = list(trigger.stages)
        if not stages:
            raise ValueError("A trigger sequence needs at least one stage.")
        supported = [c for c in capabilities if c.split("=")[0] == CAPABILITY_TRIGGER_SEQUENCE]
        if not supported:
            raise ValueError("The device cannot evaluate trigger sequences.")
        _, _, maximum = supported[0].partition("=")
        if maximum.isdigit() and len(stages) > int(maximum):
            raise ValueError(f"The device evaluates at most {maximum} stages.")
        for capability in capabilities:
            if capability.startswith(CAPABILITY_TRIGGER_CONDITIONS):
                kinds = capability[len(CAPABILITY_TRIGGER_CONDITIONS):].split("/")
                for stage in stages:
                    if stage.condition.kind.value not in kinds:
                        raise ValueError(f"The device cannot trigger on a {stage.condition.kind.value} condition.")
        session.trigger_type = TriggerType.SEQUENCE
        session.trigger_sequence = TriggerSequence(stages=stages)


def _rate_text(rate: int) -> str:
    return sigrok_session.samplerate_string(rate)


# ---------------------------------------------------------------------- capture
class Capture:
    """A capture: its :attr:`session` (settings and samples) and the regions of a ``.lac`` file."""

    def __init__(self, session: CaptureSession, regions: SequenceType[SampleRegion] = ()) -> None:
        self.session = session
        self.regions = list(regions)

    # ------------------------------------------------------------- properties
    @property
    def frequency(self) -> int:
        """Samples per second."""
        return int(self.session.frequency)

    @property
    def sample_count(self) -> int:
        return self.session.sample_count()

    @property
    def trigger_sample(self) -> int:
        """Position of the trigger (the samples before it)."""
        return int(self.session.pre_trigger_samples)

    @property
    def duration(self) -> float:
        """Seconds of the capture."""
        return self.sample_count / max(self.frequency, 1)

    @property
    def channels(self) -> list[AnalyzerChannel]:
        return list(self.session.capture_channels)

    @property
    def channel_numbers(self) -> list[int]:
        return self.session.channel_numbers

    def times(self) -> np.ndarray:
        """Time of every sample in seconds, relative to the trigger."""
        return (np.arange(self.sample_count) - self.trigger_sample) / float(max(self.frequency, 1))

    def channel(self, channel: Union[int, str]) -> AnalyzerChannel:
        """A channel by number (``3``) or name (``"SDA"``, ``"Channel 4"``)."""
        for item in self.session.capture_channels:
            if isinstance(channel, str):
                if channel in (item.display_name, item.channel_name, item.textual_channel_number):
                    return item
            elif item.channel_number == int(channel):
                return item
        known = ", ".join(f"{item.channel_number} ({item.display_name})" for item in self.session.capture_channels)
        raise KeyError(f"The capture has no channel {channel!r}; channels: {known}.")

    def samples(self, channel: Union[int, str]) -> np.ndarray:
        """Samples (0 or 1, ``uint8``) of a channel by number or name."""
        samples = self.channel(channel).samples
        return np.zeros(0, dtype=np.uint8) if samples is None else samples

    def __repr__(self) -> str:
        return (
            f"<Capture {len(self.session.capture_channels)} channels, {self.sample_count} samples "
            f"at {_rate_text(self.frequency)}>"
        )

    # ------------------------------------------------------------------- files
    def save(self, path: str, include_time: bool = False, compatible: bool = False) -> None:
        """Write the capture; the format follows the extension: ``.lac`` / ``.lac.gz``
        (openSciLab; ``compatible=True`` writes the samples as the original LogicAnalyzer
        software does, for a file it can open), ``.sr`` (sigrok, PulseView), ``.csv``
        (``include_time`` adds a time column) or ``.vcd``."""
        kind = file_format(path, WRITE_FORMATS)
        if kind == "lac":
            capture_io.save_capture(path, self.session, self.regions, compatible=compatible)
        elif kind == "sr":
            sigrok_session.save_session(path, self.session)
        elif kind == "csv":
            capture_io.export_csv(path, self.session, include_time=include_time)
        else:
            capture_io.export_vcd(path, self.session)

    # ---------------------------------------------------------------- decoding
    def decode_groups(
        self,
        decoder: str,
        channels: Optional[Mapping[Union[str, int], Union[int, str]]] = None,
        options: Optional[Mapping[str, Any]] = None,
        stack: Iterable[Union[str, tuple[str, Mapping[str, Any]]]] = (),
        registry: Optional[DecoderRegistry] = None,
        run: Optional[Callable] = None,
    ) -> list[AnnotationGroup]:
        """Like :meth:`decode`, the annotations grouped by decoder (with ``error`` of each).
        ``run(provider, session)`` runs the decoders (default: in this thread)."""
        registry = registry or decoder_registry()
        provider = SigrokProvider(registry)
        info = _decoder(registry, decoder)

        chain = [info]
        if not info.is_base_decoder:
            below = registry.provider_chain(info)
            if not below:
                raise ValueError(f"No installed decoder provides the input of '{info.id}' ({', '.join(info.inputs)}).")
            chain = below + [info]

        base = chain[0]
        parent = provider.add_instance(
            DecoderInstance(
                decoder_id=base.id,
                channel_map=self._channel_map(base, channels or {}),
                options=_options(base, options if base is info else {}),
            )
        )
        for index, stacked in enumerate(chain[1:], start=1):
            parent = provider.add_instance(
                DecoderInstance(
                    decoder_id=stacked.id,
                    options=_options(stacked, options if stacked is info else {}),
                    color_index=index,
                    parent=parent,
                )
            )
        for index, item in enumerate(stack, start=len(chain)):
            stacked_id, stacked_options = (item, {}) if isinstance(item, str) else item
            stacked = _decoder(registry, stacked_id)
            parent = provider.add_instance(
                DecoderInstance(
                    decoder_id=stacked.id,
                    options=_options(stacked, stacked_options),
                    color_index=index,
                    parent=parent,
                )
            )
        return run(provider, self.session) if run is not None else provider.run(self.session)

    def decode(
        self,
        decoder: str,
        channels: Optional[Mapping[Union[str, int], Union[int, str]]] = None,
        options: Optional[Mapping[str, Any]] = None,
        stack: Iterable[Union[str, tuple[str, Mapping[str, Any]]]] = (),
        registry: Optional[DecoderRegistry] = None,
        run: Optional[Callable] = None,
    ) -> list[AnnotationRecord]:
        """Run a sigrok decoder and return its annotations, sorted by time.

        ``channels`` maps the channels of the decoder (id, name or index: ``"scl"``, ``"SCL"``,
        ``0``) to channels of the capture (number or name); ``options`` sets decoder options
        (``{"baudrate": 115200}``). ``stack`` adds decoders on top (``["eeprom24xx"]`` or
        ``[("eeprom24xx", {"chip": "generic"})]``)::

            for item in capture.decode("uart", channels={"rx": 2}, options={"baudrate": 9600}):
                print(item.start_time, item.row, item.value)
        """
        groups = self.decode_groups(decoder, channels, options, stack, registry, run)
        return annotation_records(groups, self.session)

    def export_annotations(self, path: str, groups: SequenceType[AnnotationGroup]) -> int:
        """Write the result of :meth:`decode_groups` as ``.csv`` or ``.json``."""
        return export_annotations(path, groups, self.session)

    def _channel_map(self, info: DecoderInfo, channels: Mapping[Any, Any]) -> dict[int, int]:
        mapping: dict[int, int] = {}
        for key, value in channels.items():
            decoder_channel = _decoder_channel(info, key)
            try:
                capture_channel = self.channel(value if isinstance(value, str) and not value.isdigit() else int(value))
            except KeyError as error:
                raise ValueError(str(error.args[0])) from None
            mapping[decoder_channel] = capture_channel.channel_number
        missing = [channel.id for channel in info.required_channels if channel.index not in mapping]
        if missing:
            raise ValueError(
                f"Assign the channels {', '.join(missing)} of '{info.id}' "
                f"(e.g. channels={{'{missing[0]}': 0}})."
            )
        return mapping


def file_format(path: str, allowed: SequenceType[str]) -> str:
    """``"lac"``, ``"sr"``, ``"csv"`` or ``"vcd"`` by the extension of ``path``."""
    lower = path.lower()
    for extension in sorted(allowed, key=len, reverse=True):
        if lower.endswith(extension):
            return "lac" if extension.startswith(".lac") else extension.lstrip(".")
    raise ValueError(f"Unknown file type '{os.path.basename(path)}': use {', '.join(allowed)}.")


def load(path: str) -> Capture:
    """Read a ``.lac``, ``.lac.gz`` or ``.sr`` file::

        capture = api.load("capture.lac")
        print(capture.frequency, capture.sample_count, capture.channel_numbers)
    """
    kind = file_format(path, READ_FORMATS)
    if kind == "sr":
        return Capture(sigrok_session.load_session(path))
    exported = capture_io.load_capture(path)
    return Capture(exported.session, exported.regions)


# --------------------------------------------------------------------- decoders
_registries: dict[tuple[str, ...], DecoderRegistry] = {}


def decoder_registry(paths: SequenceType[str] = ()) -> DecoderRegistry:
    """The decoders of the usual search paths (and ``paths``), loaded once."""
    key = tuple(paths)
    registry = _registries.get(key)
    if registry is None:
        registry = _registries[key] = DecoderRegistry(paths)
    return registry


def decoders(paths: SequenceType[str] = ()) -> list[DecoderInfo]:
    """The installed sigrok decoders::

        for info in api.decoders():
            print(info.id, info.longname, [channel.id for channel in info.channels])
    """
    return decoder_registry(paths).decoders


def _decoder(registry: DecoderRegistry, decoder_id: str) -> DecoderInfo:
    info = registry.get(decoder_id)
    if info is None:
        raise ValueError(f"Decoder '{decoder_id}' is not installed (list them with decoders()).")
    return info


def _decoder_channel(info: DecoderInfo, key: Any) -> int:
    for channel in info.channels:
        if key in (channel.id, channel.name) or str(key).lower() in (channel.id.lower(), channel.name.lower()):
            return channel.index
    if isinstance(key, int) or str(key).isdigit():
        index = int(key)
        if any(channel.index == index for channel in info.channels):
            return index
    names = ", ".join(channel.id for channel in info.channels)
    raise ValueError(f"'{info.id}' has no channel '{key}' (channels: {names}).")


def _options(info: DecoderInfo, options: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    by_id = {option.id: option for option in info.options}
    result = info.default_options()
    for key, value in (options or {}).items():
        option = by_id.get(key)
        if option is None:
            names = ", ".join(by_id) or "none"
            raise ValueError(f"'{info.id}' has no option '{key}' (options: {names}).")
        if isinstance(value, str):
            value = _option_value(option.option_type, option.default, value)
        if option.values and value not in option.values and str(value) not in [str(v) for v in option.values]:
            allowed = ", ".join(str(v) for v in option.values)
            raise ValueError(f"Invalid value '{value}' of option '{key}' of '{info.id}': use {allowed}.")
        result[key] = value
    return result


def _option_value(option_type: OptionType, default: Any, text: str) -> Any:
    """A value given as text (command line) in the type of the option."""
    reference = default if option_type == OptionType.LIST else None
    if option_type == OptionType.BOOLEAN or isinstance(reference, bool):
        lowered = text.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
        raise ValueError(f"Invalid yes/no value '{text}'.")
    try:
        if option_type == OptionType.INTEGER or isinstance(reference, int):
            try:
                return int(text, 0)  # 0x50 as well
            except ValueError:
                return int(text)
        if option_type == OptionType.DOUBLE or isinstance(reference, float):
            return float(text)
    except ValueError as error:
        raise ValueError(f"Invalid number '{text}'.") from error
    return text


def run_flow(path: str, *, sim: bool = False, fast: bool = False, devices: Optional[Mapping[str, str]] = None,
             **options) -> "lab.RunResult":
    """Run the flow in ``path`` (``.flow.yaml`` or a DSL script) to its end.

    ``sim``: simulators for every device; ``fast``: virtual time; ``devices``: other addresses
    by device name; further options go to :class:`openscilab.lab.Engine` (``duration``, ``seed``,
    ``data_dir``, ...). Inside a project folder its devices and own nodes are used.
    """
    from .lab.engine import Engine
    from .lab.flow_files import load_flow
    from .lab.project import Project

    root = Project.find(path)
    project = Project.open(root) if root else None
    flow = project.load_flow(path) if project else load_flow(path)
    timeout = options.pop("timeout", None)
    options.setdefault("data_dir", project.data_dir if project else os.path.dirname(os.path.abspath(path)))
    engine = Engine(flow, registry=project.registry() if project else None, mode="virtual" if fast else "real",
                    simulate=sim, devices=dict(devices or {}), project=project, **options)
    return engine.run(timeout)
