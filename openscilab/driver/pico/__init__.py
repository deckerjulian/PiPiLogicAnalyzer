# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of openSciLab, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Raspberry Pi Pico boards (RP2040/RP2350) with the openSciLab Pico firmware, the hardware of
gusmanb's LogicAnalyzer: single boards over USB or WiFi, and multi device sets."""

from .analyzer import PicoDriver
from .detector import DetectedDevice, detect, list_serial_ports
from .multi import MultiAnalyzerDriver
from .protocol import parse_version

__all__ = [
    "DetectedDevice",
    "MultiAnalyzerDriver",
    "PicoDriver",
    "detect",
    "list_serial_ports",
    "parse_version",
]
