# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The compression of the bridge app's tiles (``docs/protocols.md``, *Bridge*).

Digital channels (``D``, 16 bits per sample): runs of a varint count and the 16 bit levels.
Analog channels (raw 8 bit samples): the first sample, then zigzag varint differences. The app
(Kotlin) and this module share the test vectors in ``tests/fixtures/bridge_codec.json``.
"""

from __future__ import annotations

import numpy as np


def varint(value: int) -> bytes:
    """LEB128: 7 bits per byte, the lowest first, the top bit set while more follow."""
    if value < 0:
        raise ValueError("a varint is not negative")
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        if offset >= len(data):
            raise ValueError("a varint is cut off")
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7


def zigzag(value: int) -> int:
    return (value << 1) ^ (value >> 63) if value < 0 else value << 1


def unzigzag(value: int) -> int:
    return (value >> 1) ^ -(value & 1)


def encode_digital(levels: np.ndarray) -> bytes:
    levels = np.asarray(levels, dtype=np.uint16)
    if not len(levels):
        return b""
    starts = np.concatenate(([0], np.flatnonzero(levels[1:] != levels[:-1]) + 1))
    counts = np.diff(np.concatenate((starts, [len(levels)])))
    return b"".join(varint(int(count)) + int(levels[start]).to_bytes(2, "little")
                    for start, count in zip(starts, counts))


def decode_digital(data: bytes) -> np.ndarray:
    parts = []
    offset = 0
    while offset < len(data):
        count, offset = read_varint(data, offset)
        if offset + 2 > len(data):
            raise ValueError("a run is cut off")
        parts.append(np.full(count, int.from_bytes(data[offset:offset + 2], "little"), dtype=np.uint16))
        offset += 2
    return np.concatenate(parts) if parts else np.zeros(0, dtype=np.uint16)


def encode_analog(samples: np.ndarray) -> bytes:
    samples = np.asarray(samples, dtype=np.int64)
    if not len(samples):
        return b""
    out = bytearray([int(samples[0]) & 0xFF])
    for difference in np.diff(samples).tolist():
        out += varint(zigzag(int(difference)))
    return bytes(out)


def decode_analog(data: bytes) -> np.ndarray:
    if not data:
        return np.zeros(0, dtype=np.uint8)
    values = [data[0]]
    offset = 1
    while offset < len(data):
        encoded, offset = read_varint(data, offset)
        values.append(values[-1] + unzigzag(encoded))
    return np.asarray(values, dtype=np.int64).astype(np.uint8)


def overview_analog(samples: np.ndarray, level: int) -> np.ndarray:
    """``(min, max)`` per bucket of 2^level samples (the reference of ``:BRIDge:OVERview?``)."""
    samples = np.asarray(samples, dtype=np.uint8)
    size = 1 << level
    whole = len(samples) // size * size
    blocks = [samples[:whole].reshape(-1, size)] if whole else []
    lows = [blocks[0].min(axis=1)] if blocks else []
    highs = [blocks[0].max(axis=1)] if blocks else []
    if whole < len(samples):
        lows.append(samples[whole:].min(keepdims=True))
        highs.append(samples[whole:].max(keepdims=True))
    if not lows:
        return np.zeros((0, 2), dtype=np.uint8)
    return np.stack([np.concatenate(lows), np.concatenate(highs)], axis=1)


def overview_digital(levels: np.ndarray, level: int) -> np.ndarray:
    """``(AND, OR)`` of the 16 bits per bucket of 2^level samples."""
    levels = np.asarray(levels, dtype=np.uint16)
    size = 1 << level
    result = []
    for start in range(0, len(levels), size):
        block = levels[start:start + size]
        result.append((int(np.bitwise_and.reduce(block)), int(np.bitwise_or.reduce(block))))
    return np.asarray(result, dtype=np.uint16).reshape(-1, 2)
