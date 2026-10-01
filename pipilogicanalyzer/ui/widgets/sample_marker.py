# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of PiPiLogicAnalyzer, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Sample ruler with selection, regions and the edit context menu.

Port of ``Controls/SampleMarker.axaml.cs``.

The selection is inclusive here (``count = end - start + 1``).  The original
mixed an exclusive count (``Math.Abs(last - first)``) with inclusive slicing in
the delete/copy handlers, so cutting a selection removed one sample more than it
copied and the regions were shifted by one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFontMetricsF, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QMenu, QSizePolicy, QToolTip, QWidget

from ...core import colors
from ...core.formatting import to_small_time
from ..view_model import CaptureViewModel
from . import overlays, time_axis

BURST_PEN_COLOR = QColor(224, 175, 29)
BURST_FILL_COLOR = QColor(224, 175, 29, 128)
MARKER_HEIGHT = 32
RULER_BACKGROUND = QColor(36, 36, 40)
RULER_BORDER = QColor(58, 58, 64)
RULER_MAJOR = QColor(140, 140, 150)
RULER_MINOR = QColor(82, 82, 90)
RULER_TEXT = QColor(200, 200, 208)


@dataclass
class Selection:
    first_sample: int
    last_sample: int

    @property
    def start(self) -> int:
        return min(self.first_sample, self.last_sample)

    @property
    def end(self) -> int:
        return max(self.first_sample, self.last_sample)

    @property
    def sample_count(self) -> int:
        return self.end - self.start + 1


