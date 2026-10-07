# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The connected devices in the device list of the sidebar: a row each with its status and what it
does, how it is connected, the settings of its next capture and how much of the device they use
(a bar; see ``core/device_summary``)."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFontMetrics, QPainter
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from ...core.device_summary import DeviceSummary
from ...core.instrument import Instrument
from ..documents.device import status_icon
from ..theme import BORDER, TEXT_MUTED, WARNING, set_role, token

#: the colour of the state of each level (``DeviceSummary.level``); ``None``: that of the device status
LEVEL_TOKENS = {"armed": None, "busy": "device.busy", "error": "device.error", "off": "device.disconnected"}
#: from this load of its link on, a stream may overflow: the bar warns (a capture that fills the memory
#: of the device is as it should be)
HIGH_LOAD = 0.9


class ElidedLabel(QLabel):
    """A line that ends with … where it is too long for its room; the whole of it in its tooltip."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._full = ""
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

    def set_full_text(self, text: str) -> None:
        self._full = text
        self.setToolTip(text)
        self._elide()

    def full_text(self) -> str:
        return self._full

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self._elide()

    def _elide(self) -> None:
        self.setText(QFontMetrics(self.font()).elidedText(self._full, Qt.ElideRight, max(self.width(), 20)))


class LoadBar(QWidget):
    """A thin bar: how much of the device the settings use (or how much of a capture arrived)."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(4)
        self.value: Optional[float] = None
        self.color = QColor(TEXT_MUTED)

    def set_value(self, value: Optional[float], color: QColor) -> None:
        self.value, self.color = value, color
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(BORDER))
        painter.drawRoundedRect(QRectF(self.rect()), 2, 2)
        if self.value:
            painter.setBrush(self.color)
            painter.drawRoundedRect(QRectF(0, 0, max(self.width() * min(self.value, 1.0), 3), self.height()), 2, 2)


class DeviceRow(QWidget):
    """A connected device: its status dot, name and state; how it is connected; the settings of its
    next capture; a bar with how much of the device they use. The list under it takes the clicks."""

    def __init__(self, instrument: Instrument, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.instrument = instrument
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.summary: Optional[DeviceSummary] = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 5, 8, 6)
        layout.setSpacing(2)
        top = QHBoxLayout()
        top.setSpacing(6)
        self.dot = QLabel(self)
        top.addWidget(self.dot)
        self.name_label = ElidedLabel(self)
        self.name_label.set_full_text(instrument.name)
        top.addWidget(self.name_label, 1)
        self.state_label = QLabel(self)
        self.state_label.setObjectName("device-state")
        top.addWidget(self.state_label)
        layout.addLayout(top)
        self.connection_label = ElidedLabel(self)
        set_role(self.connection_label, "hint")
        layout.addWidget(self.connection_label)
        self.settings_label = ElidedLabel(self)
        set_role(self.settings_label, "hint")
        layout.addWidget(self.settings_label)
        bottom = QHBoxLayout()
        bottom.setSpacing(6)
        self.bar = LoadBar(self)
        bottom.addWidget(self.bar, 1)
        self.load_label = QLabel(self)
        set_role(self.load_label, "hint")
        bottom.addWidget(self.load_label)
        layout.addLayout(bottom)

    def show_summary(self, summary: DeviceSummary) -> None:
        self.summary = summary
        self.dot.setPixmap(status_icon(self.instrument.status).pixmap(14, 14))
        level_token = LEVEL_TOKENS.get(summary.level)
        color = QColor(WARNING) if summary.level == "armed" else \
            QColor(token(level_token or self.instrument.status.token))
        self.state_label.setText(summary.state)
        self.state_label.setStyleSheet(f"color: {color.name()};")
        self.connection_label.set_full_text(summary.connection)
        self.settings_label.set_full_text(summary.settings)
        self.settings_label.setVisible(bool(summary.settings))
        bar_color = QColor(token("device.busy")) if summary.level == "busy" else \
            QColor(WARNING) if summary.load_kind == "link" and (summary.load or 0) >= HIGH_LOAD else \
            QColor(token(self.instrument.status.token))
        self.bar.set_value(summary.load, bar_color)
        self.bar.setVisible(summary.load is not None)
        self.load_label.setText(summary.load_line)
        self.load_label.setVisible(bool(summary.load_line))
