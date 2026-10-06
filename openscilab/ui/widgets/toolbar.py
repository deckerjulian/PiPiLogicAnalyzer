# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""A tool bar that fits its window and that people arrange themselves.

Every entry of an :class:`AdaptiveToolBar` has a key, a title, a group and a priority. When the bar
is too narrow it gives way step by step: first the less important entries lose their text (the icon
stays, the name is in the tooltip), then they move into the *more* menu (``⋯``) at the end of the
bar, the important ones last; entries that are *pinned* (Capture, Run) always stay. What is in the
*more* menu works there as in the bar: actions as they are, check boxes as checkable entries, combo
boxes as submenus, buttons as entries.

A right click on the bar arranges it: *Customize toolbar…* shows and hides entries and changes their
order, the display is *Automatic* (text while there is room), *Icons only* or *Icons and text*;
*Reset* returns to how openSciLab arranges it. The arrangement is kept per bar (``toolbars.json`` of
the settings directory).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup, QIcon
from PySide6.QtWidgets import (
    QAbstractButton,
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QSizePolicy,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from ...core import settings
from ..icons import icon
from ..theme import icon_px

TOOLBARS_FILE = "toolbars.json"

HIGH = 0
NORMAL = 1
LOW = 2

#: how entries show: ``auto`` text while there is room, ``icons`` only icons, ``text`` icons and text
MODES = {"auto": "Automatic (text while there is room)", "icons": "Icons only", "text": "Icons and text"}


@dataclass
class ToolItem:
    key: str
    title: str
    group: str
    priority: int = NORMAL
    #: an entry of the bar: an action (a tool button of the bar) or a widget
    action: Optional[QAction] = None
    widget: Optional[QWidget] = None
    #: never moves into the *more* menu
    pinned: bool = False
    #: shows its text in the automatic display (False: an icon, the name in the tooltip)
    text: bool = True
    #: shown unless the user hides it
    default: bool = True
    #: fills what is left (a stretch between groups)
    stretch: bool = False
    #: fills the *more* menu with what the widget does, when it is there
    overflow: Optional[Callable[[QMenu], None]] = None
    #: the action of the bar for this entry (the action, or the widget's QWidgetAction)
    bar_action: Optional[QAction] = None
    #: the entry applies now (e.g. *Roll* only while a capture streams): neither in the bar nor its menu
    available: bool = True

    @property
    def configurable(self) -> bool:
        return not self.stretch


def load_config(key: str) -> dict:
    data = settings.get_settings(TOOLBARS_FILE)
    entry = data.get(key) if isinstance(data, dict) else None
    return dict(entry) if isinstance(entry, dict) else {}


def save_config(key: str, config: Optional[dict]) -> None:
    data = settings.get_settings(TOOLBARS_FILE)
    data = data if isinstance(data, dict) else {}
    if config:
        data[key] = config
    else:
        data.pop(key, None)
    settings.persist_settings(TOOLBARS_FILE, data)


class AdaptiveToolBar(QToolBar):
    """See the module. Add the entries (:meth:`add_action`, :meth:`add_widget`, :meth:`add_stretch`),
    then :meth:`finish`."""

    #: the arrangement changed (shown entries, order, display)
    arranged = Signal()

    def __init__(self, key: str, title: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(title, parent)
        self.key = key
        self.setObjectName(f"{key}-toolbar")
        self.setProperty("adaptive", True)
        self.setMovable(False)
        self.setFloatable(False)
        self.toggleViewAction().setVisible(False)
        self.setIconSize(QSize(icon_px(16), icon_px(16)))
        self.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.setContextMenuPolicy(Qt.PreventContextMenu)  # (our own menu, see contextMenuEvent)
        self.items: list[ToolItem] = []
        self._entries: list[ToolItem] = []
        self._separators: dict[str, QAction] = {}
        self._overflowed: list[ToolItem] = []
        self._finished = False
        self._pending = False
        self.config = load_config(key)
        self.more_button = QToolButton(self)
        self.more_button.setObjectName("toolbar-more")
        self.more_button.setText("More")
        self.more_button.setIcon(icon("more"))
        self.more_button.setToolTip("More of this toolbar (right click: customize it)")
        self.more_button.setToolButtonStyle(Qt.ToolButtonIconOnly)
        self.more_button.setPopupMode(QToolButton.InstantPopup)
        self.more_menu = QMenu(self.more_button)
        self.more_menu.aboutToShow.connect(self._fill_more)
        self.more_button.setMenu(self.more_menu)
        self.more_action = QWidgetAction(self)
        self.more_action.setDefaultWidget(self.more_button)

    # ------------------------------------------------------------- entries
    def add_action(self, key: str, action: QAction, group: str = "", priority: int = NORMAL, *, pinned: bool = False,
                   text: bool = True, default: bool = True, title: Optional[str] = None) -> QAction:
        self.items.append(ToolItem(key, title or action.iconText() or action.text(), group, priority, action=action,
                                   pinned=pinned, text=text, default=default, bar_action=action))
        return action

    def add_widget(self, key: str, widget: QWidget, title: str, group: str = "", priority: int = NORMAL, *,
                   pinned: bool = False, text: bool = True, default: bool = True,
                   overflow: Optional[Callable[[QMenu], None]] = None) -> QWidget:
        holder = QWidgetAction(self)
        holder.setDefaultWidget(widget)
        if isinstance(widget, QAbstractButton):
            widget.setProperty("full_text", widget.text())
        self.items.append(ToolItem(key, title, group, priority, widget=widget, pinned=pinned, text=text,
                                   default=default, overflow=overflow, bar_action=holder))
        return widget

    def add_stretch(self, key: str = "") -> None:
        spacer = QWidget(self)
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        holder = QWidgetAction(self)
        holder.setDefaultWidget(spacer)
        self.items.append(ToolItem(key or f"stretch-{len(self.items)}", "", "", LOW, widget=spacer, pinned=True,
                                   stretch=True, bar_action=holder))

    def item(self, key: str) -> Optional[ToolItem]:
        return next((item for item in self.items if item.key == key), None)

    def button(self, key: str) -> Optional[QWidget]:
        """The button (or widget) of an entry in the bar."""
        item = self.item(key)
        if item is None:
            return None
        return item.widget if item.widget is not None else self.widgetForAction(item.action)

    def finish(self) -> AdaptiveToolBar:
        self._finished = True
        self.rebuild()
        return self

    # ---------------------------------------------------------- arrangement
    @property
    def mode(self) -> str:
        mode = self.config.get("mode", "auto")
        return mode if mode in MODES else "auto"

    def ordered(self) -> list[ToolItem]:
        """The entries in the order the user chose (new entries where openSciLab puts them)."""
        order = [key for key in self.config.get("order") or [] if self.item(key) is not None]
        if not order:
            return list(self.items)
        result = [self.item(key) for key in order]
        known = set(order)
        for index, item in enumerate(self.items):
            if item.key in known:
                continue
            # a new entry: after the entry before it in openSciLab's order
            before = next((self.items[back].key for back in range(index - 1, -1, -1) if self.items[back].key in known),
                          None)
            position = next((place + 1 for place, other in enumerate(result) if other.key == before), 0)
            result.insert(position, item)
            known.add(item.key)
        return result

    def set_available(self, key: str | tuple[str, ...] | list[str], available: bool) -> None:
        """Whether an entry (or each of several) applies now: it is left out while it does not; the
        arrangement stays."""
        changed = False
        for name in (key,) if isinstance(key, str) else key:
            item = self.item(name)
            if item is not None and item.available != bool(available):
                item.available = bool(available)
                changed = True
        if changed:
            self.rebuild()

    def shown(self, item: ToolItem) -> bool:
        if not item.available:
            return False
        return self.wanted(item)

    def wanted(self, item: ToolItem) -> bool:
        """Whether the user has the entry in the bar (whether it applies now or not)."""
        hidden = self.config.get("hidden")
        if isinstance(hidden, list) and item.key in hidden:
            return False
        shown = self.config.get("shown")
        return item.default or (isinstance(shown, list) and item.key in shown)

    def set_arrangement(self, order: list[str], visible: set[str], mode: str) -> None:
        defaults = [item.key for item in self.items]
        config: dict = {}
        if order != defaults:
            config["order"] = order
        hidden = [item.key for item in self.items if item.configurable and item.default and item.key not in visible]
        shown = [item.key for item in self.items if item.configurable and not item.default and item.key in visible]
        if hidden:
            config["hidden"] = hidden
        if shown:
            config["shown"] = shown
        if mode != "auto":
            config["mode"] = mode
        self.config = config
        save_config(self.key, config)
        self.rebuild()
        self.arranged.emit()

    def set_mode(self, mode: str) -> None:
        order = [item.key for item in self.ordered()]
        self.set_arrangement(order, {item.key for item in self.items if self.wanted(item)}, mode)

    def reset(self) -> None:
        self.config = {}
        save_config(self.key, None)
        self.rebuild()
        self.arranged.emit()

    # --------------------------------------------------------------- layout
    def rebuild(self) -> None:
        """Take the shown entries in their order, then fit them into the bar."""
        if not self._finished:
            return
        self._entries = [item for item in self.ordered() if self.shown(item)]
        self._overflowed = []
        self._place(self._actions(self._entries, {item.key: True for item in self._entries}))
        self.relayout()

    def _separator(self, key: str) -> QAction:
        separator = self._separators.get(key)
        if separator is None:
            separator = self._separators[key] = QAction(self)
            separator.setSeparator(True)
        return separator

    def _place(self, actions: list[QAction]) -> None:
        """Make ``actions`` what the bar holds, in this order. What does not fit is taken out of the bar
        rather than hidden: a hidden action is disabled by Qt - in its menus too, and so is the widget of
        a hidden QWidgetAction, which then forgets whether the application had disabled it."""
        current = self.actions()
        if current == actions:
            return
        for action in current:
            if action not in actions:
                self.removeAction(action)
        for index, action in enumerate(actions):
            current = self.actions()
            if index < len(current) and current[index] is action:
                continue
            if action in current:
                self.removeAction(action)
                current = self.actions()
            if index < len(current):
                self.insertAction(current[index], action)
            else:
                self.addAction(action)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.schedule()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.schedule()

    def schedule(self) -> None:
        if not self._pending:
            self._pending = True
            QTimer.singleShot(0, self, self._relayout_later)  # (not after the bar is gone)

    def _relayout_later(self) -> None:
        self._pending = False
        self.relayout()

    def _fits(self, entries: list[ToolItem], inside: dict) -> bool:
        """Whether every entry meant to be in the bar is there: Qt hides what does not fit (into its
        own extension, which this bar never wants)."""
        layout = self.layout()
        if layout is None:
            return True
        layout.invalidate()
        layout.activate()
        for item in entries:
            if not inside[item.key] or item.stretch:
                continue
            widget = item.widget if item.widget is not None else self.widgetForAction(item.action)
            if widget is not None and not widget.isVisibleTo(self):
                return False
        if self._overflowed and not self.more_button.isVisibleTo(self):
            return False
        extension = self.findChild(QToolButton, "qt_toolbar_ext_button")
        return extension is None or not extension.isVisibleTo(self)

    @staticmethod
    def _sync_text(widget: QWidget) -> None:
        """A button whose text the application changed (Start, Capture, Continue): that is its text now."""
        if isinstance(widget, QAbstractButton):
            current = widget.text()
            if current and current != widget.property("full_text"):
                widget.setProperty("full_text", current)

    def _has_text(self, item: ToolItem) -> bool:
        if item.action is not None:
            return bool(item.action.iconText()) and not item.action.icon().isNull()
        widget = item.widget
        self._sync_text(widget)
        return isinstance(widget, QAbstractButton) and not isinstance(widget, QCheckBox) and \
            not widget.icon().isNull() and bool(widget.property("full_text"))

    def relayout(self) -> None:
        """Text, icon or *more* menu for every shown entry, so the bar fits its width: from everything
        with text, step by step - the less important entries first, the rightmost first of each kind."""
        if not self._finished or not self.isVisible() or self.width() <= 0:
            return
        entries = self._entries
        mode = self.mode
        texts = {item.key: (mode == "text" or (mode == "auto" and item.text)) and self._has_text(item)
                 for item in entries}
        inside = {item.key: True for item in entries}
        steps: list[tuple[str, ToolItem]] = []
        for priority in (LOW, NORMAL):
            steps += [("icon", item) for item in reversed(entries) if item.priority == priority and texts[item.key]
                      and not item.pinned]
        for priority in (LOW, NORMAL):
            steps += [("out", item) for item in reversed(entries) if item.priority == priority and not item.pinned]
        steps += [("icon", item) for item in reversed(entries) if item.priority == HIGH and texts[item.key]
                  and not item.pinned]
        steps += [("out", item) for item in reversed(entries) if item.priority == HIGH and not item.pinned]
        steps += [("icon", item) for item in reversed(entries) if item.pinned and texts[item.key]]

        def apply() -> None:
            self._overflowed = [item for item in entries if not inside[item.key]]
            self._place(self._actions(entries, inside))
            for item in entries:
                if inside[item.key]:
                    self._apply(item, texts[item.key])

        def take(kind: str, item: ToolItem, value: bool) -> None:
            (texts if kind == "icon" else inside)[item.key] = value

        apply()
        taken = []
        for kind, item in steps:
            if self._fits(entries, inside):
                break
            take(kind, item, False)
            taken.append((kind, item))
            apply()
        # what went before a wide entry may fit again once that one is in the menu: back, the
        # important first
        for kind, item in reversed(taken[:-1]):
            take(kind, item, True)
            apply()
            if not self._fits(entries, inside):
                take(kind, item, False)
                apply()
        self._fits(entries, inside)  # (laid out as it is now, not as it was tried last)

    def _actions(self, entries: list[ToolItem], inside: dict) -> list[QAction]:
        """The actions of the bar: the entries in it, a separator between groups that have one there
        (none next to a stretch), the *more* button when something is in its menu."""
        actions: list[QAction] = []
        previous = None
        for item in entries:
            if item.stretch:
                actions.append(item.bar_action)
                previous = None
            elif inside[item.key]:
                if previous is not None and item.group != previous:
                    actions.append(self._separator(item.key))
                actions.append(item.bar_action)
                previous = item.group
        if any(not inside[item.key] for item in entries):
            actions.append(self.more_action)
        return actions

    def _apply(self, item: ToolItem, text: bool) -> None:
        widget = item.widget if item.widget is not None else self.widgetForAction(item.action)
        if widget is None:
            return
        if isinstance(widget, QToolButton) and item.action is not None:
            widget.setToolButtonStyle(Qt.ToolButtonTextBesideIcon if text else Qt.ToolButtonIconOnly)
            if not text and not widget.toolTip():
                widget.setToolTip(item.title)
        elif isinstance(widget, QAbstractButton) and not isinstance(widget, QCheckBox) and not widget.icon().isNull():
            full = widget.property("full_text") or ""
            widget.setText(full if text else "")
            if not text and not widget.toolTip():
                widget.setToolTip(full or item.title)
        if isinstance(widget, QAbstractButton) and not widget.accessibleName():
            widget.setAccessibleName(widget.property("full_text") or item.title)  # (also when only its icon shows)

    # ---------------------------------------------------------- more menu
    def overflowed(self) -> list[ToolItem]:
        return list(self._overflowed)

    def _fill_more(self) -> None:
        menu = self.more_menu
        menu.clear()
        group = None
        for item in self._overflowed:
            if group is not None and item.group != group:
                menu.addSeparator()
            group = item.group
            if item.action is not None:
                menu.addAction(item.action)
            elif item.overflow is not None:
                item.overflow(menu)
            else:
                menu_entry(menu, item.widget, item.title)
        menu.addSeparator()
        menu.addAction(icon("gear"), "Customize toolbar...", self.customize)

    # ----------------------------------------------------------- context
    def contextMenuEvent(self, event) -> None:
        menu = self.context_menu()
        menu.exec(event.globalPos())

    def context_menu(self) -> QMenu:
        menu = QMenu(self)
        menu.addAction(icon("gear"), "Customize toolbar...", self.customize)
        modes = QActionGroup(menu)
        for mode, title in MODES.items():
            action = menu.addAction(title)
            action.setCheckable(True)
            action.setChecked(mode == self.mode)
            action.triggered.connect(lambda _checked=False, mode=mode: self.set_mode(mode))
            modes.addAction(action)
        menu.addSeparator()
        menu.addAction(icon("reset"), "Reset toolbar", self.reset)
        return menu

    def customize(self) -> bool:
        dialog = CustomizeDialog(self, self.window())
        if not dialog.exec():
            return False
        order, visible, mode = dialog.arrangement()
        self.set_arrangement(order, visible, mode)
        return True


def menu_entry(menu: QMenu, widget: QWidget, title: str) -> None:
    """What a widget of the bar does, as an entry of a menu."""
    if isinstance(widget, QComboBox):
        sub = menu.addMenu(title)
        for index in range(widget.count()):
            action = sub.addAction(widget.itemIcon(index), widget.itemText(index))
            action.setCheckable(True)
            action.setChecked(index == widget.currentIndex())
            action.triggered.connect(lambda _checked=False, index=index: (widget.setCurrentIndex(index),
                                                                             widget.activated.emit(index)))
        sub.setEnabled(widget.isEnabled())
        return
    if isinstance(widget, QCheckBox):
        action = menu.addAction(widget.text() or title)
        action.setCheckable(True)
        action.setChecked(widget.isChecked())
        action.setEnabled(widget.isEnabled())
        action.toggled.connect(widget.setChecked)
        return
    if isinstance(widget, QAbstractButton):
        button_menu = widget.menu() if isinstance(widget, (QPushButton, QToolButton)) else None
        text = widget.property("full_text") or widget.text() or title
        if button_menu is not None:
            sub = menu.addMenu(widget.icon(), text)
            sub.aboutToShow.connect(lambda: (button_menu.aboutToShow.emit(), _copy_menu(button_menu, sub)))
            sub.setEnabled(widget.isEnabled())
            return
        action = menu.addAction(widget.icon(), text)
        action.setCheckable(widget.isCheckable())
        action.setChecked(widget.isChecked())
        action.setEnabled(widget.isEnabled())
        action.triggered.connect(lambda _checked=False: widget.click())
        return
    if isinstance(widget, QLabel):
        text = widget.text()
        if text:
            action = menu.addAction(text if "<" not in text else widget.toolTip() or title)
            action.setEnabled(False)
        return
    action = menu.addAction(title)
    action.setEnabled(False)


def _copy_menu(source: QMenu, target: QMenu) -> None:
    target.clear()
    for action in source.actions():
        target.addAction(action)


class CustomizeDialog(QDialog):
    """Shows and hides the entries of a bar, orders them, chooses the display."""

    def __init__(self, bar: AdaptiveToolBar, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        from ..dialogs.common import button_box, dialog_layout, hint

        self.bar = bar
        self.setWindowTitle(f"Customize {bar.windowTitle().lower()} toolbar")
        self.resize(420, 480)
        layout = dialog_layout(self)
        layout.addWidget(hint("Checked entries are in the toolbar; drag them, or use the arrows, to order them. "
                              "When the window is narrow, the less important ones show only their icon, then "
                              "move into the menu at the end of the bar.", self))
        row = QHBoxLayout()
        self.list = QListWidget(self)
        self.list.setDragDropMode(QListWidget.InternalMove)
        row.addWidget(self.list, 1)
        buttons = QVBoxLayout()
        self.up_button = QPushButton(self)
        self.up_button.setIcon(icon("up"))
        self.up_button.setToolTip("Move up")
        self.up_button.clicked.connect(lambda: self.move(-1))
        self.down_button = QPushButton(self)
        self.down_button.setIcon(icon("down"))
        self.down_button.setToolTip("Move down")
        self.down_button.clicked.connect(lambda: self.move(1))
        buttons.addWidget(self.up_button)
        buttons.addWidget(self.down_button)
        buttons.addStretch(1)
        row.addLayout(buttons)
        layout.addLayout(row, 1)
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Show", self))
        self.mode_box = QComboBox(self)
        for mode, title in MODES.items():
            self.mode_box.addItem(title, mode)
        mode_row.addWidget(self.mode_box, 1)
        layout.addLayout(mode_row)
        box = button_box(self, "OK")
        reset = box.addButton("Defaults", box.ButtonRole.ResetRole)
        reset.clicked.connect(self.defaults)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)
        self.fill([item.key for item in bar.ordered()], {item.key for item in bar.items if bar.wanted(item)}, bar.mode)

    def fill(self, order: list[str], visible: set[str], mode: str) -> None:
        self.list.clear()
        for key in order:
            item = self.bar.item(key)
            if item is None or not item.configurable:
                continue
            entry = QListWidgetItem(item.title or key, self.list)
            entry.setData(Qt.UserRole, key)
            entry.setFlags(entry.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsDragEnabled)
            entry.setCheckState(Qt.Checked if key in visible else Qt.Unchecked)
            entry.setIcon(item.action.icon() if item.action is not None else
                          (item.widget.icon() if isinstance(item.widget, QAbstractButton) else QIcon()))
        self.mode_box.setCurrentIndex(max(self.mode_box.findData(mode), 0))

    def defaults(self) -> None:
        self.fill([item.key for item in self.bar.items], {item.key for item in self.bar.items if item.default}, "auto")

    def move(self, step: int) -> None:
        row = self.list.currentRow()
        target = row + step
        if row < 0 or not 0 <= target < self.list.count():
            return
        entry = self.list.takeItem(row)
        self.list.insertItem(target, entry)
        self.list.setCurrentRow(target)

    def arrangement(self) -> tuple[list[str], set[str], str]:
        keys = [self.list.item(row).data(Qt.UserRole) for row in range(self.list.count())]
        visible = {self.list.item(row).data(Qt.UserRole) for row in range(self.list.count())
                   if self.list.item(row).checkState() == Qt.Checked}
        # the stretches keep their places between the entries around them
        order: list[str] = []
        configurable = iter(keys)
        for item in self.bar.ordered():
            order.append(item.key if not item.configurable else next(configurable))
        visible |= {item.key for item in self.bar.items if item.stretch}
        return order, visible, self.mode_box.currentData()
