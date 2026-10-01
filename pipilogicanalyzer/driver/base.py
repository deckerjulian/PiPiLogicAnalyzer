# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of PiPiLogicAnalyzer, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Common driver infrastructure (port of ``SharedDriver/AnalyzerDriverBase.cs``)."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import TYPE_CHECKING, Callable, Optional, Sequence

from ..core.sample_store import disk_sample_bytes
from .models import CaptureSession, TriggerType

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
        self._thread = threading.Thread(target=self._run, name="pipilogicanalyzer-disk-watch", daemon=True)
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

    @property
    def sample_count(self) -> int:
        return min((len(samples) for samples in self.samples.values()), default=0)


CaptureProgressHandler = Callable[[CaptureProgressArgs], None]


#: A titled group of ``(property, value)`` rows describing a device
DeviceSection = tuple[str, list[tuple[str, str]]]

GENERIC_SELF_TEST_DESCRIPTION = "The test checks the device with the functions its driver provides."


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

    def _raise_capture_progress(self, args: CaptureProgressArgs) -> None:
        for registered in list(self._capture_progress_handlers):
            registered(args)

    def _raise_capture_completed(
        self, args: CaptureCompletedArgs, handler: Optional[CaptureCompletedHandler] = None
    ) -> None:
        if handler is not None:
            handler(args)
            return
        for registered in list(self._capture_completed_handlers):
            registered(args)

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

    def __enter__(self) -> "AnalyzerDriverBase":
        return self

    def __exit__(self, *exc_info) -> None:
        self.dispose()
