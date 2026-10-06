# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The data of a run inside the nodes of the graph: a trace of the latest capture, the latest
number with its course, the size of a table – below the node's parameters.

A node keeps room for it (:func:`reserved_height`) so that a flow arranged once does not change
when data arrives; the room is only drawn while there is data.
"""

from __future__ import annotations

from typing import Optional

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen

from ...core import signals
from ...lab.engine.runtime import summarize
from ...lab.model import Flow
from ...lab.run_data import PortData, RunData
from ..theme import BORDER, PANEL, TEXT, TEXT_MUTED, type_color

#: height of the trace of a capture or signal
SIGNAL_HEIGHT = 40.0
#: height of a line with a value
LINE_HEIGHT = 17.0
#: lines of a capture drawn at most
SIGNAL_LINES = 3
#: types whose values are shown (devices and plain events are not data to look at)
DATA_TYPES = (signals.CAPTURE, signals.DIGITAL, signals.ANALOG, signals.SCALAR, signals.BOOL, signals.TABLE,
              signals.ANY)
#: the glyph that opens the data view, in the top right corner of the preview
OPEN_SIZE = 12.0


def reserved_height(spec, decoration: bool) -> float:
    """The room a node of ``spec`` keeps for its data (0: it shows none)."""
    if spec is None or decoration:
        return 0.0
    outputs = [port for port in spec.outputs if port.type in DATA_TYPES]
    if spec.ports is not None or any(port.type in (signals.CAPTURE, signals.DIGITAL, signals.ANALOG) for port in outputs):
        return SIGNAL_HEIGHT + 4
    inputs = [port for port in spec.inputs if port.type in DATA_TYPES]
    if not outputs and inputs and spec.group == "view":
        return SIGNAL_HEIGHT + 4  # a view node shows what arrives
    if len(outputs) >= 2:
        return 2 * LINE_HEIGHT + 4
    return LINE_HEIGHT + 4 if outputs else 0.0


def entries(data: Optional[RunData], flow: Optional[Flow], node_id: str) -> list[PortData]:
    """What the node shows: the data of its outputs, or (a node without any, e.g. a view) of the
    outputs wired to its inputs. Captures first; the single channels of a shown capture and plain
    events are left out."""
    if data is None or flow is None:
        return []
    own = data.ports(node_id)
    if not own:
        sources = {(edge.source.node, edge.source.port) for edge in flow.edges_into(node_id)}
        own = [item for item in (data.get(*source) for source in sorted(sources)) if item is not None]
    shown_capture = any(isinstance(item.latest, signals.Capture) for item in own)
    result = []
    for item in own:
        if shown_capture and isinstance(item.latest, (signals.Digital, signals.Analog)) and item.node == node_id:
            continue
        if item.kind == "event" and all(entry is None for entry in getattr(item.latest, "data", [None])):
            continue
        result.append(item)
    order = {"signal": 0, "number": 1, "table": 2, "value": 3, "event": 4}
    return sorted(result, key=lambda item: order.get(item.kind, 5))


def height_of(shown: list[PortData], reserved: float) -> float:
    if not shown or not reserved:
        return 0.0
    return reserved


def open_rect(rect: QRectF) -> QRectF:
    return QRectF(rect.right() - OPEN_SIZE - 2, rect.top() + 2, OPEN_SIZE, OPEN_SIZE)


def paint(painter: QPainter, rect: QRectF, shown: list[PortData], font: QFont) -> None:
    """Draw ``shown`` into ``rect`` (below the parameters of a node)."""
    painter.save()
    painter.setPen(QPen(QColor(BORDER), 1))
    painter.setBrush(QColor(PANEL))
    painter.drawRoundedRect(rect, 3, 3)
    painter.setFont(font)
    metrics = QFontMetrics(font)
    inner = rect.adjusted(4, 2, -OPEN_SIZE - 6, -2)
    y = inner.top()
    for item in shown:
        if y >= inner.bottom() - 4:
            break
        if item.kind == "signal":
            height = min(SIGNAL_HEIGHT - 4, inner.bottom() - y)
            _paint_signal(painter, QRectF(inner.left(), y, inner.width(), height), item, metrics)
            y += height
        else:
            height = min(LINE_HEIGHT - 2, inner.bottom() - y)
            _paint_line(painter, QRectF(inner.left(), y, inner.width(), height), item, metrics)
            y += LINE_HEIGHT - 2
    # open in a data view
    glyph = open_rect(rect)
    painter.setPen(QPen(QColor(TEXT_MUTED), 1.2))
    painter.setBrush(Qt.NoBrush)
    painter.drawRect(glyph.adjusted(1, 3, -3, -1))
    painter.drawLine(QPointF(glyph.center().x(), glyph.top() + 1), QPointF(glyph.right(), glyph.top() + 1))
    painter.drawLine(QPointF(glyph.right(), glyph.top() + 1), QPointF(glyph.right(), glyph.center().y()))
    painter.drawLine(QPointF(glyph.right(), glyph.top() + 1), QPointF(glyph.center().x() + 1, glyph.center().y() - 1))
    painter.restore()


def _label(item: PortData) -> str:
    return item.port if item.node else item.key


def _paint_line(painter: QPainter, rect: QRectF, item: PortData, metrics: QFontMetrics) -> None:
    value = item.latest
    if isinstance(value, signals.Table):
        text = f"{len(value)} rows" + (f" · {', '.join(value.columns)}" if value.columns else "")
    elif isinstance(value, signals.Event):
        text = f"{len(value)} events · {summarize(value.data[-1]) if len(value.data) else ''}"
    else:
        text = summarize(value)
    label = _label(item)
    painter.setPen(QColor(TEXT_MUTED))
    label_width = min(metrics.horizontalAdvance(label) + 6, rect.width() * 0.35)
    painter.drawText(QRectF(rect.left(), rect.top(), label_width, rect.height()), Qt.AlignLeft | Qt.AlignVCenter,
                     metrics.elidedText(label, Qt.ElideRight, int(label_width)))
    numbers = [number for _time, number in list(item.numbers)[-60:]]
    spark = rect.width() * 0.3 if len(numbers) > 1 else 0.0
    painter.setPen(QColor(TEXT))
    text_rect = QRectF(rect.left() + label_width, rect.top(), rect.width() - label_width - spark - 4, rect.height())
    painter.drawText(text_rect, Qt.AlignLeft | Qt.AlignVCenter, metrics.elidedText(text, Qt.ElideRight,
                                                                                 int(text_rect.width())))
    if spark:
        _polyline(painter, QRectF(rect.right() - spark, rect.top() + 2, spark, rect.height() - 4),
                  np.asarray(numbers, dtype=float), QColor(type_color(signals.SCALAR)))


def _paint_signal(painter: QPainter, rect: QRectF, item: PortData, metrics: QFontMetrics) -> None:
    block = item.blocks[-1] if item.blocks else item.latest
    lines: list[tuple[str, np.ndarray, bool]] = []
    if isinstance(block, signals.Capture):
        lines = [(name, np.asarray(values), True) for name, values in block.digital.items()]
        lines += [(name, np.asarray(values, dtype=float), False) for name, values in block.analog.items()]
    elif isinstance(block, signals.Digital):
        lines = [(item.port, block.values, True)]
    elif isinstance(block, signals.Analog):
        lines = [(item.port, block.values, False)]
    if not lines:
        return
    more = len(lines) - SIGNAL_LINES
    lines = lines[:SIGNAL_LINES]
    row = rect.height() / len(lines)
    label_width = min(max(metrics.horizontalAdvance(name) for name, _values, _digital in lines) + 6, rect.width() * 0.3)
    for index, (name, values, digital) in enumerate(lines):
        line_rect = QRectF(rect.left(), rect.top() + index * row, rect.width(), row)
        painter.setPen(QColor(TEXT_MUTED))
        text = name if not (more > 0 and index == len(lines) - 1) else f"{name} +{more}"
        painter.drawText(QRectF(line_rect.left(), line_rect.top(), label_width, row), Qt.AlignLeft | Qt.AlignVCenter,
                         metrics.elidedText(text, Qt.ElideRight, int(label_width)))
        trace = QRectF(line_rect.left() + label_width, line_rect.top() + 1.5, line_rect.width() - label_width,
                       max(row - 3, 2))
        color = QColor(type_color(signals.DIGITAL if digital else signals.ANALOG))
        _polyline(painter, trace, values.astype(float), color, step=digital)


def _polyline(painter: QPainter, rect: QRectF, values: np.ndarray, color: QColor, step: bool = False) -> None:
    """``values`` across ``rect`` (min and max per pixel column when there are more values than pixels)."""
    values = values[np.isfinite(values)] if values.size else values
    if values.size < 1 or rect.width() < 2:
        return
    columns = max(int(rect.width()), 2)
    if values.size > columns * 2:
        edges = np.linspace(0, values.size, columns + 1).astype(np.int64)
        lows = np.minimum.reduceat(values, edges[:-1])
        highs = np.maximum.reduceat(values, edges[:-1])
        points = np.empty(columns * 2)
        points[0::2], points[1::2] = lows, highs
        values = points
    low, high = float(values.min()), float(values.max())
    if step:
        low, high = 0.0, 1.0
    if high <= low:
        high = low + 1.0
    xs = rect.left() + np.arange(values.size) * (rect.width() / max(values.size - 1, 1))
    ys = rect.bottom() - (values - low) / (high - low) * rect.height()
    path = QPainterPath(QPointF(float(xs[0]), float(ys[0])))
    for index in range(1, values.size):
        if step:
            path.lineTo(QPointF(float(xs[index]), float(ys[index - 1])))
        path.lineTo(QPointF(float(xs[index]), float(ys[index])))
    painter.setPen(QPen(color, 1.2))
    painter.setBrush(Qt.NoBrush)
    painter.drawPath(path)
