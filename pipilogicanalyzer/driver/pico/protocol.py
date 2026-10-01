# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of PiPiLogicAnalyzer, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Wire protocol helpers for the PiPiLogicAnalyzer firmware.

The firmware expects framed packets::

    0x55 0xAA <escaped payload> 0xAA 0x55

where the bytes ``0x55``, ``0xAA`` and ``0xF0`` are escaped inside the payload
as ``0xF0 (byte ^ 0xF0)``.

The structures below mirror ``CAPTURE_REQUEST`` and ``WIFI_SETTINGS_REQUEST``
from ``Firmware/LogicAnalyzer_V2/LogicAnalyzer_Structs.h`` (natural alignment,
little endian), so the exact same bytes the C# client produced are emitted.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from typing import Optional, Sequence

from ..base import (
    CAPABILITY_PATTERN_GROUPS,
    CAPABILITY_TRIGGER_CONDITIONS,
    CAPABILITY_TRIGGER_SEQUENCE,
    SelfTestResult,
)

FRAME_START = b"\x55\xAA"
FRAME_END = b"\xAA\x55"
ESCAPE = 0xF0
_ESCAPED_BYTES = (0xAA, 0x55, 0xF0)

# Commands understood by the firmware.
CMD_GET_ID = 0
CMD_START_CAPTURE = 1
CMD_SET_WIFI = 2
CMD_GET_VOLTAGE = 3
CMD_ENTER_BOOTLOADER = 4
CMD_BLINK_ON = 5
CMD_BLINK_OFF = 6
# Extensions of this project's firmware (the original answers ERR_UNKNOWN_MSG).
CMD_SELF_TEST = 7
CMD_CAPABILITIES = 8
CMD_DEVICE_INFO = 9
#: Trigger sequence and state mode of the next capture request with ``SEQUENCE_TRIGGER_TYPE``
#: (answered ``SEQUENCE_OK`` or ``SEQUENCE_ERROR``)
CMD_TRIGGER_SEQUENCE = 10

CMD_ABORT_CAPTURE = 0xFF

@dataclass(frozen=True)
class RequestLayout:
    """Binary layout of ``CAPTURE_REQUEST``."""

    format: str
    channels: int
    max_loop_count: int

    @property
    def size(self) -> int:
        return struct.calcsize(self.format)


#: 32 channels, 16 bit loop count -- 56 bytes (the layout of V6_5).
REQUEST_LAYOUT = RequestLayout("<BBBxH32sBxIIIHBB", channels=32, max_loop_count=65534)

CAPTURE_REQUEST_SIZE = REQUEST_LAYOUT.size

#: Protocol of the firmware this application works with (``PROTOCOL:<n>`` after the
#: identification, ``FIRMWARE_PROTOCOL`` of firmware/PiPiLogicAnalyzer/CMakeLists.txt). Boards with
#: another protocol, or older firmware without one, have to be updated.
FIRMWARE_PROTOCOL = 1

#: ``WIFI_SETTINGS_REQUEST``: 116 bytes.
NET_CONFIG_FORMAT = "<33s64s16sxH"
NET_CONFIG_SIZE = struct.calcsize(NET_CONFIG_FORMAT)

# ------------------------------------------------------------ trigger sequences
#: Trigger type of a capture request using the configuration of ``CMD_TRIGGER_SEQUENCE``
#: (``TriggerType.SEQUENCE``): a trigger sequence, or the state mode with or without one
SEQUENCE_TRIGGER_TYPE = 7
SEQUENCE_FORMAT_VERSION = 1
SEQUENCE_FLAG_STATE_MODE = 0x01
SEQUENCE_FLAG_CLOCK_FALLING = 0x02
#: ``max_samples`` / ``within_samples`` without a limit
SEQUENCE_NO_LIMIT = 0xFFFFFFFF
#: Longest time of a stage in samples (the firmware compares sample numbers in 32 bits)
SEQUENCE_MAX_SAMPLES = 0x7FFFFFFF
#: Header (version, flags, clock channel, stage count) and one stage of ``CMD_TRIGGER_SEQUENCE``
SEQUENCE_HEADER_FORMAT = "<BBBB"
SEQUENCE_STAGE_FORMAT = "<BBBxIIIIII"
SEQUENCE_STAGE_SIZE = struct.calcsize(SEQUENCE_STAGE_FORMAT)

