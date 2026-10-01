# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of PiPiLogicAnalyzer, a port and extension of his software;
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

from ...core import colors
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
        width = max(int(self.width()), 1)
        height = max(int(self.height()), 1)
        sample_count = self.model.sample_count
        if not channels or sample_count == 0 or width < 2:
            return

        buffer = np.zeros((height, width, 3), dtype=np.uint8)
        buffer[:, :] = OVERVIEW_BACKGROUND

        channel_height = height / len(channels)
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
            # Activity as a faint band, levels as thin lines (a low one dimmer): a summary, not
            # a second waveform
            dim = (background + (rgb - background) * 0.35).astype(np.uint8)
            low_rgb = (background + (rgb - background) * 0.45).astype(np.uint8)
            rgb = (background + (rgb - background) * 0.85).astype(np.uint8)

            top = int(index * channel_height + channel_height * 0.2)
            bottom = int(index * channel_height + channel_height * 0.8)
            bottom = max(bottom, top + 1)

            if busy.any():
                buffer[top:bottom, busy] = dim
            buffer[top, only_high] = rgb
            buffer[bottom - 1, only_low] = low_rgb

        self._image_buffer = buffer
        self._image = QImage(buffer.data, width, height, 3 * width, QImage.Format_RGB888)

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
                region.region_color,
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
        shade = QColor(0, 0, 0, 110)
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
        delta = event.angleDelta().y()
        if delta == 0:
            event.ignore()
            return
        step = max(self.model.visible_samples // 4, 1)
        self.model.scroll_by(-step if delta > 0 else step)
        event.accept()
