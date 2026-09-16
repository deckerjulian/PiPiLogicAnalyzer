# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of PiPiLogicAnalyzer, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Human readable formatting of times, frequencies and sample counts.

Port of ``Extensions/DoubleExtensions.cs`` with correct SI symbols (the original
printed ``us`` and ``Mhz``).
"""

from __future__ import annotations


def to_small_time(seconds: float) -> str:
    """Format a duration given in seconds using the largest sensible unit."""
    if seconds is None:
        return "-"
    magnitude = abs(seconds)
    if magnitude < 1e-6:
        return f"{round(seconds * 1e9, 3):g} ns"
    if magnitude < 1e-3:
        return f"{round(seconds * 1e6, 3):g} µs"
    if magnitude < 1:
        return f"{round(seconds * 1e3, 3):g} ms"
    return f"{round(seconds, 3):g} s"


def to_large_frequency(frequency: float) -> str:
    """Format a frequency given in Hz."""
    if frequency is None:
        return "-"
    magnitude = abs(frequency)
    if magnitude >= 1e6:
        return f"{round(frequency / 1e6, 2):g} MHz"
    if magnitude >= 1e3:
        return f"{round(frequency / 1e3, 2):g} kHz"
    return f"{round(frequency, 2):g} Hz"


def to_inferred_frequency(duration: float) -> str:
    """Frequency of a square wave whose half period lasts ``duration`` seconds."""
    if not duration or duration <= 0:
        return "-"
    return to_large_frequency(1.0 / (duration * 2))


def to_thousands(value: int) -> str:
    return f"{value:,}"