#: Condition kinds and edges on the wire
SEQUENCE_KINDS = {"pattern": 0, "edge": 1, "pulse": 2, "gap": 3}
SEQUENCE_EDGES = {"rising": 0, "falling": 1, "any": 2}

#: Further capabilities of the firmware of this project: highest sample rate of a capture with a
#: trigger sequence (the evaluation keeps up with it in the worst case) and highest clock of the
#: state mode
CAPABILITY_SEQUENCE_MAX_RATE = "SEQUENCE_MAX_RATE="
CAPABILITY_STATE_MAX_CLOCK = "STATE_MAX_CLOCK="


@dataclass
class SequenceStage:
    """One stage of ``CMD_TRIGGER_SEQUENCE``; channels are channel numbers, times are samples."""

    kind: int = 0
    channel: int = 0
    edge: int = 0
    #: PATTERN: bit n = channel n
    mask: int = 0
    value: int = 0
    min_samples: int = 0
    max_samples: int = SEQUENCE_NO_LIMIT
    count: int = 1
    within_samples: int = SEQUENCE_NO_LIMIT

    def pack(self) -> bytes:
        return struct.pack(
            SEQUENCE_STAGE_FORMAT,
            self.kind & 0xFF,
            self.channel & 0xFF,
            self.edge & 0xFF,
            self.mask & 0xFFFFFFFF,
            self.value & 0xFFFFFFFF,
            self.min_samples & 0xFFFFFFFF,
            self.max_samples & 0xFFFFFFFF,
            self.count & 0xFFFFFFFF,
            self.within_samples & 0xFFFFFFFF,
        )


@dataclass
class SequenceRequest:
    """Payload of ``CMD_TRIGGER_SEQUENCE``."""

    stages: list[SequenceStage] = field(default_factory=list)
    state_mode: bool = False
    clock_channel: int = 0
    clock_falling: bool = False

    def pack(self) -> bytes:
        flags = (SEQUENCE_FLAG_STATE_MODE if self.state_mode else 0) | (
            SEQUENCE_FLAG_CLOCK_FALLING if self.clock_falling else 0
        )
        header = struct.pack(
            SEQUENCE_HEADER_FORMAT,
            SEQUENCE_FORMAT_VERSION,
            flags,
            self.clock_channel & 0xFF if self.state_mode else 0,
            len(self.stages),
        )
        return header + b"".join(stage.pack() for stage in self.stages)


def _capability_int(capabilities: frozenset[str], prefix: str) -> Optional[int]:
    for item in capabilities:
        if item.startswith(prefix):
            try:
                return int(item[len(prefix):])
            except ValueError:
                return None
    return None


def parse_trigger_sequence(capabilities: frozenset[str]) -> Optional[tuple[int, frozenset[str], Optional[int]]]:
    """Stages, condition kinds and highest sample rate (``None``: not reported) of the
    ``TRIGGER_SEQUENCE=<n>``, ``TRIGGER_CONDITIONS=`` and ``SEQUENCE_MAX_RATE=`` capabilities;
    ``None`` without trigger sequences."""
    stages = _capability_int(capabilities, CAPABILITY_TRIGGER_SEQUENCE + "=")
    if stages is None or stages < 1:
        return None
    kinds: frozenset[str] = frozenset()
    for item in capabilities:
        if item.startswith(CAPABILITY_TRIGGER_CONDITIONS):
            kinds = frozenset(
                kind.strip().lower() for kind in item[len(CAPABILITY_TRIGGER_CONDITIONS):].split("/") if kind.strip()
            )
    kinds &= frozenset(SEQUENCE_KINDS)
    if not kinds:
        return None
    rate = _capability_int(capabilities, CAPABILITY_SEQUENCE_MAX_RATE)
    return stages, kinds, rate if rate is None or rate > 0 else None


def parse_state_max_clock(capabilities: frozenset[str]) -> Optional[int]:
    """Highest clock frequency of the state mode (``STATE_MAX_CLOCK=<Hz>``), ``None`` if not reported."""
    return _capability_int(capabilities, CAPABILITY_STATE_MAX_CLOCK)


