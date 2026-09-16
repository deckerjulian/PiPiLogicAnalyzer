# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of PiPiLogicAnalyzer, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Dark theme used by every window of the application.

Widgets opt into semantic styles through dynamic properties instead of inline
colours, so every window looks the same:

* ``role`` on labels: ``hint``, ``heading``, ``title``, ``error``, ``warning``,
  ``success`` and the ``chip-*`` badges; on frames: ``banner-info``,
  ``banner-warning``, ``banner-error``, ``card`` and ``toolbar-group``;
* ``variant`` on buttons: ``primary`` (the main action of a window) and
  ``danger`` (stops or destroys something).

Use :func:`set_role` / :func:`set_variant` to change them after the widget is shown.
"""

from __future__ import annotations

import os
import tempfile
from typing import Optional

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QWidget

from .. import __version__

BACKGROUND = "#303033"
PANEL = "#242427"
PANEL_LIGHT = "#2b2b2f"
INPUT = "#1f1f22"
BORDER = "#46464c"
BORDER_STRONG = "#5c5c63"
TEXT = "#e8e8ea"
TEXT_MUTED = "#a3a3ab"
TEXT_DISABLED = "#6f6f76"
ACCENT = "#3b7ddd"
ACCENT_HOVER = "#4b8ae6"
ACCENT_PRESSED = "#2f6cc4"
SELECTION = "#35609a"
DANGER = "#c14848"
DANGER_HOVER = "#d05656"
SUCCESS = "#7fd18b"
WARNING = "#e8b04b"
ERROR = "#ff7f7f"

_CHECK_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' width='14' height='14' viewBox='0 0 14 14'>"
    "<path d='M3 7.2l2.6 2.6L11 4.4' fill='none' stroke='#ffffff' stroke-width='2' "
    "stroke-linecap='round' stroke-linejoin='round'/></svg>"
)
_ARROW_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' width='10' height='6' viewBox='0 0 10 6'>"
    f"<path d='M1 1l4 4 4-4' fill='none' stroke='{TEXT_MUTED}' stroke-width='1.6' "
    "stroke-linecap='round' stroke-linejoin='round'/></svg>"
)


def _asset(name: str, content: str) -> Optional[str]:
    """Write a small SVG next to the other theme assets; Qt style sheets only load files."""
    directory = os.path.join(tempfile.gettempdir(), f"pipilogicanalyzer-theme-{__version__}")
    path = os.path.join(directory, name)
    try:
        os.makedirs(directory, exist_ok=True)
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(content)
    except OSError:
        return None
    return path.replace("\\", "/")


def _image_rule(path: Optional[str]) -> str:
    return f"image: url({path});" if path else ""


def build_stylesheet() -> str:
    check = _image_rule(_asset("check.svg", _CHECK_SVG))
    arrow = _image_rule(_asset("arrow-down.svg", _ARROW_SVG))
    return f"""
QWidget {{
    background-color: {BACKGROUND};
    color: {TEXT};
    font-size: 12px;
}}
QMainWindow, QDialog {{
    background-color: {BACKGROUND};
}}
QLabel, QCheckBox, QRadioButton {{
    background-color: transparent;
}}
QLabel:disabled, QCheckBox:disabled, QRadioButton:disabled {{
    color: {TEXT_DISABLED};
}}

