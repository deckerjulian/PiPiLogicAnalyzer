# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Sidebar: the section chosen in the activity bar (project, devices, nodes, search)."""

from __future__ import annotations

import os
from typing import Callable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ...core import recent
from ..icons import icon
from ..theme import set_role


class Sidebar(QStackedWidget):
    """Pages by key, each with a small title."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("sidebar")
        self.setMinimumWidth(200)
        self._pages: dict[str, QWidget] = {}
        self._contents: dict[str, QWidget] = {}

    def add_section(self, key: str, title: str, widget: QWidget) -> None:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        heading = QLabel(title.upper(), page)
        set_role(heading, "sidebar-title")
        layout.addWidget(heading)
        widget.setParent(page)
        layout.addWidget(widget, 1)
        self._pages[key] = page
        self._contents[key] = widget
        self.addWidget(page)

    def section(self, key: str) -> QWidget:
        return self._contents[key]

    def show_section(self, key: str) -> None:
        if key in self._pages:
            self.setCurrentWidget(self._pages[key])

    @property
    def current_key(self) -> Optional[str]:
        for key, page in self._pages.items():
            if page is self.currentWidget():
                return key
        return None


def _list(parent: QWidget) -> QListWidget:
    widget = QListWidget(parent)
    widget.setFrameShape(QFrame.NoFrame)
    return widget


#: kinds of files in the data folder of a project: (suffixes, kind, icon)
DATA_KINDS = (
    ((".lac", ".lac.gz", ".sr"), "capture", "channels"),
    ((".csv", ".tsv"), "table", "list"),
    ((".log", ".txt"), "log", "terminal"),
    ((".html", ".htm", ".pdf"), "report", "book"),
)


def data_kind(path: str) -> Optional[tuple[str, str]]:
    lowered = path.lower()
    for suffixes, kind, icon_name in DATA_KINDS:
        if lowered.endswith(suffixes):
            return kind, icon_name
    return None


def data_files(folder: str) -> list[tuple[str, str]]:
    """``(path, kind)`` of the captures, tables, logs and reports in ``folder`` (and below), newest first."""
    found = []
    for root, _dirs, files in os.walk(folder):
        for name in files:
            path = os.path.join(root, name)
            kind = data_kind(path)
            if kind is not None:
                found.append((path, kind[0]))
    def changed(item: tuple[str, str]) -> float:
        try:
            return os.path.getmtime(item[0])
        except OSError:  # deleted while the folder was read
            return 0.0

    found.sort(key=changed, reverse=True)
    return found


def file_icon(path: str) -> str:
    """The icon of a file by what it holds (flow, panel, capture, waveform, other)."""
    lowered = path.lower()
    for endings, name in (((".flow.yaml", ".py"), "nodes"), ((".panel.yaml",), "panel"),
                          ((".lac", ".lac.gz", ".sr", ".csv", ".vcd"), "channels"), ((".wave.yaml", ".sdl"), "wave")):
        if lowered.endswith(endings):
            return name
    return "file-plus"


class ProjectSection(QWidget):
    """Open documents, the data of the project (captures, tables, logs, reports) and recent files."""

    document_activated = Signal(object)
    file_requested = Signal(str)
    #: two captures to compare: (capture, reference)
    compare_requested = Signal(str, str)
    #: a capture to export as CSV
    export_requested = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        from .sections import Part, PartStack

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.parts = PartStack(self)
        layout.addWidget(self.parts, 1)

        self.documents = _list(self)
        self.documents.itemActivated.connect(lambda item: self.document_activated.emit(item.data(Qt.UserRole)))
        self.documents.itemClicked.connect(lambda item: self.document_activated.emit(item.data(Qt.UserRole)))
        self.documents_part = self.parts.add(Part("documents", "Open documents", self.documents), 2)

        # the flows, panels and waveforms of the project of the active document (also closed ones)
        self.files = _list(self)
        self.files.itemActivated.connect(lambda item: self.file_requested.emit(item.data(Qt.UserRole)))
        self.files_part = self.parts.add(Part("files", "Project", self.files), 2)
        self.project = None

        self.data = _list(self)
        self.data.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.data.itemActivated.connect(lambda item: self.file_requested.emit(item.data(Qt.UserRole)))
        self.data.setContextMenuPolicy(Qt.CustomContextMenu)
        self.data.customContextMenuRequested.connect(self._data_menu)
        self.data_part = self.parts.add(Part("data", "Data", self.data), 2)
        self.data_part.add_button("refresh", "Read the data folder again", lambda: self.refresh_data())
        self.data_folder: Optional[str] = None

        self.recent = _list(self)
        self.recent.itemActivated.connect(lambda item: self.file_requested.emit(item.data(Qt.UserRole)))
        self.recent_part = self.parts.add(Part("recent", "Recent files", self.recent), 1)
        self.set_project(None)
        self.set_data_folder(None)

    def set_project(self, project) -> None:
        """The files of ``project`` (``None``: no project, the list is hidden)."""
        self.project = project
        self.files.clear()
        visible = project is not None
        self.files_part.setVisible(visible)
        self.parts.refresh()
        if not visible:
            return
        self.files_part.set_title(f"Project · {project.name or os.path.basename(project.root.rstrip(os.sep))}")
        try:
            paths = project.flows() + project.panels() + project.waveforms()
        except OSError:
            paths = []
        for path in paths:
            item = QListWidgetItem(icon(file_icon(path)), os.path.relpath(path, project.root), self.files)
            item.setToolTip(path)
            item.setData(Qt.UserRole, path)
        self.files_part.set_count(len(paths))
        if not paths:
            item = QListWidgetItem("No flows or panels yet", self.files)
            item.setFlags(Qt.NoItemFlags)

    def set_documents(self, documents: list, active=None) -> None:
        self.documents.clear()
        self.documents_part.set_count(len(documents))
        for document in documents:
            text = document.title + (" ●" if document.dirty else "")
            kind = getattr(document, "document_kind", "")
            icon_name = {"flow": "nodes", "panel": "panel", "data": "channels", "device": "devices",
                         "waveform": "wave", "start": "home", "hardware": "chip"}.get(kind, "file-plus")
            item = QListWidgetItem(icon(icon_name), text, self.documents)
            item.setData(Qt.UserRole, document)
            if document.path:
                item.setToolTip(document.path)
            if document is active:
                self.documents.setCurrentItem(item)

    def set_data_folder(self, folder: Optional[str]) -> None:
        """Show the data of ``folder`` (the ``data/`` of the project of the active document)."""
        self.data_folder = folder
        self.refresh_data()

    def refresh_data(self) -> None:
        self.data.clear()
        folder = self.data_folder
        visible = bool(folder and os.path.isdir(folder))
        self.data_part.setVisible(visible)
        self.parts.refresh()
        if not visible:
            return
        self.data_part.set_title(f"Data · {os.path.basename(os.path.dirname(folder)) or folder}")
        found = data_files(folder)
        self.data_part.set_count(len(found))
        if not found:
            item = QListWidgetItem("Captures, tables and reports of the flows appear here", self.data)
            item.setFlags(Qt.NoItemFlags)
        for path, kind in found:
            icon_name = data_kind(path)[1]
            item = QListWidgetItem(icon(icon_name), os.path.relpath(path, folder), self.data)
            item.setToolTip(f"{kind}: {path}")
            item.setData(Qt.UserRole, path)
            item.setData(Qt.UserRole + 1, kind)

    def selected_data(self) -> list[tuple[str, str]]:
        return [(item.data(Qt.UserRole), item.data(Qt.UserRole + 1)) for item in self.data.selectedItems()]

    def _data_menu(self, position) -> None:
        selected = self.selected_data()
        if not selected:
            return
        menu = QMenu(self)
        menu.addAction(icon("folder"), "Open", lambda: [self.file_requested.emit(path) for path, _kind in selected])
        captures = [path for path, kind in selected if kind == "capture"]
        if len(captures) == 2:
            menu.addAction(icon("compare"), "Compare the two captures",
                           lambda: self.compare_requested.emit(captures[0], captures[1]))
        if len(captures) == 1 and len(selected) == 1:
            menu.addAction(icon("export"), "Export as CSV...", lambda: self.export_requested.emit(captures[0]))
        menu.addAction(icon("refresh"), "Refresh", self.refresh_data)
        menu.exec(self.data.viewport().mapToGlobal(position))

    def refresh_recent(self) -> None:
        self.recent.clear()
        for path in recent.entries("files"):
            item = QListWidgetItem(icon(file_icon(path)), os.path.basename(path), self.recent)
            item.setToolTip(path)
            item.setData(Qt.UserRole, path)
        self.recent_part.set_count(self.recent.count() or None)
        if not self.recent.count():
            item = QListWidgetItem("Files you open appear here", self.recent)
            item.setFlags(Qt.NoItemFlags)


class SearchSection(QWidget):
    """Searches commands, open documents and recent files (the command palette in the sidebar)."""

    def __init__(self, provider: Callable[[str], list], parent: Optional[QWidget] = None) -> None:
        """``provider(query)`` returns ``(text, detail, callback)`` tuples."""
        super().__init__(parent)
        self._provider = provider
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 8)
        self.input = QLineEdit(self)
        self.input.setPlaceholderText("Search commands, documents, files")
        self.input.setClearButtonEnabled(True)
        self.input.textChanged.connect(self.refresh)
        layout.addWidget(self.input)
        self.results = _list(self)
        self.results.itemActivated.connect(self._run)
        layout.addWidget(self.results, 1)

    def refresh(self) -> None:
        self.results.clear()
        query = self.input.text()
        if not query.strip():
            return
        for text, detail, callback in self._provider(query)[:100]:
            item = QListWidgetItem(text if not detail else f"{text}    {detail}", self.results)
            item.setData(Qt.UserRole, callback)

    def _run(self, item: QListWidgetItem) -> None:
        callback = item.data(Qt.UserRole)
        if callable(callback):
            callback()


class PlaceholderSection(QWidget):
    """A section whose content comes with a later step, with a short explanation."""

    def __init__(self, text: str, button: Optional[tuple[str, Callable]] = None,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        label = QLabel(text, self)
        label.setWordWrap(True)
        set_role(label, "hint")
        layout.addWidget(label)
        if button is not None:
            push = QPushButton(button[0], self)
            push.clicked.connect(button[1])
            layout.addWidget(push)
        layout.addStretch(1)
