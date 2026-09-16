# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of PiPiLogicAnalyzer 7, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Channel header column (port of ``Controls/ChannelViewer.axaml.cs``).

Every row carries a visibility toggle, a pin, the channel label (click to change
its colour) and an editable name; rows lower than ``COMPACT_ROW_HEIGHT`` show the name in the
label instead of the name field.  Unlike the original, the visibility button
toggles: there the eye only ever *hid* a channel and the only way to get it back
was the "show all" button in the header.  Pinned channels are shown by a second
column above the scrolling one, so they stay in view while scrolling vertically.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QColorDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ...core import colors
from ...driver.models import AnalyzerChannel
from ..icons import icon
from ..theme import ACCENT_HOVER, TEXT_DISABLED
from ..view_model import CHANNEL_HEIGHT_STEP, DEFAULT_CHANNEL_HEIGHT, CaptureViewModel

CHANNEL_COLUMN_WIDTH = 150
#: Rows lower than this hide the name field.
COMPACT_ROW_HEIGHT = 40

_FLAT_BUTTON = "background: transparent; border: none; padding: 0; min-height: 0;"


class ChannelRow(QWidget):
    """One channel: visibility, pin, colour and name."""

    visibility_toggled = Signal(object)
    pin_toggled = Signal(object)
    color_changed = Signal(object)
    name_changed = Signal(object)

    def __init__(self, channel: AnalyzerChannel, background: QColor, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.channel = channel
        self.pinned = False
        self.compact = False
        self.setAutoFillBackground(True)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(2)
        self._layout = layout

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(4)

        self.visibility_button = QToolButton(self)
        self.visibility_button.setAutoRaise(True)
        self.visibility_button.setCursor(Qt.PointingHandCursor)
        self.visibility_button.setToolTip("Show/hide this channel")
        self.visibility_button.setFixedWidth(22)
        self.visibility_button.clicked.connect(lambda: self.visibility_toggled.emit(self.channel))
        header.addWidget(self.visibility_button)

        self.label = QLabel(channel.textual_channel_number, self)
        self.label.setCursor(Qt.PointingHandCursor)
        self.label.setToolTip("Click to change the colour of this channel")
        self.label.mousePressEvent = self._label_clicked  # type: ignore[assignment]
        header.addWidget(self.label, 1)

        self.pin_button = QToolButton(self)
        self.pin_button.setAutoRaise(True)
        self.pin_button.setCursor(Qt.PointingHandCursor)
        self.pin_button.setFixedWidth(20)
        self.pin_button.setStyleSheet(_FLAT_BUTTON)
        self.pin_button.clicked.connect(lambda: self.pin_toggled.emit(self.channel))
        header.addWidget(self.pin_button)
        layout.addLayout(header)

        self.name_edit = QLineEdit(channel.channel_name, self)
        self.name_edit.setPlaceholderText("name")
        self.name_edit.setAlignment(Qt.AlignCenter)
        self.name_edit.setMaximumHeight(20)
        self.name_edit.textChanged.connect(self._on_name_changed)
        layout.addWidget(self.name_edit)

        # The row height follows the channel height: the buttons and the label must not keep a
        # row taller than its channel in the waveform.
        for widget in (self.visibility_button, self.pin_button):
            widget.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Ignored)
        self.label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Ignored)

        self.set_background(background)
        self.set_row_height(DEFAULT_CHANNEL_HEIGHT)

    def set_row_height(self, height: int) -> None:
        self.setMinimumHeight(height)
        compact = height < COMPACT_ROW_HEIGHT
        self.compact = compact
        self.name_edit.setVisible(not compact)
        margin = 0 if compact else 2
        self._layout.setContentsMargins(4, margin, 4, margin)
        self.refresh()

    def _label_clicked(self, event) -> None:
        current = colors.get_channel_color(self.channel)
        color = QColorDialog.getColor(current, self, "Channel colour")
        if color.isValid():
            self.channel.channel_color = colors.color_to_uint(color)
            self.refresh()
            self.color_changed.emit(self.channel)

    def _on_name_changed(self, text: str) -> None:
        self.channel.channel_name = text
        self.refresh()
        self.name_changed.emit(self.channel)

    def set_background(self, background: QColor) -> None:
        palette = self.palette()
        palette.setColor(self.backgroundRole(), background)
        self.setPalette(palette)
        self.name_edit.setStyleSheet(
            f"background-color: {background.name()}; border: 1px solid #4a4a4a; font-size: 10px;"
        )

    def refresh(self, pinned: Optional[bool] = None) -> None:
        if pinned is not None:
            self.pinned = pinned
        color = colors.get_channel_color(self.channel)
        name = self.channel.channel_name.strip()
        self.label.setText(name if self.compact and name else self.channel.textual_channel_number)
        self.label.setStyleSheet(f"color: {color.name()}; font-weight: bold;")
        self.visibility_button.setText("●" if not self.channel.hidden else "○")
        self.visibility_button.setStyleSheet(
            f"color: {'#dddddd' if not self.channel.hidden else '#777777'}; font-size: 13px; " + _FLAT_BUTTON
        )
        self.pin_button.setIcon(icon("pin", ACCENT_HOVER if self.pinned else TEXT_DISABLED))
        self.pin_button.setToolTip(
            "Unpin: scroll this channel with the others"
            if self.pinned
            else "Pin: keep this channel at the top while scrolling"
        )


