# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Activity bar: the narrow column on the far left - the sidebar sections at the top, commands
(start page, settings) at the bottom.

A right click arranges it: which entries it shows, their order (*Move up*, *Move down*), whether
their names show under the icons, *Reset*. The arrangement is kept with the tool bars
(``toolbars.json``, key ``activity``).
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import QMenu, QSizePolicy, QToolBar, QToolButton, QWidget

from ..icons import icon
from ..theme import icon_px

CONFIG_KEY = "activity"


class ActivityBar(QToolBar):
    """One checkable button per sidebar section, commands below them.

    Clicking the button of the section that is shown hides the sidebar (``sidebar_toggled``);
    clicking another one shows that section (``section_selected``).
    """

    section_selected = Signal(str)
    sidebar_toggled = Signal(bool)
    #: the arrangement changed (shown entries, order, names)
    arranged = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__("Activity bar", parent)
        from ..widgets.toolbar import load_config

        self.setObjectName("activity-bar")
        self.setOrientation(Qt.Vertical)
        self.setMovable(False)
        self.setFloatable(False)
        self.setIconSize(QSize(icon_px(20), icon_px(20)))
        self.toggleViewAction().setVisible(False)
        self.setContextMenuPolicy(Qt.PreventContextMenu)  # (our own menu, see contextMenuEvent)
        self._group = QActionGroup(self)
        self._group.setExclusionPolicy(QActionGroup.ExclusionPolicy.ExclusiveOptional)
        self._actions: dict[str, QAction] = {}
        #: every entry in openSciLab's order: (key, action, at the bottom)
        self._entries: list[tuple[str, QAction, bool]] = []
        self._current: Optional[str] = None
        self._sidebar_visible = True
        self.config = load_config(CONFIG_KEY)
        self._spacer = QWidget(self)
        self._spacer.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        self._spacer_action = self.addWidget(self._spacer)

    # ------------------------------------------------------------- entries
    def add_section(self, key: str, title: str, icon_name: str, shortcut: Optional[str] = None) -> QAction:
        action = QAction(icon(icon_name), title, self)
        action.setCheckable(True)
        action.setObjectName(f"activity-{key}")
        action.setToolTip(title if not shortcut else f"{title} ({shortcut})")
        if shortcut:
            action.setShortcut(shortcut)
        action.triggered.connect(lambda _checked=False, key=key: self._clicked(key))
        self._group.addAction(action)
        self._actions[key] = action
        self._entries.append((key, action, False))
        self.rebuild()
        return action

    def add_command(self, key: str, action: QAction) -> QAction:
        """A command at the bottom (the start page, the settings)."""
        action.setObjectName(f"activity-{key}")
        self._entries.append((key, action, True))
        self.rebuild()
        return action

    def section_action(self, key: str) -> QAction:
        return self._actions[key]

    @property
    def current(self) -> Optional[str]:
        return self._current

    def keys(self) -> list[str]:
        """The sidebar sections."""
        return list(self._actions)

    # ---------------------------------------------------------- arrangement
    @property
    def show_labels(self) -> bool:
        return bool(self.config.get("labels", False))

    def ordered(self) -> list[tuple[str, QAction, bool]]:
        order = [key for key in self.config.get("order") or [] if any(entry[0] == key for entry in self._entries)]
        rank = {key: index for index, key in enumerate(order)}
        default = {entry[0]: index for index, entry in enumerate(self._entries)}
        return sorted(self._entries, key=lambda entry: (entry[2], rank.get(entry[0], len(rank) + default[entry[0]])))

    def shown(self, key: str) -> bool:
        return key not in (self.config.get("hidden") or [])

    def rebuild(self) -> None:
        for action in list(self.actions()):
            if action is not self._spacer_action:
                self.removeAction(action)
        top = [entry for entry in self.ordered() if not entry[2] and self.shown(entry[0])]
        bottom = [entry for entry in self.ordered() if entry[2] and self.shown(entry[0])]
        for _key, action, _bottom in top:
            self.insertAction(self._spacer_action, action)
        for _key, action, _bottom in bottom:
            self.addAction(action)
        style = Qt.ToolButtonTextUnderIcon if self.show_labels else Qt.ToolButtonIconOnly
        self.setToolButtonStyle(style)
        self.setProperty("labels", self.show_labels)
        self.style().unpolish(self)
        self.style().polish(self)
        for _key, action, _bottom in top + bottom:
            button = self.widgetForAction(action)
            if isinstance(button, QToolButton):
                button.setAccessibleName(action.text().replace("&", ""))

    def _save(self) -> None:
        from ..widgets.toolbar import save_config

        save_config(CONFIG_KEY, self.config)
        self.rebuild()
        self.arranged.emit()

    def set_shown(self, key: str, shown: bool) -> None:
        hidden = [name for name in self.config.get("hidden") or [] if name != key]
        if not shown:
            visible = [entry for entry in self._entries if not entry[2] and self.shown(entry[0]) and entry[0] != key]
            if key in self._actions and not visible:
                return  # (one section stays)
            hidden.append(key)
        self.config["hidden"] = hidden
        self._save()

    def set_show_labels(self, labels: bool) -> None:
        self.config["labels"] = bool(labels)
        self._save()

    def move(self, key: str, step: int) -> None:
        """Move the entry ``key`` up (-1) or down (+1) within its group."""
        entries = self.ordered()
        bottom = next(entry[2] for entry in entries if entry[0] == key)
        group = [entry[0] for entry in entries if entry[2] == bottom]
        index = group.index(key)
        target = max(0, min(index + step, len(group) - 1))
        group.insert(target, group.pop(index))
        others = [entry[0] for entry in entries if entry[2] != bottom]
        self.config["order"] = (others + group) if bottom else (group + others)
        self._save()

    def reset(self) -> None:
        from ..widgets.toolbar import save_config

        self.config = {}
        save_config(CONFIG_KEY, None)
        self.rebuild()
        self.arranged.emit()

    def context_menu(self, key: Optional[str] = None) -> QMenu:
        menu = QMenu(self)
        if key is not None:
            title = next(entry[1].text().replace("&", "") for entry in self._entries if entry[0] == key)
            menu.addSection(title)
            menu.addAction(icon("up"), "Move up", lambda: self.move(key, -1))
            menu.addAction(icon("down"), "Move down", lambda: self.move(key, 1))
            menu.addAction(icon("eye"), "Hide", lambda: self.set_shown(key, False))
            menu.addSeparator()
        menu.addSection("Show")
        for entry_key, action, _bottom in self.ordered():
            item = menu.addAction(action.text().replace("&", ""))
            item.setCheckable(True)
            item.setChecked(self.shown(entry_key))
            item.toggled.connect(lambda checked, entry_key=entry_key: self.set_shown(entry_key, checked))
        menu.addSeparator()
        labels = menu.addAction("Names under the icons")
        labels.setCheckable(True)
        labels.setChecked(self.show_labels)
        labels.toggled.connect(self.set_show_labels)
        menu.addAction(icon("reset"), "Reset", self.reset)
        return menu

    def contextMenuEvent(self, event) -> None:  # noqa: N802 - Qt naming
        action = self.actionAt(event.pos())
        key = next((entry[0] for entry in self._entries if entry[1] is action), None)
        self.context_menu(key).exec(event.globalPos())
        event.accept()

    # -------------------------------------------------------------- sections
    def _clicked(self, key: str) -> None:
        if key == self._current and self._sidebar_visible:
            self.set_sidebar_visible(False)
            return
        self.select(key)

    def select(self, key: str) -> None:
        """Show the section ``key`` (and the sidebar)."""
        self._current = key
        for name, action in self._actions.items():
            action.setChecked(name == key)
        self.section_selected.emit(key)
        self.set_sidebar_visible(True)

    def set_sidebar_visible(self, visible: bool) -> None:
        self._sidebar_visible = visible
        if not visible:
            for action in self._actions.values():
                action.setChecked(False)
        elif self._current in self._actions:
            self._actions[self._current].setChecked(True)
        self.sidebar_toggled.emit(visible)
