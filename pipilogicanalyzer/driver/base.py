# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of PiPiLogicAnalyzer, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Common driver infrastructure (port of ``SharedDriver/AnalyzerDriverBase.cs``)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Callable, Optional, Sequence

from .models import CaptureSession, TriggerType

#: Delays (in device clock cycles) introduced by the trigger PIO programs.
COMPLEX_TRIGGER_DELAY = 5.0
FAST_TRIGGER_DELAY = 3.0
#: The edge trigger with trigger output waits in a loop of one or two instructions, like the fast trigger.
EDGE_OUT_TRIGGER_DELAY = 3.0

#: Burst measurement limits (firmware V6_5 ``StartCaptureSimple``): the
#: timestamp block is sized by one byte and each burst needs enough samples.
MAX_MEASURED_LOOP_COUNT = 253
MIN_MEASURED_POST_SAMPLES = 100

#: Minimum firmware version supported by this client.
MIN_MAJOR_VERSION = 6
MIN_MINOR_VERSION = 0

_VERSION_RE = re.compile(r".*?V([0-9]+)_([0-9]+)$")


class DeviceConnectionError(Exception):
    """Raised when a device cannot be opened or reports an invalid identity."""


class UnsupportedFeatureError(Exception):
    """Raised when the device (or its firmware) lacks an optional function."""


#: Optional firmware functions, reported by ``CMD_CAPABILITIES``.
CAPABILITY_SELF_TEST = "SELFTEST"
CAPABILITY_SIMULATION = "SIMULATION"
CAPABILITY_DEVICE_INFO = "DEVICEINFO"
#: The firmware can drive the trigger output on an edge (trigger type ``EDGE_OUT``).
CAPABILITY_EDGE_TRIGGER_OUT = "EDGE_TRIGGER_OUT"
#: Prefix of the capability naming the channel groups a pattern trigger can cover:
#: ``PATTERN_GROUPS=0-20/21-23`` (channels on consecutive GPIOs, counted from 0).
CAPABILITY_PATTERN_GROUPS = "PATTERN_GROUPS="

#: Pattern trigger channels of firmware that does not report its groups: channels 1 to 16.
DEFAULT_PATTERN_GROUPS: tuple[tuple[int, int], ...] = ((0, 16),)


def parse_pattern_groups(capabilities: frozenset[str]) -> Optional[tuple[tuple[int, int], ...]]:
    """``(first channel, channel count)`` of every group of the ``PATTERN_GROUPS`` capability."""
    for item in capabilities:
        if not item.startswith(CAPABILITY_PATTERN_GROUPS):
            continue
        groups = []
        for part in item[len(CAPABILITY_PATTERN_GROUPS):].split("/"):
            first, _, last = part.partition("-")
            try:
                first_channel, last_channel = int(first), int(last or first)
            except ValueError:
                return None
            if 0 <= first_channel <= last_channel:
                groups.append((first_channel, last_channel - first_channel + 1))
        return tuple(groups) or None
    return None


def pattern_max_bits(trigger_type: TriggerType) -> int:
    """Longest pattern of a trigger type: 16 bits, 5 for the fast trigger (a 32 entry jump table)."""
    return 5 if trigger_type == TriggerType.FAST else 16


def pattern_fits(groups: Sequence[tuple[int, int]], first_channel: int, bit_count: int) -> bool:
    """Whether ``bit_count`` channels from ``first_channel`` lie inside one group of trigger inputs."""
    return bit_count >= 1 and any(
        first <= first_channel and first_channel + bit_count <= first + count for first, count in groups
    )


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
        return self.item.replace("_", " ").capitalize()

    @property
    def severity(self) -> str:
        """``ok``, ``fail``, ``info`` or ``warning`` (e.g. a stuck input)."""
        return {"OK": "ok", "FAIL": "fail", "INFO": "info", "SKIPPED": "info"}.get(
            self.status, "warning"
        )


def parse_self_test_line(line: str) -> Optional[SelfTestResult]:
    if not line.startswith("SELFTEST:"):
        return None
    parts = line.strip().split(":", 3)
    if len(parts) < 3:
        return None
    return SelfTestResult(
        item=parts[1], status=parts[2].strip().upper(), detail=parts[3].strip() if len(parts) > 3 else ""
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


@dataclass
class DeviceVersion:
    major: int = 0
    minor: int = 0
    is_valid: bool = False


def parse_version(device_version: Optional[str]) -> DeviceVersion:
    match = _VERSION_RE.match(device_version or "")
    if not match:
        return DeviceVersion()
    major = int(match.group(1))
    minor = int(match.group(2))
    valid = major > MIN_MAJOR_VERSION or (major == MIN_MAJOR_VERSION and minor >= MIN_MINOR_VERSION)
    return DeviceVersion(major=major, minor=minor, is_valid=valid)


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


CaptureCompletedHandler = Callable[[CaptureCompletedArgs], None]


class AnalyzerDriverBase:
    """Base class shared by every driver implementation."""

    def __init__(self) -> None:
        self.tag: object = None
        self._capture_completed_handlers: list[CaptureCompletedHandler] = []

    # ----------------------------------------------------------------- events
    def add_capture_completed_handler(self, handler: CaptureCompletedHandler) -> None:
        if handler not in self._capture_completed_handlers:
            self._capture_completed_handlers.append(handler)

    def remove_capture_completed_handler(self, handler: CaptureCompletedHandler) -> None:
        if handler in self._capture_completed_handlers:
            self._capture_completed_handlers.remove(handler)

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
        raise NotImplementedError

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
        raise NotImplementedError

    @property
    def is_network(self) -> bool:
        raise NotImplementedError

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
        raise NotImplementedError

    # ------------------------------------------------------------ device info
    def get_capture_mode(self, channels: Sequence[int]) -> CaptureMode:
        max_channel = max(channels) if channels else 0
        if max_channel < 8:
            return CaptureMode.CHANNELS_8
        if max_channel < 16:
            return CaptureMode.CHANNELS_16
        return CaptureMode.CHANNELS_24

    def get_limits(self, channels: Sequence[int]) -> CaptureLimits:
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

    # ---------------------------------------------------------------- cleanup
    def dispose(self) -> None:
        self._capture_completed_handlers.clear()

    def __enter__(self) -> "AnalyzerDriverBase":
        return self

    def __exit__(self, *exc_info) -> None:
        self.dispose()
