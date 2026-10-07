# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of openSciLab, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Line icons for buttons and menu entries.

The icons are small inline SVG drawings on a 24 x 24 grid, rendered in the text
colour of the theme (white on ``primary``/``danger`` buttons) with a separate
pixmap for the disabled state, so they follow the style of the application
without any image files.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Optional

from PySide6.QtCore import QByteArray, QRectF, QSize, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from .theme import ACCENT_HOVER, TEXT, TEXT_DISABLED

ICON_SIZE = QSize(14, 14)

#: SVG bodies; ``{color}`` is replaced for filled shapes.
_SHAPES = {
    "refresh": "<path d='M20 11a8 8 0 1 0-2.3 5.7'/><path d='M20 4v7h-7'/>",
    "plug": "<path d='M9 2v5M15 2v5'/><path d='M6 7h12v4a6 6 0 0 1-12 0z'/><path d='M12 17v5'/>",
    "unplug": (
        "<path d='M9 2v5M15 2v5'/><path d='M6 7h12v4a6 6 0 0 1-12 0z'/><path d='M12 17v5'/>"
        "<path d='M3 3l18 18'/>"
    ),
    "record": "<circle cx='12' cy='12' r='8'/><circle cx='12' cy='12' r='3.5' fill='{color}'/>",
    "repeat": (
        "<path d='M17 2l4 4-4 4'/><path d='M3 11V9a3 3 0 0 1 3-3h15'/>"
        "<path d='M7 22l-4-4 4-4'/><path d='M21 13v2a3 3 0 0 1-3 3H3'/>"
    ),
    "stop": "<rect x='6' y='6' width='12' height='12' rx='1.5' fill='{color}'/>",
    "play": "<path d='M7 4l13 8-13 8z' fill='{color}'/>",
    "folder": "<path d='M3 6a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z'/>",
    "file-plus": (
        "<path d='M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z'/>"
        "<path d='M14 3v6h6'/><path d='M12 12v6M9 15h6'/>"
    ),
    "save": (
        "<path d='M5 3h11l5 5v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z'/>"
        "<path d='M7 3v5h8V3'/><path d='M7 21v-7h10v7'/>"
    ),
    "export": (
        "<path d='M12 3v12'/><path d='M7 8l5-5 5 5'/>"
        "<path d='M5 15v4a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-4'/>"
    ),
    "import": (
        "<path d='M12 3v12'/><path d='M7 10l5 5 5-5'/>"
        "<path d='M5 15v4a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-4'/>"
    ),
    "wave": "<path d='M2 16h4V8h4v8h4V8h4v8h4'/>",
    "plus": "<path d='M12 5v14M5 12h14'/>",
    "layers": "<path d='M12 3l9 5-9 5-9-5z'/><path d='M3 13l9 5 9-5'/>",
    "sliders": (
        "<path d='M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12'/>"
        "<circle cx='16' cy='6' r='2'/><circle cx='10' cy='12' r='2'/><circle cx='18' cy='18' r='2'/>"
    ),
    "trash": (
        "<path d='M4 7h16'/><path d='M10 11v6M14 11v6'/>"
        "<path d='M6 7l1 13a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1l1-13'/><path d='M9 7V4h6v3'/>"
    ),
    "fit": "<path d='M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5'/>",
    "target": "<circle cx='12' cy='12' r='8'/><path d='M12 2v5M12 17v5M2 12h5M17 12h5'/>",
    "chip": (
        "<rect x='6' y='6' width='12' height='12' rx='1.5'/><rect x='10' y='10' width='4' height='4'/>"
        "<path d='M9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4'/>"
    ),
    "power": "<path d='M12 3v9'/><path d='M6.3 6.3a8 8 0 1 0 11.4 0'/>",
    "wifi": (
        "<path d='M2 9a15 15 0 0 1 20 0'/><path d='M5.5 12.5a10 10 0 0 1 13 0'/>"
        "<path d='M9 16a5 5 0 0 1 6 0'/><circle cx='12' cy='19.5' r='1' fill='{color}'/>"
    ),
    "info": "<circle cx='12' cy='12' r='9'/><path d='M12 11v6'/><circle cx='12' cy='7.5' r='1' fill='{color}'/>",
    "checklist": "<path d='M3 6l2 2 3-3M3 13l2 2 3-3M3 20l2 2 3-3'/><path d='M12 7h9M12 14h9M12 21h9'/>",
    "cut": "<circle cx='6' cy='18' r='3'/><circle cx='18' cy='18' r='3'/><path d='M8 16L19 4M16 16L5 4'/>",
    "paste": "<path d='M9 4h6v3H9z'/><path d='M8 5H5v16h14V5h-3'/>",
    "copy": (
        "<rect x='9' y='9' width='12' height='12' rx='2'/>"
        "<path d='M5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1'/>"
    ),
    "check": "<path d='M5 12.5l4.5 4.5L19 7'/>",
    "check-all": "<rect x='3' y='3' width='18' height='18' rx='2'/><path d='M8 12l3 3 5-6'/>",
    "clear": "<path d='M6 6l12 12M18 6L6 18'/>",
    "reset": "<path d='M4 11a8 8 0 1 1 2.3 5.7'/><path d='M4 4v7h7'/>",
    "bookmark": "<path d='M6 3h12v18l-6-4-6 4z'/>",
    "arrow-right": "<path d='M5 12h14M13 6l6 6-6 6'/>",
    "shift": "<path d='M3 12h18'/><path d='M7 8l-4 4 4 4M17 8l4 4-4 4'/>",
    "pencil": "<path d='M4 20h4L19 9l-4-4L4 16z'/><path d='M13 7l4 4'/>",
    "eye": "<path d='M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z'/><circle cx='12' cy='12' r='3'/>",
    "pin": "<path d='M9 3h6l-1 6 3 3v2H7v-2l3-3z'/><path d='M12 14v7'/>",
    "align": "<path d='M4 6h10M4 12h16M4 18h8'/><path d='M18 3v6M18 15v6'/>",
    "search": "<circle cx='10.5' cy='10.5' r='6.5'/><path d='M15.5 15.5L21 21'/>",
    "ruler": "<path d='M3 16l13-13 5 5-13 13z'/><path d='M7 12l2 2M10 9l2 2M13 6l2 2'/>",
    "list": "<path d='M9 6h12M9 12h12M9 18h12'/><path d='M4 6h1M4 12h1M4 18h1'/>",
    "bus": "<path d='M2 12l3-5h14l3 5-3 5H5z'/><path d='M9 7l6 10'/>",
    "chart": "<path d='M3 3v18h18'/><path d='M7 15l4-5 3 3 5-7'/>",
    "histogram": "<path d='M3 21h18'/><path d='M5 21v-6h3v6M10 21V8h3v13M15 21v-9h3v9'/>",
    "compare": "<path d='M8 3v18M16 3v18'/><path d='M3 8h5M16 16h5M3 16h2M19 8h2'/>",
    "gear": (
        "<circle cx='12' cy='12' r='3'/><path d='M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1"
        "M17 17l2.1 2.1M4.9 19.1L7 17M17 7l2.1-2.1'/>"
    ),
    "clock": "<circle cx='12' cy='12' r='9'/><path d='M12 7v5l3 2'/>",
    "up": "<path d='M6 15l6-6 6 6'/>",
    "down": "<path d='M6 9l6 6 6-6'/>",
    "more": "<path d='M5 12h.01M12 12h.01M19 12h.01' stroke-width='3'/>",
    "sequence": "<path d='M3 12h4l2-5 3 10 2-5h7'/>",
    "palette": "<circle cx='12' cy='12' r='9'/><circle cx='8' cy='10' r='1.3'/><circle cx='12' cy='7.5' r='1.3'/>"
               "<circle cx='16' cy='10' r='1.3'/><path d='M12 21a3 3 0 0 1 0-6h2'/>",
    "zoom-in": "<circle cx='10.5' cy='10.5' r='6.5'/><path d='M15.5 15.5L21 21M7.5 10.5h6M10.5 7.5v6'/>",
    "zoom-out": "<circle cx='10.5' cy='10.5' r='6.5'/><path d='M15.5 15.5L21 21M7.5 10.5h6'/>",
    "exit": "<path d='M14 4h5v16h-5'/><path d='M10 8l-4 4 4 4M6 12h10'/>",
    "keyboard": (
        "<rect x='2' y='6' width='20' height='12' rx='2'/>"
        "<path d='M6 10h1M10 10h1M14 10h1M18 10h1M7 14h10'/>"
    ),
    "book": "<path d='M4 4h6a2 2 0 0 1 2 2v14a2 2 0 0 0-2-2H4z'/><path d='M20 4h-6a2 2 0 0 0-2 2v14a2 2 0 0 1 2-2h6z'/>",
    # a panel: the front of an instrument - a dial in a frame
    "panel": "<rect x='2' y='3' width='20' height='18' rx='2'/><path d='M6 15a6 6 0 0 1 12 0'/><path d='M12 15l3-3'/>",
    "rows": "<path d='M4 6h16M4 12h16M4 18h16'/><path d='M2 9v6M22 9v6'/>",
    "channels": "<path d='M3 7h4v-3h4v6h4v-3h6M3 17h6v-3h4v6h8'/>",
    # Shell (openSciLab): activity bar, document area, console
    "home": "<path d='M3 11l9-7 9 7'/><path d='M5 10v10h14V10'/><path d='M10 20v-6h4v6'/>",
    "devices": (
        "<rect x='3' y='4' width='18' height='12' rx='2'/><path d='M8 20h8M12 16v4'/>"
        "<circle cx='8' cy='10' r='1.5' fill='{color}'/>"
    ),
    "nodes": (
        "<rect x='2' y='4' width='7' height='6' rx='1.5'/><rect x='15' y='14' width='7' height='6' rx='1.5'/>"
        "<path d='M9 7h3a2 2 0 0 1 2 2v6a2 2 0 0 0 2 2'/>"
    ),
    "command": "<path d='M4 6l6 6-6 6'/><path d='M12 18h8'/>",
    "split": "<rect x='3' y='4' width='18' height='16' rx='2'/><path d='M12 4v16'/>",
    "detach": "<path d='M14 4h6v6'/><path d='M20 4l-9 9'/><path d='M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5'/>",
    "close": "<path d='M6 6l12 12M18 6L6 18'/>",
    "pause": "<rect x='6' y='5' width='4' height='14' rx='1' fill='{color}'/><rect x='14' y='5' width='4' height='14' rx='1' fill='{color}'/>",
    "terminal": "<rect x='3' y='4' width='18' height='16' rx='2'/><path d='M7 9l3 3-3 3M12 15h5'/>",
    "inspector": "<rect x='3' y='4' width='18' height='16' rx='2'/><path d='M14 4v16M17 9h1M17 12h1'/>",
    "layout": "<rect x='3' y='4' width='18' height='16' rx='2'/><path d='M3 10h18M9 10v10'/>",
}

