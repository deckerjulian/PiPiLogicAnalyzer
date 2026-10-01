# Copyright (C) 2026 Julian Decker
#
# Part of PiPiLogicAnalyzer.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Charts of a capture: a bus value over time, and histograms of pulse widths, periods and values."""

from __future__ import annotations

import math
from typing import Callable, Optional

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFontMetricsF, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QSpinBox,
    QTabWidget,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from ...core import colors
from ...core.formatting import to_small_time, to_thousands
from ..theme import BORDER, TEXT_MUTED, set_role
from ..view_model import CaptureViewModel
from .common import button_box, dialog_layout

PLOT_BACKGROUND = QColor(28, 28, 31)
AXIS_COLOR = QColor(TEXT_MUTED)
GRID_COLOR = QColor(52, 52, 58)
MARGIN_LEFT, MARGIN_RIGHT, MARGIN_TOP, MARGIN_BOTTOM = 70, 16, 12, 34

RANGE_ALL = "all"
RANGE_VIEW = "view"
RANGE_CURSORS = "cursors"


def nice_ticks(low: float, high: float, count: int = 6) -> list[float]:
    """Round tick values covering [low, high]."""
    if high <= low:
        return [low]
    raw = (high - low) / max(count, 1)
    magnitude = 10 ** math.floor(math.log10(raw))
    step = next(factor * magnitude for factor in (1, 2, 2.5, 5, 10) if factor * magnitude >= raw)
    first = math.ceil(low / step) * step
    ticks = []
    value = first
    while value <= high + step * 1e-9:
        ticks.append(value)
        value += step
    return ticks


