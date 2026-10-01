# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of PiPiLogicAnalyzer, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Device drivers.

:mod:`.base` defines what the application expects of a driver; every kind of device has a
package of its own: :mod:`.pico` (the PiPiLogicAnalyzer firmware on Pico boards) and
:mod:`.dslogic` (DreamSourceLab DSLogic). :mod:`.emulated` stands in for a device while
captures are loaded or computed.
"""

from .base import (
    AnalyzerDeviceInfo,
    AnalyzerDriverBase,
    AnalyzerDriverType,
    CaptureCompletedArgs,
    CaptureError,
    CaptureLimits,
    CaptureMode,
    DeviceConnectionError,
    DeviceSection,
)
from .emulated import EmulatedAnalyzerDriver
from .models import AnalyzerChannel, BurstInfo, CaptureSession, TriggerType, build_channels

__all__ = [
    "AnalyzerChannel",
    "AnalyzerDeviceInfo",
    "AnalyzerDriverBase",
    "AnalyzerDriverType",
    "BurstInfo",
    "CaptureCompletedArgs",
    "CaptureError",
    "CaptureLimits",
    "CaptureMode",
    "CaptureSession",
    "DeviceConnectionError",
    "DeviceSection",
    "EmulatedAnalyzerDriver",
    "TriggerType",
    "build_channels",
]