def ns_to_samples(nanoseconds: int, frequency: int, round_up: bool) -> int:
    """Samples covering ``nanoseconds`` at ``frequency`` (integer arithmetic, no float rounding)."""
    product = int(nanoseconds) * int(frequency)
    if round_up:
        return -(-product // 1_000_000_000)
    return product // 1_000_000_000


def escape_payload(payload: bytes) -> bytes:
    """Escape ``payload`` as the firmware's framing expects."""
    out = bytearray()
    for byte in payload:
        if byte in _ESCAPED_BYTES:
            out.append(ESCAPE)
            out.append(byte ^ ESCAPE)
        else:
            out.append(byte)
    return bytes(out)


def build_packet(payload: bytes) -> bytes:
    """Wrap ``payload`` into a complete framed packet."""
    return FRAME_START + escape_payload(payload) + FRAME_END


def command_packet(command: int, payload: bytes = b"") -> bytes:
    return build_packet(bytes([command]) + payload)


@dataclass
class CaptureRequest:
    """Binary capture request sent to the device."""

    trigger_type: int = 0
    trigger: int = 0
    inverted_or_count: int = 0
    trigger_value: int = 0
    channels: Sequence[int] = field(default_factory=list)
    channel_count: int = 0
    frequency: int = 0
    pre_samples: int = 0
    post_samples: int = 0
    loop_count: int = 0
    measure: int = 0
    capture_mode: int = 0

    def pack(self, layout: RequestLayout = REQUEST_LAYOUT) -> bytes:
        channel_bytes = bytearray(layout.channels)
        for index, channel in enumerate(self.channels[: layout.channels]):
            channel_bytes[index] = channel & 0xFF
        loop_mask = 0xFFFF if layout.max_loop_count > 0xFF else 0xFF
        return struct.pack(
            layout.format,
            self.trigger_type & 0xFF,
            self.trigger & 0xFF,
            self.inverted_or_count & 0xFF,
            self.trigger_value & 0xFFFF,
            bytes(channel_bytes),
            self.channel_count & 0xFF,
            self.frequency & 0xFFFFFFFF,
            self.pre_samples & 0xFFFFFFFF,
            self.post_samples & 0xFFFFFFFF,
            self.loop_count & loop_mask,
            self.measure & 0xFF,
            self.capture_mode & 0xFF,
        )


def pack_net_config(access_point: str, password: str, address: str, port: int) -> bytes:
    """Pack a ``WIFI_SETTINGS_REQUEST``.

    The firmware copies fixed size buffers, so the strings are truncated to
    their maximum length (leaving room for the NUL terminator) instead of
    silently overflowing as the original client did.
    """
    return struct.pack(
        NET_CONFIG_FORMAT,
        access_point.encode("ascii", "ignore")[:32],
        password.encode("ascii", "ignore")[:63],
        address.encode("ascii", "ignore")[:15],
        port & 0xFFFF,
    )


# ------------------------------------------------------------ firmware facts
#: Delays (in device clock cycles) introduced by the trigger PIO programs.
COMPLEX_TRIGGER_DELAY = 5.0
FAST_TRIGGER_DELAY = 3.0
#: The edge trigger with trigger output waits in a loop of one or two instructions, like the fast trigger.
EDGE_OUT_TRIGGER_DELAY = 3.0


def trigger_delay_samples(delay: float, max_frequency: int, frequency: int) -> int:
    """Samples a trigger lagging ``delay`` device clock cycles covers at ``frequency``."""
    delay_ns = 1_000_000_000.0 / max_frequency * delay
    sample_period_ns = 1_000_000_000.0 / frequency
    return int(round((delay_ns / sample_period_ns) + 0.3))


_VERSION_RE = re.compile(r".*?V([0-9]+)_([0-9]+)$")


@dataclass
class DeviceVersion:
    major: int = 0
    minor: int = 0


def parse_version(device_version: Optional[str]) -> DeviceVersion:
    match = _VERSION_RE.match(device_version or "")
    if not match:
        return DeviceVersion()
    return DeviceVersion(major=int(match.group(1)), minor=int(match.group(2)))


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


def parse_self_test_line(line: str) -> Optional[SelfTestResult]:
    if not line.startswith("SELFTEST:"):
        return None
    parts = line.strip().split(":", 3)
    if len(parts) < 3:
        return None
    return SelfTestResult(
        item=parts[1], status=parts[2].strip().upper(), detail=parts[3].strip() if len(parts) > 3 else ""
    )
