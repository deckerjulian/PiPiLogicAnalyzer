# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of PiPiLogicAnalyzer, a port and extension of his software;
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

import html
from typing import Optional

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QColorDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ...core import colors
from ...driver.models import AnalyzerChannel
from ..icons import icon
from ..theme import ACCENT_HOVER, TEXT, TEXT_DISABLED, TEXT_MUTED
from ..view_model import CHANNEL_HEIGHT_STEP, DEFAULT_CHANNEL_HEIGHT, CaptureViewModel

CHANNEL_COLUMN_WIDTH = 150
#: Rows lower than this show only the channel number or name, in a smaller font.
COMPACT_ROW_HEIGHT = 28
COLOR_STRIP_WIDTH = 4

_FLAT_BUTTON = "background: transparent; border: none; padding: 0; min-height: 0;"


class ChannelRow(QWidget):
    """One channel in a single line: colour strip, visibility, number and name, pin.

    Double-click the name to rename the channel (or use the context menu), click the colour
    strip to change its colour.
    """

    visibility_toggled = Signal(object)
    pin_toggled = Signal(object)
    color_changed = Signal(object)
    name_changed = Signal(object)

    def __init__(self, channel: AnalyzerChannel, background: QColor, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.channel = channel
        self.pinned = False
        self.compact = False
        self._background = background
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_menu)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(COLOR_STRIP_WIDTH + 4, 0, 4, 1)
        layout.setSpacing(4)

        self.visibility_button = QToolButton(self)
        self.visibility_button.setAutoRaise(True)
        self.visibility_button.setCursor(Qt.PointingHandCursor)
        self.visibility_button.setToolTip("Show/hide this channel")
        self.visibility_button.setFixedWidth(18)
        self.visibility_button.setStyleSheet(_FLAT_BUTTON)
        self.visibility_button.clicked.connect(lambda: self.visibility_toggled.emit(self.channel))
        layout.addWidget(self.visibility_button)

        self.label = QLabel(self)
        self.label.setToolTip("Double-click to rename, right-click for more")
        self.label.setTextFormat(Qt.RichText)
        self.label.mouseDoubleClickEvent = lambda _event: self.start_rename()  # type: ignore[assignment]
        layout.addWidget(self.label, 1)

        self.name_edit = QLineEdit(channel.channel_name, self)
        self.name_edit.setPlaceholderText("Name")
        self.name_edit.setVisible(False)
        self.name_edit.editingFinished.connect(self._finish_rename)
        layout.addWidget(self.name_edit, 1)

        self.pin_button = QToolButton(self)
        self.pin_button.setAutoRaise(True)
        self.pin_button.setCursor(Qt.PointingHandCursor)
        self.pin_button.setFixedWidth(18)
        self.pin_button.setStyleSheet(_FLAT_BUTTON)
        self.pin_button.clicked.connect(lambda: self.pin_toggled.emit(self.channel))
        layout.addWidget(self.pin_button)

        # The row height follows the channel height: no child may keep a row taller than its
        # channel in the waveform.
        for widget in (self.visibility_button, self.pin_button, self.label, self.name_edit):
            widget.setSizePolicy(widget.sizePolicy().horizontalPolicy(), QSizePolicy.Ignored)

        self.set_row_height(DEFAULT_CHANNEL_HEIGHT)

    # ------------------------------------------------------------- appearance
    def set_row_height(self, height: int) -> None:
        self.setMinimumHeight(height)
        self.compact = height < COMPACT_ROW_HEIGHT
        self.refresh()

    def set_background(self, background: QColor) -> None:
        self._background = background
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.fillRect(self.rect(), self._background)
        color = colors.get_channel_color(self.channel)
        if self.channel.hidden:
            color = QColor(color)
            color.setAlpha(80)
        painter.fillRect(QRect(0, 0, COLOR_STRIP_WIDTH, self.height()), color)
        # The same separator as between the rows of the waveform
        painter.setPen(colors.ROW_SEPARATOR_COLOR)
        painter.drawLine(0, self.height() - 1, self.width(), self.height() - 1)

    def refresh(self, pinned: Optional[bool] = None) -> None:
        if pinned is not None:
            self.pinned = pinned
        color = colors.get_channel_color(self.channel)
        number = f"CH{self.channel.channel_number + 1}"
        name = html.escape(self.channel.channel_name.strip())
        size = "11px" if self.compact else "12px"
        muted = TEXT_DISABLED if self.channel.hidden else TEXT_MUTED
        text_color = TEXT_DISABLED if self.channel.hidden else TEXT
        number_color = TEXT_DISABLED if self.channel.hidden else color.name()
        if name:
            self.label.setText(
                f"<span style='font-size:{size}; color:{text_color}; font-weight:600'>{name}</span>"
                f"&nbsp;<span style='font-size:10px; color:{muted}'>{number}</span>"
            )
        else:
            self.label.setText(
                f"<span style='font-size:{size}; color:{number_color}; font-weight:600'>{number}</span>"
            )
        self.visibility_button.setIcon(icon("eye", TEXT_MUTED if not self.channel.hidden else TEXT_DISABLED))
        self.visibility_button.setToolTip("Hide this channel" if not self.channel.hidden else "Show this channel")
        self.pin_button.setIcon(icon("pin", ACCENT_HOVER if self.pinned else TEXT_DISABLED))
        self.pin_button.setToolTip(
            "Unpin: scroll this channel with the others"
            if self.pinned
            else "Pin: keep this channel at the top while scrolling"
        )
        self.update()

    # ---------------------------------------------------------------- editing
    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.LeftButton and event.position().x() <= COLOR_STRIP_WIDTH + 3:
            self.choose_color()
            return
        super().mousePressEvent(event)

    def start_rename(self) -> None:
        self.label.setVisible(False)
        self.name_edit.setText(self.channel.channel_name)
        self.name_edit.setVisible(True)
        self.name_edit.setFocus()
        self.name_edit.selectAll()

    def _finish_rename(self) -> None:
        if not self.name_edit.isVisible():
            return
        self.name_edit.setVisible(False)
        self.label.setVisible(True)
        text = self.name_edit.text().strip()
        if text != self.channel.channel_name:
            self.channel.channel_name = text
            self.refresh()
            self.name_changed.emit(self.channel)

    def choose_color(self) -> None:
        current = colors.get_channel_color(self.channel)
        color = QColorDialog.getColor(current, self, "Channel colour")
        if color.isValid():
            self.channel.channel_color = colors.color_to_uint(color)
            self.refresh()
            self.color_changed.emit(self.channel)

    def _show_menu(self, position) -> None:
        menu = QMenu(self)
        menu.addAction("Rename...", self.start_rename)
        menu.addAction("Colour...", self.choose_color)
        menu.addSeparator()
        menu.addAction("Show" if self.channel.hidden else "Hide", lambda: self.visibility_toggled.emit(self.channel))
        menu.addAction("Unpin" if self.pinned else "Pin to the top", lambda: self.pin_toggled.emit(self.channel))
        menu.exec(self.mapToGlobal(position))


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
