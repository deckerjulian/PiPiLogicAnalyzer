# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of PiPiLogicAnalyzer, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Colour palette of the analyzer (port of ``Classes/AnalyzerColors.cs``)."""

from __future__ import annotations

from typing import Optional

from PySide6.QtGui import QColor

from ..driver.models import AnalyzerChannel

BG_CHANNEL_COLORS = (QColor(36, 36, 36), QColor(28, 28, 28))
USER_LINE_COLOR = QColor(0, 255, 255)
TRIGGER_LINE_COLOR = QColor(255, 255, 255)
BURST_LINE_COLOR = QColor(240, 255, 255)
SAMPLE_LINE_COLOR = QColor(60, 60, 60)
SAMPLE_DASH_COLOR = QColor(60, 60, 60, 60)
ERROR_COLOR = QColor(255, 0, 0)
TEXT_COLOR = QColor(255, 255, 255)
SELECTION_COLOR = QColor(255, 255, 255, 128)
# Regions are drawn on top of the waveforms; keep them translucent enough to
# read the signals through them (the original used an alpha of 128).
DEFAULT_REGION_COLOR = QColor(255, 255, 255, 64)

_PALETTE_HEX = (
    "#FF7333", "#33FF57", "#3357FF", "#FF33A1", "#FFBD33", "#33FFF6", "#BD33FF", "#57FF33",
    "#5733FF", "#33FFBD", "#FF33BD", "#FF5733", "#BDFF33", "#33FF57", "#FF33F6", "#F6FF33",
    "#33FF73", "#FF5733", "#FF33C1", "#33FF85", "#33C1FF", "#C1FF33", "#7333FF", "#FF3385",
    "#3385FF", "#85FF33", "#33FF99", "#9933FF", "#99FF33", "#FF3399", "#FF9C33", "#FF33E7",
    "#E733FF", "#33E7FF", "#FF33C7", "#C733FF", "#FF338E", "#338EFF", "#8EFF33", "#FF338E",
    "#33FF9C", "#FF9C33", "#339CFF", "#FF339C", "#9C33FF", "#FF8E33", "#33E733", "#339CFF",
    "#9CFF33", "#FF339C", "#FF9C33", "#33FF9C", "#FF33E7", "#E7FF33", "#33FFC7", "#C7FF33",
    "#33F6FF", "#FF5733", "#FF33F6", "#F6FF33", "#5733FF", "#33BDFF", "#BD33FF", "#33FFBD",
)

PALETTE: tuple[QColor, ...] = tuple(QColor(value) for value in _PALETTE_HEX)


def get_color(index: int) -> QColor:
    return PALETTE[index % len(PALETTE)]


def get_channel_color(channel: AnalyzerChannel) -> QColor:
    if channel.channel_color is None:
        return get_color(channel.channel_number)
    return color_from_uint(channel.channel_color)


def color_from_uint(value: int) -> QColor:
    """Convert an ARGB integer (the format used by the .lac files) to a colour."""
    return QColor(
        (value >> 16) & 0xFF,
        (value >> 8) & 0xFF,
        value & 0xFF,
        (value >> 24) & 0xFF,
    )


def color_to_uint(color: QColor) -> int:
    return (color.alpha() << 24) | (color.red() << 16) | (color.green() << 8) | color.blue()


def find_contrast(color: QColor) -> QColor:
    """Black or white, whichever reads better on ``color``."""
    yiq = (color.red() * 299 + color.green() * 587 + color.blue() * 114) // 1000
    return QColor(0, 0, 0) if yiq >= 128 else QColor(255, 255, 255)


def optional_color(value: Optional[int]) -> Optional[QColor]:
    return None if value is None else color_from_uint(value)
