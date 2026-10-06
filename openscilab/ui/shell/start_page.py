# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Start page: what to do first (a simulator, a device, a new flow, a file), every project of the
library to start from - tiles by category, the first ones the plain starting points - and the
recent files and projects."""

from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ... import __version__
from ...core import recent
from ..documents.base import DocumentWidget
from ..icons import icon, set_icon
from ..theme import ACCENT, set_role, set_variant

#: the size of a tile of the library (pixels)
TILE_WIDTH = 236
TILE_HEIGHT = 112
TILE_SPACING = 10


#: lines of the description a tile shows (the tooltip has all of it)
LINES = 4


def clipped(text: str, metrics, width: int, lines: int) -> str:
    """``text`` wrapped at words into at most ``lines`` lines of ``width`` pixels, ``…`` where it is cut."""
    result: list[str] = []
    line = ""
    words = text.split()
    for index, word in enumerate(words):
        candidate = f"{line} {word}".strip()
        if metrics.horizontalAdvance(candidate) <= width:
            line = candidate
            continue
        result.append(line)
        line = word
        if len(result) == lines:
            last = result[-1]
            while last and metrics.horizontalAdvance(last + " …") > width:
                last = last.rsplit(" ", 1)[0] if " " in last else last[:-1]
            result[-1] = last + " …"
            return "\n".join(result)
    if line:
        result.append(line)
    return "\n".join(result[:lines])


def native(keys: str) -> str:
    """A shortcut as this platform writes it (⌘K on macOS, Ctrl+K elsewhere)."""
    return QKeySequence(keys).toString(QKeySequence.NativeText)


class Tile(QFrame):
    """A project of the library: its icon, title, what it does and the devices it uses; a click (or
    Enter) opens it as a new project."""

    activated = Signal(str)

    def __init__(self, example, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.key = example.key
        self.example = example
        set_role(self, "tile")
        self.setFixedSize(TILE_WIDTH, TILE_HEIGHT)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAccessibleName(example.title)
        self.setToolTip(example.description)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 8)
        layout.setSpacing(4)
        top = QHBoxLayout()
        top.setSpacing(8)
        picture = QLabel(self)
        picture.setPixmap(icon(example.icon or "nodes", ACCENT).pixmap(18, 18))
        top.addWidget(picture, 0, Qt.AlignTop)
        title = QLabel(example.title, self)
        title.setWordWrap(True)
        set_role(title, "tile-title")
        top.addWidget(title, 1)
        layout.addLayout(top)
        self.text = QLabel(self)
        self.text.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.text.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        set_role(self.text, "hint")
        layout.addWidget(self.text, 1)
        devices = sorted({str(address).split("#")[0].split("?")[0] for address in example.devices.values()})
        footer = " · ".join(devices[:3]) + (" …" if len(devices) > 3 else "")
        if example.realtime:
            footer = (footer + " · " if footer else "") + "real time"
        self.footer = QLabel(footer, self)
        set_role(self.footer, "tile-footer")
        layout.addWidget(self.footer)
        for child in self.findChildren(QLabel):
            child.setAttribute(Qt.WA_TransparentForMouseEvents)

    def showEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        self.text.setText(clipped(self.example.description, self.text.fontMetrics(), TILE_WIDTH - 24, LINES))

    def matches(self, words: list[str]) -> bool:
        text = " ".join((self.example.title, self.example.description, " ".join(map(str, self.example.devices.values())),
                         self.property("category") or "")).lower()
        return all(word in text for word in words)

    def click(self) -> None:
        self.activated.emit(self.key)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.click()
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.click()
            return
        super().keyPressEvent(event)


class Section(QWidget):
    """A category of the library: its title, what it holds and its tiles (as many in a row as fit)."""

    def __init__(self, category, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.category = category
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 6, 0, 10)
        layout.setSpacing(4)
        head = QHBoxLayout()
        head.setSpacing(8)
        picture = QLabel(self)
        picture.setPixmap(icon(category.icon or "book").pixmap(16, 16))
        head.addWidget(picture)
        title = QLabel(category.title, self)
        set_role(title, "heading")
        head.addWidget(title)
        self.count_label = QLabel(self)
        set_role(self.count_label, "hint")
        head.addWidget(self.count_label)
        head.addStretch(1)
        layout.addLayout(head)
        if category.description:
            text = QLabel(category.description, self)
            text.setWordWrap(True)
            set_role(text, "hint")
            layout.addWidget(text)
        self.grid = QGridLayout()
        self.grid.setSpacing(TILE_SPACING)
        self.grid.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        layout.addLayout(self.grid)
        self.tiles = [Tile(example, self) for example in category.examples]
        for tile in self.tiles:
            tile.setProperty("category", category.title)
        self.columns = 0

    def visible_tiles(self) -> list[Tile]:
        return [tile for tile in self.tiles if not tile.isHidden()]

    def reflow(self, width: int, force: bool = False) -> None:
        """As many tiles in a row as ``width`` holds."""
        columns = max(1, (width + TILE_SPACING) // (TILE_WIDTH + TILE_SPACING))
        if columns == self.columns and not force:
            return
        self.columns = columns
        for tile in self.tiles:
            self.grid.removeWidget(tile)
        for index, tile in enumerate(self.visible_tiles()):
            self.grid.addWidget(tile, index // columns, index % columns)
        shown = len(self.visible_tiles())
        self.count_label.setText(f"{shown}" if shown == len(self.tiles) else f"{shown} of {len(self.tiles)}")


class StartPage(DocumentWidget):
    document_kind = "start"

    #: open a project of the library (its key, ``00-start/02-logic-analyzer``) as a new project
    example_requested = Signal(str)
    file_requested = Signal(str)
    open_file_requested = Signal()
    project_requested = Signal(str)
    #: connect the simulator of this profile
    simulator_requested = Signal(str)
    #: choose a device to connect
    connect_requested = Signal()
    new_flow_requested = Signal()
    open_project_requested = Signal()

    def __init__(self, catalog: Optional[list] = None, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        if catalog is None:
            from ...lab import examples

            catalog = examples.catalog()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(32, 22, 32, 18)
        outer.setSpacing(12)

        # ------------------------------------------------------------- top
        top = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(2)
        title = QLabel("openSciLab", self)
        set_role(title, "title")
        titles.addWidget(title)
        subtitle = QLabel(f"Open measurement and control lab · {__version__}", self)
        set_role(subtitle, "hint")
        titles.addWidget(subtitle)
        top.addLayout(titles)
        top.addStretch(1)
        self.filter_edit = QLineEdit(self)
        self.filter_edit.setPlaceholderText("Search projects and examples")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.setMinimumWidth(260)
        self.filter_edit.textChanged.connect(self.apply_filter)
        top.addWidget(self.filter_edit, 0, Qt.AlignBottom)
        outer.addLayout(top)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.simulator_button = QPushButton("Try a simulator", self)
        set_icon(self.simulator_button, "play")
        set_variant(self.simulator_button, "primary")
        self.simulator_button.setToolTip("An Arduino, a Pico, a DAQ or an oscilloscope in software: capture, switch "
                                         "pins and run flows without hardware")
        self.simulator_menu = QMenu(self.simulator_button)
        self.simulator_button.setMenu(self.simulator_menu)
        self.simulator_menu.aboutToShow.connect(self._fill_simulators)
        self._fill_simulators()
        actions.addWidget(self.simulator_button)
        self.connect_button = QPushButton("Connect a device", self)
        set_icon(self.connect_button, "plug")
        self.connect_button.setToolTip("A board on USB or in the network; its device card opens")
        self.connect_button.clicked.connect(self.connect_requested.emit)
        actions.addWidget(self.connect_button)
        self.new_flow_button = QPushButton("New flow", self)
        set_icon(self.new_flow_button, "nodes")
        self.new_flow_button.clicked.connect(self.new_flow_requested.emit)
        actions.addWidget(self.new_flow_button)
        self.open_button = QPushButton("Open file...", self)
        set_icon(self.open_button, "folder")
        self.open_button.setToolTip("Open a capture (.lac, .sr) or another document")
        self.open_button.clicked.connect(self.open_file_requested.emit)
        actions.addWidget(self.open_button)
        self.open_project_button = QPushButton("Open project...", self)
        set_icon(self.open_project_button, "folder")
        self.open_project_button.clicked.connect(self.open_project_requested.emit)
        actions.addWidget(self.open_project_button)
        actions.addStretch(1)
        outer.addLayout(actions)

        # ---------------------------------------- the library, by category
        body = QHBoxLayout()
        body.setSpacing(24)
        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        content = QWidget(self.scroll)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 8, 0)
        content_layout.setSpacing(6)
        self.sections = [Section(category, content) for category in catalog]
        self.tiles: dict[str, Tile] = {}
        for section in self.sections:
            content_layout.addWidget(section)
            for tile in section.tiles:
                tile.activated.connect(self.example_requested.emit)
                self.tiles[tile.key] = tile
        self.no_match = QLabel("Nothing matches the search.", content)
        set_role(self.no_match, "hint")
        self.no_match.setVisible(False)
        content_layout.addWidget(self.no_match)
        if not catalog:
            missing = QLabel("The example library was not found (examples/library).", content)
            set_role(missing, "hint")
            content_layout.addWidget(missing)
        content_layout.addStretch(1)
        self.scroll.setWidget(content)
        body.addWidget(self.scroll, 1)

        # ------------------------------------------------------------ recent
        right = QVBoxLayout()
        right.setSpacing(8)
        recent_heading = QLabel("Recent", self)
        set_role(recent_heading, "heading")
        right.addWidget(recent_heading)
        self.recent_list = QListWidget(self)
        self.recent_list.setFrameShape(QFrame.NoFrame)
        self.recent_list.setStyleSheet("QListWidget { background: transparent; }")
        self.recent_list.itemActivated.connect(self._open_item)
        right.addWidget(self.recent_list, 1)
        self.empty_hint = QLabel("Files and projects you open appear here.", self)
        self.empty_hint.setWordWrap(True)
        set_role(self.empty_hint, "hint")
        right.addWidget(self.empty_hint)
        right.addStretch(0)
        self.recent_holder = QWidget(self)
        self.recent_holder.setLayout(right)
        self.recent_holder.setFixedWidth(220)
        body.addWidget(self.recent_holder)
        outer.addLayout(body, 1)

        shortcuts = QLabel("  ·  ".join(f"{native(keys)} {what}" for keys, what in (
            ("Ctrl+K", "command palette"), ("Ctrl+O", "open"), ("Ctrl+W", "close"), ("Ctrl+B", "sidebar"))), self)
        set_role(shortcuts, "hint")
        outer.addWidget(shortcuts)
        self.refresh()
        self.reflow()

    # ------------------------------------------------------------ library
    def reflow(self, force: bool = False) -> None:
        width = self.scroll.viewport().width() - 8
        for section in self.sections:
            section.reflow(width, force)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        # a narrow page gives the room of the recent list to the tiles
        self.recent_holder.setVisible(self.width() >= 3 * TILE_WIDTH + 340)
        self.reflow()

    def apply_filter(self, text: str = "") -> int:
        """Show the tiles whose title, description or devices hold every word of ``text``; returns how
        many are shown."""
        words = text.lower().split()
        shown = 0
        for section in self.sections:
            for tile in section.tiles:
                tile.setVisible(tile.matches(words))
            visible = len(section.visible_tiles())
            section.setVisible(visible > 0)
            shown += visible
        self.reflow(force=True)
        self.no_match.setVisible(shown == 0 and bool(words))
        return shown

    def _fill_simulators(self) -> None:
        from ...driver.simulated.profiles import available_profiles, load_profile

        self.simulator_menu.clear()
        for name in available_profiles():
            try:
                title = load_profile(name).get("title", name)
            except (OSError, ValueError, KeyError):
                continue
            self.simulator_menu.addAction(title, lambda name=name: self.simulator_requested.emit(name))

    @property
    def title(self) -> str:
        return "Start"

    def refresh(self) -> None:
        self.recent_list.clear()
        for kind, entries in (("projects", recent.entries("projects")), ("files", recent.entries("files"))):
            for path in entries:
                name = os.path.basename(path.rstrip(os.sep)) or path
                item = QListWidgetItem(icon("folder" if kind == "projects" else "file-plus"), name, self.recent_list)
                item.setToolTip(path)
                item.setData(Qt.UserRole, (kind, path))
        self.empty_hint.setVisible(self.recent_list.count() == 0)
        self.recent_list.setVisible(self.recent_list.count() > 0)

    def _open_item(self, item: QListWidgetItem) -> None:
        kind, path = item.data(Qt.UserRole)
        if kind == "projects":
            self.project_requested.emit(path)
        else:
            self.file_requested.emit(path)

    def showEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self.refresh()
        super().showEvent(event)
        self.reflow()
