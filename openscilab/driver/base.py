# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of openSciLab, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Common driver infrastructure (port of ``SharedDriver/AnalyzerDriverBase.cs``)."""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import TYPE_CHECKING, Callable, Optional, Sequence

from ..core.sample_store import disk_sample_bytes
from .models import CaptureSession, TriggerType

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    import numpy as np

#: Burst measurement limits (firmware V6_5 ``StartCaptureSimple``): the
#: timestamp block is sized by one byte and each burst needs enough samples.
MAX_MEASURED_LOOP_COUNT = 253
MIN_MEASURED_POST_SAMPLES = 100


class DeviceConnectionError(Exception):
    """Raised when a device cannot be opened or reports an invalid identity."""


class FirmwareOutdatedError(DeviceConnectionError):
    """The device runs firmware this application does not work with: it has to be updated."""

    def __init__(self, message: str, device: str = "", location: str = "") -> None:
        super().__init__(message)
        #: Identification the device reported, and where it is connected (port or address)
        self.device = device
        self.location = location


class UnsupportedFeatureError(Exception):
    """Raised when the device (or its firmware) lacks an optional function."""


#: Optional functions of a device (:meth:`AnalyzerDriverBase.capabilities`); the Pico firmware
#: reports them with ``CMD_CAPABILITIES``, other drivers name the ones their device has.
CAPABILITY_SELF_TEST = "SELFTEST"
CAPABILITY_SIMULATION = "SIMULATION"
CAPABILITY_DEVICE_INFO = "DEVICEINFO"
#: The firmware can drive the trigger output on an edge (trigger type ``EDGE_OUT``).
CAPABILITY_EDGE_TRIGGER_OUT = "EDGE_TRIGGER_OUT"
#: Prefix of the capability naming the channel groups a pattern trigger can cover:
#: ``PATTERN_GROUPS=0-20/21-23`` (channels on consecutive GPIOs, counted from 0).
CAPABILITY_PATTERN_GROUPS = "PATTERN_GROUPS="
#: Captures can start without a trigger (trigger type ``IMMEDIATE``).
CAPABILITY_IMMEDIATE_TRIGGER = "IMMEDIATE_TRIGGER"
#: The input threshold voltage can be set (``CaptureSession.threshold_voltage``).
CAPABILITY_THRESHOLD = "THRESHOLD"
#: Stream captures can run until stopped (``CaptureSession.continuous``).
CAPABILITY_CONTINUOUS_STREAM = "CONTINUOUS_STREAM"
#: Stream captures start at once, without a trigger.
CAPABILITY_STREAM_IMMEDIATE_ONLY = "STREAM_IMMEDIATE_ONLY"
#: Firmware function: stream captures over USB (``STREAM=<bytes per second>``).
CAPABILITY_STREAM = "STREAM"
#: Trigger sequences in the device (``TRIGGER_SEQUENCE=<stages>``; ``TriggerType.SEQUENCE``)
CAPABILITY_TRIGGER_SEQUENCE = "TRIGGER_SEQUENCE"
#: Condition kinds the device's trigger sequence understands (``TRIGGER_CONDITIONS=pattern/edge/pulse/gap``)
CAPABILITY_TRIGGER_CONDITIONS = "TRIGGER_CONDITIONS="
#: State mode: samples taken on the edges of a clock input (``CaptureSession.clock_channel``)
CAPABILITY_STATE_MODE = "STATE_MODE"
#: State mode on a stream: every clock edge is sent at once (live state)
CAPABILITY_STREAM_STATE = "STREAM_STATE"
#: Pins as inputs and outputs: modes, levels, pulses (``GpioFacet``)
CAPABILITY_GPIO = "GPIO"
#: Hardware PWM on pins (``GpioFacet.pwm``)
CAPABILITY_PWM = "PWM"
#: Periodic reports of the inputs (``MonitorFacet``)
CAPABILITY_MONITOR = "MONITOR"
#: Analog input channels in captures and the monitor (``ANALOG=<count>``; ``AnalogInFacet``)
CAPABILITY_ANALOG = "ANALOG="
#: Analog output (``AnalogOutFacet``)
CAPABILITY_DAC = "DAC"
#: Pattern generator on pins (``PATTERN_GEN=<max rate>,<pins>``; ``GeneratorFacet``)
CAPABILITY_PATTERN_GEN = "PATTERN_GEN="
#: Square waves on a pin by PWM or timer (``GeneratorFacet``)
CAPABILITY_GEN_SQUARE = "GEN_SQUARE"
#: Analog function/arbitrary waveform generator outputs (``GeneratorFacet``)
CAPABILITY_AFG = "AFG"
#: Protocol transmitters (``GeneratorFacet``, nodes ``gen.tx_*``)
CAPABILITY_TX_UART = "TX_UART"
CAPABILITY_TX_SPI = "TX_SPI"
CAPABILITY_TX_I2C = "TX_I2C"
#: Large captures arrive progressively: overview first, tiles on demand (``prioritize``)
CAPABILITY_PROGRESSIVE = "PROGRESSIVE"
#: The device restarts on request (``restart()``): as after power-up, the connection stays
CAPABILITY_RESTART = "RESTART"

