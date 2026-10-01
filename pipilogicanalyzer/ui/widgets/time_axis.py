# Copyright (C) 2026 Julian Decker
#
# Part of PiPiLogicAnalyzer.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Ticks of the time axis, shared by the ruler and the grid of the waveform.

The axis shows time, not sample numbers: 0 is the trigger (or the first sample of a capture
without one), the steps are 1, 2 or 5 of a decimal unit, about one label per ``LABEL_SPACING``
pixels, with minor ticks between them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

#: Pixels between two labelled ticks, at least
LABEL_SPACING = 96
_UNITS = ((1.0, "s"), (1e-3, "ms"), (1e-6, "µs"), (1e-9, "ns"))


@dataclass(frozen=True)
class Tick:
    #: position in samples (may lie between two samples)
    sample: float
    major: bool
    label: str = ""


def nice_step(seconds: float) -> tuple[float, int]:
    """The 1/2/5 step of at least ``seconds`` and the number of minor divisions of it."""
    seconds = max(seconds, 1e-15)
    magnitude = 10 ** math.floor(math.log10(seconds))
    for factor, minors in ((1, 5), (2, 4), (5, 5), (10, 5)):
        if factor * magnitude >= seconds * (1 - 1e-9):
            return factor * magnitude, minors
    return 10 * magnitude, 5


def format_time(seconds: float, step: float) -> str:
    """``seconds`` in the unit of ``step``, with as many decimals as the step needs."""
    if abs(seconds) < step * 1e-6:
        return "0"
    # The largest unit in which the step needs at most one decimal (0.2 ms rather than 200 µs)
    for scale, unit in _UNITS:
        if step >= scale * 0.0999 or scale == _UNITS[-1][0]:
            value = seconds / scale
            decimals = max(0, -math.floor(math.log10(step / scale) + 1e-9))
            text = f"{value:+.{decimals}f}" if seconds < 0 else f"{value:.{decimals}f}"
            return f"{text.replace('-', '−')} {unit}"
    return f"{seconds:g} s"


def ticks(first_sample: float, visible_samples: float, width: float, frequency: float, origin: float) -> list[Tick]:
    """Major and minor ticks of the samples ``first_sample`` … ``first_sample + visible_samples``
    drawn over ``width`` pixels; time 0 is at sample ``origin``."""
    if width <= 0 or visible_samples <= 0 or frequency <= 0:
        return []
    seconds_per_pixel = visible_samples / frequency / width
    step, minors = nice_step(seconds_per_pixel * LABEL_SPACING)
    minor = step / minors
    start_time = (first_sample - origin) / frequency
    end_time = (first_sample + visible_samples - origin) / frequency
    result = []
    index = math.floor(start_time / minor)
    while index * minor <= end_time + minor * 1e-9:
        time = index * minor
        major = index % minors == 0
        result.append(Tick(origin + time * frequency, major, format_time(time, step) if major else ""))
        index += 1
    return result