class PlotWidget(QWidget):
    """Axes with either a step line (min/max per pixel) or bars."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(480, 260)
        self.setMouseTracking(True)
        self.x_label: Callable[[float], str] = lambda value: f"{value:g}"
        self.y_label: Callable[[float], str] = lambda value: f"{value:g}"
        self.color = QColor(colors.get_color(1))
        self.message = ""
        #: Step line: x and y of every change
        self.line: Optional[tuple[np.ndarray, np.ndarray, float]] = None
        #: Bars: left edges (len n + 1), heights, and the tooltip label of a bar
        self.bars: Optional[tuple[np.ndarray, np.ndarray]] = None
        self.bar_label: Callable[[int], str] = lambda index: ""
        self._bounds = (0.0, 1.0, 0.0, 1.0)

    def set_line(self, x: np.ndarray, y: np.ndarray, x_end: float) -> None:
        self.line, self.bars, self.message = (x, y, x_end), None, ""
        y_low, y_high = (float(y.min()), float(y.max())) if len(y) else (0.0, 1.0)
        pad = max((y_high - y_low) * 0.05, 0.5)
        self._bounds = (float(x[0]) if len(x) else 0.0, float(x_end), y_low - pad, y_high + pad)
        self.update()

    def set_bars(self, edges: np.ndarray, heights: np.ndarray) -> None:
        self.bars, self.line, self.message = (edges, heights), None, ""
        top = float(heights.max()) if len(heights) else 1.0
        self._bounds = (float(edges[0]), float(edges[-1]), 0.0, top * 1.08 or 1.0)
        self.update()

    def set_message(self, text: str) -> None:
        self.line = self.bars = None
        self.message = text
        self.update()

    def _plot_rect(self) -> QRectF:
        return QRectF(MARGIN_LEFT, MARGIN_TOP, self.width() - MARGIN_LEFT - MARGIN_RIGHT, self.height() - MARGIN_TOP - MARGIN_BOTTOM)

    def _map(self, x: float, y: float) -> QPointF:
        rect = self._plot_rect()
        x0, x1, y0, y1 = self._bounds
        return QPointF(
            rect.left() + (x - x0) / max(x1 - x0, 1e-300) * rect.width(),
            rect.bottom() - (y - y0) / max(y1 - y0, 1e-300) * rect.height(),
        )

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.fillRect(self.rect(), PLOT_BACKGROUND)
        rect = self._plot_rect()
        metrics = QFontMetricsF(painter.font())
        if self.message or (self.line is None and self.bars is None):
            painter.setPen(AXIS_COLOR)
            painter.drawText(self.rect(), Qt.AlignCenter, self.message or "Nothing to show")
            return
        x0, x1, y0, y1 = self._bounds
        for tick in nice_ticks(y0, y1):
            point = self._map(x0, tick)
            painter.setPen(QPen(GRID_COLOR, 1))
            painter.drawLine(QPointF(rect.left(), point.y()), QPointF(rect.right(), point.y()))
            painter.setPen(AXIS_COLOR)
            text = self.y_label(tick)
            painter.drawText(QPointF(rect.left() - 6 - metrics.horizontalAdvance(text), point.y() + metrics.ascent() / 2 - 1), text)
        for tick in nice_ticks(x0, x1, max(int(rect.width() // 110), 2)):
            point = self._map(tick, y0)
            painter.setPen(QPen(GRID_COLOR, 1))
            painter.drawLine(QPointF(point.x(), rect.top()), QPointF(point.x(), rect.bottom()))
            painter.setPen(AXIS_COLOR)
            text = self.x_label(tick)
            painter.drawText(QPointF(point.x() - metrics.horizontalAdvance(text) / 2, rect.bottom() + metrics.ascent() + 6), text)
        painter.setPen(QPen(QColor(BORDER), 1))
        painter.drawRect(rect)

        painter.setClipRect(rect)
        painter.setRenderHint(QPainter.Antialiasing, True)
        if self.bars is not None:
            edges, heights = self.bars
            fill = QColor(self.color)
            fill.setAlpha(170)
            for index, height in enumerate(heights):
                if height <= 0:
                    continue
                top_left = self._map(float(edges[index]), float(height))
                bottom_right = self._map(float(edges[index + 1]), 0.0)
                bar = QRectF(top_left, bottom_right).adjusted(0.5, 0, -0.5, 0)
                painter.fillRect(bar, fill)
        elif self.line is not None:
            x, y, x_end = self.line
            path = QPainterPath()
            columns = int(rect.width())
            if len(x) > columns * 4:
                # Envelope: the lowest and highest value of every pixel column
                position = ((x - x0) / max(x1 - x0, 1e-300) * columns).astype(np.int64).clip(0, columns - 1)
                lows = np.full(columns, np.inf)
                highs = np.full(columns, -np.inf)
                np.minimum.at(lows, position, y)
                np.maximum.at(highs, position, y)
                fill = QColor(self.color)
                fill.setAlpha(140)
                painter.setPen(QPen(fill, 1))
                for column in np.flatnonzero(np.isfinite(lows)):
                    low = self._map(x0, lows[column]).y()
                    high = self._map(x0, highs[column]).y()
                    painter.drawLine(QPointF(rect.left() + column + 0.5, low), QPointF(rect.left() + column + 0.5, high))
                return
            for index in range(len(x)):
                point = self._map(float(x[index]), float(y[index]))
                if index == 0:
                    path.moveTo(point)
                else:
                    path.lineTo(QPointF(point.x(), path.currentPosition().y()))
                    path.lineTo(point)
            path.lineTo(QPointF(self._map(x_end, 0).x(), path.currentPosition().y()))
            painter.setPen(QPen(self.color, 1.5))
            painter.drawPath(path)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self.bars is None:
            return
        rect = self._plot_rect()
        x0, x1, _y0, _y1 = self._bounds
        value = x0 + (event.position().x() - rect.left()) / max(rect.width(), 1) * (x1 - x0)
        edges, _heights = self.bars
        index = int(np.searchsorted(edges, value, side="right")) - 1
        if 0 <= index < len(edges) - 1:
            QToolTip.showText(event.globalPosition().toPoint(), self.bar_label(index), self)


class ChartDialog(QDialog):
    def __init__(self, model: CaptureViewModel, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.model = model
        self.setWindowTitle("Charts")
        self.resize(900, 560)
        layout = dialog_layout(self)

        row = QHBoxLayout()
        row.addWidget(QLabel("Range", self))
        self.range_combo = QComboBox(self)
        self.range_combo.addItem("Whole capture", RANGE_ALL)
        self.range_combo.addItem("Visible part", RANGE_VIEW)
        self.range_combo.addItem("Between the cursors", RANGE_CURSORS)
        self.range_combo.currentIndexChanged.connect(self.refresh)
        row.addWidget(self.range_combo)
        row.addStretch(1)
        layout.addLayout(row)

        self.tabs = QTabWidget(self)
        layout.addWidget(self.tabs, 1)

        # ------------------------------------------------------- value chart
        page = QWidget(self.tabs)
        page_layout = QVBoxLayout(page)
        controls = QHBoxLayout()
        controls.addWidget(QLabel("Bus", page))
        self.chart_bus = QComboBox(page)
        for bus in model.buses:
            self.chart_bus.addItem(bus.name, bus)
        self.chart_bus.currentIndexChanged.connect(self.refresh)
        controls.addWidget(self.chart_bus, 1)
        page_layout.addLayout(controls)
        self.chart = PlotWidget(page)
        page_layout.addWidget(self.chart, 1)
        self.tabs.addTab(page, "Bus value over time")

        # --------------------------------------------------------- histogram
        page = QWidget(self.tabs)
        page_layout = QVBoxLayout(page)
        controls = QHBoxLayout()
        self.histogram_source = QComboBox(page)
        for channel in model.channels:
            for key, label in (("high", "high pulses"), ("low", "low pulses"), ("period", "periods")):
                self.histogram_source.addItem(f"{channel.display_name}: {label}", (key, channel))
        for bus in model.buses:
            self.histogram_source.addItem(f"{bus.name}: values", ("bus", bus))
        self.histogram_source.currentIndexChanged.connect(self.refresh)
        controls.addWidget(self.histogram_source, 1)
        controls.addWidget(QLabel("Bins", page))
        self.bins_box = QSpinBox(page)
        self.bins_box.setRange(5, 500)
        self.bins_box.setValue(50)
        self.bins_box.valueChanged.connect(self.refresh)
        controls.addWidget(self.bins_box)
        page_layout.addLayout(controls)
        self.histogram = PlotWidget(page)
        page_layout.addWidget(self.histogram, 1)
        self.summary = QLabel(page)
        set_role(self.summary, "hint")
        page_layout.addWidget(self.summary)
        self.tabs.addTab(page, "Histogram")
        self.tabs.currentChanged.connect(self.refresh)

        buttons = button_box(self, None)
        layout.addWidget(buttons)
        if not model.buses:
            self.tabs.setCurrentIndex(1)
        self.refresh()

    def _range(self) -> tuple[int, int]:
        model = self.model
        mode = self.range_combo.currentData()
        if mode == RANGE_VIEW:
            return model.first_sample, min(model.first_sample + model.visible_samples, model.sample_count)
        if mode == RANGE_CURSORS and model.cursor("A") is not None and model.cursor("B") is not None:
            low, high = sorted((model.cursor("A"), model.cursor("B")))
            return low, high + 1
        return 0, model.sample_count

    def refresh(self) -> None:
        if self.tabs.currentIndex() == 0:
            self._refresh_chart()
        else:
            self._refresh_histogram()

    def _refresh_chart(self) -> None:
        from ...core.buses import bus_runs, format_value

        bus = self.chart_bus.currentData()
        if bus is None:
            self.chart.set_message("Define a bus first (View > Buses and groups)")
            return
        start, end = self._range()
        values = self.model.bus_values(bus)
        if end <= start:
            self.chart.set_message("The range is empty")
            return
        starts, run_values = bus_runs(values, start, end)
        times = (starts - self.model.pre_trigger_samples) / max(self.model.frequency, 1)
        end_time = (end - self.model.pre_trigger_samples) / max(self.model.frequency, 1)
        self.chart.x_label = to_small_time
        self.chart.y_label = lambda value, bus=bus: format_value(int(round(value)), bus) if value >= 0 else ""
        self.chart.set_line(times, run_values.astype(np.float64), end_time)

    def _refresh_histogram(self) -> None:
        from ...core.statistics import bus_value_counts, histogram, pulse_widths

        data = self.histogram_source.currentData()
        if data is None:
            self.histogram.set_message("No channels")
            return
        kind, source = data
        start, end = self._range()
        bins = self.bins_box.value()
        if kind == "bus":
            from ...core.buses import format_value

            values, counts = bus_value_counts(self.model.bus_values(source)[start:end])
            if not len(values):
                self.histogram.set_message("No values in the range")
                return
            order = np.argsort(values)
            values, counts = values[order], counts[order]
            edges = np.append(values.astype(np.float64) - 0.5, float(values[-1]) + 0.5)
            self.histogram.x_label = lambda value, bus=source: format_value(int(round(value)), bus) if value >= 0 else ""
            self.histogram.y_label = lambda value: to_thousands(int(value))
            self.histogram.bar_label = lambda index: f"{format_value(int(values[index]), source)}: {to_thousands(int(counts[index]))} samples"
            self.histogram.set_bars(edges, counts.astype(np.float64))
            top = np.argsort(counts)[::-1][:5]
            self.summary.setText(
                f"{len(values)} distinct values. Most frequent: "
                + ", ".join(f"{format_value(int(values[i]), source)} ({to_thousands(int(counts[i]))})" for i in top)
            )
            return

        samples = source.samples[start:end] if source.samples is not None else None
        if samples is None or not len(samples):
            self.histogram.set_message("No samples in the range")
            return
        frequency = self.model.frequency
        if kind == "period":
            from ...core.statistics import clock_edge_positions

            rising = clock_edge_positions(samples)
            widths = np.diff(rising) / max(frequency, 1)
        else:
            widths = pulse_widths(samples, frequency, 1 if kind == "high" else 0)
        if not len(widths):
            self.histogram.set_message("No complete pulses in the range")
            return
        counts, edges = histogram(widths, bins)
        self.histogram.x_label = to_small_time
        self.histogram.y_label = lambda value: to_thousands(int(value))
        self.histogram.bar_label = (
            lambda index: f"{to_small_time(edges[index])} – {to_small_time(edges[index + 1])}: {to_thousands(int(counts[index]))}"
        )
        self.histogram.set_bars(edges, counts.astype(np.float64))
        self.summary.setText(
            f"{to_thousands(len(widths))} {'periods' if kind == 'period' else 'pulses'}: min {to_small_time(widths.min())}, "
            f"mean {to_small_time(widths.mean())}, max {to_small_time(widths.max())}, σ {to_small_time(widths.std())}"
        )
