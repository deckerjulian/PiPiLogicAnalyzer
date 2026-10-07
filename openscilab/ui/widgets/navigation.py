# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of openSciLab, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Wheel, trackpad and pinch navigation shared by the waveform widgets.

* Mouse wheel: zoom at the pointer (``Shift``: in bigger steps), ``Ctrl``: scroll horizontally,
  ``Alt``: taller or shorter channels.
* Horizontal wheel (tilt wheel, ``Shift`` + wheel on macOS): scroll horizontally.
* Trackpad: swiping scrolls horizontally through the samples and vertically through the channels;
  with ``Ctrl`` (Cmd on macOS) swiping up and down zooms at the pointer.
* Pinch gesture (macOS): zoom at the fingers; smart zoom (double tap with two fingers): fit.

Trackpads report exact pixel distances in scroll phases, mouse wheels report notches of 120 without
a phase; that is how both are told apart when the input device does not say it itself.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QInputDevice
from PySide6.QtWidgets import QScrollArea, QWidget

from ..view_model import CHANNEL_HEIGHT_STEP, CaptureViewModel

log = logging.getLogger(__name__)

ZOOM_STEP = 1.2
COARSE_ZOOM_STEP = 2.0
WHEEL_NOTCH = 120
#: Part of the visible samples one wheel notch scrolls.
NOTCH_SCROLL = 0.1
#: Trackpad pixels (with Ctrl/Cmd) that zoom by a factor of two.
PIXELS_PER_DOUBLING = 240.0


def trackpad_delta(event) -> tuple[float, float]:
    """The distance the fingers moved, in pixels (``angleDelta / 8`` when the platform gives none)."""
    pixel = event.pixelDelta()
    if not pixel.isNull():
        return float(pixel.x()), float(pixel.y())
    angle = event.angleDelta()
    return angle.x() / 8.0, angle.y() / 8.0


def pinch_factor(event) -> Optional[float]:
    """How much a pinch gesture enlarges (>1) or shrinks; ``None`` for other events."""
    if event.type() != QEvent.NativeGesture or event.gestureType() != Qt.ZoomNativeGesture:
        return None
    value = event.value()
    return 1 + value if value > -1 else None


def is_smart_zoom(event) -> bool:
    return event.type() == QEvent.NativeGesture and event.gestureType() == Qt.SmartZoomNativeGesture


def wheel_mode() -> str:
    """What a mouse wheel does without modifiers: ``"zoom"`` or ``"scroll"`` (*Settings → Navigation*)."""
    from ...core import preferences

    try:
        return preferences.get("navigation.wheel")
    except Exception:  # noqa: BLE001
        log.debug("return preferences.get('navigation.wheel') failed (ignored)", exc_info=True)
        return "zoom"


def zoom_inverted() -> bool:
    from ...core import preferences

    try:
        return bool(preferences.get("navigation.invert_zoom"))
    except Exception:  # noqa: BLE001
        log.debug("return bool(preferences.get('navigation.invert_zoom')) failed (ignored)", exc_info=True)
        return False


def is_trackpad(event) -> bool:
    device = event.device() if hasattr(event, "device") else None
    if device is not None and device.type() == QInputDevice.DeviceType.TouchPad:
        return True
    return event.phase() != Qt.NoScrollPhase and not event.pixelDelta().isNull()


class WheelNavigator:
    """Turns wheel events of a widget showing samples from ``data_left`` to its right edge."""

    def __init__(self, widget: QWidget, model: CaptureViewModel, data_left: float = 0.0,
                 alt_wheel: Optional[Callable[[float], None]] = None) -> None:
        self.widget = widget
        self.model = model
        self.data_left = data_left
        #: what Alt + wheel does (factor > 1: taller); default: the channel height
        self.alt_wheel = alt_wheel or self.model.zoom_channels
        self._remainder = 0.0

    # --------------------------------------------------------------- geometry
    def samples_per_pixel(self) -> float:
        return self.model.visible_samples / max(self.widget.width() - self.data_left, 1)

    def sample_at(self, x: float) -> float:
        return self.model.first_sample + (x - self.data_left) * self.samples_per_pixel()

    # ----------------------------------------------------------------- events
    def handle_wheel(self, event) -> bool:
        angle = event.angleDelta()

        if event.modifiers() & Qt.AltModifier:
            # Several platforms turn Alt + wheel into a horizontal wheel, so both directions count.
            delta = angle.y() or angle.x()
            if delta:
                self.alt_wheel(CHANNEL_HEIGHT_STEP if delta > 0 else 1 / CHANNEL_HEIGHT_STEP)
                return True

        if is_trackpad(event):
            dx, dy = trackpad_delta(event)
            if event.modifiers() & Qt.ControlModifier:
                if dy:
                    self.model.zoom(2 ** (-dy / PIXELS_PER_DOUBLING), self.sample_at(event.position().x()))
                return bool(dy)
            if dx:
                self.pan_pixels(dx)
            if dy:
                self.scroll_channels(dy)
            return bool(dx or dy)

        wheel_zooms = wheel_mode() == "zoom"
        control = bool(event.modifiers() & Qt.ControlModifier)
        if angle.y() and control == wheel_zooms:
            self.pan_notches(-angle.y())
            return True
        if angle.x() and abs(angle.x()) >= abs(angle.y()):
            self.pan_notches(angle.x())
            return True
        if angle.y():
            step = COARSE_ZOOM_STEP if event.modifiers() & Qt.ShiftModifier else ZOOM_STEP
            factor = 1 / step if (angle.y() > 0) != zoom_inverted() else step
            self.model.zoom(factor, self.sample_at(event.position().x()))
            return True
        return False

    def handle_gesture(self, event) -> bool:
        """Pinch to zoom (``QNativeGestureEvent``); returns whether the event was used."""
        if event.type() != QEvent.NativeGesture:
            return False
        if is_smart_zoom(event):
            self.model.zoom_to_fit()
            return True
        factor = pinch_factor(event)
        if factor is not None:
            self.model.zoom(1 / factor, self.sample_at(event.position().x()))
            return True
        return event.gestureType() in (Qt.BeginNativeGesture, Qt.EndNativeGesture)

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
