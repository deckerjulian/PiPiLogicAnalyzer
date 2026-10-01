# Copyright (C) 2026 Julian Decker
#
# Part of PiPiLogicAnalyzer.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Buses and groups: channels combined into one value.

* :func:`bus_values` packs the channels of a :class:`BusDefinition` into one
  unsigned value per sample, :func:`bus_runs` finds the runs of equal values of
  a window (what a bus row draws);
* :func:`format_value` / :func:`parse_value` convert between values and text in
  the format of the bus, using its symbol table;
* :func:`parse_symbol_table` reads symbol tables (``NAME = 0x1F``, CSV, assembler
  style ``$D020 BORDER`` lines).
"""

from __future__ import annotations

import os
import re
from typing import Union

import numpy as np

from ..driver.models import BusDefinition, BusFormat, CaptureSession

#: Samples packed at once (bounds the temporary memory of large captures)
PACK_BLOCK = 16 << 20


def bus_width(bus: BusDefinition) -> int:
    """Bits of the values of ``bus`` (at least 1)."""
    return max(len(bus.channels), 1)


def value_dtype(width: int) -> np.dtype:
    """Smallest unsigned dtype holding ``width`` bits (up to 64)."""
    for dtype in (np.uint8, np.uint16, np.uint32):
        if width <= np.dtype(dtype).itemsize * 8:
            return np.dtype(dtype)
    return np.dtype(np.uint64)


def bus_values(session: CaptureSession, bus: BusDefinition) -> np.ndarray:
    """One value per sample: bit ``n`` is the level of ``bus.channels[n]``.

    Channels of the bus that the session does not hold count as 0.
    """
    count = session.sample_count()
    dtype = value_dtype(bus_width(bus))
    values = np.zeros(count, dtype=dtype)
    by_number = {c.channel_number: c.samples for c in session.capture_channels if c.samples is not None}
    for bit, number in enumerate(bus.channels[:64]):
        samples = by_number.get(number)
        if samples is None:
            continue
        weight = dtype.type(1 << bit)
        for start in range(0, count, PACK_BLOCK):
            end = min(start + PACK_BLOCK, count)
            part = np.asarray(samples[start:end]) != 0
            values[start:end] |= part.astype(dtype) * weight
    return values


def bus_runs(values: np.ndarray, first: int, last: int) -> tuple[np.ndarray, np.ndarray]:
    """``(starts, values)`` of every run of equal values overlapping ``[first, last)``.

    The first run is reported to start at ``first`` (clipped to the capture).
    """
    first = max(int(first), 0)
    last = min(int(last), len(values))
    if last <= first:
        return np.zeros(0, dtype=np.int64), values[:0]
    window = np.asarray(values[first:last])
    starts = np.flatnonzero(window[1:] != window[:-1]) + 1
    starts = np.concatenate((np.zeros(1, dtype=starts.dtype), starts))
    return starts.astype(np.int64) + first, window[starts]


# ------------------------------------------------------------------ text
def _signed(value: int, width: int) -> int:
    return value - (1 << width) if value >> (width - 1) & 1 else value


def format_value(value: int, bus: BusDefinition) -> str:
    """Text of ``value`` in the format of ``bus`` (its symbol name if it has one)."""
    value = int(value)
    name = bus.symbols.get(value)
    if name is not None:
        return name
    width = bus_width(bus)
    fmt = bus.format
    if fmt == BusFormat.DECIMAL:
        return str(value)
    if fmt == BusFormat.SIGNED:
        return str(_signed(value, width))
    if fmt == BusFormat.BINARY:
        return f"0b{value:0{width}b}"
    if fmt == BusFormat.ASCII and value <= 0xFF:
        if 0x20 <= value < 0x7F:
            return f"'{chr(value)}'"
        return f"\\x{value:02X}"
    return f"0x{value:0{(width + 3) // 4}X}"


_ESCAPES = {"n": 10, "r": 13, "t": 9, "0": 0, "\\": 92, "'": 39, '"': 34}


def _parse_char(text: str) -> int:
    body = text[1:-1]
    if len(body) == 1:
        return ord(body)
    if body.startswith("\\"):
        if len(body) == 2 and body[1] in _ESCAPES:
            return _ESCAPES[body[1]]
        if body[1:2] in ("x", "X") and len(body) > 2:
            return int(body[2:], 16)
    raise ValueError(f"Invalid character: {text}")


def parse_number(text: str) -> int:
    """``0x1F``, ``$1F``, ``0b11111``, ``0o37`` or decimal (with sign); raises ``ValueError``."""
    text = text.strip()
    sign = 1
    if text[:1] in "+-":
        sign = -1 if text[0] == "-" else 1
        text = text[1:].lstrip()
    if text.startswith("$"):
        return sign * int(text[1:], 16)
    lower = text.lower()
    if lower.startswith(("0x", "0b", "0o")):
        return sign * int(text, 0)
    if not text.isdigit():
        raise ValueError(f"Not a number: {text!r}")
    return sign * int(text, 10)


def parse_value(text: str, bus: BusDefinition) -> int:
    """Value of ``text`` for ``bus``: a symbol name (any case), a quoted character,
    ``0x``/``$`` hex, ``0b`` binary, ``\\xNN`` or decimal (negative for SIGNED buses).

    Raises ``ValueError`` for anything else or a value that does not fit the bus.
    """
    text = text.strip()
    if not text:
        raise ValueError("Empty value")
    folded = text.casefold()
    for value, name in bus.symbols.items():
        if name.casefold() == folded:
            return int(value)
    width = bus_width(bus)
    if len(text) >= 3 and text[0] == text[-1] and text[0] in "'\"":
        value = _parse_char(text)
    elif text[:2] in ("\\x", "\\X") and len(text) > 2:
        value = int(text[2:], 16)  # an ASCII escape as shown by format_value
    else:
        value = parse_number(text)
    if value < 0:
        if bus.format != BusFormat.SIGNED or value < -(1 << (width - 1)):
            raise ValueError(f"{text} is out of range for {width} bits")
        value += 1 << width
    if value >> width:
        raise ValueError(f"{text} is out of range for {width} bits")
    return value


# ---------------------------------------------------------- symbol tables
_COMMENT = re.compile(r"(#|;|//)")


def _strip_name(name: str) -> str:
    name = name.strip()
    if len(name) >= 2 and name[0] == name[-1] and name[0] in "'\"":
        name = name[1:-1].strip()
    return name


def _is_number(text: str) -> bool:
    try:
        parse_number(text)
    except ValueError:
        return False
    return True


def _symbol_line(line: str) -> tuple[int, str]:
    if "=" in line:
        left, right = (part.strip() for part in line.split("=", 1))
        if _is_number(right) and left:
            return parse_number(right), _strip_name(left)
        if _is_number(left) and right:
            return parse_number(left), _strip_name(right)
        raise ValueError("expected NAME = value")
    if "," in line:
        left, right = (part.strip() for part in line.split(",", 1))
    else:
        parts = line.split(None, 1)
        if len(parts) < 2:
            raise ValueError("expected a value and a name")
        if _is_number(parts[0]):
            left, right = parts
        else:
            left, right = line.rsplit(None, 1)
    left, right = _strip_name(left), _strip_name(right)
    if _is_number(left) and right:
        return parse_number(left), right
    if _is_number(right) and left:
        return parse_number(right), left
    raise ValueError("expected a value and a name")


def parse_symbol_table(text: str) -> dict[int, str]:
    """Symbols of a table, one per line.

    Accepted lines: ``NAME = 0x1F``, ``NAME = 31``, ``0x1F NAME``, ``$D020 NAME``,
    ``0x1F,NAME`` and ``NAME,0x1F``; ``#``, ``;`` and ``//`` start comments. A first
    CSV line without a number is taken as a header. Raises ``ValueError`` naming
    the line of anything else.
    """
    symbols: dict[int, str] = {}
    first = True
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if line.startswith(("#", ";", "//")):
            continue
        # A "#" or ";" after the value starts a comment as well ("$" hex never contains them).
        match = _COMMENT.search(line)
        if match:
            line = line[: match.start()].strip()
        if not line:
            continue
        try:
            value, name = _symbol_line(line)
        except ValueError as error:
            if first and "," in line:
                first = False
                continue
            raise ValueError(f"Line {number}: {error}: {raw.strip()!r}") from None
        first = False
        if value < 0:
            raise ValueError(f"Line {number}: negative value: {raw.strip()!r}")
        symbols[value] = name
    return symbols


def load_symbol_file(path: Union[str, os.PathLike]) -> dict[int, str]:
    """Symbols of the table file at ``path`` (see :func:`parse_symbol_table`)."""
    with open(path, encoding="utf-8", errors="replace") as handle:
        return parse_symbol_table(handle.read())


def symbol_table_text(symbols: dict[int, str]) -> str:
    """``NAME = 0x..`` lines sorted by value, readable by :func:`parse_symbol_table`."""
    if not symbols:
        return ""
    digits = max(2, max(len(f"{v:X}") for v in symbols))
    digits += digits % 2
    return "".join(f"{symbols[v]} = 0x{v:0{digits}X}\n" for v in sorted(symbols))
