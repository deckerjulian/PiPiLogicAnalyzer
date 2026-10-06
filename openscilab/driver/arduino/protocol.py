# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The wire protocol of the openSciLab Arduino firmware (``docs/protocols.md``, *Arduino firmware*).

Frames are COBS-encoded payloads ended by ``0x00``: ``COBS(type, sequence, payload, crc8)``.
Everything here is plain Python without I/O, shared by the driver and the simulated Arduino.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Iterable, Optional

PROTOCOL = 1

REQUEST = 0x01
ANSWER = 0x02
EVENT = 0x03
ERROR = 0x04

#: payload bytes of one frame at most (type, sequence, command and data; without the CRC)
MAX_PAYLOAD = 250

# commands
HELLO = 0x01
PINS = 0x02
CAPS = 0x03
PIN_MODE = 0x10
WRITE = 0x11
READ = 0x12
PWM = 0x13
DAC = 0x14
PULSE = 0x15
ADC_READ = 0x16
MONITOR = 0x17
HEARTBEAT = 0x18
SAFE = 0x19
CAPTURE_SETUP = 0x20
CAPTURE_START = 0x21
CAPTURE_ABORT = 0x22
GEN_LOAD = 0x30
GEN_START = 0x31
GEN_STOP = 0x32
GEN_STATUS = 0x33
SQUARE = 0x34
ARB_LOAD = 0x35
ARB_START = 0x36
TX_UART = 0x40
TX_SPI = 0x41
TX_I2C = 0x42

# events
EVENT_STATE = 0x01
EVENT_DATA = 0x02
EVENT_DONE = 0x03
EVENT_WATCHDOG = 0x04

# kinds of DATA events
DATA_DIGITAL = 0
DATA_ANALOG = 1
DATA_STATE = 2

# capture modes
MODE_STREAM = 0
MODE_BUFFER = 1
MODE_STATE = 2

# DONE status
DONE_COMPLETE = 0
DONE_OVERFLOW = 1
DONE_STOPPED = 2

# error codes
ERR_UNKNOWN = 1
ERR_ARGUMENTS = 2
ERR_RESERVED = 3
ERR_BUSY = 4
ERR_UNSUPPORTED = 5
ERR_NACK = 6
ERR_OVERFLOW = 7
ERROR_TEXTS = {ERR_UNKNOWN: "unknown command", ERR_ARGUMENTS: "bad arguments", ERR_RESERVED: "the pin is reserved",
               ERR_BUSY: "busy", ERR_UNSUPPORTED: "not supported by this board", ERR_NACK: "no acknowledge",
               ERR_OVERFLOW: "overflow"}

#: pin modes, as the Pico's command 12
MODE_CODES = {"input": 0, "input_pullup": 1, "input_pulldown": 2, "output": 3, "pwm": 4, "analog": 5}


class ProtocolError(ValueError):
    """A frame that cannot be read (damaged on the line)."""


def crc8(data: bytes) -> int:
    """CRC-8, polynomial 0x07, initial value 0, not reflected."""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def cobs_encode(data: bytes) -> bytes:
    """Consistent Overhead Byte Stuffing: no zero byte in the result (the frame end)."""
    out = bytearray([0])
    code_index, code = 0, 1
    for byte in data:
        if byte == 0:
            out[code_index] = code
            code_index, code = len(out), 1
            out.append(0)
            continue
        out.append(byte)
        code += 1
        if code == 0xFF:
            out[code_index] = code
            code_index, code = len(out), 1
            out.append(0)
    out[code_index] = code
    return bytes(out)


def cobs_decode(data: bytes) -> bytes:
    out = bytearray()
    index = 0
    while index < len(data):
        code = data[index]
        if code == 0:
            raise ProtocolError("a zero byte inside a frame")
        index += 1
        block = data[index:index + code - 1]
        if len(block) != code - 1:
            raise ProtocolError("a frame ends inside a block")
        out += block
        index += code - 1
        if code != 0xFF and index < len(data):
            out.append(0)
    return bytes(out)


@dataclass(frozen=True)
class Frame:
    type: int
    sequence: int
    payload: bytes

    @property
    def command(self) -> int:
        """The command (requests, answers, errors) or the event code (events)."""
        return self.payload[0] if self.payload else 0

    @property
    def data(self) -> bytes:
        return self.payload[1:]


def encode(frame_type: int, sequence: int, payload: bytes) -> bytes:
    """One frame on the wire, ended by its zero byte."""
    body = bytes([frame_type & 0xFF, sequence & 0xFF]) + bytes(payload)
    if len(body) > MAX_PAYLOAD + 2:
        raise ValueError(f"a frame holds at most {MAX_PAYLOAD} payload bytes")
    return cobs_encode(body + bytes([crc8(body)])) + b"\x00"


def decode(encoded: bytes) -> Frame:
    """A frame from the bytes between two zero bytes (without them)."""
    body = cobs_decode(encoded)
    if len(body) < 3:
        raise ProtocolError("a frame is too short")
    if crc8(body[:-1]) != body[-1]:
        raise ProtocolError("the checksum of a frame does not fit")
    return Frame(body[0], body[1], bytes(body[2:-1]))


class Decoder:
    """Collects frames from the bytes of the line as they arrive."""

    def __init__(self) -> None:
        self._pending = bytearray()
        #: frames that could not be read (damaged on the line)
        self.damaged = 0

    def feed(self, data: bytes) -> list[Frame]:
        frames = []
        self._pending += data
        while True:
            end = self._pending.find(0)
            if end < 0:
                break
            encoded = bytes(self._pending[:end])
            del self._pending[:end + 1]
            if not encoded:
                continue
            try:
                frames.append(decode(encoded))
            except ProtocolError:
                self.damaged += 1
        return frames


# --------------------------------------------------------------- payloads
def request(command: int, data: bytes = b"") -> bytes:
    return bytes([command]) + bytes(data)


def parse_text(data: bytes) -> dict[str, str]:
    """``key=value;key=value`` (the answer of HELLO)."""
    result = {}
    for item in data.decode("ascii", "replace").split(";"):
        key, sep, value = item.partition("=")
        if sep:
            result[key.strip()] = value.strip()
    return result


def runs(levels: Iterable[int]) -> list[tuple[int, int]]:
    """Run lengths ``(count, level)`` of samples, at most 65535 each (the pattern generator)."""
    result: list[tuple[int, int]] = []
    for level in levels:
        level = int(level)
        if result and result[-1][1] == level and result[-1][0] < 0xFFFF:
            result[-1] = (result[-1][0] + 1, level)
        else:
            result.append((1, level))
    return result


def pack_runs(items: Iterable[tuple[int, int]]) -> bytes:
    return b"".join(struct.pack("<HI", count, level) for count, level in items)


def unpack_runs(data: bytes) -> list[tuple[int, int]]:
    return [struct.unpack_from("<HI", data, offset) for offset in range(0, len(data) - len(data) % 6, 6)]


def error_text(frame: Frame) -> str:
    code = frame.data[0] if frame.data else 0
    text = frame.data[1:].decode("utf-8", "replace") if len(frame.data) > 1 else ""
    return text or ERROR_TEXTS.get(code, f"error {code}")


def error_code(frame: Frame) -> Optional[int]:
    return frame.data[0] if frame.data else None
