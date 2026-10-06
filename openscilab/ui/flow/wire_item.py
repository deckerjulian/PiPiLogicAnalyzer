# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""A wire on the canvas: a curve from an output to an input, coloured by the type it carries.

While the flow runs, the wire shows the last value (a number, or a sparkline of the recent
numbers) and flashes for events.
"""

from __future__ import annotations

from collections import deque
from typing import Optional

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPen,
)
from PySide6.QtWidgets import QGraphicsItem, QGraphicsPathItem

from ...core import signals
from ...lab.engine.runtime import summarize
from ...lab.model import Edge
from ..theme import PANEL, SELECTION, TEXT, qcolor, type_color

SPARK_POINTS = 40


#: a wire that runs back (its target left of its source) leaves and enters this far from the nodes
BACK_REACH = 36.0


def curve(start: QPointF, end: QPointF, lane: Optional[float] = None) -> QPainterPath:
    """The path of a wire from an output (``start``) to an input (``end``). A wire that runs back -
    feedback, e.g. a measurement to the next step of a sweep - goes round below the nodes, along
    ``lane`` (below both of them), instead of through them."""
    path = QPainterPath(start)
    if lane is not None and end.x() < start.x() + BACK_REACH:
        reach = BACK_REACH
        bend = min(24.0, max((lane - max(start.y(), end.y())) / 2, 8.0))
        path.cubicTo(QPointF(start.x() + reach, start.y()), QPointF(start.x() + reach, lane - bend),
                     QPointF(start.x() + reach / 2, lane))
        path.lineTo(QPointF(end.x() - reach / 2, lane))
        path.cubicTo(QPointF(end.x() - reach, lane - bend), QPointF(end.x() - reach, end.y()), end)
        return path
    dx = max(abs(end.x() - start.x()) * 0.5, 40.0)
    path.cubicTo(QPointF(start.x() + dx, start.y()), QPointF(end.x() - dx, end.y()), end)
    return path


def numeric(value) -> Optional[float]:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, (signals.Scalar, signals.Bool)):
        return float(value.value)
    return None


class WireItem(QGraphicsPathItem):
    def __init__(self, edge: Edge, type_name: str) -> None:
        super().__init__()
        self.edge = edge
        self.type_name = type_name
        self.label = ""
        self.history: deque = deque(maxlen=SPARK_POINTS)
        self.flash = False
        self.invalid = False
        self._timer: Optional[QTimer] = None
        self.setFlags(QGraphicsItem.ItemIsSelectable)
        self.setZValue(-1)
        self.setAcceptHoverEvents(True)
        self.setToolTip(f"{edge} ({type_name})")

    def set_ends(self, start: QPointF, end: QPointF, lane: Optional[float] = None) -> None:
        self.setPath(curve(start, end, lane))

    def shape(self) -> QPainterPath:
        stroker = QPainterPathStroker()
        stroker.setWidth(10)
        return stroker.createStroke(self.path())

    def boundingRect(self) -> QRectF:  # noqa: N802 - Qt naming
        rect = self.path().boundingRect().adjusted(-6, -6, 6, 6)
        if self.label:
            middle = self.path().pointAtPercent(0.5)
            rect = rect.united(QRectF(middle.x() - 70, middle.y() - 26, 140, 30))
        return rect

    def show_value(self, value) -> None:
        self.show_values([value])

    def show_values(self, values: list) -> None:
        """Values that passed (oldest first): all of them go into the sparkline, the last one is
        the label – one repaint for the whole batch."""
        if not values:
            return
        self.prepareGeometryChange()
        for value in values[-SPARK_POINTS:]:
            number = numeric(value)
            if number is not None:
                self.history.append(number)
        self.label = summarize(values[-1])
        if any(isinstance(value, signals.Event) for value in values):
            self.flash = True
            if self._timer is None:
                self._timer = QTimer()
                self._timer.setSingleShot(True)
                self._timer.timeout.connect(self._end_flash)
            self._timer.start(150)
        self.update()

    def clear_value(self) -> None:
        self.prepareGeometryChange()
        self.label = ""
        self.history.clear()
        self.update()

    def _end_flash(self) -> None:
        self.flash = False
        self.update()

    def paint(self, painter: QPainter, option, widget=None) -> None:
        painter.setRenderHint(QPainter.Antialiasing)
        color = QColor(type_color(self.type_name))
        if self.invalid:
            color = qcolor("wire.invalid")
        width = 2.0
        if self.isSelected():
            color = QColor(SELECTION).lighter(150)
            width = 3.0
        if self.flash:
            color = color.lighter(160)
            width = 3.5
        painter.setPen(QPen(color, width, Qt.DashLine if self.invalid else Qt.SolidLine))
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(self.path())
        if not self.label:
            return
        middle = self.path().pointAtPercent(0.5)
        font = QFont(painter.font())
        font.setPointSizeF(max(font.pointSizeF() - 1.5, 7))
        painter.setFont(font)
        metrics = QFontMetrics(font)
        text = metrics.elidedText(self.label, Qt.ElideRight, 110)
        box = QRectF(middle.x() - metrics.horizontalAdvance(text) / 2 - 5, middle.y() - 22,
                     metrics.horizontalAdvance(text) + 10, 16)
        painter.setPen(QPen(color, 1))
        painter.setBrush(QColor(PANEL))
        painter.drawRoundedRect(box, 4, 4)
        painter.setPen(QColor(TEXT))
        painter.drawText(box, Qt.AlignCenter, text)
        if len(self.history) > 1:
            low, high = min(self.history), max(self.history)
            span = (high - low) or 1.0
            spark = QPainterPath()
            left, top, w, h = box.left(), box.bottom() + 2, max(box.width(), 50), 8
            for index, value in enumerate(self.history):
                x = left + w * index / (len(self.history) - 1)
                y = top + h - h * (value - low) / span
                if index == 0:
                    spark.moveTo(x, y)
                else:
                    spark.lineTo(x, y)
            painter.setPen(QPen(color, 1))
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(spark)
