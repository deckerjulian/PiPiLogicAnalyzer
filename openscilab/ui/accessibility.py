# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Names for screen readers where the window has none of its own.

Buttons that show only an icon get the text of their tooltip (or of their action) as accessible
name when they are shown – wherever they come from, so new buttons need nothing extra. Painted
views (waveform, flow graph) describe themselves with :func:`describe`.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import QAbstractButton, QApplication, QToolButton, QWidget


def accessible_text(button: QAbstractButton) -> str:
    """What an icon-only button does, in words (empty when nothing says it)."""
    if button.text().strip():
        return ""
    if button.objectName() == "qt_toolbar_ext_button":
        return "More tools"  # the overflow of a toolbar that is too narrow
    text = button.toolTip().strip()
    if not text and isinstance(button, QToolButton) and button.defaultAction() is not None:
        action = button.defaultAction()
        text = (action.toolTip() or action.text()).strip()
    if not text:
        text = str(button.property("iconName") or "").replace("-", " ")
    return text.split("\n", 1)[0].replace("&", "")


def name_button(button: QAbstractButton) -> None:
    if button.accessibleName():
        return
    text = accessible_text(button)
    if text:
        button.setAccessibleName(text)


class AccessibleNames(QObject):
    """Application-wide: names icon-only buttons when they are first shown."""

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt naming
        if event.type() == QEvent.Show and isinstance(watched, QAbstractButton):
            name_button(watched)
        return False


_filter: AccessibleNames | None = None


def install(application: QApplication | None = None) -> None:
    """Name icon-only buttons in every window of ``application`` (once)."""
    global _filter
    application = application or QApplication.instance()
    if application is None or _filter is not None:
        return
    _filter = AccessibleNames(application)
    application.installEventFilter(_filter)


def unnamed_buttons(root: QWidget) -> list[QAbstractButton]:
    """Visible icon-only buttons below ``root`` without an accessible name (for tests)."""
    return [button for button in root.findChildren(QAbstractButton)
            if button.isVisibleTo(root) and not button.text().strip() and not button.icon().isNull()
            and not button.accessibleName()]


def describe(widget: QWidget, name: str, description: str) -> None:
    """Name and state of a painted view (it has no child widgets a screen reader could read)."""
    if widget.accessibleName() != name:
        widget.setAccessibleName(name)
    if widget.accessibleDescription() != description:
        widget.setAccessibleDescription(description)
