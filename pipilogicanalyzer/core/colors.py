# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of PiPiLogicAnalyzer, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Colour palette of the analyzer (port of ``Classes/AnalyzerColors.cs``)."""

from __future__ import annotations

from PySide6.QtGui import QColor

from ..driver.models import AnalyzerChannel

#: Waveform background: two close tones, so the rows stay apart without stripes
BG_CHANNEL_COLORS = (QColor(28, 28, 31), QColor(31, 31, 35))
#: Line between two channel rows
ROW_SEPARATOR_COLOR = QColor(44, 44, 49)
#: Vertical time grid of the waveform, at the labelled ticks of the ruler and between them
GRID_MAJOR_COLOR = QColor(58, 58, 64)
GRID_MINOR_COLOR = QColor(40, 40, 45)
USER_LINE_COLOR = QColor(90, 209, 230)
TRIGGER_LINE_COLOR = QColor(240, 165, 60)
BURST_LINE_COLOR = QColor(225, 225, 232)
SAMPLE_LINE_COLOR = QColor(52, 52, 58)
SAMPLE_DASH_COLOR = QColor(60, 60, 66, 60)
TEXT_COLOR = QColor(255, 255, 255)
SELECTION_COLOR = QColor(90, 140, 220, 70)
#: Measurement cursors A and B, named markers (bookmarks) and the matches of a search
CURSOR_COLORS = {"A": QColor(98, 196, 255), "B": QColor(255, 128, 196)}
BOOKMARK_COLOR = QColor(140, 214, 120)
SEARCH_HIT_COLOR = QColor(255, 214, 90, 150)
SEARCH_CURRENT_COLOR = QColor(255, 214, 90)
# Regions are drawn on top of the waveforms; keep them translucent enough to
# read the signals through them (the original used an alpha of 128).
DEFAULT_REGION_COLOR = QColor(255, 255, 255, 64)

#: Channel colours of a new capture: 16 distinct hues of similar brightness, softer than pure
#: RGB, which read well on the dark background and next to each other (channel n: entry n % 16).
#: Captures keep the colours stored in their files.
_PALETTE_HEX = (
    "#F2A33A", "#56B6F7", "#6CCB85", "#EF6F78", "#AD8AF0", "#48CCC0", "#F0CF58", "#E685CB",
    "#8AAEFF", "#A2CF62", "#FF9466", "#5FC4E6", "#D8A472", "#B3A5F2", "#76D3A6", "#E3DB78",
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
