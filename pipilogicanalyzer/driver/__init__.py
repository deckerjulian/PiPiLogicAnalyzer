# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of PiPiLogicAnalyzer 7, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Device drivers for the LogicAnalyzer hardware."""

from .analyzer import PiPiLogicAnalyzerDriver
from .base import (
    AnalyzerDeviceInfo,
    AnalyzerDriverBase,
    AnalyzerDriverType,
    CaptureCompletedArgs,
    CaptureError,
    CaptureLimits,
    CaptureMode,
    DeviceConnectionError,
    parse_version,
)
from .detector import DetectedDevice, detect, list_serial_ports
from .emulated import EmulatedAnalyzerDriver
from .models import AnalyzerChannel, BurstInfo, CaptureSession, TriggerType, build_channels
from .multi import MultiAnalyzerDriver

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
    "DetectedDevice",
    "DeviceConnectionError",
    "EmulatedAnalyzerDriver",
    "PiPiLogicAnalyzerDriver",
    "MultiAnalyzerDriver",
    "TriggerType",
    "build_channels",
    "detect",
    "list_serial_ports",
    "parse_version",
]
