# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of PiPiLogicAnalyzer 7, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Message boxes with a clear title, a short headline and explicit button labels.

Every window asks and reports through these functions instead of the static
``QMessageBox`` helpers, so questions name the action ("Delete profile") rather
than offering *Yes*/*No*, and the tests have a single place to answer them.
"""

from __future__ import annotations

from typing import Optional, Sequence

from PySide6.QtWidgets import QMessageBox, QPushButton, QWidget

from .theme import set_variant


def _box(
    parent: Optional[QWidget], icon: QMessageBox.Icon, title: str, text: str, details: Optional[str]
) -> QMessageBox:
    box = QMessageBox(parent)
    box.setIcon(icon)
    box.setWindowTitle(title)
    box.setText(text)
    if details:
        box.setInformativeText(details)
    return box


def _exec(box: QMessageBox) -> Optional[QPushButton]:
    """Show the box and return the button that closed it (patched by the tests)."""
    box.exec()
    return box.clickedButton()


def error(parent: Optional[QWidget], title: str, text: str, details: Optional[str] = None) -> None:
    box = _box(parent, QMessageBox.Critical, title, text, details)
    box.addButton(QMessageBox.Close)
    _exec(box)


def warning(parent: Optional[QWidget], title: str, text: str, details: Optional[str] = None) -> None:
    box = _box(parent, QMessageBox.Warning, title, text, details)
    box.addButton(QMessageBox.Ok)
    _exec(box)


def info(parent: Optional[QWidget], title: str, text: str, details: Optional[str] = None) -> None:
    box = _box(parent, QMessageBox.Information, title, text, details)
    box.addButton(QMessageBox.Ok)
    _exec(box)


def confirm(
    parent: Optional[QWidget],
    title: str,
    text: str,
    action: str,
    details: Optional[str] = None,
    destructive: bool = False,
) -> bool:
    """Ask before doing ``action``; destructive actions default to *Cancel*."""
    box = _box(parent, QMessageBox.Warning if destructive else QMessageBox.Question, title, text, details)
    accept = box.addButton(action, QMessageBox.AcceptRole)
    cancel = box.addButton(QMessageBox.Cancel)
    set_variant(accept, "danger" if destructive else "primary")
    box.setDefaultButton(cancel if destructive else accept)
    box.setEscapeButton(cancel)
    return _exec(box) is accept


def choose(
    parent: Optional[QWidget],
    title: str,
    text: str,
    options: Sequence[str],
    details: Optional[str] = None,
) -> Optional[int]:
    """Let the user pick one of ``options``; returns its index or ``None`` when cancelled."""
    box = _box(parent, QMessageBox.Question, title, text, details)
    buttons = [box.addButton(option, QMessageBox.AcceptRole) for option in options]
    cancel = box.addButton(QMessageBox.Cancel)
    if buttons:
        set_variant(buttons[0], "primary")
        box.setDefaultButton(buttons[0])
    box.setEscapeButton(cancel)
    clicked = _exec(box)
    return buttons.index(clicked) if clicked in buttons else None
