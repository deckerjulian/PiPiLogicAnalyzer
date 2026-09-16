# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of PiPiLogicAnalyzer 7, a port and extension of his software;
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
from ..view_model import CaptureViewModel

PREVIEW_HEIGHT = 120
MAX_PREVIEW_CHANNELS = 32


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
        buffer[:, :] = (34, 34, 34)

        channel_height = height / len(channels)
        boundaries = np.linspace(0, sample_count, width + 1).astype(np.int64)
        boundaries[-1] = sample_count
        starts = boundaries[:-1]
        counts = np.maximum(np.diff(boundaries), 0)
        reduce_positions = np.minimum(starts, sample_count - 1)

        for index, channel in enumerate(channels):
            samples = channel.samples
            if samples is None or samples.size == 0:
                continue

            sums = np.add.reduceat(samples.astype(np.int64), reduce_positions)
            # Columns with no samples inherit the value of the previous column.
            sums = np.where(counts > 0, sums, 0)
            effective = np.maximum(counts, 1)
            any_high = sums > 0
            any_low = sums < effective

            # Columns that cover no sample at all (more pixels than samples).
            empty = counts <= 0
            if empty.any():
                nearest = np.minimum(starts, sample_count - 1)
                values = samples[nearest] != 0
                any_high = np.where(empty, values, any_high)
                any_low = np.where(empty, ~values, any_low)

            color = colors.get_channel_color(channel)
            rgb = np.array([color.red(), color.green(), color.blue()], dtype=np.uint8)
            dim = (rgb * 0.6).astype(np.uint8)

            top = int(index * channel_height + channel_height * 0.2)
            bottom = int(index * channel_height + channel_height * 0.8)
            bottom = max(bottom, top + 1)

            busy = any_high & any_low
            # Also fill the columns where the level changed from the previous
            # one, so a fast signal reads as a continuous trace instead of a
            # dashed line.
            pure_level = any_high & ~any_low
            changed = np.zeros_like(busy)
            changed[1:] = pure_level[1:] != pure_level[:-1]
            busy = busy | changed

            if busy.any():
                buffer[top:bottom, busy] = dim
            only_high = any_high & ~busy
            only_low = any_low & ~busy
            buffer[top, only_high] = rgb
            buffer[bottom - 1, only_low] = rgb

        self._image_buffer = buffer
        self._image = QImage(buffer.data, width, height, 3 * width, QImage.Format_RGB888)

    # --------------------------------------------------------------- painting
    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        bounds = QRectF(0, 0, self.width(), self.height())
        painter.fillRect(bounds, QColor(34, 34, 34))

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
        painter.fillRect(view, QColor(255, 255, 255, 40))
        painter.setPen(QPen(QColor(255, 255, 255, 160), 1))
        painter.drawRect(view)

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
