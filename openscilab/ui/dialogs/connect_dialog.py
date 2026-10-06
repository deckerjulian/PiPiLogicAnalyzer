# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""*Devices → Connect…*: every device that can be connected, in one list.

Detected boards first, then the ways to add one by hand (network, several boards), then the
simulators. A double-click or *Connect* chooses one; the shell connects it.
"""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QHBoxLayout, QListWidget, QListWidgetItem, QPushButton, QWidget

from .. import devices
from ..icons import icon, set_icon
from .common import accept_button, button_box, dialog_layout, hint

GROUPS = (("detected", "Detected"), ("manual", "Add by hand"), ("simulators", "Simulators"))


def connectable_entries(exclude_uris: frozenset = frozenset()) -> list[tuple[str, devices.DeviceEntry]]:
    """``(group, entry)`` of everything that can be connected now (open devices left out)."""
    result: list[tuple[str, devices.DeviceEntry]] = []
    for backend in devices.backends():
        try:
            detected = backend.detected()
        except Exception:  # noqa: BLE001 - one kind of device must not hide the others
            detected = []
        try:
            manual = backend.manual_entries()
        except Exception:  # noqa: BLE001
            manual = []
        # every simulator can be connected again: a second, third, ... one beside the first
        result += [("simulators", entry) for entry in detected + manual if entry.simulated]
        result += [("detected", entry) for entry in detected
                   if not entry.simulated and devices.entry_uri(entry) not in exclude_uris]
        result += [("manual", entry) for entry in manual if not entry.simulated]
    order = {key: index for index, (key, _title) in enumerate(GROUPS)}
    return sorted(result, key=lambda pair: order[pair[0]])


class ConnectDialog(QDialog):
    """Choose a device to connect (``entry`` after *Connect*)."""

    def __init__(self, parent: Optional[QWidget] = None, exclude_uris: frozenset = frozenset(),
                 entries: Optional[Callable[[], list]] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Connect a device")
        self.resize(460, 420)
        self.entry: Optional[devices.DeviceEntry] = None
        self._exclude = exclude_uris
        self._entries = entries or (lambda: connectable_entries(self._exclude))
        layout = dialog_layout(self)
        layout.addWidget(hint("Boards connected by USB appear by themselves; a simulator behaves like a device "
                              "and needs no hardware.", self))
        self.list = QListWidget(self)
        self.list.itemDoubleClicked.connect(lambda _item: self._accept())
        self.list.currentItemChanged.connect(lambda *_args: self._update())
        layout.addWidget(self.list, 1)
        buttons = button_box(self, "Connect", icon="plug")
        row = QHBoxLayout()
        self.refresh_button = QPushButton("Refresh", self)
        set_icon(self.refresh_button, "refresh")
        self.refresh_button.clicked.connect(self.refresh)
        row.addWidget(self.refresh_button)
        row.addStretch(1)
        row.addWidget(buttons)
        layout.addLayout(row)
        self.connect_button = accept_button(buttons)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        self.refresh()

    def refresh(self) -> None:
        self.list.clear()
        current_group = None
        titles = dict(GROUPS)
        for group, entry in self._entries():
            if group != current_group:
                current_group = group
                heading = QListWidgetItem(titles[group], self.list)
                heading.setFlags(Qt.NoItemFlags)
                font = heading.font()
                font.setBold(True)
                heading.setFont(font)
            item = QListWidgetItem(icon("chip" if group != "manual" else "plus"), entry.label, self.list)
            item.setData(Qt.UserRole, entry)
            item.setToolTip("Double-click to connect")
        if not any(group == "detected" for group, _entry in self._entries()):
            item = QListWidgetItem("No board detected. Plug one in and press Refresh.", self.list)
            item.setFlags(Qt.NoItemFlags)
        selectable = [self.list.item(row) for row in range(self.list.count())
                      if self.list.item(row).data(Qt.UserRole) is not None]
        if selectable:
            self.list.setCurrentItem(selectable[0])
        self._update()

    def _update(self) -> None:
        item = self.list.currentItem()
        self.connect_button.setEnabled(item is not None and item.data(Qt.UserRole) is not None)

    def _accept(self) -> None:
        item = self.list.currentItem()
        if item is None or item.data(Qt.UserRole) is None:
            return
        self.entry = item.data(Qt.UserRole)
        self.accept()
