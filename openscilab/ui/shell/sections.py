# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Collapsible parts of a sidebar section (*Open documents*, *Project*, *Data*, *Connected*, ...).

A :class:`Part` has a header - a chevron, its title, how many entries it has, buttons of its own -
and its content; a click on the header opens or closes it. A :class:`PartStack` holds the parts of
a section: the open ones share the height, the closed ones keep only their header. Which parts
are closed is kept (``toolbars.json``, key ``sidebar``).
"""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QToolButton, QVBoxLayout, QWidget

from ..icons import icon
from ..theme import TEXT_MUTED, set_role

CONFIG_KEY = "sidebar"


def _closed() -> set[str]:
    from ..widgets.toolbar import load_config

    return set(load_config(CONFIG_KEY).get("closed") or [])


def _remember(key: str, opened: bool) -> None:
    from ..widgets.toolbar import load_config, save_config

    config = load_config(CONFIG_KEY)
    closed = set(config.get("closed") or [])
    (closed.discard if opened else closed.add)(key)
    config["closed"] = sorted(closed)
    save_config(CONFIG_KEY, config)


class Part(QWidget):
    """A collapsible part of a sidebar section (see the module)."""

    toggled = Signal(bool)

    def __init__(self, key: str, title: str, content: QWidget, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.key = key
        self.content = content
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.header = QWidget(self)
        self.header.setObjectName("part-header")
        self.header.setCursor(Qt.PointingHandCursor)
        row = QHBoxLayout(self.header)
        row.setContentsMargins(6, 3, 6, 3)
        row.setSpacing(4)
        self.chevron = QLabel(self.header)
        row.addWidget(self.chevron)
        self.title_label = QLabel(title, self.header)
        set_role(self.title_label, "part-title")
        self.title_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        row.addWidget(self.title_label, 1)
        self.count_label = QLabel(self.header)
        set_role(self.count_label, "part-count")
        row.addWidget(self.count_label)
        self.buttons = QHBoxLayout()
        self.buttons.setSpacing(0)
        row.addLayout(self.buttons)
        layout.addWidget(self.header)
        content.setParent(self)
        layout.addWidget(content, 1)
        self._open = key not in _closed()
        self._apply()

    # ---------------------------------------------------------------- state
    @property
    def is_open(self) -> bool:
        return self._open

    def set_open(self, opened: bool, remember: bool = True) -> None:
        if opened == self._open:
            return
        self._open = opened
        self._apply()
        if remember:
            _remember(self.key, opened)
        self.toggled.emit(opened)

    def _apply(self) -> None:
        self.chevron.setPixmap(icon("down" if self._open else "arrow-right", TEXT_MUTED).pixmap(10, 10))
        self.content.setVisible(self._open)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding if self._open else QSizePolicy.Fixed)

    def set_title(self, title: str) -> None:
        self.title_label.setText(title)
        self.title_label.setToolTip(title)

    def set_count(self, count: Optional[int]) -> None:
        self.count_label.setText("" if count is None else str(count))

    def add_button(self, icon_name: str, tip: str, slot: Callable[[], object]) -> QToolButton:
        """A small button in the header (*Refresh*, *Disconnect all*, ...)."""
        button = QToolButton(self.header)
        button.setObjectName("part-button")
        button.setIcon(icon(icon_name, TEXT_MUTED))
        button.setIconSize(QSize(12, 12))
        button.setAutoRaise(True)
        button.setToolTip(tip)
        button.setAccessibleName(tip)
        button.clicked.connect(lambda _checked=False: slot())
        self.buttons.addWidget(button)
        return button

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.LeftButton and self.header.geometry().contains(event.position().toPoint()):
            self.set_open(not self._open)
            event.accept()
            return
        super().mouseReleaseEvent(event)


class PartStack(QWidget):
    """The parts of a sidebar section, one below the other (see the module)."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        self.parts: list[Part] = []
        self._layout.addStretch(0)

    def add(self, part: Part, weight: int = 1) -> Part:
        part.setParent(self)
        part.weight = weight
        self._layout.insertWidget(self._layout.count() - 1, part)
        self.parts.append(part)
        part.toggled.connect(lambda _open: self._share())
        self._share()
        return part

    def add_widget(self, widget: QWidget) -> None:
        """Something below the parts that is no part (a notice, buttons)."""
        self._layout.insertWidget(self._layout.count() - 1, widget)

    def _share(self) -> None:
        any_open = False
        for part in self.parts:
            index = self._layout.indexOf(part)
            opened = part.is_open and not part.isHidden()
            self._layout.setStretch(index, part.weight if opened else 0)
            any_open = any_open or opened
        self._layout.setStretch(self._layout.count() - 1, 0 if any_open else 1)

    def refresh(self) -> None:
        self._share()