#: Facet (class name in ``core/instrument.py``) unlocked by each capability (prefix).
FACET_CAPABILITIES = {
    CAPABILITY_GPIO: "GpioFacet",
    CAPABILITY_PWM: "GpioFacet",
    CAPABILITY_MONITOR: "MonitorFacet",
    CAPABILITY_ANALOG: "AnalogInFacet",
    CAPABILITY_DAC: "AnalogOutFacet",
    CAPABILITY_PATTERN_GEN: "GeneratorFacet",
    CAPABILITY_GEN_SQUARE: "GeneratorFacet",
    CAPABILITY_AFG: "GeneratorFacet",
    CAPABILITY_TX_UART: "GeneratorFacet",
    CAPABILITY_TX_SPI: "GeneratorFacet",
    CAPABILITY_TX_I2C: "GeneratorFacet",
}


def capability_value(capabilities, prefix: str) -> Optional[str]:
    """The value of a capability with a value (``ANALOG=4`` → ``"4"``), ``None`` without it."""
    for capability in capabilities:
        if capability.startswith(prefix):
            return capability[len(prefix):]
    return None


def facets_of(capabilities) -> set[str]:
    """Names of the facets the ``capabilities`` unlock."""
    result = set()
    for capability in capabilities:
        for prefix, facet in FACET_CAPABILITIES.items():
            if capability == prefix or (prefix.endswith("=") and capability.startswith(prefix)):
                result.add(facet)
    return result

#: The application keeps one byte per sample and channel: a stream is limited to about 1 GiB of them.
MAX_SAMPLE_BYTES = 1 << 30


#: Seconds between two checks of the free disk space while a stream is recorded to disk
DISK_CHECK_INTERVAL = 1.0
DISK_FULL_ERROR = (
    "The disk is almost full, so the stream recorded to it was stopped. Free some space or keep "
    "fewer samples."
)


def disk_space_low() -> bool:
    """The free disk space reached the reserve: a stream recorded to disk has to stop, since
    writing a full memory-mapped file would crash the application."""
    return disk_sample_bytes() <= 0


