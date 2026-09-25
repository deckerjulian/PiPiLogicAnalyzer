# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of PiPiLogicAnalyzer, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Offline driver used for loaded captures and manually created samples.

Port of ``SharedDriver/EmulatedAnalyzerDriver.cs``.  ``blast_frequency`` returns
0 instead of throwing (the original threw ``NotImplementedException``, which
crashed the capture dialog when it queried the property).
"""

from __future__ import annotations

import threading
from typing import Optional, Sequence

from ..core import simulation
from .base import (
    CAPABILITY_SIMULATION,
    AnalyzerDriverBase,
    AnalyzerDriverType,
    CaptureCompletedArgs,
    CaptureCompletedHandler,
    CaptureError,
    CaptureLimits,
    CaptureMode,
)
from .models import CaptureSession, TriggerType

CHANNELS_PER_DEVICE = 24


class EmulatedAnalyzerDriver(AnalyzerDriverBase):
    def __init__(self, device_count: int = 5) -> None:
        super().__init__()
        self._device_count = max(1, device_count)
        self._channel_count = self._device_count * CHANNELS_PER_DEVICE
        self._version = f"EMULATED_ANALYZER_{self._device_count}_DEVICES"

    @property
    def device_version(self) -> Optional[str]:
        return self._version

    @property
    def max_frequency(self) -> int:
        return 200_000_000

    @property
    def blast_frequency(self) -> int:
        return 0

    @property
    def channel_count(self) -> int:
        return self._channel_count

    @property
    def buffer_size(self) -> int:
        return 1024 * 1024

    @property
    def driver_type(self) -> AnalyzerDriverType:
        return AnalyzerDriverType.EMULATED

    @property
    def is_network(self) -> bool:
        return False

    @property
    def is_capturing(self) -> bool:
        return False

    def capabilities(self) -> frozenset[str]:
        return frozenset({CAPABILITY_SIMULATION})

    def start_capture(
        self, session: CaptureSession, completed_handler: Optional[CaptureCompletedHandler] = None
    ) -> CaptureError:
        """Only simulated captures: the same test signals the firmware generates."""
        if session.trigger_type != TriggerType.SIMULATION:
            return CaptureError.HARDWARE_ERROR

        total = session.pre_trigger_samples + session.post_trigger_samples
        if (
            not session.capture_channels
            or session.loop_count
            or total <= 0
            or session.frequency <= 0
            or not 0 <= session.trigger_pattern < len(simulation.SimulationPattern)
        ):
            return CaptureError.BAD_PARAMS

        levels = simulation.generate(session.trigger_pattern, len(session.capture_channels), total)
        for channel, samples in zip(session.capture_channels, levels):
            channel.samples = samples
        session.bursts = None

        # Complete asynchronously, like a device does.
        threading.Thread(
            target=self._raise_capture_completed,
            args=(CaptureCompletedArgs(success=True, session=session), completed_handler),
            name="pipilogicanalyzer-simulation",
            daemon=True,
        ).start()
        return CaptureError.NONE

    def stop_capture(self) -> bool:
        return False

    def enter_bootloader(self) -> bool:
        return False

    def _split_channels_per_device(self, channels: Sequence[int]) -> list[list[int]]:
        return [
            [c - device * CHANNELS_PER_DEVICE
             for c in channels
             if device * CHANNELS_PER_DEVICE <= c < (device + 1) * CHANNELS_PER_DEVICE]
            for device in range(self._device_count)
        ]

    def get_capture_mode(self, channels: Sequence[int]) -> CaptureMode:
        split = self._split_channels_per_device(channels)
        max_channel = max((max(group) for group in split if group), default=0)
        if max_channel < 8:
            return CaptureMode.CHANNELS_8
        if max_channel < 16:
            return CaptureMode.CHANNELS_16
        return CaptureMode.CHANNELS_24

    def get_limits(self, channels: Sequence[int], acquisition_mode: Optional[str] = None) -> CaptureLimits:
        split = self._split_channels_per_device(channels)
        limits = [AnalyzerDriverBase.get_limits(self, group) for group in split]
        return CaptureLimits(
            min_pre_samples=max(limit.min_pre_samples for limit in limits),
            max_pre_samples=min(limit.max_pre_samples for limit in limits),
            min_post_samples=max(limit.min_post_samples for limit in limits),
            max_post_samples=min(limit.max_post_samples for limit in limits),
        )
