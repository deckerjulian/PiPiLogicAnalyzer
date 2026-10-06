# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of openSciLab, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Overview of the whole capture (port of ``Controls/SamplePreviewer.axaml.cs``).

The original built the preview by drawing one line per sample *and* per channel
with Skia, which took seconds for a large capture.  Here the image is produced
directly as a ``numpy`` array: each pixel column is reduced to "any high" / "any
low" with a single pass over the samples, so building the preview of a one
million sample capture takes a few milliseconds.

The preview is also interactive: clicking or dragging moves the viewport.
"""

from __future__ import annotations

from typing import Optional

import numpy as np
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from .. import colors
from ..theme import ACCENT
from ..view_model import CaptureViewModel

PREVIEW_HEIGHT = 120
MAX_PREVIEW_CHANNELS = 32
OVERVIEW_BACKGROUND = (26, 26, 29)


class SamplePreviewer(QWidget):
    """A miniature of the complete capture with the current viewport marked."""

    def __init__(self, model: CaptureViewModel, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.model = model
        self.setFixedHeight(PREVIEW_HEIGHT)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Overview of the capture - click or drag to move the view")

        self._image: Optional[QImage] = None
        self._image_buffer: Optional[np.ndarray] = None
        self._dirty = True

        model.capture_changed.connect(self.invalidate)
        model.samples_appended.connect(self.invalidate)
        model.channels_changed.connect(self.invalidate)
        model.view_changed.connect(self.update)
        model.regions_changed.connect(self.update)

    # ------------------------------------------------------------------ image
    def invalidate(self) -> None:
        self._dirty = True
        self.update()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._dirty = True
        super().resizeEvent(event)

    def _build_image(self) -> None:
        self._dirty = False
        self._image = None
        self._image_buffer = None

        channels = self.model.visible_channels[:MAX_PREVIEW_CHANNELS]
        analog = self.model.visible_analog[:max(MAX_PREVIEW_CHANNELS - len(channels), 0)]
        width = max(int(self.width()), 1)
        height = max(int(self.height()), 1)
        sample_count = self.model.sample_count
        if not (channels or analog) or sample_count == 0 or width < 2:
            return

        buffer = np.zeros((height, width, 3), dtype=np.uint8)
        buffer[:, :] = OVERVIEW_BACKGROUND

        channel_height = height / (len(channels) + len(analog))
        # From the edge index, not the samples: a column costs two searches, whatever it covers
        # (a capture recorded to disk is never read at once)
        boundaries = np.linspace(0, sample_count, width + 1)

        for index, channel in enumerate(channels):
            transitions = self.model.transitions_for(channel)
            if transitions is None or transitions.sample_count == 0:
                continue

            level, edges_before = transitions.levels_and_edges(boundaries)
            # A column with an edge is drawn as busy, so a fast signal reads as a continuous
            # trace; the others at their level.
            busy = np.diff(edges_before) > 0
            only_high = (level != 0) & ~busy
            only_low = (level == 0) & ~busy

            color = colors.get_channel_color(channel)
            rgb = np.array([color.red(), color.green(), color.blue()], dtype=np.float64)
            background = np.array(OVERVIEW_BACKGROUND, dtype=np.float64)
            # Activity as a band between the two levels (its edges in the colour of the channel),
            # steady levels as lines (a low one dimmer): a summary, not a second waveform
            dim = (background + (rgb - background) * 0.45).astype(np.uint8)
            low_rgb = (background + (rgb - background) * 0.6).astype(np.uint8)
            rgb = (background + (rgb - background) * 0.95).astype(np.uint8)

            top = int(index * channel_height + channel_height * 0.2)
            bottom = int(index * channel_height + channel_height * 0.8)
            bottom = max(bottom, top + 1)

            if busy.any():
                buffer[top:bottom, busy] = dim
                buffer[top, busy] = rgb
                buffer[bottom - 1, busy] = rgb
            buffer[top, only_high] = rgb
            buffer[bottom - 1, only_low] = low_rgb

        for offset, channel in enumerate(analog):
            self._draw_analog(buffer, channel, len(channels) + offset, channel_height, width)

        self._image_buffer = buffer
        self._image = QImage(buffer.data, width, height, 3 * width, QImage.Format_RGB888)

    @staticmethod
    def _draw_analog(buffer: np.ndarray, channel, row: int, row_height: float, width: int) -> None:
        """The lowest and highest value of every column of an analog channel, as a filled envelope."""
        from .analog_viewer import analog_color

        raw = channel.raw
        if raw is None or len(raw) < 2:
            return
        count = len(raw)
        step = max(count // (width * 64), 1)  # (at most 64 samples a column are looked at)
        values = np.asarray(raw[::step], dtype=np.float64)
        starts = np.linspace(0, len(values), width + 1).astype(np.int64)[:-1]
        starts = np.minimum(starts, len(values) - 1)
        low = np.minimum.reduceat(values, starts)
        high = np.maximum.reduceat(values, starts)
        lowest, highest = float(values.min()), float(values.max())
        span = highest - lowest or 1.0
        top = row * row_height + row_height * 0.15
        usable = row_height * 0.7
        y_high = (top + (1 - (high - lowest) / span) * usable).astype(np.int64)
        y_low = (top + (1 - (low - lowest) / span) * usable).astype(np.int64)
        color = analog_color(channel)
        rgb = np.array([color.red(), color.green(), color.blue()], dtype=np.float64)
        background = np.array(OVERVIEW_BACKGROUND, dtype=np.float64)
        fill = (background + (rgb - background) * 0.45).astype(np.uint8)
        line = (background + (rgb - background) * 0.95).astype(np.uint8)
        rows = buffer.shape[0]
        for column in range(min(width, len(y_high))):
            first, last = sorted((int(y_high[column]), int(y_low[column])))
            first, last = max(first, 0), min(last, rows - 1)
            buffer[first:last + 1, column] = fill
            buffer[first, column] = line
            buffer[last, column] = line

    # --------------------------------------------------------------- painting
    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        bounds = QRectF(0, 0, self.width(), self.height())
        painter.fillRect(bounds, QColor(*OVERVIEW_BACKGROUND))

        if self._dirty:
            self._build_image()

        if self._image is not None:
            painter.drawImage(bounds, self._image)

        sample_count = self.model.sample_count
        if sample_count == 0:
            return

        ratio = bounds.width() / sample_count

        for region in self.model.regions:
            painter.fillRect(
                QRectF(region.start * ratio, 0, max(region.sample_count * ratio, 1.0), bounds.height()),
                colors.region_color(region),
            )

        session = self.model.session
        if session is not None and session.pre_trigger_samples:
            painter.setPen(QPen(colors.TRIGGER_LINE_COLOR, 1))
            x = session.pre_trigger_samples * ratio
            painter.drawLine(int(x), 0, int(x), int(bounds.height()))

        view = QRectF(
            self.model.first_sample * ratio,
            0,
            max(self.model.visible_samples * ratio, 2.0),
            bounds.height(),
        )
        # What lies outside of the waveform view is dimmed; the view is outlined in the accent
        shade = QColor(0, 0, 0, 55)
        painter.fillRect(QRectF(0, 0, view.left(), bounds.height()), shade)
        painter.fillRect(QRectF(view.right(), 0, bounds.width() - view.right(), bounds.height()), shade)
        painter.setPen(QPen(QColor(ACCENT), 1))
        painter.drawRect(view.adjusted(0.5, 0.5, -0.5, -0.5))

    # ------------------------------------------------------------ interaction
    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._move_view(event.position().x())

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.buttons() & Qt.LeftButton:
            self._move_view(event.position().x())

    def _move_view(self, x: float) -> None:
        sample_count = self.model.sample_count
        if sample_count == 0 or self.width() <= 0:
            return
        sample = int(x / self.width() * sample_count)
        self.model.center_on(sample)

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt naming
        """Scroll through the capture: trackpad by the distance swiped, wheel by a quarter view a notch."""
        from .navigation import WHEEL_NOTCH, is_trackpad, trackpad_delta

        if is_trackpad(event):
            dx, dy = trackpad_delta(event)
            distance = dx if abs(dx) >= abs(dy) else dy
            samples = -distance / max(self.width(), 1) * max(self.model.visible_samples, 1) * 4
        else:
            angle = event.angleDelta()
            delta = angle.x() if abs(angle.x()) > abs(angle.y()) else angle.y()
            samples = -delta / WHEEL_NOTCH * max(self.model.visible_samples // 4, 1)
        whole = int(round(samples))
        if whole == 0:
            event.ignore()
            return
        self.model.scroll_by(whole)
        event.accept()