class SampleMarker(QWidget):
    """The ruler drawn above the waveforms."""

    create_region_requested = Signal(int, int)
    delete_region_requested = Signal(object)
    measure_requested = Signal(int, int)
    copy_requested = Signal(int, int)
    cut_requested = Signal(int, int)
    paste_requested = Signal(int)
    insert_requested = Signal(int)
    delete_requested = Signal(int, int)
    shift_requested = Signal()

    def __init__(self, model: CaptureViewModel, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.model = model
        self.setMouseTracking(True)
        self.setFixedHeight(MARKER_HEIGHT)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setContextMenuPolicy(Qt.DefaultContextMenu)

        self.selection: Optional[Selection] = None
        self.has_clipboard = False
        self._dragging = False
        self._dragged_cursor: Optional[str] = None

        model.view_changed.connect(self.update)
        model.regions_changed.connect(self.update)
        model.capture_changed.connect(self.update)
        model.marker_changed.connect(self.update)
        model.cursors_changed.connect(self.update)
        model.bookmarks_changed.connect(self.update)
        model.search_changed.connect(self.update)

    # --------------------------------------------------------------- geometry
    def sample_width(self) -> float:
        return self.width() / max(self.model.visible_samples, 1)

    def sample_at(self, x: float) -> int:
        # Qt keeps reporting positions left and right of the widget while a drag is held.
        sample = self.model.sample_at(x, self.width())
        return min(max(sample, 0), max(self.model.sample_count - 1, 0))

    def _x_for(self, sample: float) -> float:
        return (sample - self.model.first_sample) * self.sample_width()

    # --------------------------------------------------------------- painting
    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        bounds = QRectF(0, 0, self.width(), self.height())
        painter.fillRect(bounds, RULER_BACKGROUND)
        painter.setPen(QPen(RULER_BORDER, 1))
        painter.drawLine(QPointF(0, bounds.height() - 0.5), QPointF(bounds.width(), bounds.height() - 0.5))

        if self.model.visible_samples == 0:
            return

        sample_width = self.sample_width()

        if self.selection is not None:
            start = self._x_for(self.selection.start)
            width = max(self.selection.sample_count * sample_width, 1.0)
            painter.fillRect(QRectF(start, 0, width, bounds.height()), colors.SELECTION_COLOR)

        for region in self.model.regions_in_view():
            start = self._x_for(region.start)
            width = max(region.sample_count * sample_width, 1.0)
            painter.fillRect(QRectF(start, 0, width, bounds.height()), region.region_color)
            if region.region_name:
                painter.setPen(QPen(QColor(255, 255, 255)))
                metrics = QFontMetricsF(painter.font())
                text_width = metrics.horizontalAdvance(region.region_name)
                painter.drawText(
                    QPointF(start + width / 2 - text_width / 2, metrics.ascent() + 2),
                    region.region_name,
                )

        self._draw_ticks(painter, bounds)
        self._draw_bursts(painter, bounds)
        self._draw_marker(painter, bounds)
        overlays.draw_search_hits(painter, self.model, self._x_for, bounds, ruler=True)
        overlays.draw_bookmarks(painter, self.model, self._x_for, bounds, ruler=True)
        overlays.draw_cursors(painter, self.model, self._x_for, bounds, ruler=True)

    def _draw_ticks(self, painter: QPainter, bounds: QRectF) -> None:
        """Time labels (0 at the trigger), the same divisions as the grid of the waveform."""
        width = bounds.width()
        height = bounds.height()
        metrics = QFontMetricsF(painter.font())
        for tick in time_axis.ticks(
            self.model.first_sample, self.model.visible_samples, width, self.model.frequency,
            self.model.pre_trigger_samples,
        ):
            x = round(self._x_for(tick.sample)) + 0.5
            if not 0 <= x <= width:
                continue
            painter.setPen(QPen(RULER_MAJOR if tick.major else RULER_MINOR, 1))
            painter.drawLine(QPointF(x, height * (0.5 if tick.major else 0.75)), QPointF(x, height))
            if tick.label:
                painter.setPen(QPen(RULER_TEXT))
                text_width = metrics.horizontalAdvance(tick.label)
                painter.drawText(QPointF(min(max(x + 4, 1), width - text_width - 1), metrics.ascent() + 3), tick.label)

        # Sample boundaries when zoomed far in
        sample_width = self.sample_width()
        if sample_width > 6:
            painter.setPen(QPen(RULER_MINOR, 1))
            for index in range(self.model.visible_samples + 1):
                x = round(index * sample_width) + 0.5
                painter.drawLine(QPointF(x, height * 0.85), QPointF(x, height))

        self._draw_trigger_flag(painter, bounds)

    def _draw_trigger_flag(self, painter: QPainter, bounds: QRectF) -> None:
        session = self.model.session
        if session is None or not session.pre_trigger_samples:
            return
        x = round(self._x_for(session.pre_trigger_samples)) + 0.5
        if not -8 <= x <= bounds.width() + 8:
            return
        height = bounds.height()
        flag = QPainterPath()
        flag.moveTo(x - 6, height - 12)
        flag.lineTo(x + 6, height - 12)
        flag.lineTo(x, height)
        flag.closeSubpath()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.fillPath(flag, colors.TRIGGER_LINE_COLOR)
        painter.setRenderHint(QPainter.Antialiasing, False)

    def _draw_bursts(self, painter: QPainter, bounds: QRectF) -> None:
        session = self.model.session
        if session is None or not session.bursts:
            return

        burst_width = 16.0
        painter.setPen(QPen(BURST_PEN_COLOR, 1))
        painter.setBrush(BURST_FILL_COLOR)
        for burst in session.bursts:
            center = self._x_for(burst.burst_sample_start)
            if center < -burst_width or center > self.width() + burst_width:
                continue
            x1 = center - burst_width / 2
            x2 = center + burst_width / 2
            height = bounds.height()
            path = QPainterPath()
            path.moveTo(x1, 0)
            path.lineTo(x2, 0)
            path.lineTo(x2, height / 2)
            path.lineTo(center, height)
            path.lineTo(x1, height / 2)
            path.closeSubpath()
            painter.drawPath(path)
        painter.setBrush(Qt.NoBrush)

    def _draw_marker(self, painter: QPainter, bounds: QRectF) -> None:
        marker = self.model.user_marker
        if marker is None:
            return
        pen = QPen(colors.USER_LINE_COLOR, 1.5)
        pen.setStyle(Qt.DashLine)
        painter.setPen(pen)
        x = round(self._x_for(marker)) + 0.5
        painter.drawLine(QPointF(x, 0), QPointF(x, bounds.height()))

    # ------------------------------------------------------------ interaction
    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() != Qt.LeftButton or self.model.sample_count == 0:
            super().mousePressEvent(event)
            return

        self._dragged_cursor = overlays.cursor_at(self.model, event.position().x(), self._x_for)
        if self._dragged_cursor is not None:
            return
        sample = self.sample_at(event.position().x())
        if self.selection is not None and event.modifiers() & Qt.ShiftModifier:
            self.selection.last_sample = sample
        else:
            self.selection = Selection(sample, sample)
        self._dragging = True
        self.update()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        sample = self.sample_at(event.position().x())

        if self._dragged_cursor is not None:
            self.model.set_cursor(self._dragged_cursor, sample)
            return
        if overlays.cursor_at(self.model, event.position().x(), self._x_for):
            self.setCursor(Qt.SizeHorCursor)
        else:
            self.unsetCursor()

        if self._dragging and self.selection is not None:
            self.selection.last_sample = sample
            self.update()
            return

        self._show_tooltip(event, sample)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.LeftButton and self._dragged_cursor is not None:
            self._dragged_cursor = None
            return
        if event.button() == Qt.LeftButton and self._dragging:
            self._dragging = False
            sample = self.sample_at(event.position().x())
            if self.selection is not None:
                self.selection.last_sample = sample
                if self.selection.start == self.selection.end:
                    # A click (no drag) places or removes the user marker.
                    self.selection = None
                    current = self.model.user_marker
                    self.model.set_user_marker(None if current == sample else sample)
            self.update()
        super().mouseReleaseEvent(event)

    def _show_tooltip(self, event, sample: int) -> None:
        session = self.model.session
        if session is None:
            return

        if session.bursts:
            for burst in session.bursts:
                center = self._x_for(burst.burst_sample_start)
                if abs(event.position().x() - center) <= 8:
                    QToolTip.showText(event.globalPosition().toPoint(), str(burst), self)
                    return

        time_from_trigger = (sample - session.pre_trigger_samples) / max(session.frequency, 1)
        text = f"Sample {sample:,}\nt = {to_small_time(time_from_trigger)}"
        if self.selection is not None:
            duration = self.selection.sample_count / max(session.frequency, 1)
            text += (
                f"\nSelection: {self.selection.start:,} - {self.selection.end:,}"
                f" ({self.selection.sample_count:,} samples, {to_small_time(duration)})"
            )
        QToolTip.showText(event.globalPosition().toPoint(), text, self)

    def contextMenuEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self.model.sample_count == 0:
            return

        sample = self.sample_at(event.pos().x())
        region = self.model.region_at(sample)
        in_selection = (
            self.selection is not None and self.selection.start <= sample <= self.selection.end
        )

        menu = QMenu(self)

        action_cursor_a = menu.addAction("Place cursor A here")
        action_cursor_b = menu.addAction("Place cursor B here")
        action_cursors_selection = menu.addAction("Cursors to the selection")
        action_cursors_selection.setEnabled(self.selection is not None)
        action_bookmark = menu.addAction("Add marker here")
        bookmark = next(
            (item for item in self.model.bookmarks if abs(self._x_for(item.sample) - event.pos().x()) <= 6), None
        )
        action_remove_bookmark = menu.addAction(f"Remove marker '{bookmark.name}'" if bookmark else "Remove marker")
        action_remove_bookmark.setEnabled(bookmark is not None)
        menu.addSeparator()

        action_copy = menu.addAction("Copy samples")
        action_cut = menu.addAction("Cut samples")
        action_paste = menu.addAction("Paste samples here")
        action_insert = menu.addAction("Insert samples here...")
        action_delete = menu.addAction("Delete samples")
        menu.addSeparator()
        action_shift = menu.addAction("Shift channels...")
        action_measure = menu.addAction("Measure selection...")
        menu.addSeparator()
        action_region = menu.addAction("Create region from selection...")
        action_delete_region = menu.addAction("Delete region")

        for action in (action_copy, action_cut, action_delete, action_measure, action_region):
            action.setEnabled(in_selection)
        action_paste.setEnabled(self.has_clipboard)
        action_delete_region.setEnabled(region is not None)

        chosen = menu.exec(event.globalPos())
        if chosen is None:
            return

        selection = self.selection
        if chosen is action_cursor_a:
            self.model.set_cursor("A", sample)
        elif chosen is action_cursor_b:
            self.model.set_cursor("B", sample)
        elif chosen is action_cursors_selection and selection:
            self.model.set_cursor("A", selection.start)
            self.model.set_cursor("B", selection.end + 1)
        elif chosen is action_bookmark:
            self.model.add_bookmark(sample)
        elif chosen is action_remove_bookmark and bookmark is not None:
            self.model.remove_bookmark(bookmark)
        elif chosen is action_copy and selection:
            self.copy_requested.emit(selection.start, selection.sample_count)
        elif chosen is action_cut and selection:
            self.cut_requested.emit(selection.start, selection.sample_count)
            self.clear_selection()
        elif chosen is action_paste:
            self.paste_requested.emit(sample)
        elif chosen is action_insert:
            self.insert_requested.emit(sample)
        elif chosen is action_delete and selection:
            self.delete_requested.emit(selection.start, selection.sample_count)
            self.clear_selection()
        elif chosen is action_shift:
            self.shift_requested.emit()
        elif chosen is action_measure and selection:
            self.measure_requested.emit(selection.start, selection.sample_count)
        elif chosen is action_region and selection:
            self.create_region_requested.emit(selection.start, selection.end)
        elif chosen is action_delete_region and region is not None:
            self.delete_region_requested.emit(region)

    def clear_selection(self) -> None:
        self.selection = None
        self.update()

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt naming
        from .navigation import WheelNavigator

        if getattr(self, "_navigator", None) is None:
            self._navigator = WheelNavigator(self, self.model)
        if self._navigator.handle_wheel(event):
            event.accept()
        else:
            event.ignore()
