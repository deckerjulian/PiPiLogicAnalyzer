# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""*Help → Keyboard shortcuts*: the shortcuts of the menus and the active documents, and the keys
and gestures that work inside the views; a search field narrows the list."""

from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import QDialog, QHeaderView, QLineEdit, QTableWidget, QTableWidgetItem, QWidget

from .common import button_box, dialog_layout

#: keys and gestures inside the views (they are no menu entries): (where, input, what it does)
VIEW_INPUT = (
    ("Flow graph", "Two fingers / pinch", "Move the canvas / zoom at the fingers"),
    ("Flow graph", "Mouse wheel / Ctrl+wheel / Shift+wheel", "Zoom / move sideways / move up and down"),
    ("Flow graph", "Space + drag, middle button", "Move the canvas"),
    ("Flow graph", "Tab, or type a name", "Add a node"),
    ("Flow graph", "Delete, Backspace", "Delete the selection"),
    ("Flow graph", "+ / -", "Zoom in / out"),
    ("Flow graph", "Drag a wire onto the canvas", "Add a node it fits"),
    ("Waveform", "Two fingers / pinch / double tap", "Move / zoom at the fingers / fit"),
    ("Waveform", "Mouse wheel / Ctrl+wheel / Alt+wheel", "Zoom / move / channel height"),
    ("Waveform", "A / B / M", "Cursor A / cursor B / marker at the pointer"),
    ("Waveform", "← → / Shift+← →", "Scroll 10 % / one sample"),
    ("Waveform", "Drag a channel name", "Move the channel"),
    ("Charts", "Wheel, pinch, drag / double-click", "Zoom and move / automatic range"),
    ("Panel editor", "Drag a widget / its corner", "Move it / resize it"),
    ("Panel editor", "Alt + arrows", "Move the selected widget"),
    ("Panel", "F11, Esc", "Full screen, leave it"),
    ("Device card", "F5", "Capture"),
    ("Device card", "Esc (Pins tab)", "All outputs safe"),
)


class ShortcutsDialog(QDialog):
    def __init__(self, rows: list[tuple[str, str]], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Keyboard shortcuts")
        self.resize(720, 560)
        layout = dialog_layout(self)
        self.search = QLineEdit(self)
        self.search.setPlaceholderText("Search shortcuts")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        layout.addWidget(self.search)
        entries = [(title, shortcut) for title, shortcut in rows] + [
            (f"{where} › {what}", keys) for where, keys, what in VIEW_INPUT]
        self.table = QTableWidget(len(entries), 2, self)
        self.table.setHorizontalHeaderLabels(["Command", "Keys"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        for row, (title, keys) in enumerate(entries):
            self.table.setItem(row, 0, QTableWidgetItem(title))
            self.table.setItem(row, 1, QTableWidgetItem(keys))
        layout.addWidget(self.table, 1)
        buttons = button_box(self, None)
        layout.addWidget(buttons)
        self.search.setFocus()

    def _filter(self, text: str) -> None:
        words = text.lower().split()
        for row in range(self.table.rowCount()):
            line = f"{self.table.item(row, 0).text()} {self.table.item(row, 1).text()}".lower()
            self.table.setRowHidden(row, not all(word in line for word in words))

    def visible_rows(self) -> list[tuple[str, str]]:
        return [(self.table.item(row, 0).text(), self.table.item(row, 1).text())
                for row in range(self.table.rowCount()) if not self.table.isRowHidden(row)]
