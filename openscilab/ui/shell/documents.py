# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The document area in the middle of the shell.

Documents are tabs in *groups* (like the editor groups of VS Code): a group is a tab widget, the
groups sit side by side or one above the other in splitters. A document can move to another
group or into a window of its own (*detach*, e.g. a scope on the second screen) and back.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTabBar,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..icons import icon
from ..theme import TEXT_MUTED, _repolish


class DocumentGroup(QTabWidget):
    """One group of document tabs."""

    def __init__(self, area: "DocumentArea") -> None:
        super().__init__()
        self.area = area
        self.setObjectName("document-group")
        self.setDocumentMode(True)
        # the close button of every tab on its left (made in tabInserted; Qt would put it right)
        self.setTabsClosable(False)
        self.setMovable(True)
        self.setElideMode(Qt.ElideRight)
        # whole names where they fit; more tabs scroll (the scroll buttons are drawn opaque)
        self.tabBar().setUsesScrollButtons(True)
        self.tabBar().setDrawBase(False)
        self.tabCloseRequested.connect(lambda index: area.close_document(self.widget(index)))
        self.currentChanged.connect(lambda _index: area._group_activated(self))
        self.tabBar().setContextMenuPolicy(Qt.CustomContextMenu)
        self.tabBar().customContextMenuRequested.connect(self._tab_menu)
        self.tabBar().tabBarClicked.connect(lambda _index: area._group_activated(self))

    def documents(self) -> list:
        return [self.widget(index) for index in range(self.count())]

    def tabInserted(self, index: int) -> None:  # noqa: N802 - Qt naming
        super().tabInserted(index)
        button = QToolButton(self.tabBar())
        button.setObjectName("tab-close")
        button.setIcon(icon("close", TEXT_MUTED))
        button.setIconSize(QSize(10, 10))
        button.setFixedSize(16, 16)
        button.setAutoRaise(True)
        button.setCursor(Qt.PointingHandCursor)
        button.setToolTip("Close")
        button.setAccessibleName("Close the tab")
        button.clicked.connect(lambda _checked=False, button=button: self._close_tab_of(button))
        self.tabBar().setTabButton(index, QTabBar.LeftSide, button)

    def _close_tab_of(self, button: QToolButton) -> None:
        bar = self.tabBar()
        for index in range(bar.count()):
            if bar.tabButton(index, QTabBar.LeftSide) is button:
                self.tabCloseRequested.emit(index)
                return

    def _tab_menu(self, position) -> None:
        index = self.tabBar().tabAt(position)
        if index < 0:
            return
        document = self.widget(index)
        menu = QMenu(self)
        menu.addAction(icon("close"), "Close", lambda: self.area.close_document(document))
        menu.addAction("Close others", lambda: self.area.close_others(document))
        menu.addSeparator()
        menu.addAction(icon("split"), "Split right", lambda: self.area.split(document, Qt.Horizontal))
        menu.addAction("Split down", lambda: self.area.split(document, Qt.Vertical))
        if len(self.area.groups()) > 1:
            menu.addAction("Move to the next group", lambda: self.area.move_to_next_group(document))
        menu.addAction(icon("detach"), "Open in a new window", lambda: self.area.detach(document))
        menu.exec(self.tabBar().mapToGlobal(position))


class DetachedWindow(QMainWindow):
    """A document in a window of its own; closing the window puts it back into the area."""

    def __init__(self, area: "DocumentArea", document: QWidget) -> None:
        super().__init__(area.window())
        self.setWindowFlags(Qt.Window)
        self.area = area
        self.document = document
        self.setWindowTitle(document.title)
        self.setCentralWidget(document)
        document.show()
        self.resize(max(document.sizeHint().width(), 900), max(document.sizeHint().height(), 600))
        self._returning = False
        # the shortcuts of the shell (save, undo, close, command palette, ...) work here too, and
        # the menus of the document are in this window's menu bar
        for action in area.global_actions():
            self.addAction(action)
        for menu in getattr(document, "document_menus", lambda: [])():
            self.menuBar().addMenu(menu)

    def take_document(self) -> QWidget:
        self._returning = True
        document = self.takeCentralWidget()
        self.close()
        self.deleteLater()  # (it would stay as a hidden window holding the document's menus)
        return document

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if not self._returning:
            self._returning = True
            self.area.attach(self.document)
        super().closeEvent(event)


