# Copyright (C) 2026 Julian Decker
#
# Part of PiPiLogicAnalyzer.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Buses and groups: the value of several channels as one row of hex, decimal or symbol boxes."""

from __future__ import annotations

from typing import Optional

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFontMetricsF, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QMenu, QSizePolicy, QToolTip, QWidget

from ...core import colors
from ...core.formatting import to_small_time, to_thousands
from ..view_model import CaptureViewModel
from . import overlays
from .channel_viewer import CHANNEL_COLUMN_WIDTH
from .navigation import WheelNavigator

BUS_ROW_HEIGHT = 28
#: Runs narrower than this many pixels are merged into a "busy" band
MIN_RUN_WIDTH = 2.0
BUS_BASE_COLOR = QColor("#8AAEFF")


def bus_color(bus, index: int) -> QColor:
    return colors.color_from_uint(bus.color) if bus.color is not None else colors.get_color(index + 3)


class BusViewer(QWidget):
    """One row per bus of the capture, aligned with the waveform below it."""

    #: The definition of a bus was asked for (``None``: a new bus)
    edit_requested = Signal(object)

    def __init__(self, model: CaptureViewModel, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.model = model
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFixedHeight(0)

        model.buses_changed.connect(self._on_buses_changed)
        model.capture_changed.connect(self._on_buses_changed)
        model.samples_appended.connect(self.update)
        for signal in (model.view_changed, model.cursors_changed, model.bookmarks_changed, model.search_changed):
            signal.connect(self.update)

    def _on_buses_changed(self) -> None:
        count = len(self.model.buses) if self.model.sample_count else 0
        self.setFixedHeight(count * BUS_ROW_HEIGHT)
        self.update()

    def _data_width(self) -> float:
        return max(self.width() - CHANNEL_COLUMN_WIDTH, 1)

    def _x_for(self, sample: float) -> float:
        return CHANNEL_COLUMN_WIDTH + (sample - self.model.first_sample) * self._data_width() / max(
            self.model.visible_samples, 1
        )

    # --------------------------------------------------------------- painting
    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        buses = self.model.buses
        if not buses or not self.model.sample_count:
            return
        painter = QPainter(self)
        painter.fillRect(QRectF(0, 0, CHANNEL_COLUMN_WIDTH, self.height()), colors.BG_CHANNEL_COLORS[1])
        painter.fillRect(
            QRectF(CHANNEL_COLUMN_WIDTH, 0, self._data_width(), self.height()), colors.BG_CHANNEL_COLORS[0]
        )
        metrics = QFontMetricsF(painter.font())
        for index, bus in enumerate(buses):
            top = index * BUS_ROW_HEIGHT
            self._draw_name(painter, metrics, bus, index, top)
            if self.model.is_live:
                # The values of every sample would be computed again for each frame of a stream
                painter.setPen(QColor(150, 150, 160))
                painter.drawText(
                    QRectF(CHANNEL_COLUMN_WIDTH + 8, top, self._data_width(), BUS_ROW_HEIGHT),
                    Qt.AlignVCenter | Qt.AlignLeft, "shown when the capture is complete",
                )
                continue
            painter.save()
            painter.setClipRect(QRectF(CHANNEL_COLUMN_WIDTH, top, self._data_width(), BUS_ROW_HEIGHT))
            painter.setRenderHint(QPainter.Antialiasing, True)
            self._draw_values(painter, metrics, bus, index, top)
            painter.restore()
            painter.setPen(QPen(colors.ROW_SEPARATOR_COLOR, 1))
            painter.drawLine(QPointF(0, top + BUS_ROW_HEIGHT - 0.5), QPointF(self.width(), top + BUS_ROW_HEIGHT - 0.5))

        data = QRectF(CHANNEL_COLUMN_WIDTH, 0, self._data_width(), self.height())
        painter.setClipRect(data)
        overlays.draw_cursors(painter, self.model, self._x_for, QRectF(0, 0, self.width(), self.height()))

    def _draw_name(self, painter: QPainter, metrics: QFontMetricsF, bus, index: int, top: float) -> None:
        color = bus_color(bus, index)
        painter.fillRect(QRectF(0, top, 4, BUS_ROW_HEIGHT), color)
        painter.setPen(colors.TEXT_COLOR)
        name = metrics.elidedText(bus.name, Qt.ElideRight, CHANNEL_COLUMN_WIDTH - 60)
        baseline = top + BUS_ROW_HEIGHT / 2 + metrics.ascent() / 2 - 1
        painter.drawText(QPointF(12, baseline), name)
        painter.setPen(QColor(150, 150, 160))
        width = f"[{len(bus.channels)}]"
        painter.drawText(QPointF(CHANNEL_COLUMN_WIDTH - 8 - metrics.horizontalAdvance(width), baseline), width)

    def _draw_values(self, painter: QPainter, metrics: QFontMetricsF, bus, index: int, top: float) -> None:
        from ...core.buses import bus_runs, format_value

        model = self.model
        values = model.bus_values(bus)
        first = model.first_sample
        last = min(first + model.visible_samples, len(values))
        if last <= first:
            return
        color = bus_color(bus, index)
        fill = QColor(color)
        fill.setAlpha(46)
        busy = QColor(color)
        busy.setAlpha(110)
        pixels_per_sample = self._data_width() / max(model.visible_samples, 1)
        starts, run_values = bus_runs(values, first, last)
        ends = np.append(starts[1:], last)
        rect_top, rect_bottom = top + 4, top + BUS_ROW_HEIGHT - 4
        middle = (rect_top + rect_bottom) / 2

        # Runs too narrow to draw are shown as one band per pixel column
        wide = (ends - starts) * pixels_per_sample >= MIN_RUN_WIDTH
        if not wide.all():
            narrow_x = ((starts[~wide] - first) * pixels_per_sample).astype(np.int64)
            for x in np.unique(narrow_x):
                painter.fillRect(QRectF(CHANNEL_COLUMN_WIDTH + x, rect_top, 1, rect_bottom - rect_top), busy)

        pen = QPen(color, 1.2)
        for start, end, value in zip(starts[wide], ends[wide], run_values[wide]):
            x1, x2 = self._x_for(start), self._x_for(end)
            slope = min(4.0, (x2 - x1) / 4)
            path = QPainterPath()
            path.moveTo(x1, middle)
            path.lineTo(x1 + slope, rect_top)
            path.lineTo(x2 - slope, rect_top)
            path.lineTo(x2, middle)
            path.lineTo(x2 - slope, rect_bottom)
            path.lineTo(x1 + slope, rect_bottom)
            path.closeSubpath()
            painter.fillPath(path, fill)
            painter.setPen(pen)
            painter.drawPath(path)
            text = format_value(int(value), bus)
            available = x2 - x1 - 2 * slope - 4
            if available > 8:
                painter.setPen(colors.TEXT_COLOR)
                shown = metrics.elidedText(text, Qt.ElideRight, available)
                # Keep the label of a run that starts left of the view readable
                left = max(x1 + slope + 2, CHANNEL_COLUMN_WIDTH + 2)
                center = max((x1 + x2) / 2, left + metrics.horizontalAdvance(shown) / 2)
                center = min(center, x2 - slope - 2 - metrics.horizontalAdvance(shown) / 2)
                painter.drawText(QPointF(center - metrics.horizontalAdvance(shown) / 2, middle + metrics.ascent() / 2 - 1), shown)

    # ------------------------------------------------------------ interaction
    def _bus_at(self, y: float):
        buses = self.model.buses
        index = int(y // BUS_ROW_HEIGHT)
        return (index, buses[index]) if 0 <= index < len(buses) else (None, None)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        from ...core.buses import format_value

        index, bus = self._bus_at(event.position().y())
        if bus is None or event.position().x() < CHANNEL_COLUMN_WIDTH or not self.model.sample_count or self.model.is_live:
            QToolTip.hideText()
            return
        sample = self.model.sample_at(event.position().x() - CHANNEL_COLUMN_WIDTH, self._data_width())
        values = self.model.bus_values(bus)
        if not 0 <= sample < len(values):
            return
        value = int(values[sample])
        text = (
            f"{bus.name}: {format_value(value, bus)}\n"
            f"0x{value:X} = {value}\n"
            f"Sample {to_thousands(sample)}, t = {to_small_time(self.model.time_of(sample))}"
        )
        QToolTip.showText(event.globalPosition().toPoint(), text, self)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 - Qt naming
        _index, bus = self._bus_at(event.position().y())
        if bus is not None:
            self.edit_requested.emit(bus)

    def contextMenuEvent(self, event) -> None:  # noqa: N802 - Qt naming
        _index, bus = self._bus_at(event.pos().y())
        menu = QMenu(self)
        edit = menu.addAction("Edit bus...")
        edit.setEnabled(bus is not None)
        remove = menu.addAction("Remove bus")
        remove.setEnabled(bus is not None)
        menu.addSeparator()
        add = menu.addAction("New bus...")
        chosen = menu.exec(event.globalPos())
        if chosen is edit:
            self.edit_requested.emit(bus)
        elif chosen is remove:
            self.model.set_buses([item for item in self.model.buses if item is not bus])
        elif chosen is add:
            self.edit_requested.emit(None)

    @property
    def navigator(self) -> WheelNavigator:
        if getattr(self, "_navigator", None) is None:
            self._navigator = WheelNavigator(self, self.model)
        return self._navigator

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self.navigator.handle_wheel(event):
            event.accept()
        else:
            event.ignore()
