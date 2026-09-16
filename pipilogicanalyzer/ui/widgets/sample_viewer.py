# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of PiPiLogicAnalyzer, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Waveform display (port of ``Controls/SampleViewer.axaml.cs``).

Rendering strategy
------------------
The original renderer walked every visible sample and, for each of them, every
channel, which made the UI unusable as soon as more than a few thousand samples
were on screen.  Two paths are used here instead, both proportional to the
*width in pixels* of the widget rather than to the number of samples:

``exact``
    when a sample is at least one pixel wide the transitions inside the window
    are read from the pre-built edge index and drawn as a single path;

``dense``
    when more samples than pixels are visible the state of every pixel column is
    derived from the edge index with two ``searchsorted`` calls (low, high or
    "busy"), consecutive equal columns are merged and drawn as a handful of
    lines and rectangles.
"""

from __future__ import annotations

from typing import Optional

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFontMetricsF, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QToolTip, QWidget

from ...core import colors
from ...core.analysis import ChannelTransitions
from ...core.formatting import to_inferred_frequency, to_small_time
from ..theme import WARNING
from ..view_model import DEFAULT_CHANNEL_HEIGHT, CaptureViewModel
from .navigation import WheelNavigator

#: Default minimum height of a channel; the current one is ``CaptureViewModel.channel_height``.
MIN_CHANNEL_HEIGHT = DEFAULT_CHANNEL_HEIGHT
GRID_SAMPLE_LIMIT = 200
DOT_GRID_SAMPLE_LIMIT = 100


class SampleViewer(QWidget):
    """Draws the captured channels."""

    sample_double_clicked = Signal(int)

    def __init__(self, model: CaptureViewModel, parent: Optional[QWidget] = None, section: str = "all") -> None:
        super().__init__(parent)
        self.model = model
        #: Part of the waveform: ``all``, ``pinned`` or ``scrolling`` channels.
        self.section = section
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumHeight(model.channel_height)
        self.setAutoFillBackground(False)
        self.setFocusPolicy(Qt.StrongFocus)

        self._pan_origin: Optional[tuple[float, int]] = None
        self._last_tooltip = ""

        model.capture_changed.connect(self._on_capture_changed)
        model.view_changed.connect(self.update)
        model.regions_changed.connect(self.update)
        model.marker_changed.connect(self.update)
        model.hover_changed.connect(self.update)
        model.channels_changed.connect(self._on_capture_changed)
        model.channel_height_changed.connect(self._on_capture_changed)

    # ------------------------------------------------------------------ state
    def _channels(self) -> list:
        return self.model.section_channels(self.section)

    def _on_capture_changed(self) -> None:
        channel_count = max(len(self._channels()), 1)
        self.setMinimumHeight(channel_count * self.model.channel_height)
        self.update()

    def channel_height(self) -> float:
        channels = max(len(self._channels()), 1)
        return self.height() / channels

    def sample_width(self) -> float:
        return self.width() / max(self.model.visible_samples, 1)

    # --------------------------------------------------------------- painting
    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)
        bounds = QRectF(0, 0, self.width(), self.height())

        channels = self._channels()
        if not channels or self.model.sample_count == 0:
            painter.fillRect(bounds, colors.BG_CHANNEL_COLORS[0])
            return

        channel_height = bounds.height() / len(channels)
        for index in range(len(channels)):
            painter.fillRect(
                QRectF(0, index * channel_height, bounds.width(), channel_height),
                colors.BG_CHANNEL_COLORS[index % 2],
            )

        self._draw_regions(painter, bounds)
        self._draw_grid(painter, bounds)

        margin = channel_height / 5.0
        for index, channel in enumerate(channels):
            transitions = self.model.transitions_for(channel)
            if transitions is None or transitions.sample_count == 0:
                continue
            top = index * channel_height + margin
            bottom = top + channel_height - margin * 2
            self._draw_channel(painter, transitions, top, bottom, channel)

        self._draw_markers(painter, bounds)
        self._draw_hover(painter, bounds, channel_height)

    def _x_for(self, sample: float) -> float:
        return (sample - self.model.first_sample) * self.sample_width()

    def _draw_regions(self, painter: QPainter, bounds: QRectF) -> None:
        sample_width = self.sample_width()
        for region in self.model.regions_in_view():
            start = self._x_for(region.start)
            width = max(region.sample_count * sample_width, 1.0)
            painter.fillRect(QRectF(start, 0, width, bounds.height()), region.region_color)

    def _draw_grid(self, painter: QPainter, bounds: QRectF) -> None:
        visible = self.model.visible_samples
        if visible >= GRID_SAMPLE_LIMIT:
            return

        sample_width = self.sample_width()
        first = self.model.first_sample
        last = min(first + visible, self.model.sample_count)

        pen = QPen(colors.SAMPLE_LINE_COLOR, 1)
        painter.setPen(pen)
        for sample in range(first, last):
            x = (sample - first) * sample_width + sample_width / 2.0
            painter.drawLine(QPointF(x, 0), QPointF(x, bounds.height()))

        if visible < DOT_GRID_SAMPLE_LIMIT:
            painter.setPen(QPen(colors.SAMPLE_DASH_COLOR, 1))
            for sample in range(first, last):
                x = (sample - first) * sample_width
                painter.drawLine(QPointF(x, 0), QPointF(x, bounds.height()))

    def _draw_channel(
        self,
        painter: QPainter,
        transitions: ChannelTransitions,
        top: float,
        bottom: float,
        channel,
    ) -> None:
        color = colors.get_channel_color(channel)
        pen = QPen(color, 2)
        pen.setCosmetic(True)
        painter.setPen(pen)

        first = self.model.first_sample
        visible = self.model.visible_samples
        width = self.width()

        if visible <= width:
            self._draw_exact(painter, transitions, top, bottom, first, visible)
        else:
            self._draw_dense(painter, transitions, top, bottom, first, visible, color)

    def _draw_exact(
        self,
        painter: QPainter,
        transitions: ChannelTransitions,
        top: float,
        bottom: float,
        first: int,
        visible: int,
    ) -> None:
        last = min(first + visible, transitions.sample_count) - 1
        starts, values = transitions.runs_in_range(first, last)
        if starts.size == 0:
            return

        sample_width = self.sample_width()
        path = QPainterPath()

        def x_of(sample: float) -> float:
            return (sample - first) * sample_width

        y_of = lambda value: top if value else bottom  # noqa: E731 - tiny helper

        path.moveTo(x_of(max(int(starts[0]), first)), y_of(values[0]))
        for index in range(1, starts.size):
            edge_x = x_of(int(starts[index]))
            path.lineTo(edge_x, y_of(values[index - 1]))
            path.lineTo(edge_x, y_of(values[index]))

        end_sample = min(first + visible, transitions.sample_count)
        path.lineTo(x_of(end_sample), y_of(values[-1]))
        painter.drawPath(path)

    def _draw_dense(
        self,
        painter: QPainter,
        transitions: ChannelTransitions,
        top: float,
        bottom: float,
        first: int,
        visible: int,
        color: QColor,
    ) -> None:
        width = int(self.width())
        if width <= 0:
            return

        boundaries = first + (np.arange(width + 1, dtype=np.float64) * visible / width)
        boundaries = np.clip(boundaries, 0, max(transitions.sample_count, 1))

        run_at_boundary = np.searchsorted(transitions.starts, boundaries[:-1], side="right") - 1
        run_at_boundary = np.clip(run_at_boundary, 0, len(transitions) - 1)
        level = transitions.values[run_at_boundary]

        edges_before = np.searchsorted(transitions.starts, boundaries, side="left")
        edges_in_column = np.diff(edges_before)

        # 0 = low, 1 = high, 2 = at least one edge inside the column.
        state = np.where(edges_in_column > 0, 2, level).astype(np.int8)

        changes = np.flatnonzero(state[1:] != state[:-1]) + 1
        starts = np.concatenate(([0], changes))
        ends = np.concatenate((changes, [width]))

        busy_color = QColor(color)
        busy_color.setAlpha(150)

        for start, end in zip(starts, ends):
            value = state[start]
            if value == 2:
                painter.fillRect(QRectF(float(start), top, float(end - start), bottom - top), busy_color)
            else:
                y = top if value else bottom
                painter.drawLine(QPointF(float(start), y), QPointF(float(end), y))

    def _draw_markers(self, painter: QPainter, bounds: QRectF) -> None:
        session = self.model.session
        first = self.model.first_sample
        last = first + self.model.visible_samples

        if session is not None and first <= session.pre_trigger_samples <= last:
            painter.setPen(QPen(colors.TRIGGER_LINE_COLOR, 2))
            x = self._x_for(session.pre_trigger_samples)
            painter.drawLine(QPointF(x, 0), QPointF(x, bounds.height()))

        if session is not None and session.bursts:
            pen = QPen(colors.BURST_LINE_COLOR, 2)
            pen.setStyle(Qt.DashDotLine)
            painter.setPen(pen)
            for burst in session.bursts:
                if first <= burst.burst_sample_start <= last:
                    x = self._x_for(burst.burst_sample_start)
                    painter.drawLine(QPointF(x, 0), QPointF(x, bounds.height()))

        marker = self.model.user_marker
        if marker is not None and first <= marker <= last:
            pen = QPen(colors.USER_LINE_COLOR, 2)
            pen.setStyle(Qt.DashDotLine)
            painter.setPen(pen)
            x = self._x_for(marker)
            painter.drawLine(QPointF(x, 0), QPointF(x, bounds.height()))

    def _draw_hover(self, painter: QPainter, bounds: QRectF, channel_height: float) -> None:
        """Mark the hovered annotation and label the level and weight of every channel it read."""
        hover = self.model.hover
        if hover is None:
            return

        segment = hover.segment
        color = QColor(colors.get_color(hover.group.color_index))
        start = self._x_for(segment.first_sample)
        end = self._x_for(max(segment.last_sample, segment.first_sample + 1))
        band = QColor(color)
        band.setAlpha(45)
        painter.fillRect(QRectF(start, 0, max(end - start, 2.0), bounds.height()), band)
        edge = QColor(color)
        edge.setAlpha(210)
        painter.setPen(QPen(edge, 1))
        painter.drawLine(QPointF(start, 0), QPointF(start, bounds.height()))
        painter.drawLine(QPointF(end, 0), QPointF(end, bounds.height()))

        composition = hover.composition
        if composition is None:
            return

        sample_x = self._x_for(composition.sample) + self.sample_width() / 2.0
        pen = QPen(colors.TEXT_COLOR, 1)
        pen.setStyle(Qt.DashLine)
        painter.setPen(pen)
        painter.drawLine(QPointF(sample_x, 0), QPointF(sample_x, bounds.height()))

        bits = {bit.capture_index: bit for bit in composition.bits}
        capture_indexes = {id(channel): index for index, channel in enumerate(self.model.channels)}
        painter.setRenderHint(QPainter.Antialiasing, True)
        metrics = QFontMetricsF(painter.font())

        for row, channel in enumerate(self._channels()):
            bit = bits.get(capture_indexes.get(id(channel), -1))
            if bit is None:
                continue
            text = bit.label
            bus = composition.bus_of(bit)
            if bus is not None and bus.bits[0] is bit:
                # The most significant bit carries the value of the whole bus.
                text += f"   {bus.name} = {bus.hexadecimal}"

            width = metrics.horizontalAdvance(text) + 14
            height = metrics.height() + 6
            y = row * channel_height + (channel_height - height) / 2
            x = sample_x + 8
            if x + width > bounds.width():
                x = sample_x - 8 - width
            box = QRectF(x, y, width, height)

            painter.setBrush(QColor(18, 18, 20, 225))
            if bit.changing:
                # The level changes next to the read point: the value may be unreliable.
                painter.setPen(QPen(QColor(WARNING), 2))
            else:
                painter.setPen(QPen(colors.get_channel_color(channel), 1.5 if bit.level else 1))
            painter.drawRoundedRect(box, 4, 4)
            painter.setPen(QPen(colors.TEXT_COLOR))
            painter.drawText(box, Qt.AlignCenter, text)

        self._draw_bus_summary(painter, bounds, composition, sample_x, color, metrics)
        painter.setBrush(Qt.NoBrush)

    def _draw_bus_summary(self, painter: QPainter, bounds: QRectF, composition, sample_x: float,
                          color: QColor, metrics: QFontMetricsF) -> None:
        """Bus values at the top of the visible part, the bus rows may be scrolled out of view."""
        if not composition.buses:
            return
        lines = [bus.describe() for bus in composition.buses]
        width = max(metrics.horizontalAdvance(line) for line in lines) + 16
        height = metrics.height() * len(lines) + 10
        visible = QRectF(self.visibleRegion().boundingRect())
        if visible.isEmpty():
            visible = bounds
        # In the top corner away from the sample point, clear of the channel labels next to it.
        if sample_x < bounds.width() / 2:
            x = max(bounds.width() - width - 8, 0.0)
        else:
            x = 8.0
        box = QRectF(x, visible.top() + 6, width, height)

        painter.setBrush(QColor(18, 18, 20, 235))
        painter.setPen(QPen(color, 1.5))
        painter.drawRoundedRect(box, 5, 5)
        painter.setPen(QPen(colors.TEXT_COLOR))
        for index, line in enumerate(lines):
            painter.drawText(
                QPointF(box.left() + 8, box.top() + 5 + metrics.ascent() + index * metrics.height()), line
            )

    # ------------------------------------------------------------ interaction
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

    def event(self, event) -> bool:  # noqa: A003 - Qt naming
        if self.navigator.handle_gesture(event):
            return True
        return super().event(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.LeftButton:
            self._pan_origin = (event.position().x(), self.model.first_sample)
            self.setCursor(Qt.ClosedHandCursor)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.LeftButton:
            self._pan_origin = None
            self.unsetCursor()
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 - Qt naming
        sample = self.model.sample_at(event.position().x(), self.width())
        self.sample_double_clicked.emit(sample)
        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._pan_origin is not None:
            origin_x, origin_sample = self._pan_origin
            delta_samples = (origin_x - event.position().x()) / max(self.sample_width(), 1e-9)
            self.model.scroll_to(int(origin_sample + delta_samples))
            return

        self._update_tooltip(event)
        super().mouseMoveEvent(event)

    def _update_tooltip(self, event) -> None:
        channels = self._channels()
        if not channels:
            return

        channel_index = int(event.position().y() // max(self.channel_height(), 1))
        if not 0 <= channel_index < len(channels):
            QToolTip.hideText()
            return

        transitions = self.model.transitions_for(channels[channel_index])
        if transitions is None:
            return

        sample = self.model.sample_at(event.position().x(), self.width())
        interval = transitions.interval_at(sample)
        if interval is None:
            QToolTip.hideText()
            self._last_tooltip = ""
            return

        text = (
            f"{channels[channel_index].display_name}\n"
            f"Sample: {sample:,}\n"
            f"State: {'High' if interval.value else 'Low'}\n"
            f"Length: {to_small_time(interval.duration)} ({interval.sample_count:,} samples)\n"
            f"Inferred frequency: {to_inferred_frequency(interval.duration)}"
        )
        if text != self._last_tooltip:
            self._last_tooltip = text
        QToolTip.showText(event.globalPosition().toPoint(), text, self)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        QToolTip.hideText()
        self._last_tooltip = ""
        super().leaveEvent(event)
