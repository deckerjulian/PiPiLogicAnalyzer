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

import struct
from dataclasses import dataclass, field
from typing import Sequence

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

CMD_ABORT_CAPTURE = 0xFF

@dataclass(frozen=True)
class RequestLayout:
    """Binary layout of ``CAPTURE_REQUEST`` for one firmware generation."""

    format: str
    channels: int
    max_loop_count: int

    @property
    def size(self) -> int:
        return struct.calcsize(self.format)


#: Firmware V6_0: 24 channels, 8 bit loop count -- 48 bytes.
LAYOUT_V6_0 = RequestLayout("<BBBxH24sBxIIIBBBx", channels=24, max_loop_count=254)
#: Firmware V6_5 and newer: 32 channels, 16 bit loop count -- 56 bytes.
LAYOUT_V6_5 = RequestLayout("<BBBxH32sBxIIIHBB", channels=32, max_loop_count=65534)

CAPTURE_REQUEST_SIZE = LAYOUT_V6_0.size

#: ``WIFI_SETTINGS_REQUEST``: 116 bytes.
NET_CONFIG_FORMAT = "<33s64s16sxH"
NET_CONFIG_SIZE = struct.calcsize(NET_CONFIG_FORMAT)

def layout_for_version(major: int, minor: int) -> RequestLayout:
    """Request layout understood by a device reporting ``V<major>_<minor>``."""
    return LAYOUT_V6_5 if (major, minor) >= (6, 5) else LAYOUT_V6_0


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

    def pack(self, layout: RequestLayout = LAYOUT_V6_0) -> bytes:
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
