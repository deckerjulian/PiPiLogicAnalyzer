# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Cursors, named markers and search matches, drawn over the ruler and the waveform."""

from __future__ import annotations

from typing import Callable, Optional

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFontMetricsF, QPainter, QPainterPath, QPen

from .. import colors
from ..view_model import CURSORS, CaptureViewModel

#: Pixels from a cursor line within which a press grabs it
GRAB_DISTANCE = 5


def _visible(model: CaptureViewModel, sample: int) -> bool:
    return model.first_sample <= sample <= model.first_sample + model.visible_samples


def draw_search_hits(
    painter: QPainter, model: CaptureViewModel, x_for: Callable[[float], float], bounds: QRectF,
    ruler: bool = False,
) -> None:
    """Matches as bands (with an end) or ticks; on the ruler as small marks at the bottom."""
    hits = model.search_hits
    if not len(hits):
        return
    first, last = model.first_sample, model.first_sample + model.visible_samples
    low = int(np.searchsorted(hits.starts, first - (model.visible_samples if hits.ends is not None else 0)))
    high = int(np.searchsorted(hits.starts, last, side="right"))
    # More matches than pixels: one mark per pixel column is enough
    step = max(1, (high - low) // max(int(bounds.width()), 1))
    for index in range(low, high, step):
        start = int(hits.starts[index])
        end = int(hits.ends[index]) if hits.ends is not None else start
        current = index == hits.current
        color = colors.SEARCH_CURRENT_COLOR if current else colors.SEARCH_HIT_COLOR
        x1, x2 = x_for(start), x_for(end)
        if ruler:
            painter.fillRect(QRectF(x1 - 1, bounds.height() - 5, max(x2 - x1, 0) + 3, 4), color)
        elif x2 - x1 >= 3:
            fill = QColor(color)
            fill.setAlpha(60 if current else 30)
            painter.fillRect(QRectF(x1, 0, x2 - x1, bounds.height()), fill)
        else:
            painter.setPen(QPen(color, 2 if current else 1))
            x = round(x1) + 0.5
            painter.drawLine(QPointF(x, 0), QPointF(x, bounds.height()))


def draw_bookmarks(
    painter: QPainter, model: CaptureViewModel, x_for: Callable[[float], float], bounds: QRectF,
    ruler: bool = False,
) -> None:
    pen = QPen(colors.BOOKMARK_COLOR, 1)
    pen.setStyle(Qt.DotLine)
    metrics = QFontMetricsF(painter.font())
    for bookmark in model.bookmarks:
        if not _visible(model, bookmark.sample):
            continue
        x = round(x_for(bookmark.sample)) + 0.5
        if ruler:
            path = QPainterPath()
            path.moveTo(x, 2)
            path.lineTo(x + 7, 2)
            path.lineTo(x + 7, 9)
            path.lineTo(x, 13)
            path.closeSubpath()
            painter.fillPath(path, colors.BOOKMARK_COLOR)
            painter.setPen(colors.BOOKMARK_COLOR)
            painter.drawText(QPointF(x + 10, metrics.ascent() + 1), bookmark.name)
        else:
            painter.setPen(pen)
            painter.drawLine(QPointF(x, 0), QPointF(x, bounds.height()))


def draw_selection(painter: QPainter, model: CaptureViewModel, x_for: Callable[[float], float],
                   bounds: QRectF) -> None:
    """The samples selected on the ruler, shaded over a track (the ruler draws its own)."""
    selection = model.selection
    if selection is None:
        return
    first, last = selection
    left, right = x_for(first), x_for(last + 1)
    painter.fillRect(QRectF(left, 0, max(right - left, 1.0), bounds.height()), colors.SELECTION_COLOR)
    painter.setPen(QPen(colors.SELECTION_EDGE, 1))
    for x in (round(left) + 0.5, round(right) - 0.5):
        painter.drawLine(QPointF(x, 0), QPointF(x, bounds.height()))


def draw_cursors(
    painter: QPainter, model: CaptureViewModel, x_for: Callable[[float], float], bounds: QRectF,
    ruler: bool = False,
) -> None:
    """Cursor lines; on the ruler with a labelled flag, and the span between A and B shaded."""
    a, b = model.cursor("A"), model.cursor("B")
    if ruler and a is not None and b is not None:
        left, right = sorted((x_for(a), x_for(b)))
        painter.fillRect(QRectF(left, 0, right - left, bounds.height()), QColor(255, 255, 255, 18))
    for name in CURSORS:
        sample = model.cursor(name)
        if sample is None or not _visible(model, sample):
            continue
        color = colors.CURSOR_COLORS[name]
        x = round(x_for(sample)) + 0.5
        painter.setPen(QPen(color, 1.5))
        painter.drawLine(QPointF(x, 0), QPointF(x, bounds.height()))
        if ruler:
            flag = QRectF(x - 7, bounds.height() - 15, 14, 13)
            painter.fillRect(flag, color)
            painter.setPen(QColor(20, 20, 24))
            painter.drawText(flag, Qt.AlignCenter, name)


def cursor_at(model: CaptureViewModel, x: float, x_for: Callable[[float], float]) -> Optional[str]:
    """The cursor whose line is under ``x`` (the nearest one)."""
    best, distance = None, GRAB_DISTANCE + 1.0
    for name in CURSORS:
        sample = model.cursor(name)
        if sample is None:
            continue
        gap = abs(x_for(sample) - x)
        if gap <= GRAB_DISTANCE and gap < distance:
            best, distance = name, gap
    return best
