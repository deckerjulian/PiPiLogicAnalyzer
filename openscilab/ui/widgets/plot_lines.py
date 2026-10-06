# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Lines of charts on the screen, shared by the chart documents and the charts of panels.

Values that are not finite (a measurement without a result is NaN) are left out, and a line
with many more points than pixels is drawn as its envelope: the lowest and the highest value of
every pixel column.
"""

from __future__ import annotations

from typing import Optional, Sequence

import numpy as np
from PySide6.QtCore import QPointF, QRectF

#: points per pixel column above which a line is drawn as its envelope
DENSE = 4


def finite(xs: Sequence[float], ys: Sequence[float]) -> tuple[np.ndarray, np.ndarray]:
    """``xs`` and ``ys`` as arrays of the same length without the points that are not finite."""
    count = min(len(xs), len(ys))
    x = np.asarray(xs, dtype=np.float64)[:count]
    y = np.asarray(ys, dtype=np.float64)[:count]
    keep = np.isfinite(x) & np.isfinite(y)
    return (x, y) if keep.all() else (x[keep], y[keep])


def data_bounds(series: Sequence[tuple[Sequence[float], Sequence[float]]]) -> Optional[tuple[float, float, float, float]]:
    """``(x0, x1, y0, y1)`` of the finite points of ``series`` (``None`` without any)."""
    lows_x, highs_x, lows_y, highs_y = [], [], [], []
    for xs, ys in series:
        x, y = finite(xs, ys)
        if len(x):
            lows_x.append(x.min())
            highs_x.append(x.max())
            lows_y.append(y.min())
            highs_y.append(y.max())
    if not lows_x:
        return None
    return float(min(lows_x)), float(max(highs_x)), float(min(lows_y)), float(max(highs_y))


def screen_points(xs: Sequence[float], ys: Sequence[float], bounds: tuple[float, float, float, float],
                  area: QRectF, envelope: bool = True) -> list[QPointF]:
    """The points of a line in ``area`` showing ``bounds`` (``x0, x1, y0, y1``).

    ``envelope``: a line whose x values only grow (a strip chart, a spectrum) and that has many
    more points than ``area`` has pixel columns becomes two points per column, its lowest and
    its highest value there – what a line through all of them would paint, without drawing
    hundreds of thousands of segments.
    """
    x, y = finite(xs, ys)
    if not len(x):
        return []
    x0, x1, y0, y1 = bounds
    width_x = (x1 - x0) or 1.0
    width_y = (y1 - y0) or 1.0
    px = area.left() + (x - x0) / width_x * area.width()
    py = area.bottom() - (y - y0) / width_y * area.height()
    columns = max(int(area.width()), 1)
    if envelope and len(px) > DENSE * columns and bool(np.all(px[1:] >= px[:-1])):
        column = np.floor(px).astype(np.int64)
        starts = np.flatnonzero(np.concatenate(([True], column[1:] != column[:-1])))
        low = np.minimum.reduceat(py, starts)
        high = np.maximum.reduceat(py, starts)
        first = py[starts]
        last = py[np.concatenate((starts[1:] - 1, [len(py) - 1]))]
        # within a column: from where the line comes in to where it leaves, through both extremes
        rising = first >= last  # (screen y grows downwards)
        px = np.repeat(column[starts].astype(np.float64), 2)
        py = np.empty(len(px), dtype=np.float64)
        py[0::2] = np.where(rising, high, low)
        py[1::2] = np.where(rising, low, high)
    return [QPointF(a, b) for a, b in zip(px.tolist(), py.tolist())]
