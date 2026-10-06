# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Zoom and pan in charts (chart documents, plots of panels), as in the waveform and the flow:

* two fingers move, pinch zooms at the fingers, the mouse wheel zooms at the pointer (``Shift``:
  only the values, ``Ctrl``/Cmd: moves sideways; *Settings → Navigation* can swap wheel and
  ``Ctrl``), dragging with the mouse moves;
* a double-click or the smart zoom gesture goes back to the automatic range, which follows the
  values (a strip chart keeps showing the newest ones).
"""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt
from PySide6.QtWidgets import QWidget

from . import navigation

Bounds = tuple[float, float, float, float]


def wide_enough(low: float, high: float) -> bool:
    """Whether ``low``..``high`` is a range a chart can show: wider than the precision of its
    values (relative to them – a millionth of a volt is a fine range, a billionth of a second at
    100 s is none), so that ticks and coordinates can still be told apart."""
    return high - low > max(abs(low), abs(high)) * 1e-9 and high - low > 1e-300


class PlotNavigator:
    """The range a chart shows: ``None`` (automatic) or one the user zoomed or moved to."""

    def __init__(self, widget: QWidget, plot_rect: Callable[[], QRectF]) -> None:
        self.widget = widget
        self.plot_rect = plot_rect
        self.view: Optional[Bounds] = None
        self._last: Optional[Bounds] = None
        self._drag: Optional[QPointF] = None

    @property
    def automatic(self) -> bool:
        return self.view is None

    def bounds(self, automatic: Optional[Bounds]) -> Optional[Bounds]:
        """What to draw: the user's range, else ``automatic``."""
        shown = self.view if self.view is not None else automatic
        self._last = shown
        return shown

    def reset(self) -> None:
        self.view = None
        self.widget.update()

    # ----------------------------------------------------------- geometry
    def _value_at(self, point: QPointF) -> Optional[tuple[float, float]]:
        if self._last is None:
            return None
        x0, x1, y0, y1 = self._last
        plot = self.plot_rect()
        if plot.width() <= 0 or plot.height() <= 0:
            return None
        return (x0 + (point.x() - plot.left()) / plot.width() * (x1 - x0),
                y1 - (point.y() - plot.top()) / plot.height() * (y1 - y0))

    def zoom(self, factor: float, at: QPointF, x: bool = True, y: bool = True) -> None:
        """``factor`` > 1 shows more (out), < 1 less (in), keeping the value under ``at``."""
        anchor = self._value_at(at)
        if anchor is None:
            return
        x0, x1, y0, y1 = self._last
        if x:
            x0, x1 = anchor[0] + (x0 - anchor[0]) * factor, anchor[0] + (x1 - anchor[0]) * factor
        if y:
            y0, y1 = anchor[1] + (y0 - anchor[1]) * factor, anchor[1] + (y1 - anchor[1]) * factor
        if wide_enough(x0, x1) and wide_enough(y0, y1):
            self.view = (x0, x1, y0, y1)
            self.widget.update()

    def pan_pixels(self, dx: float, dy: float) -> None:
        """Move as the fingers or the mouse move (content follows)."""
        if self._last is None:
            return
        x0, x1, y0, y1 = self._last
        plot = self.plot_rect()
        if plot.width() <= 0 or plot.height() <= 0:
            return
        shift_x = -dx / plot.width() * (x1 - x0)
        shift_y = dy / plot.height() * (y1 - y0)
        self.view = (x0 + shift_x, x1 + shift_x, y0 + shift_y, y1 + shift_y)
        self.widget.update()

    # -------------------------------------------------------------- input
    def wheel(self, event) -> bool:
        position = event.position()
        if navigation.is_trackpad(event):
            dx, dy = navigation.trackpad_delta(event)
            if event.modifiers() & Qt.ControlModifier:
                self.zoom(2 ** (-dy / navigation.PIXELS_PER_DOUBLING), position)
            else:
                self.pan_pixels(dx, dy)
            return True
        angle = event.angleDelta()
        delta = angle.y() or angle.x()
        if not delta:
            return False
        control = bool(event.modifiers() & Qt.ControlModifier)
        if control == (navigation.wheel_mode() == "zoom"):
            plot = self.plot_rect()
            self.pan_pixels(delta / navigation.WHEEL_NOTCH * plot.width() / 10, 0)
            return True
        inverted = navigation.zoom_inverted()
        factor = 1 / navigation.ZOOM_STEP if (delta > 0) != inverted else navigation.ZOOM_STEP
        self.zoom(factor, position, x=not (event.modifiers() & Qt.ShiftModifier))
        return True

    def gesture(self, event) -> bool:
        if event.type() != QEvent.NativeGesture:
            return False
        if navigation.is_smart_zoom(event):
            self.reset()
            return True
        factor = navigation.pinch_factor(event)
        if factor is not None:
            self.zoom(1 / factor, event.position())
            return True
        return event.gestureType() in (Qt.BeginNativeGesture, Qt.EndNativeGesture)

    def press(self, event) -> bool:
        if event.button() == Qt.LeftButton and self.plot_rect().contains(event.position()):
            self._drag = event.position()
            self.widget.setCursor(Qt.ClosedHandCursor)
            return True
        return False

    def move(self, event) -> bool:
        if self._drag is None:
            return False
        delta = event.position() - self._drag
        self._drag = event.position()
        self.pan_pixels(delta.x(), delta.y())
        return True

    def release(self, event) -> bool:
        if self._drag is None:
            return False
        self._drag = None
        self.widget.unsetCursor()
        return True

    def hint(self) -> str:
        return "" if self.automatic else "zoomed · double-click: automatic"


class NavigablePlot:
    """Mixin for a ``QWidget`` that draws a chart in ``plot_area()``: the events go to its navigator."""

    navigator: PlotNavigator

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self.navigator.wheel(event):
            event.accept()
        else:
            event.ignore()

    def event(self, event) -> bool:  # noqa: A003 - Qt naming
        if self.navigator.gesture(event):
            return True
        return super().event(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if not self.navigator.press(event):
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if not self.navigator.move(event):
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if not self.navigator.release(event):
            super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self.navigator.reset()