class DiskWatch:
    """Checks :func:`disk_space_low` in a thread of its own: under heavy writing the query can
    take half a second, which must not hold up the thread reading the device."""

    def __init__(
        self, interval: float = DISK_CHECK_INTERVAL, on_low: Optional[Callable[[], None]] = None
    ) -> None:
        self.low = threading.Event()
        self._on_low = on_low
        self._stop = threading.Event()
        self._interval = interval
        self._thread = threading.Thread(target=self._run, name="openscilab-disk-watch", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            if disk_space_low():
                self.low.set()
                if self._on_low is not None:
                    self._on_low()
                return

    def stop(self) -> None:
        self._stop.set()


def stream_sample_bytes(to_disk: bool = False, continuous: bool = False) -> int:
    """Bytes of samples a stream may keep, over all channels.

    In memory about 1 GiB; on disk the free space less a reserve, of which an endless stream uses
    half (its ring buffer writes every sample twice).
    """
    if not to_disk:
        return MAX_SAMPLE_BYTES
    available = disk_sample_bytes()
    return available // 2 if continuous else available

#: Acquisition modes (``CaptureSession.acquisition_mode``): into the memory of the device, or
#: streamed over USB while capturing.
ACQUISITION_BUFFER = "buffer"
ACQUISITION_STREAM = "stream"

#: Pattern trigger channels of firmware that does not report its groups: channels 1 to 16.
DEFAULT_PATTERN_GROUPS: tuple[tuple[int, int], ...] = ((0, 16),)


def pattern_max_bits(trigger_type: TriggerType) -> int:
    """Longest pattern of a trigger type: 16 bits, 5 for the fast trigger (a 32 entry jump table)."""
    return 5 if trigger_type == TriggerType.FAST else 16


def pattern_fits(groups: Sequence[tuple[int, int]], first_channel: int, bit_count: int) -> bool:
    """Whether ``bit_count`` channels from ``first_channel`` lie inside one group of trigger inputs."""
    return bit_count >= 1 and any(
        first <= first_channel and first_channel + bit_count <= first + count for first, count in groups
    )


#: Words of self-test items that stay in capitals in their titles
TITLE_ACRONYMS = frozenset({"RAM", "USB", "FPGA"})


@dataclass
class SelfTestResult:
    """One line of the board self-test (``SELFTEST:<item>:<status>:<detail>``)."""

    item: str
    status: str
    detail: str = ""

    @property
    def title(self) -> str:
        if self.item.startswith("CH") and self.item[2:].isdigit():
            return f"Channel {self.item[2:]}"
        text = " ".join(word if word in TITLE_ACRONYMS else word.lower() for word in self.item.split("_"))
        return text[:1].upper() + text[1:]

    @property
    def severity(self) -> str:
        """``ok``, ``fail``, ``info`` or ``warning`` (e.g. a stuck input)."""
        return {"OK": "ok", "FAIL": "fail", "INFO": "info", "SKIPPED": "info"}.get(
            self.status, "warning"
        )


class CaptureMode(IntEnum):
    CHANNELS_8 = 0
    CHANNELS_16 = 1
    CHANNELS_24 = 2

    @property
    def bytes_per_sample(self) -> int:
        return {CaptureMode.CHANNELS_8: 1, CaptureMode.CHANNELS_16: 2, CaptureMode.CHANNELS_24: 4}[self]


class CaptureError(Enum):
    NONE = "none"
    BUSY = "busy"
    BAD_PARAMS = "bad_params"
    HARDWARE_ERROR = "hardware_error"
    UNEXPECTED_ERROR = "unexpected_error"
    UNSUPPORTED = "unsupported"

    @property
    def message(self) -> str:
        return {
            CaptureError.NONE: "",
            CaptureError.BUSY: "Device is busy, stop the capture before starting a new one.",
            CaptureError.BAD_PARAMS: (
                "Specified parameters are incorrect. Check the documentation in the "
                "repository to validate them."
            ),
            CaptureError.HARDWARE_ERROR: (
                "Device reported an error starting the capture. Restart the device and try again."
            ),
            CaptureError.UNEXPECTED_ERROR: (
                "Unexpected error, restart the application and the device and try again."
            ),
            CaptureError.UNSUPPORTED: (
                "The firmware of the device does not support this function. Flash the "
                "firmware from the firmware/ folder of this project."
            ),
        }[self]


class AnalyzerDriverType(Enum):
    SERIAL = "Serial"
    NETWORK = "Network"
    MULTI = "Multi"
    EMULATED = "Emulated"
    #: DreamSourceLab DSLogic (USB)
    DSLOGIC = "DSLogic"
    #: A driver added later; it names itself with ``driver_id``
    OTHER = "Other"


def capture_mode_for_bits(bits: Sequence[int]) -> CaptureMode:
    """Narrowest mode whose samples hold every bit."""
    highest = max(bits, default=0)
    if highest < 8:
        return CaptureMode.CHANNELS_8
    if highest < 16:
        return CaptureMode.CHANNELS_16
    return CaptureMode.CHANNELS_24


@dataclass
class CaptureLimits:
    min_pre_samples: int = 0
    max_pre_samples: int = 0
    min_post_samples: int = 0
    max_post_samples: int = 0

    @property
    def max_total_samples(self) -> int:
        return self.min_pre_samples + self.max_post_samples


@dataclass
class AnalyzerDeviceInfo:
    name: str
    max_frequency: int
    blast_frequency: int
    channels: int
    buffer_size: int
    mode_limits: list[CaptureLimits] = field(default_factory=list)


@dataclass
class CaptureCompletedArgs:
    success: bool
    session: CaptureSession
    error: Optional[str] = None
    #: stream position of the first sample kept (> 0 when an endless stream dropped samples)
    first_sample: int = 0
    #: ``time.monotonic`` when the driver had the capture (set when it is raised)
    arrived: Optional[float] = None


CaptureCompletedHandler = Callable[[CaptureCompletedArgs], None]


@dataclass
class CaptureProgressArgs:
    """The samples a streaming capture received so far (sent from the capture thread).

    ``samples`` maps the channel numbers of ``session`` to views of the arrays the driver fills;
    they stay valid, the driver only writes behind them.
    """

    session: CaptureSession
    samples: dict[int, "np.ndarray"]
    #: position of ``samples[...][0]`` in the stream: > 0 once an endless stream drops samples
    first_sample: int = 0
    #: samples the device could not deliver so far (a link too slow for the rate)
    lost: int = 0
    #: views of the analog channels by analog channel number, like ``samples`` (raw ``int16``)
    analog: dict[int, "np.ndarray"] = field(default_factory=dict)
    #: state mode on a stream: the times of the states so far (microseconds), if the device stamps them
    state_times: Optional["np.ndarray"] = None
    #: ``time.monotonic`` when the last of these samples arrived (set when it is raised, in the
    #: thread that read them: the handlers run later, in the driver's notifier thread)
    arrived: Optional[float] = None

    @property
    def sample_count(self) -> int:
        return min((len(samples) for samples in self.samples.values()), default=0)


CaptureProgressHandler = Callable[[CaptureProgressArgs], None]


@dataclass
class CaptureTileArgs:
    """Part of a large capture arrived (progressive transfer, ``CAPABILITY_PROGRESSIVE``).

    The capture was already reported complete with ``session.progressive`` set; its arrays fill
    tile by tile. ``first``/``count``: the samples that arrived; ``complete``: the last tile.
    """

    session: CaptureSession
    first: int
    count: int
    complete: bool = False
    error: Optional[str] = None


CaptureTileHandler = Callable[[CaptureTileArgs], None]


#: A titled group of ``(property, value)`` rows describing a device
DeviceSection = tuple[str, list[tuple[str, str]]]

GENERIC_SELF_TEST_DESCRIPTION = "The test checks the device with the functions its driver provides."


class _Notifier:
    """Runs the handlers of a driver's events in a thread of its own, in the order they were
    raised: the thread that reads a device only hands them over and goes on reading - it never
    waits for what the application does with them (``docs/timing.md``)."""

    #: seconds without events after which the thread ends (a new one starts with the next event)
    IDLE = 5.0

    def __init__(self, name: str) -> None:
        self.name = name
        self._calls: deque = deque()
        self._condition = threading.Condition()
        self._thread: Optional[threading.Thread] = None

    def put(self, call: Callable[[], None]) -> None:
        with self._condition:
            self._calls.append(call)
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name=f"openscilab-notify-{self.name}", daemon=True)
                self._thread.start()
            self._condition.notify()

    def _run(self) -> None:
        while True:
            with self._condition:
                if not self._calls:
                    self._condition.wait(self.IDLE)
                if not self._calls:
                    self._thread = None
                    return
                call = self._calls.popleft()
            try:
                call()
            except Exception:  # noqa: BLE001 - one handler must not stop the events of the others
                log.exception("A handler of %s failed", self.name)

    def wait(self, timeout: float = 5.0) -> bool:
        """Until every event raised so far was handled (``False`` after ``timeout``)."""
        done = threading.Event()
        if threading.current_thread() is self._thread:
            return True
        self.put(done.set)
        return done.wait(timeout)