#: Icon colours (normal, disabled) per button variant of the style sheet.
VARIANT_COLORS = {
    None: (TEXT, TEXT_DISABLED),
    "primary": ("#ffffff", "#7d8796"),
    "danger": ("#ffffff", TEXT_DISABLED),
    "link": (ACCENT_HOVER, TEXT_DISABLED),
    "tool": (TEXT, TEXT_DISABLED),
}


def names() -> list[str]:
    return sorted(_SHAPES)


def _svg(name: str, color: str) -> bytes:
    body = _SHAPES[name].replace("{color}", color)
    return (
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' "
        f"stroke='{color}' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'>"
        f"{body}</svg>"
    ).encode("utf-8")


#: Buttons with text get the gap between icon and text drawn into the icon, as
#: style sheets cannot set the spacing of a push button.
TEXT_GAP = 5
PADDED_ICON_SIZE = QSize(ICON_SIZE.width() + TEXT_GAP, ICON_SIZE.height())


#: Application icon and logo: a lab flask with a digital signal in it and rising bubbles, on a dark
#: rounded square - measuring (the signal) and experimenting (the flask).
APP_ICON_SVG = b"""<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 1024 1024'>
  <defs>
    <linearGradient id='background' x1='0' y1='0' x2='0' y2='1'>
      <stop offset='0' stop-color='#3a3a41'/>
      <stop offset='1' stop-color='#1b1b1f'/>
    </linearGradient>
    <linearGradient id='liquid' x1='0' y1='0' x2='0' y2='1'>
      <stop offset='0' stop-color='#4b8ae6' stop-opacity='0.35'/>
      <stop offset='1' stop-color='#4b8ae6' stop-opacity='0.12'/>
    </linearGradient>
  </defs>
  <rect x='64' y='64' width='896' height='896' rx='200' fill='url(#background)'/>
  <rect x='64' y='64' width='896' height='896' rx='200' fill='none' stroke='#55555e' stroke-width='8'/>
  <path d='M326 640 L262 778 Q246 846 318 846 H706 Q778 846 762 778 L698 640 Z' fill='url(#liquid)'/>
  <path d='M300 660 H372 V572 H456 V660 H540 V572 H624 V660 H724' fill='none' stroke='#7fd18b'
        stroke-width='40' stroke-linecap='round' stroke-linejoin='round'/>
  <path d='M392 196 H632 M432 196 V408 L246 790 Q226 854 300 854 H724 Q798 854 778 790 L592 408 V196'
        fill='none' stroke='#e8ecf2' stroke-width='44' stroke-linecap='round' stroke-linejoin='round'/>
  <circle cx='520' cy='480' r='26' fill='#e8b04b'/>
  <circle cx='568' cy='392' r='17' fill='#e8b04b'/>
  <circle cx='498' cy='318' r='12' fill='#e8b04b'/>
</svg>"""


