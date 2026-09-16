# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of PiPiLogicAnalyzer, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Wheel, trackpad and pinch navigation shared by the waveform widgets.

* Mouse wheel: zoom at the pointer (``Shift``: in bigger steps), ``Ctrl``: scroll horizontally,
  ``Alt``: taller or shorter channels.
* Horizontal wheel (tilt wheel, ``Shift`` + wheel on macOS): scroll horizontally.
* Trackpad: swiping scrolls horizontally through the samples and vertically through the channels.
* Pinch gesture (macOS): zoom at the fingers.

Trackpads report exact pixel distances in scroll phases, mouse wheels report notches of 120 without
a phase; that is how both are told apart when the input device does not say it itself.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QInputDevice
from PySide6.QtWidgets import QScrollArea, QWidget

from ..view_model import CHANNEL_HEIGHT_STEP, CaptureViewModel

ZOOM_STEP = 1.2
COARSE_ZOOM_STEP = 2.0
WHEEL_NOTCH = 120
#: Part of the visible samples one wheel notch scrolls.
NOTCH_SCROLL = 0.1


def is_trackpad(event) -> bool:
    device = event.device() if hasattr(event, "device") else None
    if device is not None and device.type() == QInputDevice.DeviceType.TouchPad:
        return True
    return event.phase() != Qt.NoScrollPhase and not event.pixelDelta().isNull()


class WheelNavigator:
    """Turns wheel events of a widget showing samples from ``data_left`` to its right edge."""

    def __init__(self, widget: QWidget, model: CaptureViewModel, data_left: float = 0.0) -> None:
        self.widget = widget
        self.model = model
        self.data_left = data_left
        self._remainder = 0.0

    # --------------------------------------------------------------- geometry
    def samples_per_pixel(self) -> float:
        return self.model.visible_samples / max(self.widget.width() - self.data_left, 1)

    def sample_at(self, x: float) -> float:
        return self.model.first_sample + (x - self.data_left) * self.samples_per_pixel()

    # ----------------------------------------------------------------- events
    def handle_wheel(self, event) -> bool:
        pixel = event.pixelDelta()
        angle = event.angleDelta()

        if event.modifiers() & Qt.AltModifier:
            # Several platforms turn Alt + wheel into a horizontal wheel, so both directions count.
            delta = angle.y() or angle.x()
            if delta:
                self.model.zoom_channels(CHANNEL_HEIGHT_STEP if delta > 0 else 1 / CHANNEL_HEIGHT_STEP)
                return True

        if is_trackpad(event):
            dx, dy = (pixel.x(), pixel.y()) if not pixel.isNull() else (angle.x() / 8.0, angle.y() / 8.0)
            if dx:
                self.pan_pixels(dx)
            if dy:
                self.scroll_channels(dy)
            return bool(dx or dy)

        if event.modifiers() & Qt.ControlModifier and angle.y():
            self.pan_notches(-angle.y())
            return True
        if angle.x() and abs(angle.x()) >= abs(angle.y()):
            self.pan_notches(angle.x())
            return True
        if angle.y():
            step = COARSE_ZOOM_STEP if event.modifiers() & Qt.ShiftModifier else ZOOM_STEP
            factor = 1 / step if angle.y() > 0 else step
            self.model.zoom(factor, self.sample_at(event.position().x()))
            return True
        return False

    def handle_gesture(self, event) -> bool:
        """Pinch to zoom (``QNativeGestureEvent``); returns whether the event was used."""
        if event.type() != QEvent.NativeGesture or event.gestureType() != Qt.ZoomNativeGesture:
            return False
        value = event.value()
        if value > -1:
            self.model.zoom(1 / (1 + value), self.sample_at(event.position().x()))
        return True

    # ------------------------------------------------------------------ moves
    def pan_pixels(self, dx: float) -> None:
        """Follow the fingers: content moving right shows earlier samples."""
        samples = -dx * self.samples_per_pixel() + self._remainder
        whole = int(samples)
        self._remainder = samples - whole
        if whole:
            self.model.scroll_by(whole)

    def pan_notches(self, delta: int) -> None:
        """Wheel notches: a positive delta (wheel left/up) shows earlier samples."""
        step = max(int(self.model.visible_samples * NOTCH_SCROLL), 1)
        samples = int(round(-delta / WHEEL_NOTCH * step)) or (-1 if delta > 0 else 1)
        self.model.scroll_by(samples)

    def scroll_channels(self, dy: float) -> None:
        area = self._scroll_area()
        if area is not None:
            bar = area.verticalScrollBar()
            bar.setValue(bar.value() - int(round(dy)))

    def _scroll_area(self) -> Optional[QScrollArea]:
        parent = self.widget.parentWidget()
        while parent is not None and not isinstance(parent, QScrollArea):
            parent = parent.parentWidget()
        return parent