class DocumentArea(QWidget):
    """Tab groups in splitters; tracks the active document."""

    #: The active document changed (``None`` when there is none).
    active_changed = Signal(object)
    #: the active document changed something (its title, its saved state, what can be undone)
    document_updated = Signal(object)
    #: A document was added, closed, moved, or changed its title or unsaved state.
    documents_changed = Signal()
    #: a button of the empty area: "start", "flow", "open" or "connect"
    empty_action = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget(self)
        layout.addWidget(self.stack)
        self.root = QSplitter(Qt.Horizontal, self.stack)
        self.root.setChildrenCollapsible(False)
        self.stack.addWidget(self.root)
        self.empty_page = self._build_empty_page()
        self.stack.addWidget(self.empty_page)
        self.documents_changed.connect(self._update_empty)
        self._active_group: Optional[DocumentGroup] = None
        self._active_document: Optional[QWidget] = None
        self._detached: dict[int, DetachedWindow] = {}
        #: Asked before a document with unsaved changes is closed: ``callback(document) -> bool``.
        self.confirm_close = lambda document: True
        #: whether closing ``document`` needs a question although it is not dirty (the last
        #: document of a temporary project)
        self.needs_confirm = lambda document: False
        self._revealing = False
        #: the shell's actions that also work in a document's own window (set by the shell)
        self.global_actions = lambda: []
        self._new_group(self.root)
        self._update_empty()
        application = QApplication.instance()
        if application is not None:
            application.focusChanged.connect(self._focus_changed)

    # --------------------------------------------------------------- empty
    def _build_empty_page(self) -> QWidget:
        """What the area shows when every document is closed: where to go from here."""
        from ..icons import set_icon
        from ..theme import set_role, set_variant

        page = QWidget(self.stack)
        page.setObjectName("empty-area")
        outer = QVBoxLayout(page)
        outer.addStretch(1)
        title = QLabel("No document open", page)
        set_role(title, "heading")
        title.setAlignment(Qt.AlignCenter)
        outer.addWidget(title)
        hint = QLabel("Start a flow, connect a device or open a file.", page)
        set_role(hint, "hint")
        hint.setAlignment(Qt.AlignCenter)
        outer.addWidget(hint)
        row = QHBoxLayout()
        row.addStretch(1)
        self.empty_buttons: dict[str, QPushButton] = {}
        for key, text, icon_name in (("flow", "New flow", "nodes"), ("connect", "Connect a device...", "devices"),
                                     ("open", "Open file...", "folder"), ("start", "Start page", "home")):
            button = QPushButton(text, page)
            set_icon(button, icon_name)
            if key == "flow":
                set_variant(button, "primary")
            button.clicked.connect(lambda _checked=False, key=key: self.empty_action.emit(key))
            row.addWidget(button)
            self.empty_buttons[key] = button
        row.addStretch(1)
        outer.addLayout(row)
        outer.addStretch(2)
        return page

    def _update_empty(self) -> None:
        self.stack.setCurrentWidget(self.root if self.documents() else self.empty_page)

    # -------------------------------------------------------------- groups
    def _new_group(self, splitter: QSplitter, index: int = -1) -> DocumentGroup:
        group = DocumentGroup(self)
        if index < 0:
            splitter.addWidget(group)
        else:
            splitter.insertWidget(index, group)
        if self._active_group is None:
            self._set_active_group(group)
        return group

    def groups(self) -> list[DocumentGroup]:
        return [group for group in self.root.findChildren(DocumentGroup) if group.area is self]

    def group_of(self, document: QWidget) -> Optional[DocumentGroup]:
        for group in self.groups():
            if group.indexOf(document) >= 0:
                return group
        return None

    def _set_active_group(self, group: DocumentGroup) -> None:
        if self._active_group is not None and self._active_group is not group:
            try:
                self._active_group.setProperty("active", False)
                _repolish(self._active_group)
            except RuntimeError:  # deleted
                pass
        self._active_group = group
        group.setProperty("active", True)
        _repolish(group)

    def _group_activated(self, group: DocumentGroup) -> None:
        if group.area is not self or self._revealing:
            return
        self._set_active_group(group)
        self._set_active_document(group.currentWidget())

    def _focus_changed(self, _old, new) -> None:
        """The group of the widget that got the focus becomes the active group."""
        widget = new
        try:
            while widget is not None:
                if isinstance(widget, DocumentGroup) and widget.area is self:
                    self._group_activated(widget)
                    return
                widget = widget.parentWidget()
        except RuntimeError:  # the area or the widget is being deleted
            return

    def _set_active_document(self, document: Optional[QWidget]) -> None:
        if document is not self._active_document:
            self._active_document = document
            self.active_changed.emit(document)

    def _remove_empty_groups(self) -> None:
        groups = self.groups()
        for group in groups:
            if group.count() == 0 and len(self.groups()) > 1:
                splitter = group.parentWidget()
                group.setParent(None)
                group.deleteLater()
                if group is self._active_group:
                    self._active_group = None
                # Splitters left with a single child are flattened.
                if isinstance(splitter, QSplitter) and splitter is not self.root and splitter.count() == 1:
                    parent = splitter.parentWidget()
                    if isinstance(parent, QSplitter):
                        child = splitter.widget(0)
                        index = parent.indexOf(splitter)
                        parent.insertWidget(index, child)
                        splitter.setParent(None)
                        splitter.deleteLater()
        if self._active_group is None:
            remaining = self.groups()
            if remaining:
                self._set_active_group(remaining[0])
                self._set_active_document(remaining[0].currentWidget())

    # ----------------------------------------------------------- documents
    def documents(self) -> list[QWidget]:
        """Every document, in the groups and in their own windows."""
        result = []
        for group in self.groups():
            result += group.documents()
        result += [window.document for window in self._detached.values()]
        return result

    def active_document(self) -> Optional[QWidget]:
        return self._active_document

    def active_group(self) -> DocumentGroup:
        if self._active_group is None or self._active_group.area is not self:
            self._set_active_group(self.groups()[0])
        return self._active_group

    def add_document(self, document: QWidget, group: Optional[DocumentGroup] = None,
                     activate: bool = True) -> QWidget:
        group = group or self.active_group()
        index = group.addTab(document, document.title)
        self._update_tab(document)
        changed = getattr(document, "document_changed", None)
        if changed is not None:
            changed.connect(lambda document=document: self._document_changed(document))
        if activate:
            group.setCurrentIndex(index)
            self.activate(document)
        self.documents_changed.emit()
        return document

    def _update_tab(self, document: QWidget) -> None:
        group = self.group_of(document)
        title = document.title + (" ●" if getattr(document, "dirty", False) else "")
        if group is not None:
            index = group.indexOf(document)
            group.setTabText(index, title)
            group.setTabToolTip(index, getattr(document, "path", None) or document.title)
        elif id(document) in self._detached:
            self._detached[id(document)].setWindowTitle(title)

    def _document_changed(self, document: QWidget) -> None:
        try:
            self._update_tab(document)
        except RuntimeError:  # the document was deleted meanwhile
            return
        self.documents_changed.emit()
        if document is self._active_document:
            # the same document, changed (an edit, a save): not another document, so the shell
            # does not build its menus again
            self.document_updated.emit(document)

    def reveal(self, document: QWidget) -> None:
        """Show ``document`` in its group without making it the active one (the focus stays)."""
        window = self._detached.get(id(document))
        if window is not None:
            window.raise_()
            return
        group = self.group_of(document)
        if group is None or group.currentWidget() is document:
            return
        active = self._active_document
        self._revealing = True
        try:
            group.setCurrentWidget(document)
        finally:
            self._revealing = False
        if active is None or self.group_of(active) is group:
            self._group_activated(group)  # the shown document of the active group is the active one

    def activate(self, document: QWidget) -> None:
        """Show ``document`` and make it the active one."""
        window = self._detached.get(id(document))
        if window is not None:
            window.raise_()
            window.activateWindow()
            self._set_active_document(document)
            return
        group = self.group_of(document)
        if group is None:
            return
        group.setCurrentWidget(document)
        self._set_active_group(group)
        self._set_active_document(document)

    def close_document(self, document: QWidget, force: bool = False) -> bool:
        """Close ``document`` (asking about unsaved changes unless ``force``); ``True`` when closed."""
        if not force and (getattr(document, "dirty", False) or self.needs_confirm(document)) \
                and not self.confirm_close(document):
            return False
        window = self._detached.pop(id(document), None)
        if window is not None:
            window.take_document()
        group = self.group_of(document)
        if group is not None:
            group.removeTab(group.indexOf(document))
        shutdown = getattr(document, "shutdown", None)
        if shutdown is not None:
            shutdown()
        document.setParent(None)
        document.deleteLater()
        if document is self._active_document:
            self._active_document = None
            current = self.active_group().currentWidget() if self.groups() else None
            self._set_active_document(current)
            if current is None:
                self.active_changed.emit(None)
        self._remove_empty_groups()
        self.documents_changed.emit()
        return True

    def close_others(self, keep: QWidget) -> None:
        for document in list(self.documents()):
            if document is not keep:
                self.close_document(document)

    def close_all(self, force: bool = False) -> bool:
        for document in list(self.documents()):
            if not self.close_document(document, force=force):
                return False
        return True

    # ------------------------------------------------------- arrangement
    def split(self, document: QWidget, orientation=Qt.Horizontal) -> DocumentGroup:
        """Move ``document`` into a new group beside (``Horizontal``) or below its group."""
        group = self.group_of(document)
        if group is None:
            group = self.active_group()
        splitter = group.parentWidget()
        if not isinstance(splitter, QSplitter):  # pragma: no cover - groups always live in splitters
            splitter = self.root
        index = splitter.indexOf(group)
        if splitter.orientation() == orientation or splitter.count() == 1:
            splitter.setOrientation(orientation)
            new_group = self._new_group(splitter, index + 1)
        else:
            # A splitter of the other orientation in place of the group holds both.
            inner = QSplitter(orientation)
            inner.setChildrenCollapsible(False)
            splitter.insertWidget(index, inner)
            inner.addWidget(group)
            new_group = self._new_group(inner)
        if group.indexOf(document) >= 0:
            group.removeTab(group.indexOf(document))
        new_group.addTab(document, document.title)
        self._update_tab(document)
        new_group.setCurrentWidget(document)
        self._set_active_group(new_group)
        self._set_active_document(document)
        self._remove_empty_groups()
        sizes = [1] * splitter.count()
        splitter.setSizes([10_000 * size for size in sizes])
        self.documents_changed.emit()
        return new_group

    def move_to_next_group(self, document: QWidget) -> None:
        groups = self.groups()
        group = self.group_of(document)
        if group is None or len(groups) < 2:
            return
        self.move_to_group(document, groups[(groups.index(group) + 1) % len(groups)])

    def move_to_group(self, document: QWidget, target: "DocumentGroup") -> None:
        group = self.group_of(document)
        if group is None or target is None or group is target:
            return
        group.removeTab(group.indexOf(document))
        target.addTab(document, document.title)
        self._update_tab(document)
        target.setCurrentWidget(document)
        self._set_active_group(target)
        self._set_active_document(document)
        self._remove_empty_groups()
        self.documents_changed.emit()

    def detach(self, document: QWidget) -> DetachedWindow:
        """Open ``document`` in a window of its own."""
        group = self.group_of(document)
        if group is not None:
            group.removeTab(group.indexOf(document))
        window = DetachedWindow(self, document)
        self._detached[id(document)] = window
        self._update_tab(document)
        window.show()
        self._remove_empty_groups()
        self._set_active_document(document)
        self.documents_changed.emit()
        return window

    def is_detached(self, document: QWidget) -> bool:
        return id(document) in self._detached

    def attach(self, document: QWidget) -> None:
        """Put a detached document back into the active group."""
        window = self._detached.pop(id(document), None)
        if window is None:
            return
        if not window._returning:
            window.take_document()
        group = self.active_group()
        group.addTab(document, document.title)
        self._update_tab(document)
        group.setCurrentWidget(document)
        self.activate(document)
        self.documents_changed.emit()