/* ------------------------------------------------------------ text roles */
QLabel[role="hint"] {{ color: {TEXT_MUTED}; }}
QLabel[role="heading"] {{ font-size: 13px; font-weight: 600; }}
QLabel[role="title"] {{ font-size: 20px; font-weight: 600; }}
QLabel[role="section"] {{ color: {TEXT_MUTED}; font-size: 11px; font-weight: 600; }}
QLabel[role="error"] {{ color: {ERROR}; }}
QLabel[role="warning"] {{ color: {WARNING}; }}
QLabel[role="success"] {{ color: {SUCCESS}; }}
QLabel[role^="chip"] {{
    border-radius: 9px;
    padding: 2px 9px;
    font-weight: 600;
}}
QLabel[role="chip-ok"] {{ background-color: #1f5a2b; color: #d6f5dc; }}
QLabel[role="chip-medium"] {{ background-color: #6b4a14; color: #fbe6c2; }}
QLabel[role="chip-high"] {{ background-color: #7a2323; color: #ffd9d9; }}
QLabel[role="chip-neutral"] {{ background-color: {PANEL_LIGHT}; color: {TEXT_MUTED}; border: 1px solid {BORDER}; }}

/* --------------------------------------------------------------- frames */
QFrame[role="card"] {{
    background-color: {PANEL_LIGHT};
    border: 1px solid {BORDER};
    border-radius: 6px;
}}
QFrame[role^="banner"] {{
    border-radius: 5px;
    border: 1px solid {BORDER};
}}
QFrame[role="banner-info"] {{ background-color: #26344a; border-color: #3b5577; }}
QFrame[role="banner-warning"] {{ background-color: #3d331f; border-color: #6e5626; }}
QFrame[role="banner-error"] {{ background-color: #432626; border-color: #7a3838; }}
QFrame[role^="banner"] QLabel {{ background-color: transparent; }}
QFrame[role="card"] QLabel {{ background-color: transparent; }}

/* ------------------------------------------------------------ menus/bars */
QMenuBar {{
    background-color: {PANEL};
    color: {TEXT};
    border-bottom: 1px solid {BORDER};
}}
QMenuBar::item {{
    padding: 4px 10px;
    background: transparent;
}}
QMenuBar::item:selected {{
    background-color: {PANEL_LIGHT};
}}
QMenu {{
    background-color: {PANEL};
    border: 1px solid {BORDER};
    padding: 4px 0;
}}
QMenu::item {{
    padding: 5px 24px 5px 20px;
}}
QMenu::item:selected {{
    background-color: {SELECTION};
}}
QMenu::item:disabled {{
    color: {TEXT_DISABLED};
}}
QMenu::separator {{
    height: 1px;
    background: {BORDER};
    margin: 4px 8px;
}}
QToolBar {{
    background-color: {PANEL};
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 5px 8px;
    spacing: 6px;
}}
QToolBar QWidget {{
    background-color: transparent;
}}
QToolBar::separator {{
    width: 1px;
    background: {BORDER};
    margin: 4px 6px;
}}
QStatusBar {{
    background-color: {PANEL};
    border-top: 1px solid {BORDER};
    color: {TEXT_MUTED};
}}
QStatusBar QLabel {{
    color: {TEXT_MUTED};
    padding: 0 6px;
}}
QToolTip {{
    background-color: {PANEL};
    color: {TEXT};
    border: 1px solid {BORDER};
    padding: 4px;
}}

/* -------------------------------------------------------------- buttons */
QPushButton, QToolButton {{
    background-color: {PANEL_LIGHT};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 4px 12px;
    min-height: 20px;
}}
QToolButton {{
    padding: 3px 6px;
}}
QPushButton:hover:!disabled, QToolButton:hover:!disabled {{
    background-color: #36363b;
    border-color: {BORDER_STRONG};
}}
QPushButton:pressed, QToolButton:pressed {{
    background-color: #404046;
}}
QPushButton:disabled, QToolButton:disabled {{
    color: {TEXT_DISABLED};
    border-color: #3a3a3f;
}}
QPushButton:focus, QToolButton:focus {{
    border-color: {ACCENT};
}}
QPushButton::menu-indicator {{
    subcontrol-origin: padding;
    subcontrol-position: center right;
    right: 6px;
    {arrow}
}}
QPushButton[variant="primary"] {{
    background-color: {ACCENT};
    border-color: {ACCENT};
    color: #ffffff;
    font-weight: 600;
}}
QPushButton[variant="primary"]:hover:!disabled {{
    background-color: {ACCENT_HOVER};
    border-color: {ACCENT_HOVER};
}}
QPushButton[variant="primary"]:pressed {{
    background-color: {ACCENT_PRESSED};
}}
QPushButton[variant="primary"]:disabled {{
    background-color: #2d3b50;
    border-color: #2d3b50;
    color: #7d8796;
}}
QPushButton[variant="danger"] {{
    background-color: {DANGER};
    border-color: {DANGER};
    color: #ffffff;
    font-weight: 600;
}}
QPushButton[variant="danger"]:hover:!disabled {{
    background-color: {DANGER_HOVER};
    border-color: {DANGER_HOVER};
}}
QPushButton[variant="danger"]:disabled {{
    background-color: {PANEL_LIGHT};
    border-color: #3a3a3f;
    color: {TEXT_DISABLED};
}}
QPushButton[variant="link"] {{
    background: transparent;
    border: none;
    color: {ACCENT_HOVER};
    padding: 2px 4px;
}}
QPushButton[variant="link"]:hover:!disabled {{
    background: transparent;
    text-decoration: underline;
}}

/* --------------------------------------------------------------- inputs */
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QTextEdit {{
    background-color: {INPUT};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 3px 6px;
    selection-background-color: {SELECTION};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus,
QPlainTextEdit:focus, QTextEdit:focus {{
    border-color: {ACCENT};
}}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {{
    color: {TEXT_DISABLED};
    background-color: {PANEL_LIGHT};
}}
QComboBox::drop-down {{
    border: none;
    width: 20px;
}}
QComboBox::down-arrow {{
    {arrow}
}}
QComboBox QAbstractItemView {{
    background-color: {PANEL};
    border: 1px solid {BORDER};
    selection-background-color: {SELECTION};
    outline: none;
}}
QListWidget, QTreeWidget, QTableView {{
    background-color: {INPUT};
    border: 1px solid {BORDER};
    border-radius: 4px;
    selection-background-color: {SELECTION};
    alternate-background-color: {PANEL_LIGHT};
    outline: none;
}}
QListWidget::item {{
    padding: 4px 6px;
}}
QListWidget::item:selected, QTreeWidget::item:selected, QTableView::item:selected {{
    background-color: {SELECTION};
    color: #ffffff;
}}
QTreeWidget::item {{
    padding: 2px 0;
}}
QTableView {{
    gridline-color: {BORDER};
}}
QHeaderView::section {{
    background-color: {PANEL};
    color: {TEXT_MUTED};
    border: none;
    border-right: 1px solid {BORDER};
    border-bottom: 1px solid {BORDER};
    padding: 4px 6px;
    font-weight: 600;
}}
QCheckBox, QRadioButton {{
    spacing: 6px;
}}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 14px;
    height: 14px;
    background-color: {INPUT};
    border: 1px solid {BORDER_STRONG};
}}
QCheckBox::indicator {{
    border-radius: 3px;
}}
QRadioButton::indicator {{
    border-radius: 8px;
}}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{
    border-color: {ACCENT};
}}
QCheckBox::indicator:checked {{
    background-color: {ACCENT};
    border-color: {ACCENT};
    {check}
}}
QRadioButton::indicator:checked {{
    background-color: qradialgradient(cx:0.5, cy:0.5, radius:0.5, fx:0.5, fy:0.5,
        stop:0 #ffffff, stop:0.38 #ffffff, stop:0.48 {ACCENT}, stop:1 {ACCENT});
    border-color: {ACCENT};
}}
QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{
    background-color: {PANEL_LIGHT};
    border-color: #3a3a3f;
}}

/* ----------------------------------------------------------- containers */
QGroupBox {{
    border: 1px solid {BORDER};
    border-radius: 6px;
    margin-top: 14px;
    padding: 10px 8px 8px 8px;
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
    color: {TEXT};
}}
QGroupBox > QWidget {{
    font-weight: normal;
}}
QScrollArea {{
    border: none;
}}
QScrollBar:horizontal {{
    background: {PANEL};
    height: 12px;
    margin: 0;
}}
QScrollBar:vertical {{
    background: {PANEL};
    width: 12px;
    margin: 0;
}}
QScrollBar::handle {{
    background: #55555c;
    border-radius: 4px;
    min-width: 28px;
    min-height: 28px;
    margin: 2px;
}}
QScrollBar::handle:hover {{
    background: #6c6c74;
}}
QScrollBar::add-line, QScrollBar::sub-line {{
    width: 0;
    height: 0;
}}
QScrollBar::add-page, QScrollBar::sub-page {{
    background: none;
}}
QSlider::groove:horizontal {{
    height: 4px;
    background: {BORDER};
    border-radius: 2px;
}}
QSlider::sub-page:horizontal {{
    background: {ACCENT};
    border-radius: 2px;
}}
QSlider::handle:horizontal {{
    background: #e6e6e6;
    width: 14px;
    margin: -5px 0;
    border-radius: 7px;
}}
QSplitter::handle {{
    background-color: {BORDER};
}}
QSplitter::handle:horizontal {{
    width: 1px;
}}
QTabWidget::pane {{
    border: 1px solid {BORDER};
    border-radius: 4px;
    top: -1px;
}}
QTabBar::tab {{
    background: {PANEL};
    padding: 6px 14px;
    border: 1px solid {BORDER};
    border-bottom: none;
    border-top-left-radius: 4px;
    border-top-right-radius: 4px;
    margin-right: 2px;
    color: {TEXT_MUTED};
}}
QTabBar::tab:selected {{
    background: {BACKGROUND};
    color: {TEXT};
}}
QProgressBar {{
    background-color: {INPUT};
    border: 1px solid {BORDER};
    border-radius: 4px;
    text-align: center;
}}
QProgressBar::chunk {{
    background-color: {ACCENT};
    border-radius: 3px;
}}
QMessageBox QLabel {{
    min-width: 320px;
}}
"""


STYLESHEET = build_stylesheet()


def _repolish(widget: QWidget) -> None:
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def apply_palette(target) -> None:
    """Colours the style sheet cannot set: links in rich text labels."""
    palette = target.palette()
    for group in (QPalette.Active, QPalette.Inactive, QPalette.Disabled):
        palette.setColor(group, QPalette.Link, QColor(ACCENT_HOVER))
        palette.setColor(group, QPalette.LinkVisited, QColor(ACCENT_HOVER))
    target.setPalette(palette)


def set_role(widget: QWidget, role: Optional[str]) -> QWidget:
    """Give a label or frame one of the semantic roles of the style sheet."""
    if widget.property("role") != role:
        widget.setProperty("role", role)
        _repolish(widget)
    return widget


def set_variant(button: QWidget, variant: Optional[str]) -> QWidget:
    """Mark a button as ``primary``, ``danger`` or ``link`` (``None`` for a normal button)."""
    if button.property("variant") != variant:
        button.setProperty("variant", variant)
        _repolish(button)
        if button.property("iconName"):
            from .icons import refresh_icon  # icons import the theme colours

            refresh_icon(button)
    return button


JITTER_LOW = QColor(27, 128, 5)
JITTER_MEDIUM = QColor(163, 81, 10)
JITTER_HIGH = QColor(184, 0, 0)
