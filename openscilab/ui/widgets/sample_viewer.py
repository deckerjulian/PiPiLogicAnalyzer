# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of openSciLab, a port and extension of his software;
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
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFontMetricsF, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QToolTip, QWidget

from .. import colors
from ...core.analysis import ChannelTransitions
from ...core.overview import column_envelope
from ...core.formatting import to_inferred_frequency, to_small_time
from ..theme import WARNING
from ..view_model import DEFAULT_CHANNEL_HEIGHT, CaptureViewModel
from . import overlays, time_axis
from .navigation import WheelNavigator

#: Default minimum height of a channel; the current one is ``CaptureViewModel.channel_height``.
MIN_CHANNEL_HEIGHT = DEFAULT_CHANNEL_HEIGHT
#: Sample boundaries are drawn from this width of a sample in pixels on
SAMPLE_GRID_WIDTH = 12
#: Values listed next to an entry made of several (the bus cycles of an instruction)
MAX_PART_LINES = 12
#: Width of the signal line, and the opacity (0-255) of the area under a high level and of a
#: column with edges
LINE_WIDTH = 1.5
HIGH_FILL_ALPHA = 34
BUSY_FILL_ALPHA = 80


class SampleViewer(QWidget):
    """Draws the captured channels."""

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
        #: Cursor being dragged, and the sample under the pointer (for the A/B/M keys)
        self._dragged_cursor: Optional[str] = None
        self._pointer_sample: Optional[int] = None

        model.capture_changed.connect(self._on_capture_changed)
        model.view_changed.connect(self.update)
        model.samples_appended.connect(self.update)
        model.regions_changed.connect(self.update)
        model.marker_changed.connect(self.update)
        model.hover_changed.connect(self.update)
        model.channels_changed.connect(self._on_capture_changed)
        model.channel_height_changed.connect(self._on_capture_changed)
        model.cursors_changed.connect(self.update)
        model.bookmarks_changed.connect(self.update)
        model.search_changed.connect(self.update)
        model.tiles_arrived.connect(self.update)

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

        self._draw_grid(painter, bounds)
        self._draw_regions(painter, bounds)

        painter.setPen(QPen(colors.ROW_SEPARATOR_COLOR, 1))
        for index in range(1, len(channels)):
            y = round(index * channel_height) - 0.5
            painter.drawLine(QPointF(0, y), QPointF(bounds.width(), y))

        # Crisp levels: on pixel centres, antialiased edges
        margin = max(channel_height * 0.22, 3.0)
        painter.setRenderHint(QPainter.Antialiasing, True)
        progressive = self.model.progressive
        for index, channel in enumerate(channels):
            top = round(index * channel_height + margin) + 0.5
            bottom = round((index + 1) * channel_height - margin) - 0.5
            if progressive is not None:
                self._draw_progressive(painter, channel, top, bottom, progressive)
                continue
            transitions = self.model.transitions_for(channel)
            if transitions is None or transitions.sample_count == 0:
                continue
            self._draw_channel(painter, transitions, top, bottom, channel)
        painter.setRenderHint(QPainter.Antialiasing, False)
        if progressive is not None:
            self._draw_missing(painter, bounds, progressive)

        self._draw_markers(painter, bounds)
        overlays.draw_search_hits(painter, self.model, self._x_for, bounds)
        overlays.draw_bookmarks(painter, self.model, self._x_for, bounds)
        overlays.draw_selection(painter, self.model, self._x_for, bounds)
        overlays.draw_cursors(painter, self.model, self._x_for, bounds)
        self._draw_hover(painter, bounds, channel_height)

    def _x_for(self, sample: float) -> float:
        return (sample - self.model.first_sample) * self.sample_width()

    def _draw_regions(self, painter: QPainter, bounds: QRectF) -> None:
        sample_width = self.sample_width()
        for region in self.model.regions_in_view():
            start = self._x_for(region.start)
            width = max(region.sample_count * sample_width, 1.0)
            painter.fillRect(QRectF(start, 0, width, bounds.height()), colors.region_color(region))

    def _draw_grid(self, painter: QPainter, bounds: QRectF) -> None:
        """The time divisions of the ruler, and the sample boundaries when zoomed far in."""
        first = self.model.first_sample
        visible = self.model.visible_samples
        sample_width = self.sample_width()
        if sample_width >= SAMPLE_GRID_WIDTH:
            painter.setPen(QPen(colors.SAMPLE_LINE_COLOR, 1))
            for sample in range(first, min(first + visible, self.model.sample_count) + 1):
                x = round(self._x_for(sample)) + 0.5
                painter.drawLine(QPointF(x, 0), QPointF(x, bounds.height()))

        for tick in time_axis.model_ticks(self.model, bounds.width()):
            x = round(self._x_for(tick.sample)) + 0.5
            if 0 <= x <= bounds.width():
                painter.setPen(QPen(colors.GRID_MAJOR_COLOR if tick.major else colors.GRID_MINOR_COLOR, 1))
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
        pen = QPen(color, LINE_WIDTH)
        pen.setCosmetic(True)
        pen.setJoinStyle(Qt.MiterJoin)
        pen.setCapStyle(Qt.FlatCap)
        painter.setPen(pen)

        first = self.model.first_sample
        visible = self.model.visible_samples
        width = self.width()

        if visible <= width:
            self._draw_exact(painter, transitions, top, bottom, first, visible, color)
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
        color: QColor,
    ) -> None:
        last = min(first + visible, transitions.sample_count) - 1
        starts, values = transitions.runs_in_range(first, last)
        if starts.size == 0:
            return

        sample_width = self.sample_width()
        path = QPainterPath()

        def x_of(sample: float) -> float:
            return round((sample - first) * sample_width) + 0.5

        y_of = lambda value: top if value else bottom  # noqa: E731 - tiny helper

        end_sample = min(first + visible, transitions.sample_count)
        edges = [x_of(max(int(starts[0]), first))] + [x_of(int(start)) for start in starts[1:]] + [x_of(end_sample)]

        # The area under a high level, lightly filled
        fill = QColor(color)
        fill.setAlpha(HIGH_FILL_ALPHA)
        for index, value in enumerate(values):
            if value:
                painter.fillRect(QRectF(edges[index], top, edges[index + 1] - edges[index], bottom - top), fill)

        path.moveTo(edges[0], y_of(values[0]))
        for index in range(1, starts.size):
            path.lineTo(edges[index], y_of(values[index - 1]))
            path.lineTo(edges[index], y_of(values[index]))
        path.lineTo(edges[-1], y_of(values[-1]))
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

        level, edges_before = transitions.levels_and_edges(boundaries)
        edges_in_column = np.diff(edges_before)

        # 0 = low, 1 = high, 2 = at least one edge inside the column.
        state = np.where(edges_in_column > 0, 2, level).astype(np.int8)

        changes = np.flatnonzero(state[1:] != state[:-1]) + 1
        starts = np.concatenate(([0], changes))
        ends = np.concatenate((changes, [width]))

        # Columns with edges: a translucent band between both levels with its envelope; the
        # others at their level, a high one lightly filled like in the exact view.
        busy = QColor(color)
        busy.setAlpha(BUSY_FILL_ALPHA)
        fill = QColor(color)
        fill.setAlpha(HIGH_FILL_ALPHA)
        previous = None
        for start, end in zip(starts, ends):
            value = int(state[start])
            left, right = float(start), float(end)
            if value == 2:
                painter.fillRect(QRectF(left, top, right - left, bottom - top), busy)
                painter.drawLine(QPointF(left, top), QPointF(right, top))
                painter.drawLine(QPointF(left, bottom), QPointF(right, bottom))
            else:
                if value:
                    painter.fillRect(QRectF(left, top, right - left, bottom - top), fill)
                y = top if value else bottom
                painter.drawLine(QPointF(left, y), QPointF(right, y))
                if previous is not None and previous != 2 and previous != value:
                    painter.drawLine(QPointF(left, top), QPointF(left, bottom))
            previous = value

    # ------------------------------------------------------------ progressive
    def _column_states(self, channel) -> np.ndarray:
        """0 low, 1 high, 2 changing, per pixel column, of a capture still arriving: from the
        samples where they arrived and the view is not too wide, else from the overview."""
        width = max(int(self.width()), 1)
        model = self.model
        first, visible = model.first_sample, model.visible_samples
        last = min(first + visible, model.sample_count)
        progressive = model.progressive
        columns = max(int(round(width * (last - first) / max(visible, 1))), 1)
        if last <= first:
            return np.zeros(0, dtype=np.int8)
        if channel.samples is not None and progressive is not None and progressive.is_loaded(first, last) \
                and (last - first) <= width * 64:
            low, high = column_envelope(np.asarray(channel.samples[first:last]), columns)
        else:
            overview = model.digital_overview(channel)
            if overview is None:
                return np.zeros(0, dtype=np.int8)
            low, high = overview.envelope(first, last, columns)
        return np.where(low != high, 2, high).astype(np.int8)

    def _draw_progressive(self, painter: QPainter, channel, top: float, bottom: float, progressive) -> None:
        """A channel of a capture still arriving (``progressive`` as the paint read it: the model
        may drop it while the paint runs, when the transfer ends)."""
        color = colors.get_channel_color(channel)
        pen = QPen(color, LINE_WIDTH)
        pen.setCosmetic(True)
        painter.setPen(pen)
        model = self.model
        first, visible = model.first_sample, model.visible_samples
        last = min(first + visible, model.sample_count)
        if visible <= self.width() and channel.samples is not None and progressive.is_loaded(first, last):
            values = np.asarray(channel.samples[first:last]).astype(np.int8)
            if not len(values):
                return
            starts = np.concatenate(([0], np.flatnonzero(np.diff(values) != 0) + 1))
            sample_width = self.sample_width()
            edges = [round(start * sample_width) + 0.5 for start in starts] + [round(len(values) * sample_width) + 0.5]
            fill = QColor(color)
            fill.setAlpha(HIGH_FILL_ALPHA)
            path = QPainterPath()
            for index, start in enumerate(starts):
                value = values[start]
                y = top if value else bottom
                if value:
                    painter.fillRect(QRectF(edges[index], top, edges[index + 1] - edges[index], bottom - top), fill)
                if index == 0:
                    path.moveTo(edges[0], y)
                else:
                    path.lineTo(edges[index], y)
                path.lineTo(edges[index + 1], y)
            painter.drawPath(path)
            return
        state = self._column_states(channel)
        self._draw_states(painter, state, top, bottom, color)

    def _draw_states(self, painter: QPainter, state: np.ndarray, top: float, bottom: float, color: QColor) -> None:
        """Columns: 0 low, 1 high, 2 at least one edge (a band between both levels)."""
        if not len(state):
            return
        width = float(self.width()) * min(len(state) / max(self.width(), 1), 1.0)
        scale = width / len(state)
        changes = np.flatnonzero(state[1:] != state[:-1]) + 1
        starts = np.concatenate(([0], changes))
        ends = np.concatenate((changes, [len(state)]))
        busy = QColor(color)
        busy.setAlpha(BUSY_FILL_ALPHA)
        fill = QColor(color)
        fill.setAlpha(HIGH_FILL_ALPHA)
        previous = None
        for start, end in zip(starts, ends):
            value = int(state[start])
            left, right = float(start) * scale, float(end) * scale
            if value == 2:
                painter.fillRect(QRectF(left, top, right - left, bottom - top), busy)
                painter.drawLine(QPointF(left, top), QPointF(right, top))
                painter.drawLine(QPointF(left, bottom), QPointF(right, bottom))
            else:
                if value:
                    painter.fillRect(QRectF(left, top, right - left, bottom - top), fill)
                y = top if value else bottom
                painter.drawLine(QPointF(left, y), QPointF(right, y))
                if previous is not None and previous != 2 and previous != value:
                    painter.drawLine(QPointF(left, top), QPointF(left, bottom))
            previous = value

    def _draw_missing(self, painter: QPainter, bounds: QRectF, progressive) -> None:
        """Hatch the tiles that did not arrive yet."""
        first = self.model.first_sample
        for tile in progressive.tiles_of(first, first + self.model.visible_samples):
            if progressive.loaded[tile]:
                continue
            start, end = progressive.tile_range(tile)
            left, right = max(self._x_for(start), 0.0), min(self._x_for(end), bounds.width())
            painter.fillRect(QRectF(left, 0, right - left, bounds.height()), QColor(255, 255, 255, 10))
            painter.setPen(QPen(QColor(255, 255, 255, 20), 1))
            x = left
            while x < right:
                painter.drawLine(QPointF(x, bounds.height()), QPointF(min(x + 12, right), bounds.height() - 12))
                x += 10

    def _draw_markers(self, painter: QPainter, bounds: QRectF) -> None:
        session = self.model.session
        first = self.model.first_sample
        last = first + self.model.visible_samples

        if session is not None and session.pre_trigger_samples and first <= session.pre_trigger_samples <= last:
            pen = QPen(colors.TRIGGER_LINE_COLOR, 1.5)
            pen.setStyle(Qt.DashLine)
            painter.setPen(pen)
            x = round(self._x_for(session.pre_trigger_samples)) + 0.5
            painter.drawLine(QPointF(x, 0), QPointF(x, bounds.height()))

        if session is not None and session.bursts:
            pen = QPen(colors.BURST_LINE_COLOR, 1.5)
            pen.setStyle(Qt.DashDotLine)
            painter.setPen(pen)
            for burst in session.bursts:
                if first <= burst.burst_sample_start <= last:
                    x = self._x_for(burst.burst_sample_start)
                    painter.drawLine(QPointF(x, 0), QPointF(x, bounds.height()))

        marker = self.model.user_marker
        if marker is not None and first <= marker <= last:
            pen = QPen(colors.USER_LINE_COLOR, 1.5)
            pen.setStyle(Qt.DashLine)
            painter.setPen(pen)
            x = round(self._x_for(marker)) + 0.5
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

        if hover.parts:
            self._draw_parts(painter, bounds, hover, color, start, end)
            return
        composition = hover.composition
        if composition is None:
            return

        sample_x = self._x_for(composition.sample) + self.sample_width() / 2.0
        pen = QPen(colors.TEXT_COLOR, 1)
        pen.setStyle(Qt.DashLine)
        painter.setPen(pen)
        painter.drawLine(QPointF(sample_x, 0), QPointF(sample_x, bounds.height()))

        bits = {bit.capture_number: bit for bit in composition.bits}
        painter.setRenderHint(QPainter.Antialiasing, True)
        metrics = QFontMetricsF(painter.font())
        #: Widest channel label, the summary of the buses goes next to them
        label_width = 0.0
        labels_left = False

        for row, channel in enumerate(self._channels()):
            bit = bits.get(channel.channel_number)
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
                labels_left = True
            label_width = max(label_width, width)
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

        self._draw_bus_summary(painter, bounds, composition, sample_x, color, metrics, label_width, labels_left)
        painter.setBrush(Qt.NoBrush)

    def _draw_parts(self, painter: QPainter, bounds: QRectF, hover, color: QColor, start: float, end: float) -> None:
        """An entry made of several values (an instruction of bus cycles): where each was read,
        and the values next to the marked span."""
        from ...sigrok.composition import sample_point

        pen = QPen(QColor(255, 255, 255, 120), 1)
        pen.setStyle(Qt.DotLine)
        painter.setPen(pen)
        first, last = self.model.first_sample, self.model.first_sample + self.model.visible_samples
        for part in hover.parts:
            point = sample_point(part)
            if first <= point <= last:
                x = self._x_for(point) + self.sample_width() / 2.0
                painter.drawLine(QPointF(x, 0), QPointF(x, bounds.height()))

        metrics = QFontMetricsF(painter.font())
        lines = [hover.segment.values[0] if hover.segment.values else ""]
        lines += [f"  {part.values[0]}" for part in hover.parts[:MAX_PART_LINES] if part.values]
        if len(hover.parts) > MAX_PART_LINES:
            lines.append(f"  … {len(hover.parts) - MAX_PART_LINES} more")
        width = max(metrics.horizontalAdvance(line) for line in lines) + 16
        height = metrics.height() * len(lines) + 10
        visible = QRectF(self.visibleRegion().boundingRect())
        if visible.isEmpty():
            visible = bounds
        # Next to the marked span, on the side with room
        x = end + 8 if end + 8 + width <= bounds.width() else max(start - 8 - width, 4.0)
        box = QRectF(x, visible.top() + 6, width, height)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setBrush(QColor(18, 18, 20, 235))
        painter.setPen(QPen(color, 1.5))
        painter.drawRoundedRect(box, 5, 5)
        painter.setPen(QPen(colors.TEXT_COLOR))
        for index, line in enumerate(lines):
            painter.drawText(QPointF(box.left() + 8, box.top() + 5 + metrics.ascent() + index * metrics.height()), line)
        painter.setBrush(Qt.NoBrush)

    def _draw_bus_summary(self, painter: QPainter, bounds: QRectF, composition, sample_x: float,
                          color: QColor, metrics: QFontMetricsF, label_width: float = 0.0,
                          labels_left: bool = False) -> None:
        """Bus values at the top of the visible part (the bus rows may be scrolled out of view),
        next to the channel labels at the read point."""
        if not composition.buses:
            return
        lines = [bus.describe() for bus in composition.buses]
        width = max(metrics.horizontalAdvance(line) for line in lines) + 16
        height = metrics.height() * len(lines) + 10
        visible = QRectF(self.visibleRegion().boundingRect())
        if visible.isEmpty():
            visible = bounds
        # Beside the labels at the read point, on the side they are on (else on the other side)
        right = sample_x + 8 + label_width + 8
        left = sample_x - 8 - label_width - 8 - width
        if labels_left:
            x = left if left >= 4 else sample_x + 8
        else:
            x = right if right + width <= bounds.width() else max(sample_x - 8 - width, 4.0)
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
            self._dragged_cursor = overlays.cursor_at(self.model, event.position().x(), self._x_for)
            if self._dragged_cursor is None:
                self._pan_origin = (event.position().x(), self.model.first_sample)
                self.setCursor(Qt.ClosedHandCursor)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.LeftButton:
            self._pan_origin = None
            self._dragged_cursor = None
            self.unsetCursor()
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        """A / B place a cursor at the pointer, M adds a named marker there."""
        sample = self._pointer_sample
        key = event.key()
        if sample is not None and self.model.sample_count and event.modifiers() in (Qt.NoModifier, Qt.KeypadModifier):
            if key == Qt.Key_A:
                self.model.set_cursor("A", sample)
                return
            if key == Qt.Key_B:
                self.model.set_cursor("B", sample)
                return
            if key == Qt.Key_M:
                self.model.add_bookmark(sample)
                return
        super().keyPressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._pointer_sample = self.model.sample_at(event.position().x(), self.width())
        if self._dragged_cursor is not None:
            self.model.set_cursor(self._dragged_cursor, self._pointer_sample)
            return
        if self._pan_origin is None:
            grabbed = overlays.cursor_at(self.model, event.position().x(), self._x_for)
            self.setCursor(Qt.SizeHorCursor) if grabbed else self.unsetCursor()
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
            return

        text = (
            f"{channels[channel_index].display_name}\n"
            f"Sample: {sample:,}\n"
            f"State: {'High' if interval.value else 'Low'}\n"
            f"Length: {to_small_time(interval.duration)} ({interval.sample_count:,} samples)\n"
            f"Inferred frequency: {to_inferred_frequency(interval.duration)}"
        )
        QToolTip.showText(event.globalPosition().toPoint(), text, self)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._pointer_sample = None
        QToolTip.hideText()
        super().leaveEvent(event)
