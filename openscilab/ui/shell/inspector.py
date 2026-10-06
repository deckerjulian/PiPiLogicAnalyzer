# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Inspector: properties of what is selected in the active document (non-modal, on the right)."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QLabel, QScrollArea, QVBoxLayout, QWidget

from ..theme import set_role


class Inspector(QWidget):
    """Shows the widget a document returns from ``inspector_widget(selection)``.

    The inspector does not own the widgets it shows: a document keeps its inspector widget and
    gets it back (re-parented to the document) when another one is shown.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("inspector")
        self.setMinimumWidth(220)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.heading = QLabel("INSPECTOR", self)
        set_role(self.heading, "sidebar-title")
        layout.addWidget(self.heading)
        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        # the content takes the width of the inspector (texts wrap instead of being cut off)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        layout.addWidget(self.scroll, 1)
        self.empty = QLabel("Nothing selected", self)
        self.empty.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        self.empty.setContentsMargins(10, 12, 10, 10)
        self.empty.setWordWrap(True)
        set_role(self.empty, "hint")
        self.scroll.setWidget(self.empty)
        self._content: Optional[QWidget] = None
        self._owner: Optional[QWidget] = None

    @property
    def content(self) -> Optional[QWidget]:
        return self._content

    def show_widget(self, widget: Optional[QWidget], owner: Optional[QWidget] = None,
                    title: str = "Inspector", empty_text: str = "Nothing selected") -> None:
        """Show ``widget`` (``None``: ``empty_text``); ``owner`` gets it back later."""
        if widget is self._content:
            self.heading.setText(title.upper())
            return
        self._release()
        self.heading.setText(title.upper())
        if widget is None:
            self.empty = QLabel(empty_text, self)
            self.empty.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
            self.empty.setContentsMargins(10, 12, 10, 10)
            self.empty.setWordWrap(True)
            set_role(self.empty, "hint")
            self.scroll.setWidget(self.empty)
            return
        self._content = widget
        self._owner = owner
        self.scroll.setWidget(widget)
        widget.show()

    def _release(self) -> None:
        """Give the shown widget back to its document instead of deleting it with the scroll area."""
        if self._content is None:
            return
        widget = self.scroll.takeWidget()
        if widget is not None:
            try:
                widget.hide()
                widget.setParent(self._owner)
            except RuntimeError:  # the owner is gone
                pass
        self._content = None
        self._owner = None

    def clear(self) -> None:
        self.show_widget(None)
