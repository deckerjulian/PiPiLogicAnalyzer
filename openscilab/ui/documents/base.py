# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""What every document of the shell offers.

A document is a widget in a tab of the document area (a flow, a panel, captures in a data
view, a device card, ...). The shell asks it for its title, file, unsaved state, menus, actions
(for the command palette), toolbar, views and the inspector content. :class:`Document` is a mixin
with defaults; widgets inherit from their Qt base class first, e.g.
``class FlowDocument(QWidget, Document)``, and declare their own ``document_changed`` signal
(``DocumentWidget`` does that for plain widgets).
"""

from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu, QToolBar, QWidget


class Document:
    """Mixin with the document interface; every method has a sensible default."""

    #: Kind of document ("data", "start", "flow", ...); used by the shell and the tests.
    document_kind = "document"

    # ------------------------------------------------------------ identity
    @property
    def title(self) -> str:
        """Text of the tab."""
        path = self.path
        return os.path.basename(path) if path else "Untitled"

    @property
    def path(self) -> Optional[str]:
        """File the document was loaded from or saved to, ``None`` before it is saved."""
        return None

    @property
    def dirty(self) -> bool:
        """``True`` while there are changes that were not saved."""
        return False

    # ------------------------------------------------------- shell content
    def document_actions(self) -> list[QAction]:
        """Actions the command palette offers beside those of :meth:`document_menus`.

        (``actions()`` is taken by ``QWidget``.)
        """
        return []

    def document_menus(self) -> list[QMenu]:
        """Menus shown in the menu bar of the shell while the document is active."""
        return []

    def document_file_actions(self) -> list[QAction]:
        """Entries the *Project* menu shows for this document (e.g. *Export*)."""
        return []

    def toolbar(self) -> Optional[QToolBar]:
        """The narrow toolbar of the document (shown inside the document)."""
        return None

    def views(self) -> list[str]:
        """Names of the views the document can switch between (``Graph``, ``YAML``, ...)."""
        return []

    def current_view(self) -> Optional[str]:
        views = self.views()
        return views[0] if views else None

    def set_view(self, name: str) -> None:
        """Switch to the view ``name`` (one of :meth:`views`)."""

    def inspector_widget(self, selection=None) -> Optional[QWidget]:
        """Properties of ``selection`` (or of the document) for the inspector, ``None`` for none."""
        return None

    def undo_stack(self):
        """The ``QUndoStack`` of the document, if it has one (Edit → Undo/Redo)."""
        return None

    # -------------------------------------------------------------- files
    def can_save(self) -> bool:
        return False

    def save(self) -> bool:
        """Save to :attr:`path` (or ask for one); ``True`` when saved."""
        return False

    def save_as(self) -> bool:
        return False

    def shutdown(self) -> None:
        """Release devices and threads; called once before the document is closed for good."""


_untitled: dict[str, int] = {}


def untitled_title(kind: str) -> str:
    """``Untitled flow``, ``Untitled flow 2``, … for documents that have no name of their own yet."""
    number = _untitled.get(kind, 0) + 1
    _untitled[kind] = number
    return f"Untitled {kind}" + (f" {number}" if number > 1 else "")


class DocumentWidget(QWidget, Document):
    """A plain widget as a document."""

    #: Title, path, dirty state or selection changed: the shell updates tab, menus and inspector.
    document_changed = Signal()
