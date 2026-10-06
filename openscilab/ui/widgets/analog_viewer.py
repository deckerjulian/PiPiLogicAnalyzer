# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Analog tracks below the digital channels.

Each analog channel is a track with its name and scale on the left and the waveform on the right,
on the same time axis as the digital channels. Three ways to draw it, by the samples per pixel:

* **envelope** (zoomed out): the minimum and maximum of every pixel column, from the samples or –
  for long or still arriving captures – from the min/max overview (``core/overview.py``);
* **line** (about a sample per pixel or fewer): a line through the samples;
* **points** (zoomed far in): the line and a dot per sample.

The pointer shows the value in the channel's unit; the context menu makes a digital channel from
a threshold with hysteresis.
"""

from __future__ import annotations

from typing import Optional

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QMenu, QSizePolicy, QToolTip, QWidget

from ...core import units
from .. import colors
from ...core.overview import column_envelope
from ...driver.models import AnalogChannel
from ..theme import DISPLAY

# the waveform display is dark in both themes
BORDER = DISPLAY["BORDER"]
TEXT = DISPLAY["TEXT"]
TEXT_MUTED = DISPLAY["TEXT_MUTED"]
from ..view_model import CaptureViewModel
from . import overlays, time_axis
from .channel_viewer import CHANNEL_COLUMN_WIDTH
from .navigation import WheelNavigator

#: Samples per pixel up to which the envelope is computed from the samples (beyond: the overview)
RAW_ENVELOPE_LIMIT = 64
#: Pixels per sample from which every sample gets a dot
POINT_WIDTH = 6.0
#: Colours of the analog tracks (after the digital palette)
ANALOG_COLORS = ("#f5c542", "#4fc3f7", "#e57373", "#81c784", "#ba68c8", "#ffb74d", "#4db6ac", "#f06292")


def analog_color(channel: AnalogChannel) -> QColor:
    if channel.channel_color is not None:
        return colors.color_from_uint(channel.channel_color)
    return QColor(ANALOG_COLORS[channel.channel_number % len(ANALOG_COLORS)])


class AnalogViewer(QWidget):
    """The analog tracks of the capture (hidden while there are none)."""

    #: (channel, threshold, hysteresis): make a digital channel from it
    derive_requested = Signal(object)

    def __init__(self, model: CaptureViewModel, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.model = model
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)
        for signal in (model.view_changed, model.samples_appended, model.cursors_changed, model.tiles_arrived,
                       model.marker_changed):
            signal.connect(self.update)
        for signal in (model.capture_changed, model.channels_changed, model.analog_height_changed):
            signal.connect(self._resize)
        self._resize()

    # ------------------------------------------------------------------ layout
    def channels(self) -> list[AnalogChannel]:
        return self.model.visible_analog

    def _resize(self) -> None:
        count = len(self.channels())
        self.setVisible(count > 0)
        self.setFixedHeight(max(count * self.model.analog_height, 0))
        self.update()

    def plot_width(self) -> float:
        return max(self.width() - CHANNEL_COLUMN_WIDTH, 1)

    def sample_width(self) -> float:
        return self.plot_width() / max(self.model.visible_samples, 1)

    def x_for(self, sample: float) -> float:
        """x of a sample of the session, in the plot area."""
        return (sample - self.model.first_sample) * self.sample_width()

    def track_at(self, y: float) -> Optional[AnalogChannel]:
        channels = self.channels()
        index = int(y // max(self.model.analog_height, 1))
        return channels[index] if 0 <= index < len(channels) else None

    # ---------------------------------------------------------------- values
    def envelope(self, channel: AnalogChannel, columns: int) -> tuple[np.ndarray, np.ndarray, bool]:
        """Minimum and maximum raw value of each pixel column; ``True`` when exact."""
        model = self.model
        first = model.analog_index(model.first_sample, channel)
        last = model.analog_index(model.first_sample + model.visible_samples, channel)
        length = channel.sample_count
        last = min(last, length)
        if last <= first or columns <= 0:
            return np.zeros(0), np.zeros(0), True
        progressive = model.progressive
        loaded = channel.raw is not None and (progressive is None or progressive.is_loaded(int(first), int(last) + 1))
        if loaded and (last - first) / columns <= RAW_ENVELOPE_LIMIT:
            values = np.asarray(channel.raw[int(first):int(np.ceil(last))])
            low, high = column_envelope(values, columns)
            return low, high, True
        overview = model.analog_overview(channel)
        if overview is not None:
            low, high = overview.envelope(first, last, columns)
            return low, high, False
        if channel.raw is None:
            return np.zeros(0), np.zeros(0), False
        low, high = column_envelope(np.asarray(channel.raw[int(first):int(np.ceil(last))]), columns)
        return low, high, True

    def value_at(self, channel: AnalogChannel, sample: float) -> Optional[float]:
        index = int(self.model.analog_index(sample, channel))
        if channel.raw is None or not 0 <= index < len(channel.raw):
            return None
        progressive = self.model.progressive
        if progressive is not None and not progressive.is_loaded(index, index + 1):
            return None
        return float(channel.raw[index]) * channel.scale + channel.offset

    # ---------------------------------------------------------------- paint
    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        channels = self.channels()
        height = self.model.analog_height
        width = self.plot_width()
        for index, channel in enumerate(channels):
            top = index * height
            painter.fillRect(QRectF(0, top, self.width(), height), colors.BG_CHANNEL_COLORS[index % 2])
            self._draw_name(painter, channel, QRectF(0, top, CHANNEL_COLUMN_WIDTH, height))
            painter.save()
            painter.translate(CHANNEL_COLUMN_WIDTH, top)
            painter.setClipRect(QRectF(0, 0, width, height))
            self._draw_track(painter, channel, QRectF(0, 0, width, height))
            painter.restore()
            painter.setPen(QPen(colors.ROW_SEPARATOR_COLOR, 1))
            painter.drawLine(QPointF(0, top + height - 0.5), QPointF(self.width(), top + height - 0.5))
        painter.save()
        painter.translate(CHANNEL_COLUMN_WIDTH, 0)
        bounds = QRectF(0, 0, width, self.height())
        overlays.draw_selection(painter, self.model, self.x_for, bounds)
        overlays.draw_cursors(painter, self.model, self.x_for, bounds)
        painter.restore()

    def _draw_name(self, painter: QPainter, channel: AnalogChannel, rect: QRectF) -> None:
        color = analog_color(channel)
        painter.fillRect(QRectF(rect.left(), rect.top(), 4, rect.height()), color)
        painter.setPen(QColor(TEXT))
        font = QFont(painter.font())
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(rect.adjusted(12, 4, -6, -rect.height() / 2), Qt.AlignLeft | Qt.AlignTop, channel.display_name)
        font.setBold(False)
        font.setPointSizeF(max(font.pointSizeF() - 1.5, 7))
        painter.setFont(font)
        painter.setPen(QColor(TEXT_MUTED))
        low, high = self.model.analog_range(channel)
        painter.drawText(rect.adjusted(12, 0, -6, -4), Qt.AlignLeft | Qt.AlignBottom,
                         f"{units.format_quantity(low, channel.unit, 3)} … {units.format_quantity(high, channel.unit, 3)}")

    def _draw_track(self, painter: QPainter, channel: AnalogChannel, rect: QRectF) -> None:
        model = self.model
        low, high = self.model.analog_range(channel)
        span = high - low or 1.0

        def y_of(raw: np.ndarray) -> np.ndarray:
            volts = np.asarray(raw, dtype=np.float64) * channel.scale + channel.offset
            return rect.bottom() - 3 - (volts - low) / span * (rect.height() - 6)

        # grid: the time divisions and the zero line
        for tick in time_axis.model_ticks(model, rect.width()):
            x = round(self.x_for(tick.sample)) + 0.5
            painter.setPen(QPen(colors.GRID_MAJOR_COLOR if tick.major else colors.GRID_MINOR_COLOR, 1))
            painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
        if low < 0 < high:
            zero = float(y_of(np.asarray([(0 - channel.offset) / (channel.scale or 1)]))[0])
            painter.setPen(QPen(QColor(BORDER), 1, Qt.DashLine))
            painter.drawLine(QPointF(0, zero), QPointF(rect.width(), zero))

        color = analog_color(channel)
        painter.setRenderHint(QPainter.Antialiasing, True)
        first_index = model.analog_index(model.first_sample, channel)
        last_index = min(model.analog_index(model.first_sample + model.visible_samples, channel), channel.sample_count)
        columns = int(rect.width())
        per_pixel = (last_index - first_index) / max(columns, 1)
        progressive = model.progressive
        if per_pixel <= 1.0 and channel.raw is not None and (
                progressive is None or progressive.is_loaded(int(first_index), int(last_index) + 1)):
            start = max(int(np.floor(first_index)), 0)
            end = min(int(np.ceil(last_index)) + 1, len(channel.raw))
            if end - start < 1:
                return
            raw = np.asarray(channel.raw[start:end])
            positions = np.arange(start, end, dtype=np.float64)
            factor = model.frequency / channel.rate if channel.rate and channel.rate != model.frequency else 1.0
            xs = (positions * factor - model.first_sample) * self.sample_width()
            ys = y_of(raw)
            path = QPainterPath(QPointF(xs[0], ys[0]))
            for x, y in zip(xs[1:], ys[1:]):
                path.lineTo(QPointF(x, y))
            painter.setPen(QPen(color, 1.5))
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(path)
            if self.sample_width() * factor >= POINT_WIDTH:
                painter.setBrush(color)
                painter.setPen(Qt.NoPen)
                for x, y in zip(xs, ys):
                    painter.drawEllipse(QPointF(x, y), 2.2, 2.2)
        else:
            mins, maxs, exact = self.envelope(channel, columns)
            if not len(mins):
                return
            used = len(mins)
            span_x = rect.width() * min((last_index - first_index) / max(model.analog_index(
                model.visible_samples, channel), 1), 1.0)
            xs = np.arange(used) * span_x / used
            top_y, bottom_y = y_of(maxs), y_of(mins)
            fill = QColor(color)
            fill.setAlpha(110 if exact else 70)
            band = QPainterPath(QPointF(xs[0], top_y[0]))
            for x, y in zip(xs[1:], top_y[1:]):
                band.lineTo(QPointF(x, y))
            for x, y in zip(xs[::-1], bottom_y[::-1]):
                band.lineTo(QPointF(x + span_x / used, y))
            band.closeSubpath()
            painter.setPen(QPen(color, 1))
            painter.setBrush(fill)
            painter.drawPath(band)
        painter.setRenderHint(QPainter.Antialiasing, False)
        if progressive is not None:
            self._draw_missing(painter, rect)
        session = model.session
        if session is not None and session.pre_trigger_samples:
            x = round(self.x_for(session.pre_trigger_samples)) + 0.5
            painter.setPen(QPen(colors.TRIGGER_LINE_COLOR, 1, Qt.DashLine))
            painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))

    def _draw_missing(self, painter: QPainter, rect: QRectF) -> None:
        """Hatch the parts that did not arrive yet."""
        progressive = self.model.progressive
        hatch = QColor(255, 255, 255, 22)
        for tile in progressive.tiles_of(self.model.first_sample, self.model.first_sample + self.model.visible_samples):
            if progressive.loaded[tile]:
                continue
            start, end = progressive.tile_range(tile)
            left, right = self.x_for(start), self.x_for(end)
            painter.fillRect(QRectF(left, rect.top(), right - left, rect.height()), QColor(255, 255, 255, 8))
            painter.setPen(QPen(hatch, 1))
            x = left - rect.height()
            while x < right:
                painter.drawLine(QPointF(max(x, left), rect.bottom() - max(left - x, 0)),
                                 QPointF(min(x + rect.height(), right), rect.top() + max(x + rect.height() - right, 0)))
                x += 8

    # ---------------------------------------------------------------- mouse
    def sample_at(self, x: float) -> float:
        return self.model.first_sample + (x - CHANNEL_COLUMN_WIDTH) / self.sample_width()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        position = event.position()
        channel = self.track_at(position.y())
        if channel is None or position.x() < CHANNEL_COLUMN_WIDTH:
            QToolTip.hideText()
            return
        sample = self.sample_at(position.x())
        value = self.value_at(channel, sample)
        time = self.model.time_of(sample)
        text = f"{channel.display_name}: " + (units.format_quantity(value, channel.unit, 5) if value is not None
                                              else "not loaded yet") + f"\n{units.format_quantity(time, 's', 6)}"
        QToolTip.showText(event.globalPosition().toPoint(), text, self)

    @property
    def navigator(self) -> WheelNavigator:
        if getattr(self, "_navigator", None) is None:
            self._navigator = WheelNavigator(
                self, self.model, data_left=CHANNEL_COLUMN_WIDTH,
                alt_wheel=lambda factor: self.model.set_analog_height(int(round(self.model.analog_height * factor))))
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

    def _menu(self, position) -> None:
        channel = self.track_at(position.y())
        if channel is None:
            return
        menu = QMenu(self)
        menu.addAction("Digital channel from a threshold...", lambda: self.derive_requested.emit(channel))
        menu.addAction("Hide", lambda: self._hide(channel))
        menu.addSeparator()
        menu.addAction("Taller tracks", lambda: self.model.set_analog_height(int(self.model.analog_height * 1.25)))
        menu.addAction("Shorter tracks", lambda: self.model.set_analog_height(int(self.model.analog_height * 0.8)))
        menu.exec(self.mapToGlobal(position))

    def _hide(self, channel: AnalogChannel) -> None:
        channel.hidden = True
        self.model.notify_channels_changed()


def derive_digital(channel: AnalogChannel, threshold: float, hysteresis: float, number: int):
    """A digital channel of ``channel`` above ``threshold`` (with ``hysteresis``)."""
    from ...core import signals
    from ...driver.models import AnalyzerChannel

    analog = signals.Analog(values=channel.volts(), unit=channel.unit, time=signals.TimeBase.uniform(channel.rate or 1))
    digital = analog.to_digital(threshold, hysteresis)
    name = f"{channel.display_name} > {units.format_quantity(threshold, channel.unit, 3)}"
    return AnalyzerChannel(channel_number=number, channel_name=name, samples=digital.values)
