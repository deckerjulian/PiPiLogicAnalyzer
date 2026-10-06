# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Quantities with units in flows: ``"200 ms"``, ``"1 kHz"``, ``"3.3 V"``, ``"50 %"``."""

from __future__ import annotations

import math
import re
from typing import Optional, Union

PREFIXES = {
    "p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3,
    "": 1.0, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9,
}
#: Units a quantity may carry (the base unit of each dimension)
UNITS = ("s", "Hz", "V", "A", "Ω", "Ohm", "F", "%", "Sa/s", "S/s", "sps", "B", "bit", "°C")
_ALIASES = {"Ohm": "Ω", "S/s": "Sa/s", "sps": "Sa/s"}

_QUANTITY_RE = re.compile(
    r"^\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*"
    r"(p|n|u|µ|μ|m|k|K|M|G)?\s*(s|Hz|V|A|Ω|Ohm|F|%|Sa/s|S/s|sps|B|bit|°C|min|h)?\s*$"
)

Number = Union[int, float]


class UnitError(ValueError):
    pass


def parse(value, unit: str = "") -> float:
    """``value`` as a number in the base unit (seconds, hertz, volts, ...).

    Numbers are taken as they are; strings may have an SI prefix and a unit: ``"200 ms"`` → 0.2,
    ``"1 kHz"`` → 1000, ``"50 %"`` → 0.5, ``"2 min"`` → 120. When ``unit`` is given, a string
    with another unit is refused.
    """
    if isinstance(value, bool):
        raise UnitError(f"{value!r} is not a quantity")
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    match = _QUANTITY_RE.match(text)
    if match is None:
        # "M" alone is mega, but "m" alone is milli: "10M" is 10e6, "5m" is 5e-3.
        raise UnitError(f"{value!r} is not a quantity (e.g. 200 ms, 1 kHz, 3.3 V)")
    number, prefix, found = match.group(1), match.group(2) or "", match.group(3) or ""
    if found in ("min", "h"):
        if prefix:
            raise UnitError(f"{value!r}: no prefix before {found}")
        factor = 60.0 if found == "min" else 3600.0
        found = "s"
    else:
        factor = PREFIXES[prefix]
    found = _ALIASES.get(found, found)
    if unit and found and _ALIASES.get(unit, unit) != found:
        raise UnitError(f"{value!r} is not in {unit}")
    result = float(number) * factor
    if found == "%":
        result /= 100.0
    return result


def parse_optional(value, unit: str = "") -> Optional[float]:
    return None if value is None or value == "" else parse(value, unit)


def split(value) -> tuple[float, str]:
    """A quantity as its number in the base unit and that unit: ``"20 mA"`` → ``(0.02, "A")``,
    ``"2 min"`` → ``(120.0, "s")``; a plain number has no unit: ``"20"`` → ``(20.0, "")``."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value), ""
    match = _QUANTITY_RE.match(str(value).strip())
    if match is None:
        raise UnitError(f"{value!r} is not a quantity (e.g. 200 ms, 1 kHz, 3.3 V)")
    found = match.group(3) or ""
    found = "s" if found in ("min", "h") else _ALIASES.get(found, found)
    return parse(value), found


def scale_of(unit: str) -> tuple[float, str]:
    """A unit as the factor to its base unit and that base unit: ``"mA"`` → ``(1e-3, "A")``,
    ``"kHz"`` → ``(1e3, "Hz")``, ``"%"`` → ``(0.01, "%")``. A unit openSciLab does not know (``"mm"``,
    ``"Pa"``) is its own base unit."""
    text = (unit or "").strip()
    if text in ("min", "h"):
        return (60.0 if text == "min" else 3600.0), "s"
    base = _ALIASES.get(text, text)
    if base in UNITS:
        return (0.01 if base == "%" else 1.0), base
    if len(text) > 1 and text[0] in PREFIXES and _ALIASES.get(text[1:], text[1:]) in UNITS and text[1:] != "%":
        factor, base = scale_of(text[1:])
        return PREFIXES[text[0]] * factor, base
    return 1.0, text


def in_unit(value, unit: str) -> float:
    """``value`` as a number in ``unit`` (the unit of the signal it goes with): ``"20 mA"`` in mA → 20,
    ``"0.02 A"`` in mA → 20; a plain number is already in that unit. A quantity of another unit is
    refused (:class:`UnitError`)."""
    number, found = split(value)
    if not found:
        return number
    factor, base = scale_of(unit)
    if base and found != base:
        raise UnitError(f"{value!r} is not in {unit}")
    return float(f"{number / factor:.12g}")  # (0.02 A in mA is 20, not 20.000000000000004)


def format_quantity(value: Number, unit: str = "", digits: int = 4) -> str:
    """``0.2, "s"`` → ``"200 ms"``; ``1000, "Hz"`` → ``"1 kHz"``."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "-"
    if unit == "%":
        return f"{value * 100:.{digits}g} %"
    if value == 0:
        return f"0 {unit}".strip()
    magnitude = abs(value)
    for prefix, factor in (("G", 1e9), ("M", 1e6), ("k", 1e3), ("", 1.0), ("m", 1e-3), ("µ", 1e-6),
                           ("n", 1e-9), ("p", 1e-12)):
        if magnitude >= factor * 0.9999999:
            return f"{value / factor:.{digits}g} {prefix}{unit}".strip()
    return f"{value:.{digits}g} {unit}".strip()
