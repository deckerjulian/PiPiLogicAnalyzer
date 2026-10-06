# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The *Cache* tab of a device card: the captures a device keeps in its own cache (the bridge
app of an oscilloscope), with their size; delete them or limit the cache."""

from __future__ import annotations

import time
from typing import Optional

from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core.instrument import CacheFacet, InstrumentError
from .. import background
from ..dialogs.common import InlineMessage, hint


class CachePanel(QWidget):
    def __init__(self, cache: CacheFacet, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.cache = cache
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.addWidget(hint("Captures the device keeps in its own storage, newest first.", self))
        self.table = QTableWidget(0, 3, self)
        self.table.setHorizontalHeaderLabels(["Capture", "Taken", "Samples"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table, 1)
        buttons = QHBoxLayout()
        self.refresh_button = QPushButton("Refresh", self)
        self.delete_button = QPushButton("Delete", self)
        self.delete_all_button = QPushButton("Delete all", self)
        for button in (self.refresh_button, self.delete_button, self.delete_all_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        self.limit_box = QSpinBox(self)
        self.limit_box.setRange(1, 1_000_000)
        self.limit_box.setValue(1024)
        self.limit_box.setSuffix(" MB")
        self.limit_button = QPushButton("Set limit", self)
        buttons.addWidget(self.limit_box)
        buttons.addWidget(self.limit_button)
        layout.addLayout(buttons)
        self.message = InlineMessage(self)
        layout.addWidget(self.message)
        self.refresh_button.clicked.connect(self.refresh)
        self.delete_button.clicked.connect(self.delete_selected)
        self.delete_all_button.clicked.connect(lambda: self._run("Deleting...", lambda: self.cache.delete(None)))
        self.limit_button.clicked.connect(
            lambda: self._run("Setting the limit...", lambda: self.cache.set_limit(self.limit_box.value() << 20)))

    def _run(self, text: str, call) -> bool:
        self.message.clear()
        try:
            background.run(self, text, call)
        except background.Cancelled:
            return False
        except InstrumentError as error:
            self.message.show_error(str(error))
            return False
        self.refresh()
        return True

    def refresh(self) -> None:
        try:
            entries = background.run(self, "Reading the cache...", self.cache.entries)
        except background.Cancelled:
            return
        except InstrumentError as error:
            self.message.show_error(str(error))
            return
        self.table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            values = (entry.id, time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry.time)), f"{entry.points:,}")
            for column, value in enumerate(values):
                self.table.setItem(row, column, QTableWidgetItem(value))
        self.delete_button.setEnabled(bool(entries))
        self.delete_all_button.setEnabled(bool(entries))

    def delete_selected(self) -> None:
        rows = sorted({index.row() for index in self.table.selectionModel().selectedRows()})
        ids = [self.table.item(row, 0).text() for row in rows]
        if ids:
            self._run("Deleting...", lambda: [self.cache.delete(entry_id) for entry_id in ids])
