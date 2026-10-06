# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Defines a bus: its channels in bit order, how values are shown, and a symbol table."""

from __future__ import annotations

import copy
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...driver.models import AnalyzerChannel, BusDefinition, BusFormat
from ..icons import set_icon
from .common import InlineMessage, button_box, dialog_layout, heading, hint

FORMAT_LABELS = {
    BusFormat.HEX: "Hexadecimal",
    BusFormat.DECIMAL: "Decimal",
    BusFormat.SIGNED: "Signed decimal",
    BusFormat.BINARY: "Binary",
    BusFormat.ASCII: "ASCII",
}
SYMBOL_FILE_FILTER = "Symbol tables (*.sym *.txt *.csv *.lbl *.map);;All files (*)"


class BusDialog(QDialog):
    """Edits a copy of ``bus``; :attr:`bus` holds the result after *OK*."""

    def __init__(
        self, channels: list[AnalyzerChannel], bus: Optional[BusDefinition] = None, parent: Optional[QWidget] = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Bus" if bus is None else f"Bus {bus.name}")
        self.resize(560, 620)
        self.bus = copy.deepcopy(bus) if bus is not None else BusDefinition(name="Bus")
        layout = dialog_layout(self)

        form = QFormLayout()
        self.name_edit = QLineEdit(self.bus.name, self)
        form.addRow("Name", self.name_edit)
        self.format_combo = QComboBox(self)
        for bus_format, label in FORMAT_LABELS.items():
            self.format_combo.addItem(label, bus_format)
        self.format_combo.setCurrentIndex(max(self.format_combo.findData(self.bus.format), 0))
        form.addRow("Show values as", self.format_combo)
        layout.addLayout(form)

        layout.addWidget(heading("Channels", self))
        layout.addWidget(hint("Tick the channels of the bus; the topmost is bit 0 (least significant).", self))
        row = QHBoxLayout()
        self.channel_list = QListWidget(self)
        self.channel_list.setDragDropMode(QListWidget.InternalMove)
        chosen = list(self.bus.channels)
        by_number = {channel.channel_number: channel for channel in channels}
        ordered = [by_number[number] for number in chosen if number in by_number] + [
            channel for channel in channels if channel.channel_number not in chosen
        ]
        for channel in ordered:
            item = QListWidgetItem(channel.display_name, self.channel_list)
            item.setData(Qt.UserRole, channel.channel_number)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if channel.channel_number in chosen else Qt.Unchecked)
        row.addWidget(self.channel_list, 1)
        buttons = QVBoxLayout()
        for icon_name, tip, step in (("up", "Lower bit", -1), ("down", "Higher bit", 1)):
            button = QPushButton(self)
            set_icon(button, icon_name)
            button.setToolTip(tip)
            button.clicked.connect(lambda _checked=False, step=step: self._move(step))
            buttons.addWidget(button)
        reverse = QPushButton("Reverse", self)
        reverse.setToolTip("Swap the bit order of the ticked channels (MSB first ↔ LSB first)")
        reverse.clicked.connect(self._reverse)
        buttons.addWidget(reverse)
        buttons.addStretch(1)
        row.addLayout(buttons)
        layout.addLayout(row, 1)

        layout.addWidget(heading("Symbols", self))
        layout.addWidget(
            hint("Names shown instead of values, one per line: NAME = 0x1F, 0x1F NAME, $D020 NAME or CSV.", self)
        )
        from ...core.buses import symbol_table_text

        self.symbols_edit = QPlainTextEdit(symbol_table_text(self.bus.symbols), self)
        self.symbols_edit.setPlaceholderText("IDLE = 0x00\nSTART = 0x01")
        layout.addWidget(self.symbols_edit, 1)
        load = QPushButton("Load symbol file...", self)
        set_icon(load, "import")
        load.clicked.connect(self._load_symbols)
        layout.addWidget(load, 0, Qt.AlignLeft)

        self.message = InlineMessage(self)
        layout.addWidget(self.message)
        box = button_box(self, "OK")
        box.accepted.connect(self._accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)

    def _checked_rows(self) -> list[int]:
        return [row for row in range(self.channel_list.count()) if self.channel_list.item(row).checkState() == Qt.Checked]

    def _move(self, step: int) -> None:
        row = self.channel_list.currentRow()
        target = row + step
        if row < 0 or not 0 <= target < self.channel_list.count():
            return
        item = self.channel_list.takeItem(row)
        self.channel_list.insertItem(target, item)
        self.channel_list.setCurrentRow(target)

    def _reverse(self) -> None:
        rows = self._checked_rows()
        items = [self.channel_list.item(row) for row in rows]
        states = [(item.text(), item.data(Qt.UserRole)) for item in items]
        for item, (text, number) in zip(items, reversed(states)):
            item.setText(text)
            item.setData(Qt.UserRole, number)

    def _load_symbols(self) -> None:
        from ...core.buses import load_symbol_file, symbol_table_text

        path, _ = QFileDialog.getOpenFileName(self, "Symbol table", "", SYMBOL_FILE_FILTER)
        if not path:
            return
        try:
            self.symbols_edit.setPlainText(symbol_table_text(load_symbol_file(path)))
        except (OSError, ValueError) as error:
            self.message.show_error(str(error))

    def _accept(self) -> None:
        from ...core.buses import parse_symbol_table

        channels = [self.channel_list.item(row).data(Qt.UserRole) for row in self._checked_rows()]
        if not channels:
            self.message.show_error("Tick at least one channel.")
            return
        if len(channels) > 64:
            self.message.show_error("A bus can have at most 64 channels.")
            return
        try:
            symbols = parse_symbol_table(self.symbols_edit.toPlainText())
        except ValueError as error:
            self.message.show_error(f"Symbols: {error}")
            return
        self.bus.name = self.name_edit.text().strip() or "Bus"
        self.bus.channels = channels
        self.bus.format = self.format_combo.currentData()
        self.bus.symbols = symbols
        self.accept()