def render_app_icon(size: int):
    """The application icon as a ``QImage`` of ``size`` x ``size`` pixels."""
    from PySide6.QtGui import QImage

    image = QImage(size, size, QImage.Format_ARGB32)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing)
    QSvgRenderer(QByteArray(APP_ICON_SVG)).render(painter, QRectF(0, 0, size, size))
    painter.end()
    return image


@lru_cache(maxsize=None)
def app_icon() -> QIcon:
    """Window icon of the application."""
    result = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        result.addPixmap(QPixmap.fromImage(render_app_icon(size)))
    return result


def logo_pixmap(size: int, ratio: float = 1.0) -> QPixmap:
    """The logo mark for the header: ``size`` logical pixels on a screen with ``ratio``."""
    pixmap = QPixmap.fromImage(render_app_icon(round(size * ratio)))
    pixmap.setDevicePixelRatio(ratio)
    return pixmap


def _render(name: str, color: str, size: int, padded: bool) -> QPixmap:
    renderer = QSvgRenderer(QByteArray(_svg(name, color)))
    width = round(size * PADDED_ICON_SIZE.width() / ICON_SIZE.width()) if padded else size
    pixmap = QPixmap(width, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    renderer.render(painter, QRectF(0, 0, size, size))
    painter.end()
    return pixmap


@lru_cache(maxsize=None)
def known(name: str, fallback: str = "nodes") -> str:
    """``name`` when it is an icon, else ``fallback`` - for names from elsewhere (a plugin's node)."""
    return name if name in _SHAPES else fallback


def icon(name: str, color: str = TEXT, disabled_color: str = TEXT_DISABLED, padded: bool = False) -> QIcon:
    """The icon ``name`` in ``color`` (a QApplication must exist)."""
    result = QIcon()
    for mode, fill in ((QIcon.Normal, color), (QIcon.Disabled, disabled_color)):
        for size in (16, 32, 48):
            result.addPixmap(_render(name, fill, size, padded), mode)
    return result


def refresh_icon(button) -> None:
    """Redraw the icon of a button after its ``variant`` or text changed."""
    name = button.property("iconName")
    if not name:
        button.setIcon(QIcon())
        return
    normal, disabled = VARIANT_COLORS.get(button.property("variant"), VARIANT_COLORS[None])
    padded = bool(button.text())
    button.setIcon(icon(name, normal, disabled, padded))
    button.setIconSize(PADDED_ICON_SIZE if padded else ICON_SIZE)


def set_icon(button, name: Optional[str]):
    """Give a push or tool button an icon matching its variant."""
    button.setProperty("iconName", name)
    refresh_icon(button)
    return button
