# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of openSciLab, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Protocol decoder annotations (port of ``Controls/AnnotationViewer.axaml.cs``).

Resting the pointer on an annotation marks it in every view: the waveform shows its samples
and how the channels the decoder read make up the value (see ``sigrok.composition``).
Clicking the name of a row, or double-clicking an annotation, asks for the row as a list.
"""

from __future__ import annotations

from typing import Optional

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFontMetricsF, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QToolTip, QWidget

from .. import colors
from ...sigrok.composition import compose
from ...sigrok.links import linked_segments, read_parts, segment_index
from ..view_model import AnnotationHover, CaptureViewModel
from .navigation import WheelNavigator

ANNOTATION_HEIGHT = 26
NAME_COLUMN_WIDTH = 150
#: Segments are drawn at least this wide, so they can be hovered at every zoom level.
MIN_SEGMENT_WIDTH = 3.0
#: Entries per pixel of a row above which the narrow ones are drawn as blocks
DENSE_SEGMENTS = 2
#: Pixels from which an entry is drawn with its shape (and its text when it fits)
WIDE_SEGMENT = 8.0
#: Segments checked before the one starting at the pointer (overlapping classes in a row).
LOOKBEHIND_SEGMENTS = 16
#: Values listed in the tooltip of an entry made of several
MAX_LISTED_PARTS = 12


def build_hover(groups, channels, group, segment) -> AnnotationHover:
    """The hover of ``segment``: the entries that belong to it in every row, and either the values
    it is made of (an instruction: its bus cycles) or the channel levels it was read from."""
    links = linked_segments(groups, segment)
    parts = read_parts(links, group)
    composition = None
    if not parts and getattr(group, "info", None) is not None:
        composition = compose(group.info, group.instance, channels, segment)
    return AnnotationHover(group=group, segment=segment, composition=composition, links=links, parts=parts)


class AnnotationViewer(QWidget):
    """Draws the annotation rows produced by the decoders."""

    #: (group, annotation row, segment or ``None``) when a row is clicked to be listed.
    row_clicked = Signal(object, object, object)

    def __init__(self, model: CaptureViewModel, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.model = model
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFixedHeight(0)

        model.annotations_changed.connect(self._on_annotations_changed)
        model.view_changed.connect(self.update)
        model.regions_changed.connect(self.update)
        model.marker_changed.connect(self.update)
        model.hover_changed.connect(self.update)

    # ------------------------------------------------------------------ state
    def _rows(self) -> list[tuple[QColor, str, object, object]]:
        """(colour, title, annotation, group) of every annotation row."""
        rows: list[tuple[QColor, str, object, object]] = []
        for group in self.model.annotation_groups:
            color = colors.get_color(group.color_index)
            for annotation in group.annotations:
                rows.append((color, f"{group.decoder_name}: {annotation.name}", annotation, group))
        return rows

    def _row_at(self, y: float) -> Optional[tuple[QColor, str, object, object]]:
        rows = self._rows()
        index = int(y // ANNOTATION_HEIGHT)
        return rows[index] if 0 <= index < len(rows) else None

    def _on_annotations_changed(self) -> None:
        self.setFixedHeight(len(self._rows()) * ANNOTATION_HEIGHT)
        self.setVisible(bool(self._rows()))
        self.update()

    def _data_width(self) -> float:
        return max(self.width() - NAME_COLUMN_WIDTH, 1)

    def _x_for(self, sample: float) -> float:
        ratio = self._data_width() / max(self.model.visible_samples, 1)
        return NAME_COLUMN_WIDTH + (sample - self.model.first_sample) * ratio

    @staticmethod
    def _start_index(annotation) -> np.ndarray:
        return segment_index(annotation)[0]

    # --------------------------------------------------------------- painting
    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        rows = self._rows()
        if not rows:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        height = self.height()

        painter.fillRect(QRectF(0, 0, NAME_COLUMN_WIDTH, height), colors.BG_CHANNEL_COLORS[1])
        painter.fillRect(
            QRectF(NAME_COLUMN_WIDTH, 0, self.width() - NAME_COLUMN_WIDTH, height),
            colors.BG_CHANNEL_COLORS[0],
        )

        for region in self.model.regions_in_view():
            start = self._x_for(region.start)
            end = self._x_for(region.end)
            painter.fillRect(QRectF(start, 0, max(end - start, 1.0), height), colors.region_color(region))

        self._draw_hover_band(painter, height)

        metrics = QFontMetricsF(painter.font())
        for index, (color, name, annotation, _group) in enumerate(rows):
            top = index * ANNOTATION_HEIGHT
            self._draw_name(painter, metrics, color, name, top)
            self._draw_segments(painter, metrics, annotation, top)

        marker = self.model.user_marker
        if marker is not None:
            pen = QPen(colors.USER_LINE_COLOR, 2)
            pen.setStyle(Qt.DashDotLine)
            painter.setPen(pen)
            x = self._x_for(marker)
            painter.drawLine(QPointF(x, 0), QPointF(x, height))

    def _draw_hover_band(self, painter: QPainter, height: float) -> None:
        hover = self.model.hover
        if hover is None:
            return
        segment = hover.segment
        start = self._x_for(segment.first_sample)
        end = self._x_for(max(segment.last_sample, segment.first_sample + 1))
        band = QColor(colors.get_color(hover.group.color_index))
        band.setAlpha(45)
        painter.fillRect(QRectF(start, 0, max(end - start, MIN_SEGMENT_WIDTH), height), band)

    def _draw_name(
        self, painter: QPainter, metrics: QFontMetricsF, color: QColor, name: str, top: float
    ) -> None:
        painter.setBrush(color)
        painter.setPen(QPen(QColor(0, 0, 0), 1))
        painter.drawEllipse(QPointF(12, top + ANNOTATION_HEIGHT / 2), 7, 7)
        painter.setBrush(Qt.NoBrush)

        painter.setPen(QPen(colors.TEXT_COLOR))
        text = metrics.elidedText(name, Qt.ElideRight, NAME_COLUMN_WIDTH - 28)
        painter.drawText(
            QPointF(24, top + ANNOTATION_HEIGHT / 2 + metrics.ascent() / 2 - 1), text
        )

    def _draw_segments(
        self, painter: QPainter, metrics: QFontMetricsF, annotation, top: float
    ) -> None:
        first = self.model.first_sample
        last = first + self.model.visible_samples

        segments = annotation.segments
        if not segments:
            return

        # Binary search the visible window instead of scanning every segment: from the first
        # segment that reaches into it (a long one may begin far before it) to the last that
        # begins in it.
        starts, ends, reach, types = segment_index(annotation)
        begin = int(np.searchsorted(reach, first, side="left"))
        end = int(np.searchsorted(starts, last, side="right"))
        if end <= begin:
            return

        painter.save()
        painter.setClipRect(
            QRectF(NAME_COLUMN_WIDTH, top, self.width() - NAME_COLUMN_WIDTH, ANNOTATION_HEIGHT)
        )

        hover = self.model.hover
        hovered = hover.segment if hover is not None else None
        linked = hover.linked_ids if hover is not None else set()

        def draw(segment) -> None:
            x_start = self._x_for(segment.first_sample)
            x_end = self._x_for(max(segment.last_sample, segment.first_sample + 1))
            width = max(x_end - x_start, MIN_SEGMENT_WIDTH)
            rect = QRectF(x_start, top + 2, width, ANNOTATION_HEIGHT - 4)
            self._draw_segment(painter, metrics, segment, rect, segment is hovered, id(segment) in linked)

        if end - begin <= DENSE_SEGMENTS * self._data_width():
            for segment in segments[begin:end]:
                if segment.last_sample >= first:
                    draw(segment)
        else:
            # Far more entries than pixels (a whole capture of bytes in view): drawing each one
            # takes most of a second. Those too narrow for a shape become blocks, one per run of
            # pixel columns they cover; the wide ones (few fit) are drawn as always.
            ratio = self._data_width() / max(self.model.visible_samples, 1)
            left = NAME_COLUMN_WIDTH + (starts[begin:end] - first) * ratio
            width = np.maximum((np.maximum(ends[begin:end], starts[begin:end] + 1) - starts[begin:end]) * ratio,
                               MIN_SEGMENT_WIDTH)
            visible = ends[begin:end] >= first
            narrow = visible & (width < WIDE_SEGMENT)
            columns = int(self._data_width()) + 1
            first_column = np.clip(np.floor(left[narrow]) - NAME_COLUMN_WIDTH, 0, columns).astype(np.int64)
            last_column = np.clip(np.ceil(left[narrow] + width[narrow]) - NAME_COLUMN_WIDTH, 0, columns).astype(np.int64)
            covered = np.cumsum(np.bincount(first_column, minlength=columns + 1)
                                - np.bincount(last_column, minlength=columns + 1))[:columns] > 0
            edges = np.flatnonzero(np.diff(np.concatenate(([False], covered, [False])).astype(np.int8)))
            narrow_types = types[begin:end][narrow]
            painter.setPen(Qt.NoPen)
            for run_start, run_end in zip(edges[0::2].tolist(), edges[1::2].tolist()):
                # the colour of the first entry of the run
                index = min(int(np.searchsorted(first_column, run_start, side="left")), len(narrow_types) - 1)
                painter.setBrush(colors.get_color(int(narrow_types[index]) % len(colors.PALETTE)))
                painter.drawRect(QRectF(NAME_COLUMN_WIDTH + run_start, top + 5, run_end - run_start,
                                        ANNOTATION_HEIGHT - 10))
            for index in np.flatnonzero(visible & ~narrow).tolist():
                draw(segments[begin + index])
            if hovered is not None and first <= max(hovered.last_sample, hovered.first_sample) \
                    and hovered.first_sample <= last and any(segment is hovered for segment in annotation.segments[
                        max(int(np.searchsorted(starts, hovered.first_sample, side="left")), 0):
                        int(np.searchsorted(starts, hovered.first_sample, side="right"))]):
                draw(hovered)  # the entry under the pointer stands out of its block

        painter.restore()

    def _draw_segment(
        self, painter: QPainter, metrics: QFontMetricsF, segment, rect: QRectF, hovered: bool = False,
        linked: bool = False,
    ) -> None:
        color = colors.get_color(segment.type_id % len(colors.PALETTE))
        text_color = colors.find_contrast(color)

        if hovered:
            painter.setBrush(color.lighter(125))
            painter.setPen(QPen(colors.TEXT_COLOR, 2))
        elif linked:
            # Belongs to the hovered entry (its parts, or the entry around it)
            painter.setBrush(color.lighter(112))
            painter.setPen(QPen(colors.TEXT_COLOR, 1.2, Qt.DashLine))
        else:
            painter.setBrush(color)
            painter.setPen(QPen(QColor(0, 0, 0), 1))

        if rect.width() < 8:
            painter.drawEllipse(rect.center(), rect.width() / 2, rect.height() / 2)
            margin = 2.0
        elif rect.width() < 24:
            painter.drawRoundedRect(rect, 4, 4)
            margin = 2.0
        else:
            path = QPainterPath()
            mid_y = rect.center().y()
            path.moveTo(rect.left(), mid_y)
            path.lineTo(rect.left() + 5, rect.top())
            path.lineTo(rect.right() - 5, rect.top())
            path.lineTo(rect.right(), mid_y)
            path.lineTo(rect.right() - 5, rect.bottom())
            path.lineTo(rect.left() + 5, rect.bottom())
            path.closeSubpath()
            painter.drawPath(path)
            margin = 10.0

        painter.setBrush(Qt.NoBrush)

        available = rect.width() - margin
        for value in segment.values:
            if metrics.horizontalAdvance(value) <= available:
                painter.setPen(QPen(text_color))
                painter.drawText(
                    QPointF(
                        rect.center().x() - metrics.horizontalAdvance(value) / 2,
                        rect.center().y() + metrics.ascent() / 2 - 1,
                    ),
                    value,
                )
                break

    # ------------------------------------------------------------ interaction
    def hover_at(self, position: QPointF) -> Optional[AnnotationHover]:
        """The annotation at a widget position, with the composition of its value."""
        if position.x() < NAME_COLUMN_WIDTH:
            return None
        row = self._row_at(position.y())
        if row is None:
            return None

        _color, _name, annotation, group = row
        segments = annotation.segments
        if not segments:
            return None

        ratio = self._data_width() / max(self.model.visible_samples, 1)
        sample = (position.x() - NAME_COLUMN_WIDTH) / ratio + self.model.first_sample
        tolerance = MIN_SEGMENT_WIDTH / ratio  # narrow segments are drawn wider than they are

        starts = self._start_index(annotation)
        index = int(np.searchsorted(starts, sample, side="right")) - 1
        for candidate in range(index, max(index - LOOKBEHIND_SEGMENTS, -1), -1):
            segment = segments[candidate]
            end = max(segment.last_sample, segment.first_sample + 1)
            if segment.first_sample <= sample <= max(end, segment.first_sample + tolerance):
                current = self.model.hover
                if current is not None and current.segment is segment:
                    return current  # the links are computed once per entry
                return build_hover(self.model.annotation_groups, self.model.channels, group, segment)
        return None

    @staticmethod
    def tooltip_text(hover: AnnotationHover) -> str:
        lines = [hover.segment.values[0] if hover.segment.values else ""]
        if hover.parts:
            lines += [f"  {part.values[0]}" for part in hover.parts[:MAX_LISTED_PARTS] if part.values]
            if len(hover.parts) > MAX_LISTED_PARTS:
                lines.append(f"  … {len(hover.parts) - MAX_LISTED_PARTS} more")
        composition = hover.composition
        if composition is not None:
            details = composition.describe()
            if details:
                lines.append(details)
            lines.append(f"Read at sample {composition.sample:,}")
        return "\n".join(line for line in lines if line)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        position = event.position()
        name_row = self._row_at(position.y()) if position.x() < NAME_COLUMN_WIDTH else None
        if name_row is not None:
            self.setCursor(Qt.PointingHandCursor)
        else:
            self.unsetCursor()

        hover = self.hover_at(position)
        self.model.set_hover(hover)
        if name_row is not None:
            text = f"{name_row[1]}\nClick to show the annotations of this row as a list"
            QToolTip.showText(event.globalPosition().toPoint(), text, self)
            return
        if hover is None:
            QToolTip.hideText()
            return
        QToolTip.showText(event.globalPosition().toPoint(), self.tooltip_text(hover), self)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        position = event.position()
        row = self._row_at(position.y())
        if event.button() == Qt.LeftButton and position.x() < NAME_COLUMN_WIDTH and row is not None:
            self.row_clicked.emit(row[3], row[2], None)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 - Qt naming
        position = event.position()
        row = self._row_at(position.y())
        if event.button() == Qt.LeftButton and position.x() >= NAME_COLUMN_WIDTH and row is not None:
            hover = self.hover_at(position)
            self.row_clicked.emit(row[3], row[2], hover.segment if hover is not None else None)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self.model.set_hover(None)
        QToolTip.hideText()
        super().leaveEvent(event)

    @property
    def navigator(self) -> WheelNavigator:
        if getattr(self, "_navigator", None) is None:
            self._navigator = WheelNavigator(self, self.model, data_left=NAME_COLUMN_WIDTH)
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
