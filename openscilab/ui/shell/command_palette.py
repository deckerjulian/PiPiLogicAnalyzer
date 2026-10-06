# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Command palette (Ctrl+K): every action of the shell and the active document, fuzzy searched,
the recently used ones first."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QVBoxLayout,
    QWidget,
)

from ...core import fuzzy, settings

HISTORY_FILE = "command-history.json"
HISTORY_LENGTH = 30


@dataclass
class Command:
    """One entry of the palette."""

    #: e.g. "Capture › Start"
    title: str
    callback: Callable[[], None]
    shortcut: str = ""
    #: Stable key for the history (the title by default).
    key: str = ""
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.key:
            self.key = self.title


def _clean(text: str) -> str:
    return text.replace("&&", "\0").replace("&", "").replace("\0", "&").rstrip(".").replace("...", "").strip()


def commands_from_actions(actions: Iterable[QAction], prefix: str = "") -> list[Command]:
    """Commands of ``actions``, descending into sub menus (``prefix`` names the menu)."""
    result: list[Command] = []
    for action in actions:
        if action.isSeparator() or not action.text():
            continue
        name = _clean(action.text())
        title = f"{prefix} › {name}" if prefix else name
        menu: Optional[QMenu] = action.menu()
        if menu is not None:
            # Menus built on demand (e.g. the profiles) fill themselves when shown.
            menu.aboutToShow.emit()
            result += commands_from_actions(menu.actions(), title)
            continue
        if not action.isVisible():
            continue
        shortcut = action.shortcut().toString(QKeySequence.NativeText)
        if not shortcut and isinstance(action.data(), str):
            shortcut = action.data()  # a key that works in one place only (e.g. "Esc (Pins tab)")
        result.append(Command(title, action.trigger, shortcut, enabled=action.isEnabled()))
    return result


def commands_from_menus(menus: Iterable[QMenu]) -> list[Command]:
    result: list[Command] = []
    for menu in menus:
        menu.aboutToShow.emit()
        result += commands_from_actions(menu.actions(), _clean(menu.title()))
    return result


def unique(commands: Iterable[Command]) -> list[Command]:
    seen: set[str] = set()
    result = []
    for command in commands:
        if command.key in seen:
            continue
        seen.add(command.key)
        result.append(command)
    return result


def history() -> list[str]:
    data = settings.get_settings(HISTORY_FILE)
    return [entry for entry in data if isinstance(entry, str)] if isinstance(data, list) else []


def remember(command: Command) -> None:
    entries = [command.key] + [entry for entry in history() if entry != command.key]
    settings.persist_settings(HISTORY_FILE, entries[:HISTORY_LENGTH])


def search(commands: list[Command], query: str) -> list[Command]:
    """Matching commands, best first; recently used ones first for an empty query."""
    return fuzzy.rank(query, commands, key=lambda command: command.title, boost=history())


class CommandPalette(QDialog):
    """A frameless popup near the top of the window: a search field and the matching commands."""

    def __init__(self, commands: list[Command], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent, Qt.Popup | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_DeleteOnClose, False)
        self.commands = commands
        self.executed: Optional[Command] = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        frame = QFrame(self)
        frame.setObjectName("command-palette")
        outer.addWidget(frame)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(8, 8, 8, 8)
        self.input = QLineEdit(frame)
        self.input.setObjectName("palette-input")
        self.input.setPlaceholderText("Type a command")
        self.input.textChanged.connect(self.refresh)
        self.input.installEventFilter(self)
        layout.addWidget(self.input)
        self.results = QListWidget(frame)
        self.results.setFrameShape(QFrame.NoFrame)
        self.results.itemActivated.connect(self._activate_item)
        self.results.itemClicked.connect(self._activate_item)
        layout.addWidget(self.results)
        self.resize(560, 380)
        self.refresh()

    def refresh(self) -> None:
        self.results.clear()
        for command in search(self.commands, self.input.text())[:200]:
            text = command.title if not command.shortcut else f"{command.title}\t{command.shortcut}"
            item = QListWidgetItem(text, self.results)
            item.setData(Qt.UserRole, command)
            if not command.enabled:
                item.setFlags(item.flags() & ~Qt.ItemIsEnabled)
        for row in range(self.results.count()):
            if self.results.item(row).flags() & Qt.ItemIsEnabled:
                self.results.setCurrentRow(row)
                break

    def visible_commands(self) -> list[Command]:
        return [self.results.item(row).data(Qt.UserRole) for row in range(self.results.count())]

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt naming
        if watched is self.input and event.type() == QEvent.KeyPress:
            key = event.key()
            if key in (Qt.Key_Down, Qt.Key_Up):
                step = 1 if key == Qt.Key_Down else -1
                row = self.results.currentRow() + step
                if 0 <= row < self.results.count():
                    self.results.setCurrentRow(row)
                return True
            if key in (Qt.Key_Return, Qt.Key_Enter):
                self.execute_current()
                return True
            if key == Qt.Key_Escape:
                self.reject()
                return True
        return super().eventFilter(watched, event)

    def _activate_item(self, item: QListWidgetItem) -> None:
        self.results.setCurrentItem(item)
        self.execute_current()

    def execute_current(self) -> Optional[Command]:
        item = self.results.currentItem()
        if item is None or not item.flags() & Qt.ItemIsEnabled:
            return None
        command: Command = item.data(Qt.UserRole)
        self.executed = command
        self.accept()
        remember(command)
        command.callback()
        return command

    def popup(self) -> None:
        """Show below the top edge of the parent window, centred."""
        parent = self.parentWidget()
        if parent is not None:
            window = parent.window()
            top_left = window.mapToGlobal(window.rect().topLeft())
            x = top_left.x() + (window.width() - self.width()) // 2
            self.move(x, top_left.y() + 60)
        self.show()
        self.input.setFocus()