class ChannelViewer(QWidget):
    """The channel column of one part of the waveform (``all``, ``pinned`` or ``scrolling``)."""

    channels_changed = Signal()

    def __init__(self, model: CaptureViewModel, parent: Optional[QWidget] = None, section: str = "all") -> None:
        super().__init__(parent)
        self.model = model
        self.section = section
        self.setFixedWidth(CHANNEL_COLUMN_WIDTH)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        self._rows: list[ChannelRow] = []

        model.capture_changed.connect(self.rebuild)
        model.channels_changed.connect(self.refresh)
        model.channel_height_changed.connect(self.refresh)

    def rebuild(self) -> None:
        for row in self._rows:
            row.setParent(None)
            row.deleteLater()
        self._rows.clear()

        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().setParent(None)

        channels = self.model.channels
        for index, channel in enumerate(channels):
            row = ChannelRow(channel, colors.BG_CHANNEL_COLORS[index % 2], self)
            row.visibility_toggled.connect(self._toggle_visibility)
            row.pin_toggled.connect(self._toggle_pin)
            row.color_changed.connect(lambda _channel: self._emit_changed())
            row.name_changed.connect(lambda _channel: self._emit_changed())
            self._layout.addWidget(row, 1)
            self._rows.append(row)

        self.refresh()

    def visible_rows(self) -> list[ChannelRow]:
        return [row for row in self._rows if not row.isHidden()]

    def refresh(self) -> None:
        shown = {id(channel) for channel in self.model.section_channels(self.section)}
        height = self.model.channel_height
        visible_index = 0
        for row in self._rows:
            visible = id(row.channel) in shown
            row.setVisible(visible)
            if visible:
                row.set_background(colors.BG_CHANNEL_COLORS[visible_index % 2])
                visible_index += 1
            row.pinned = self.model.is_pinned(row.channel)
            row.set_row_height(height)

        minimum = 0 if self.section == "pinned" else 1
        self.setMinimumHeight(max(visible_index, minimum) * height)

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt naming
        # Alt + wheel over the channel names changes the channel height, like over the waveform.
        if event.modifiers() & Qt.AltModifier:
            delta = event.angleDelta().y() or event.angleDelta().x()
            if delta:
                self.model.zoom_channels(CHANNEL_HEIGHT_STEP if delta > 0 else 1 / CHANNEL_HEIGHT_STEP)
                event.accept()
                return
        event.ignore()

    def _toggle_visibility(self, channel: AnalyzerChannel) -> None:
        channel.hidden = not channel.hidden
        self._emit_changed()

    def _toggle_pin(self, channel: AnalyzerChannel) -> None:
        self.model.set_pinned(channel, not self.model.is_pinned(channel))
        self.channels_changed.emit()

    def show_all_channels(self) -> None:
        for channel in self.model.channels:
            channel.hidden = False
        self._emit_changed()

    def _emit_changed(self) -> None:
        self.model.notify_channels_changed()
        self.channels_changed.emit()
