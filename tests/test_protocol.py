"""Wire protocol tests: the bytes must match what the firmware expects."""

from __future__ import annotations

import struct

import pytest

from pipilogicanalyzer.driver import protocol


def test_frame_escapes_reserved_bytes():
    packet = protocol.build_packet(bytes([0x01, 0x55, 0xAA, 0xF0, 0x02]))
    assert packet == bytes(
        [0x55, 0xAA, 0x01, 0xF0, 0xA5, 0xF0, 0x5A, 0xF0, 0x00, 0x02, 0xAA, 0x55]
    )


def test_command_packet_prefixes_the_command():
    assert protocol.command_packet(protocol.CMD_GET_ID) == b"\x55\xAA\x00\xAA\x55"


def test_capture_request_matches_the_firmware_struct():
    # CAPTURE_REQUEST is 48 bytes with natural alignment on the RP2040.
    assert protocol.CAPTURE_REQUEST_SIZE == 48

    request = protocol.CaptureRequest(
        trigger_type=1,
        trigger=2,
        inverted_or_count=3,
        trigger_value=0x1234,
        channels=[0, 1, 2, 3],
        channel_count=4,
        frequency=100_000_000,
        pre_samples=512,
        post_samples=1024,
        loop_count=5,
        measure=1,
        capture_mode=2,
    )
    raw = request.pack()
    assert len(raw) == 48

    assert raw[0:3] == bytes([1, 2, 3])
    assert struct.unpack_from("<H", raw, 4)[0] == 0x1234
    assert raw[6:10] == bytes([0, 1, 2, 3])
    assert raw[30] == 4
    assert struct.unpack_from("<III", raw, 32) == (100_000_000, 512, 1024)
    assert raw[44:47] == bytes([5, 1, 2])


def test_capture_request_truncates_extra_channels():
    request = protocol.CaptureRequest(channels=list(range(40)), channel_count=40)
    raw = request.pack()
    assert raw[6:30] == bytes(range(24))


def test_net_config_layout():
    assert protocol.NET_CONFIG_SIZE == 116
    raw = protocol.pack_net_config("ap", "secret", "192.168.1.5", 24000)
    assert raw[:2] == b"ap"
    assert raw[33:39] == b"secret"
    assert raw[97:108] == b"192.168.1.5"
    assert struct.unpack_from("<H", raw, 114)[0] == 24000


@pytest.mark.parametrize(
    "access_point,password,address",
    [("a" * 64, "b" * 128, "1" * 32)],
)
def test_net_config_truncates_long_strings(access_point, password, address):
    raw = protocol.pack_net_config(access_point, password, address, 1)
    assert len(raw) == protocol.NET_CONFIG_SIZE
    # Each field keeps room for the NUL terminator the firmware expects.
    assert raw[32] == 0
    assert raw[33 + 63] == 0
    assert raw[97 + 15] == 0
