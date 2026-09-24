# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of PiPiLogicAnalyzer, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Building blocks shared by the dialogs: button rows, inline messages and banners."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..icons import set_icon
from ..theme import set_role, set_variant

DIALOG_MARGIN = 16
DIALOG_SPACING = 12


def dialog_layout(dialog: QDialog) -> QVBoxLayout:
    """Vertical layout with the margins every dialog uses."""
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(DIALOG_MARGIN, DIALOG_MARGIN, DIALOG_MARGIN, DIALOG_MARGIN)
    layout.setSpacing(DIALOG_SPACING)
    return layout


def button_box(
    dialog: QDialog,
    accept_text: Optional[str] = "OK",
    cancel_text: str = "Cancel",
    variant: Optional[str] = "primary",
    icon: Optional[str] = None,
) -> QDialogButtonBox:
    """Cancel + a named main action (``accept_text=None`` gives a single *Close* button).

    ``accepted``/``rejected`` still have to be connected by the dialog, which
    usually validates before accepting.
    """
    buttons = QDialogButtonBox(dialog)
    if accept_text is None:
        close = buttons.addButton(QDialogButtonBox.Close)
        close.setDefault(True)
        buttons.rejected.connect(dialog.reject)
        return buttons
    accept = buttons.addButton(accept_text, QDialogButtonBox.AcceptRole)
    buttons.addButton(cancel_text, QDialogButtonBox.RejectRole)
    accept.setDefault(True)
    set_variant(accept, variant)
    if icon:
        set_icon(accept, icon)
    return buttons


def accept_button(buttons: QDialogButtonBox) -> Optional[QPushButton]:
    for button in buttons.buttons():
        if buttons.buttonRole(button) == QDialogButtonBox.AcceptRole:
            return button
    return None


def hint(text: str, parent: Optional[QWidget] = None, word_wrap: bool = True) -> QLabel:
    label = QLabel(text, parent)
    label.setWordWrap(word_wrap)
    set_role(label, "hint")
    return label


def heading(text: str, parent: Optional[QWidget] = None) -> QLabel:
    return set_role(QLabel(text, parent), "heading")  # type: ignore[return-value]


class InlineMessage(QLabel):
    """Validation or status text shown inside a dialog instead of a message box."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWordWrap(True)
        self.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.setVisible(False)

    def _show(self, text: str, role: Optional[str]) -> None:
        self.setText(text)
        set_role(self, role)
        self.setVisible(bool(text))

    def show_error(self, text: str) -> None:
        self._show(text, "error")

    def show_success(self, text: str) -> None:
        self._show(text, "success")

    def clear(self) -> None:
        self._show("", None)


class Banner(QFrame):
    """Coloured strip with a message and an optional action button."""

    def __init__(self, kind: str = "info", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        set_role(self, f"banner-{kind}")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 8, 8)
        layout.setSpacing(10)
        self.label = QLabel(self)
        self.label.setWordWrap(True)
        self.label.setTextFormat(Qt.RichText)
        layout.addWidget(self.label, 1)
        self.button = QPushButton(self)
        self.button.setVisible(False)
        layout.addWidget(self.button, 0, Qt.AlignVCenter)

    def set_message(self, text: str, button_text: Optional[str] = None) -> None:
        self.label.setText(text)
        self.button.setText(button_text or "")
        self.button.setVisible(bool(button_text))
        self.setVisible(bool(text))
