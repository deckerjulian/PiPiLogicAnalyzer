# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Documents of the view nodes: strip chart, XY chart and spectrum (drawn here, no plotting
library), and the plain views number, LED, log and table."""

from __future__ import annotations

import math
from typing import Any, Optional

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QLabel,
    QPlainTextEdit,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core import signals, units
from ..theme import BORDER, PANEL, TEXT, TEXT_MUTED, qcolor, set_role
from ..widgets.plot_lines import data_bounds, screen_points
from ..widgets.plot_navigation import NavigablePlot, PlotNavigator
from .base import DocumentWidget

#: colours of the traces, in order
TRACE_COLORS = ("#4b8ae6", "#7fd18b", "#e8b04b", "#e07fd1", "#58c4c4", "#ff9a62", "#b0a3ff", "#c9c9cf")
CHART_KINDS = ("strip_chart", "xy", "spectrum")
VALUE_KINDS = ("number", "led", "log", "table")


#: more ticks than this are none (the range cannot be divided as asked)
MAX_TICKS = 100


def nice_ticks(low: float, high: float, count: int = 6) -> list[float]:
    """Round tick values covering ``low``..``high``."""
    if not math.isfinite(low) or not math.isfinite(high):
        return []
    if high <= low:
        high = low + 1.0
    raw = (high - low) / max(count, 1)
    if not math.isfinite(raw) or raw <= 0:
        return []
    magnitude = 10 ** math.floor(math.log10(raw))
    step = next(factor * magnitude for factor in (1, 2, 2.5, 5, 10) if factor * magnitude >= raw)
    # Counted, not added up: "value += step" never arrives when the step is smaller than the
    # precision of the values (a deep zoom far from zero), which froze the window.
    first, last = math.ceil(low / step), math.floor(high / step + 1e-9)
    if not (math.isfinite(first) and math.isfinite(last)) or last - first > MAX_TICKS:
        return []
    return [float(f"{index * step:.12g}") for index in range(int(first), int(last) + 1)]


class ChartWidget(NavigablePlot, QWidget):
    """Lines (strip chart, spectrum) or points (XY) with axes and grid; zoom and move like the
    waveform (see :mod:`..widgets.plot_navigation`)."""

    def __init__(self, kind: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.kind = kind
        self.navigator = PlotNavigator(self, self.plot_area)
        self.setToolTip("Wheel or pinch: zoom · drag: move · double-click: automatic range")
        #: name -> (xs, ys)
        self.series: dict[str, tuple[list[float], list[float]]] = {}
        self.x_label = "s" if kind == "strip_chart" else ("Hz" if kind == "spectrum" else "x")
        self.y_label = "dB" if kind == "spectrum" else ""
        self.setMinimumSize(320, 220)

    def set_series(self, series: dict[str, tuple[list[float], list[float]]], x_label: str = "", y_label: str = "") -> None:
        self.series = series
        if x_label:
            self.x_label = x_label
        if y_label:
            self.y_label = y_label
        self.update()

    def plot_area(self) -> QRectF:
        return QRectF(self.rect()).adjusted(64, 16, -16, -40)

    def bounds(self) -> Optional[tuple[float, float, float, float]]:
        return self.navigator.bounds(self.automatic_bounds())

    def automatic_bounds(self) -> Optional[tuple[float, float, float, float]]:
        found = data_bounds(list(self.series.values()))
        if found is None:
            return None
        low_x, high_x, low_y, high_y = found
        margin = (high_y - low_y) * 0.08 or max(abs(high_y) * 0.1, 1.0)
        return low_x, high_x, low_y - margin, high_y + margin

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor(PANEL))
        plot = self.plot_area()
        painter.setPen(QPen(QColor(BORDER), 1))
        painter.drawRect(plot)
        if not self.navigator.automatic:
            painter.setPen(QColor(TEXT_MUTED))
            painter.drawText(QRectF(plot.left(), 0, plot.width(), 16), Qt.AlignRight | Qt.AlignVCenter,
                             self.navigator.hint())
        bounds = self.bounds()
        font = QFont(painter.font())
        font.setPointSizeF(max(font.pointSizeF() - 1, 7))
        painter.setFont(font)
        if bounds is None:
            painter.setPen(QColor(TEXT_MUTED))
            painter.drawText(plot, Qt.AlignCenter, "Waiting for values")
            return
        x0, x1, y0, y1 = bounds
        if x1 <= x0:
            x1 = x0 + 1.0
        if y1 <= y0:
            y1 = y0 + 1.0

        def to_point(x: float, y: float) -> QPointF:
            return QPointF(plot.left() + (x - x0) / (x1 - x0) * plot.width(),
                           plot.bottom() - (y - y0) / (y1 - y0) * plot.height())

        grid = QPen(qcolor("chart.grid"), 1)
        for tick in nice_ticks(x0, x1):
            point = to_point(tick, y0)
            painter.setPen(grid)
            painter.drawLine(QPointF(point.x(), plot.top()), QPointF(point.x(), plot.bottom()))
            painter.setPen(QColor(TEXT_MUTED))
            painter.drawText(QRectF(point.x() - 40, plot.bottom() + 4, 80, 16), Qt.AlignHCenter,
                             units.format_quantity(tick, self.x_label if self.x_label in ("s", "Hz") else "", 3))
        for tick in nice_ticks(y0, y1):
            point = to_point(x0, tick)
            painter.setPen(grid)
            painter.drawLine(QPointF(plot.left(), point.y()), QPointF(plot.right(), point.y()))
            painter.setPen(QColor(TEXT_MUTED))
            painter.drawText(QRectF(0, point.y() - 8, plot.left() - 6, 16), Qt.AlignRight | Qt.AlignVCenter,
                             f"{tick:.4g}")
        painter.setPen(QColor(TEXT_MUTED))
        painter.drawText(QRectF(plot.left(), plot.bottom() + 20, plot.width(), 16), Qt.AlignHCenter, self.x_label)
        painter.save()
        painter.translate(12, plot.center().y())
        painter.rotate(-90)
        painter.drawText(QRectF(-plot.height() / 2, -8, plot.height(), 16), Qt.AlignHCenter, self.y_label)
        painter.restore()

        painter.setClipRect(plot)
        for index, (name, (xs, ys)) in enumerate(self.series.items()):
            color = QColor(TRACE_COLORS[index % len(TRACE_COLORS)])
            # (an XY chart marks every point, so its line is not reduced to an envelope)
            points = screen_points(xs, ys, (x0, x1, y0, y1), plot, envelope=self.kind != "xy")
            if self.kind == "xy":
                painter.setPen(QPen(color, 1.5))
                painter.setBrush(color)
                for point in points:
                    painter.drawEllipse(point, 3, 3)
                if len(points) > 1:
                    painter.setBrush(Qt.NoBrush)
                    color.setAlpha(120)
                    painter.setPen(QPen(color, 1))
                    painter.drawPolyline(points)
            elif points:
                painter.setPen(QPen(color, 1.5))
                painter.setBrush(Qt.NoBrush)
                painter.drawPolyline(points)
        painter.setClipping(False)
        if len(self.series) > 1:
            y = plot.top() + 6
            for index, name in enumerate(self.series):
                painter.setPen(QColor(TRACE_COLORS[index % len(TRACE_COLORS)]))
                painter.drawText(QRectF(plot.right() - 160, y, 150, 14), Qt.AlignRight, name)
                y += 14


class ChartDocument(DocumentWidget):
    """A strip chart, XY chart or spectrum of a view node."""

    document_kind = "chart"

    def __init__(self, kind: str, title: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.kind = kind
        self._title = title
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        self.chart = ChartWidget(kind, self)
        layout.addWidget(self.chart, 1)
        self.summary = QLabel("", self)
        set_role(self.summary, "hint")
        layout.addWidget(self.summary)

    @property
    def title(self) -> str:
        return self._title

    def show_value(self, value: Any, options: dict) -> None:
        title = options.get("title") or self._title
        renamed, self._title = title != self._title, title
        if self.kind == "strip_chart" and isinstance(value, dict):
            self.chart.set_series({name: (list(times), list(values)) for name, (times, values) in value.items()})
            count = sum(len(times) for times, _values in value.values())
            self.summary.setText(f"{count} points")
        elif isinstance(value, signals.Table):
            if self.kind == "xy":
                self.chart.set_series({"y": (list(value.columns.get("x", [])), list(value.columns.get("y", [])))},
                                      options.get("x_label", "x"), options.get("y_label", "y"))
                self.summary.setText(f"{len(value)} points")
            else:
                self.chart.set_series({"magnitude": (list(value.columns.get("frequency", [])),
                                                     list(value.columns.get("magnitude", [])))})
                self.summary.setText(f"{len(value)} frequencies")
        if renamed:
            self.document_changed.emit()  # the tab shows the title; a new value changes nothing there


class ValueDocument(DocumentWidget):
    """A number, an LED, a log or a table of a view node."""

    document_kind = "value"

    def __init__(self, kind: str, title: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.kind = kind
        self._title = title
        self.value: Any = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        if kind == "number":
            self.label = QLabel("–", self)
            self.label.setAlignment(Qt.AlignCenter)
            font = self.label.font()
            font.setPointSize(42)
            font.setBold(True)
            self.label.setFont(font)
            layout.addWidget(self.label, 1)
        elif kind == "led":
            self.led = _Led(self)
            layout.addWidget(self.led, 1)
        elif kind == "log":
            self.text = QPlainTextEdit(self)
            self.text.setReadOnly(True)
            layout.addWidget(self.text, 1)
        else:
            self.table = QTableWidget(self)
            self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
            self._columns: list[str] = []
            layout.addWidget(self.table, 1)

    @property
    def title(self) -> str:
        return self._title

    def show_value(self, value: Any, options: dict) -> None:
        title = options.get("title") or self._title
        renamed, self._title = title != self._title, title
        self.value = value
        if self.kind == "number":
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                text = units.format_quantity(value, options.get("unit", ""), int(options.get("digits", 4)))
            else:
                text = str(value)
            self.label.setText(text)
        elif self.kind == "led":
            self.led.set_state(bool(value), options.get("color") or "#7fd18b")
        elif self.kind == "log":
            self.text.setPlainText("\n".join(value if isinstance(value, list) else [str(value)]))
            self.text.verticalScrollBar().setValue(self.text.verticalScrollBar().maximum())
        elif isinstance(value, signals.Table):
            names = list(value.columns)
            rows = len(value)
            # a table that grew by rows (the usual case while a flow runs): only the new ones; one
            # that slid on or was made anew (a buffer, a single value): every row again
            first = self.table.rowCount() if names == self._columns and rows > self.table.rowCount() else 0
            if names != self._columns:
                self._columns = names
                self.table.setColumnCount(len(names))
                self.table.setHorizontalHeaderLabels(names)
            self.table.setRowCount(rows)
            for column, name in enumerate(names):
                cells = value.columns[name]
                for row in range(first, min(rows, len(cells))):
                    cell = cells[row]
                    text = f"{cell:.6g}" if isinstance(cell, float) else str(cell)
                    self.table.setItem(row, column, QTableWidgetItem(text))
        if renamed:
            self.document_changed.emit()


class _Led(QWidget):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.on = False
        self.color = "#7fd18b"
        self.setMinimumSize(80, 80)

    def set_state(self, on: bool, color: str) -> None:
        self.on = on
        self.color = color
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        size = min(self.width(), self.height()) * 0.6
        rect = QRectF((self.width() - size) / 2, (self.height() - size) / 2, size, size)
        color = QColor(self.color if self.on else qcolor("led.off"))
        painter.setPen(QPen(QColor(TEXT if self.on else BORDER), 2))
        painter.setBrush(color)
        painter.drawEllipse(rect)


def view_document(kind: str, title: str) -> Optional[DocumentWidget]:
    if kind in CHART_KINDS:
        return ChartDocument(kind, title)
    if kind in VALUE_KINDS:
        return ValueDocument(kind, title)
    return None
