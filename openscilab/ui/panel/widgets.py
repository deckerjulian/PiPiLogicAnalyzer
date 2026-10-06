# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The widgets of a panel: controls send values (``sent``), displays show them (``show_value``).

Each one draws a :class:`~openscilab.lab.panel_model.PanelWidget`; what it is bound to is the
business of the panel document.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any, Optional

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ...core import signals, units
from ...lab.panel_model import PanelWidget, display_number
from ..widgets.plot_lines import data_bounds, screen_points
from ..widgets.plot_navigation import NavigablePlot, PlotNavigator, wide_enough
from ..theme import BORDER, TEXT_MUTED, qcolor, set_role, set_variant, token

SLIDER_STEPS = 1000


class PanelItem(QFrame):
    """A widget of the panel in a card with its title."""

    #: the value a control sends into the flow
    sent = Signal(object)

    def __init__(self, model: PanelWidget, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.model = model
        set_role(self, "card")
        self.setObjectName(f"panel-{model.id}")
        self.layout_ = QVBoxLayout(self)
        self.layout_.setContentsMargins(10, 8, 10, 8)
        self.layout_.setSpacing(4)
        if model.kind != "label":
            self.title_label = QLabel(model.label, self)
            set_role(self.title_label, "hint")
            self.layout_.addWidget(self.title_label)
        self.build()

    def build(self) -> None:
        """Add the content (subclasses)."""

    def show_value(self, value: Any) -> None:
        """A value of the port the display is bound to."""

    def reset(self) -> None:
        """The flow starts again."""

    def current(self) -> Any:
        """The value a control holds (sent once when the flow starts), ``None`` for none."""
        return None


class SwitchItem(PanelItem):
    def build(self) -> None:
        self.button = QPushButton("Off", self)
        self.button.setCheckable(True)
        self.button.setChecked(bool(self.model.option("value", False)))
        self.button.setText("On" if self.button.isChecked() else "Off")
        self.button.toggled.connect(self._toggled)
        self.layout_.addWidget(self.button)

    def _toggled(self, on: bool) -> None:
        self.button.setText("On" if on else "Off")
        self.sent.emit(on)

    def current(self) -> Any:
        return self.button.isChecked()


class ButtonItem(PanelItem):
    def build(self) -> None:
        self.title_label.hide()
        self.button = QPushButton(self.model.label, self)
        set_variant(self.button, "primary")
        self.button.setMinimumHeight(36)
        self.button.clicked.connect(lambda: self.sent.emit(True))
        self.layout_.addWidget(self.button)


class SliderItem(PanelItem):
    def build(self) -> None:
        self.low = float(self.model.option("min", 0.0))
        self.high = float(self.model.option("max", 1.0))
        self.unit = str(self.model.option("unit", ""))
        row = QHBoxLayout()
        self.slider = QSlider(Qt.Horizontal, self)
        self.slider.setRange(0, SLIDER_STEPS)
        row.addWidget(self.slider, 1)
        self.value_label = QLabel(self)
        self.value_label.setMinimumWidth(70)
        row.addWidget(self.value_label)
        self.layout_.addLayout(row)
        self.set_value(float(self.model.option("value", self.low)))
        self.slider.valueChanged.connect(self._moved)

    def value(self) -> float:
        value = self.low + (self.high - self.low) * self.slider.value() / SLIDER_STEPS
        step = self.model.option("step")
        if step:
            value = round((value - self.low) / float(step)) * float(step) + self.low
        return value

    def set_value(self, value: float) -> None:
        span = (self.high - self.low) or 1.0
        self.slider.setValue(int(round((value - self.low) / span * SLIDER_STEPS)))
        self.value_label.setText(units.format_quantity(self.value(), self.unit, 4))

    def _moved(self) -> None:
        self.value_label.setText(units.format_quantity(self.value(), self.unit, 4))
        self.sent.emit(self.value())

    def current(self) -> Any:
        return self.value()


class InputItem(PanelItem):
    def build(self) -> None:
        row = QHBoxLayout()
        self.spin = QDoubleSpinBox(self)
        self.spin.setRange(-1e12, 1e12)
        self.spin.setDecimals(6)
        self.spin.setValue(float(self.model.option("value", 0.0)))
        unit = str(self.model.option("unit", ""))
        if unit:
            self.spin.setSuffix(f" {unit}")
        row.addWidget(self.spin, 1)
        self.set_button = QPushButton("Set", self)
        self.set_button.clicked.connect(lambda: self.sent.emit(self.spin.value()))
        self.spin.editingFinished.connect(lambda: self.sent.emit(self.spin.value()))
        row.addWidget(self.set_button)
        self.layout_.addLayout(row)

    def current(self) -> Any:
        return self.spin.value()


class ChoiceItem(PanelItem):
    def build(self) -> None:
        self.box = QComboBox(self)
        for option in self.model.option("options", []) or []:
            self.box.addItem(str(option), option)
        value = self.model.option("value")
        if value is not None:
            self.box.setCurrentIndex(max(self.box.findData(value), 0))
        self.box.currentIndexChanged.connect(lambda _index: self.sent.emit(self.box.currentData()))
        self.layout_.addWidget(self.box)

    def current(self) -> Any:
        return self.box.currentData()


class NumberItem(PanelItem):
    def build(self) -> None:
        self.value_label = QLabel("–", self)
        self.value_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        font = self.value_label.font()
        font.setPointSizeF(font.pointSizeF() * 2.2)
        font.setBold(True)
        self.value_label.setFont(font)
        self.layout_.addWidget(self.value_label)

    def show_value(self, value: Any) -> None:
        number = display_number(value)
        if number is None:
            self.value_label.setText(str(value) if not isinstance(value, signals.Signal) else value.describe())
            return
        unit = str(self.model.option("unit", "") or getattr(value, "unit", "") or "")
        digits = int(self.model.option("digits", 4))
        self.value_label.setText(units.format_quantity(number, unit, digits) if unit else f"{number:.{digits}g}")

    def reset(self) -> None:
        self.value_label.setText("–")


class Led(QWidget):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.on = False
        self.color = QColor("#7fd18b")
        self.setMinimumSize(28, 28)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        size = min(self.width(), self.height()) - 4
        rect = QRectF((self.width() - size) / 2, (self.height() - size) / 2, size, size)
        painter.setBrush(self.color if self.on else qcolor("led.off"))
        painter.setPen(QPen(QColor(BORDER), 1))
        painter.drawEllipse(rect)


class LedItem(PanelItem):
    def build(self) -> None:
        self.led = Led(self)
        color = self.model.option("color")
        if color:
            self.led.color = QColor(str(color))
        self.layout_.addWidget(self.led, 1, Qt.AlignCenter)

    def show_value(self, value: Any) -> None:
        number = display_number(value)
        self.led.on = bool(value) if number is None else number >= float(self.model.option("threshold", 0.5))
        self.led.update()

    def reset(self) -> None:
        self.led.on = False
        self.led.update()


class Plot(NavigablePlot, QWidget):
    """Lines of (x, y) points (zoom and move them like a chart), or a capture (digital rows,
    analog lines)."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.navigator = PlotNavigator(self, lambda: QRectF(self.rect()).adjusted(40, 6, -6, -16))
        self.points: deque = deque(maxlen=500)
        self.capture: Any = None
        self.unit = ""
        self.setMinimumHeight(100)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        area = QRectF(self.rect()).adjusted(40, 6, -6, -16)
        painter.setPen(QPen(QColor(BORDER), 1))
        painter.drawRect(area)
        if self.capture is not None:
            self._draw_capture(painter, area)
        elif len(self.points) >= 2:
            self._draw_points(painter, area)

    def _draw_points(self, painter: QPainter, area: QRectF) -> None:
        xs = [point[0] for point in self.points]
        ys = [point[1] for point in self.points]
        # a measurement without a result is NaN: such points are left out, the others are drawn
        found = data_bounds([(xs, ys)])
        if found is None:
            return
        x0, x1, y0, y1 = found
        if not wide_enough(x0, x1):
            x1 = x0 + 1
        if not wide_enough(y0, y1):
            y0, y1 = y0 - 1, y1 + 1
        x0, x1, y0, y1 = self.navigator.bounds((x0, x1, y0, y1))
        painter.save()
        painter.setClipRect(area)
        painter.setPen(QPen(QColor(token("type.scalar")), 1.5))
        painter.drawPolyline(screen_points(xs, ys, (x0, x1, y0, y1), area))
        painter.restore()
        painter.setPen(QColor(TEXT_MUTED))
        painter.drawText(QRectF(0, area.top() - 2, 38, 14), Qt.AlignRight, units.format_quantity(y1, self.unit, 3))
        painter.drawText(QRectF(0, area.bottom() - 10, 38, 14), Qt.AlignRight, units.format_quantity(y0, self.unit, 3))

    def _draw_capture(self, painter: QPainter, area: QRectF) -> None:
        value = self.capture
        lines: list[tuple[str, np.ndarray, bool]] = []
        if isinstance(value, signals.Capture):
            lines += [(name, values, True) for name, values in value.digital.items()]
            lines += [(name, values, False) for name, values in value.analog.items()]
        elif isinstance(value, signals.Digital):
            lines.append((value.name, value.values, True))
        elif isinstance(value, signals.Analog):
            lines.append((value.name, value.values, False))
        if not lines:
            return
        row = area.height() / len(lines)
        for index, (name, values, digital) in enumerate(lines):
            values = np.asarray(values, dtype=np.float64)
            if not len(values):
                continue
            step = max(len(values) // int(max(area.width(), 1) * 2), 1)
            values = values[::step]
            low, high = (0.0, 1.0) if digital else (float(values.min()), float(values.max()))
            if high - low < 1e-15:
                high = low + 1
            top, bottom = area.top() + index * row + 3, area.top() + (index + 1) * row - 3
            path = QPainterPath()
            for sample, level in enumerate(values):
                point = QPointF(area.left() + sample * area.width() / len(values),
                                bottom - (level - low) / (high - low) * (bottom - top))
                path.moveTo(point) if sample == 0 else path.lineTo(point)
            painter.setPen(QPen(QColor(token("type.digital" if digital else "type.analog")), 1.2))
            painter.drawPath(path)
            painter.setPen(QColor(TEXT_MUTED))
            painter.drawText(QRectF(0, top, 38, bottom - top), Qt.AlignRight | Qt.AlignVCenter, name)


class ChartItem(PanelItem):
    def build(self) -> None:
        self.plot = Plot(self)
        self.plot.points = deque(maxlen=int(self.model.option("points", 500)))
        self.plot.unit = str(self.model.option("unit", ""))
        self.layout_.addWidget(self.plot, 1)
        self._start = time.monotonic()

    def show_value(self, value: Any) -> None:
        if isinstance(value, dict) and "x" in value and "y" in value:  # a point of an XY pair
            self.plot.points.append((float(value["x"]), float(value["y"])))
        elif isinstance(value, (tuple, list)) and len(value) == 2 and all(isinstance(v, (int, float)) for v in value):
            self.plot.points.append((float(value[0]), float(value[1])))
        else:
            number = display_number(value)
            if number is None:
                return
            at = getattr(value, "at", None)
            # (a value without a time has at == 0: all of those would pile up at the left edge)
            x = float(at) if at else time.monotonic() - self._start
            self.plot.points.append((x, number))
            if not self.plot.unit:
                self.plot.unit = getattr(value, "unit", "") or ""
        self.plot.update()

    def reset(self) -> None:
        self.plot.points.clear()
        self._start = time.monotonic()
        self.plot.update()


class ScopeItem(PanelItem):
    def build(self) -> None:
        self.plot = Plot(self)
        self.layout_.addWidget(self.plot, 1)

    def show_value(self, value: Any) -> None:
        if isinstance(value, (signals.Capture, signals.Digital, signals.Analog)):
            self.plot.capture = value
            self.plot.update()

    def reset(self) -> None:
        self.plot.capture = None
        self.plot.update()


class LabelItem(PanelItem):
    def build(self) -> None:
        self.text = QLabel(str(self.model.option("text", self.model.label)), self)
        self.text.setWordWrap(True)
        self.layout_.addWidget(self.text)


ITEMS = {"switch": SwitchItem, "button": ButtonItem, "slider": SliderItem, "input": InputItem,
         "choice": ChoiceItem, "number": NumberItem, "led": LedItem, "chart": ChartItem, "scope": ScopeItem,
         "label": LabelItem}


def make_item(model: PanelWidget, parent: Optional[QWidget] = None) -> PanelItem:
    return ITEMS[model.kind](model, parent)