class AnalyzerDriverBase:
    """Base class shared by every driver implementation.

    The application only talks to devices through this class: a driver describes what its
    device can do (the properties and methods below) instead of the application asking which
    kind of device it is. ``docs/drivers.md`` explains how to add a driver.
    """

    def __init__(self) -> None:
        self.tag: object = None
        self._capture_completed_handlers: list[CaptureCompletedHandler] = []
        self._capture_progress_handlers: list[CaptureProgressHandler] = []
        self._capture_tile_handlers: list[CaptureTileHandler] = []

    # ----------------------------------------------------------------- events
    def add_capture_completed_handler(self, handler: CaptureCompletedHandler) -> None:
        if handler not in self._capture_completed_handlers:
            self._capture_completed_handlers.append(handler)

    def remove_capture_completed_handler(self, handler: CaptureCompletedHandler) -> None:
        if handler in self._capture_completed_handlers:
            self._capture_completed_handlers.remove(handler)

    def add_capture_progress_handler(self, handler: CaptureProgressHandler) -> None:
        """Called while a capture streams its samples (DSLogic stream mode)."""
        if handler not in self._capture_progress_handlers:
            self._capture_progress_handlers.append(handler)

    def remove_capture_progress_handler(self, handler: CaptureProgressHandler) -> None:
        if handler in self._capture_progress_handlers:
            self._capture_progress_handlers.remove(handler)

    def add_capture_tile_handler(self, handler: CaptureTileHandler) -> None:
        """Called when parts of a progressively transferred capture arrive."""
        if handler not in self._capture_tile_handlers:
            self._capture_tile_handlers.append(handler)

    def remove_capture_tile_handler(self, handler: CaptureTileHandler) -> None:
        if handler in self._capture_tile_handlers:
            self._capture_tile_handlers.remove(handler)

    def _notifier(self) -> _Notifier:
        notifier = self.__dict__.get("_events")
        if notifier is None:
            notifier = self.__dict__.setdefault("_events", _Notifier(type(self).__name__))
        return notifier

    def wait_for_events(self, timeout: float = 5.0) -> bool:
        """Until the handlers of every event raised so far ran (they run in a thread of the driver)."""
        return self._notifier().wait(timeout)

    def _raise_capture_tile(self, args: CaptureTileArgs) -> None:
        handlers = list(self._capture_tile_handlers)
        self._notifier().put(lambda: [registered(args) for registered in handlers])

    def _raise_capture_progress(self, args: CaptureProgressArgs) -> None:
        if args.arrived is None:
            args.arrived = time.monotonic()
        handlers = list(self._capture_progress_handlers)
        self._notifier().put(lambda: [registered(args) for registered in handlers])

    def _raise_capture_completed(
        self, args: CaptureCompletedArgs, handler: Optional[CaptureCompletedHandler] = None
    ) -> None:
        if args.arrived is None:
            args.arrived = time.monotonic()
        handlers = [handler] if handler is not None else list(self._capture_completed_handlers)
        self._notifier().put(lambda: [registered(args) for registered in handlers])

    # ------------------------------------------------------------- properties
    @property
    def device_version(self) -> Optional[str]:
        raise NotImplementedError

    @property
    def blast_frequency(self) -> int:
        """Rate of the blast mode; 0: the device has none."""
        return 0

    @property
    def max_frequency(self) -> int:
        raise NotImplementedError

    @property
    def min_frequency(self) -> int:
        return (self.max_frequency * 2) // 65535

    @property
    def channel_count(self) -> int:
        raise NotImplementedError

    @property
    def max_loop_count(self) -> int:
        """Highest burst loop count the device accepts (bursts - 1)."""
        return 254

    @property
    def buffer_size(self) -> int:
        raise NotImplementedError

    @property
    def driver_type(self) -> AnalyzerDriverType:
        return AnalyzerDriverType.OTHER

    @property
    def driver_id(self) -> str:
        """Short, stable name of the kind of device; it names the stored capture settings.

        Drivers added later override it (their ``driver_type`` is ``OTHER``).
        """
        return self.driver_type.value.lower()

    @property
    def is_network(self) -> bool:
        """Connected over the network (its power status is polled)."""
        return False

    @property
    def is_capturing(self) -> bool:
        raise NotImplementedError

    # ---------------------------------------------------------------- capture
    def start_capture(
        self, session: CaptureSession, completed_handler: Optional[CaptureCompletedHandler] = None
    ) -> CaptureError:
        raise NotImplementedError

    def stop_capture(self) -> bool:
        raise NotImplementedError

    def enter_bootloader(self) -> bool:
        """Restart into the bootloader (see ``supports_bootloader``)."""
        return False

    def restart(self) -> bool:
        """Restart the device (capability ``RESTART``): outputs are released, what was set on it is
        gone, it answers again on the same connection. ``False`` when it did not (or cannot)."""
        return False

    # ------------------------------------------------------------ device info
    def sample_bits(self, channels: Sequence[int]) -> list[int]:
        """Bit of every channel in the samples of the device (the channel number on most boards)."""
        return list(channels)

    def get_capture_mode(self, channels: Sequence[int]) -> CaptureMode:
        return capture_mode_for_bits(self.sample_bits(channels))

    def get_limits(
        self,
        channels: Sequence[int],
        acquisition_mode: Optional[str] = None,
        *,
        to_disk: bool = False,
        continuous: bool = False,
    ) -> CaptureLimits:
        """Limits of a capture; ``to_disk`` and ``continuous`` matter for streams only."""
        mode = self.get_capture_mode(channels)
        total_samples = self.buffer_size // mode.bytes_per_sample
        return CaptureLimits(
            min_pre_samples=2,
            max_pre_samples=total_samples // 10,
            min_post_samples=2,
            max_post_samples=total_samples - 2,
        )

    def get_device_info(self) -> AnalyzerDeviceInfo:
        limits = [
            self.get_limits(range(0, 8)),
            self.get_limits(range(0, 16)),
            self.get_limits(range(0, 24)),
        ]
        return AnalyzerDeviceInfo(
            name=self.device_version or "Unknown",
            max_frequency=self.max_frequency,
            blast_frequency=self.blast_frequency,
            channels=self.channel_count,
            buffer_size=self.buffer_size,
            mode_limits=limits,
        )

    def acquisition_modes(self) -> tuple[str, ...]:
        """``ACQUISITION_*`` modes the device offers; empty: the only mode is the buffer."""
        return ()

    def max_frequency_for(self, channels: Sequence[int], acquisition_mode: Optional[str] = None) -> int:
        """Highest rate of ``channels`` in ``acquisition_mode`` (a stream is limited by its link)."""
        return self.max_frequency

    def sample_rates(
        self, channels: Sequence[int], acquisition_mode: Optional[str] = None
    ) -> Optional[list[int]]:
        """Fixed sampling rates usable with ``channels``; ``None``: any rate up to the maximum."""
        return None

    def has_external_trigger(self) -> bool:
        """The edge trigger can use an external trigger input (channel ``channel_count``)."""
        return True

    # ---------------------------------------------------------------- network
    def get_voltage_status(self) -> Optional[str]:
        return "UNSUPPORTED"

    def send_network_config(self, access_point: str, password: str, address: str, port: int) -> bool:
        return False

    # --------------------------------------------------------------- triggers
    @property
    def channels_per_device(self) -> int:
        """Channels of one board (a multi device set has several)."""
        return self.channel_count

    def pattern_trigger_groups(self) -> tuple[tuple[int, int], ...]:
        """Channel groups ``(first, count)`` a pattern trigger can cover."""
        return DEFAULT_PATTERN_GROUPS

    def edge_trigger_channels(self) -> list[int]:
        """Channels an edge trigger can use; the external trigger input is ``channel_count``."""
        return list(range(self.channel_count))

    # ------------------------------------------------------------ board test
    # ---------------------------------------------------------------- analog
    @property
    def analog_channel_count(self) -> int:
        """Analog inputs of the device (``ANALOG=<n>``)."""
        value = capability_value(self.capabilities(), CAPABILITY_ANALOG)
        try:
            return int(value) if value else 0
        except ValueError:
            return 0

    def analog_channel_names(self) -> list[str]:
        return [f"A{index}" for index in range(self.analog_channel_count)]

    def memory_depth(self, digital: int, analog: int) -> Optional[int]:
        """Samples a buffer capture can hold with ``digital`` and ``analog`` channels (``None``:
        the limits of :meth:`get_limits` alone)."""
        return None

    # ------------------------------------------------------------ state mode
    def supports_state_mode(self) -> bool:
        return CAPABILITY_STATE_MODE in self.capabilities()

    def state_clock_channels(self) -> list[int]:
        """Channels that can clock the state mode (pins with ``CLOCK`` or ``CLOCK_SW``)."""
        return list(range(self.channel_count)) if self.supports_state_mode() else []

    def supports_stream_state(self) -> bool:
        """State mode on a stream (every clock edge sent at once, ``STREAM_STATE``)."""
        return CAPABILITY_STREAM_STATE in self.capabilities()

    # ------------------------------------------------------ progressive transfer
    def prioritize(self, session: CaptureSession, first: int, last: int) -> None:
        """The display shows ``[first, last)`` of a progressively transferred capture: fetch it first."""
        progressive = getattr(session, "progressive", None)
        if progressive is not None:
            progressive.prioritize(first, last)

    def resume_transfer(self, session: CaptureSession) -> bool:
        """Continue an interrupted progressive transfer; ``False`` when the device cannot."""
        return False

    # ----------------------------------------------------------- capabilities
    def capabilities(self) -> frozenset[str]:
        """Optional functions (``CAPABILITY_*``) the device supports."""
        return frozenset()

    def run_self_test(self) -> list[SelfTestResult]:
        raise UnsupportedFeatureError("This device has no self-test.")

    def device_details(self) -> dict[str, str]:
        """Board and firmware build details reported by the firmware (may be empty)."""
        return {}

    @property
    def has_self_test(self) -> bool:
        """The driver offers :meth:`run_self_test` (it may still find the firmware lacks it)."""
        return False

    @property
    def self_test_description(self) -> str:
        """What :meth:`run_self_test` checks, shown above the results."""
        return GENERIC_SELF_TEST_DESCRIPTION

    def describe(self) -> list[DeviceSection]:
        """Board, firmware and connection of the device for the *Device information* dialog.

        The capture limits are added by the application.
        """
        return [("Device", [("Identification", self.device_version or "-")])]

    # --------------------------------------------------------------- features
    @property
    def address(self) -> Optional[str]:
        """Where the device is, as :func:`~.discovery.open_device` takes it (``pico:/dev/cu.usbmodem1``,
        ``pico:192.168.1.5:4045``); ``None`` when the driver cannot say."""
        return None

    #: a simulator standing in for a device (``sim:``); new settings capture at once there
    is_simulator = False

    @property
    def is_hardware(self) -> bool:
        """A real device; ``False`` for the emulated driver behind loaded and computed captures."""
        return True

    def boards(self) -> list["AnalyzerDriverBase"]:
        """The single devices this driver captures with (several for a multi device set)."""
        return [self]

    @property
    def board_count(self) -> int:
        return len(self.boards())

    @property
    def supports_bootloader(self) -> bool:
        """:meth:`enter_bootloader` restarts the device for a firmware update."""
        return False

    @property
    def supports_network_config(self) -> bool:
        """:meth:`send_network_config` stores WiFi settings on the device."""
        return False

    # ---------------------------------------------------------------- cleanup
    def dispose(self) -> None:
        self._capture_completed_handlers.clear()
        self._capture_progress_handlers.clear()
        self._capture_tile_handlers.clear()

    def __enter__(self) -> "AnalyzerDriverBase":
        return self

    def __exit__(self, *exc_info) -> None:
        self.dispose()
