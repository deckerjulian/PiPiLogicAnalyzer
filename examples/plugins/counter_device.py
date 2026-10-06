# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""An example plugin: a device that is not built in (``docs/drivers.md``, *Plugins*).

Copy this file into the ``plugins`` folder of the settings directory (``openscilab-cli plugins``
names it) or point ``OPENSCILAB_PLUGINS`` at this folder, then start openSciLab: the device list
shows *Counter (8 channels)*, flows open it with ``address: counter:8`` and the command line with
``openscilab-cli capture -d counter:8 ...``.

The "device" counts: channel ``n`` is bit ``n`` of the sample number. A real driver talks to its
hardware in ``start_capture`` (a thread of its own, the samples into the session's channels, then
``_raise_capture_completed``); everything else is the same.
"""

from __future__ import annotations

import threading
from typing import Optional

import numpy as np

from openscilab.driver import kinds
from openscilab.driver.base import (
    CAPABILITY_IMMEDIATE_TRIGGER,
    AnalyzerDriverBase,
    CaptureCompletedArgs,
    CaptureError,
    DeviceConnectionError,
)


class CounterDriver(AnalyzerDriverBase):
    """A logic analyzer whose channels show a binary counter."""

    def __init__(self, channels: int = 8) -> None:
        super().__init__()
        self._channels = channels
        self._capturing = False

    # ------------------------------------------------------------ what it is
    @property
    def driver_id(self) -> str:
        return "counter"

    @property
    def device_version(self) -> Optional[str]:
        return f"Counter ({self._channels} channels)"

    @property
    def max_frequency(self) -> int:
        return 100_000_000

    @property
    def channel_count(self) -> int:
        return self._channels

    @property
    def buffer_size(self) -> int:
        return 4_000_000

    def capabilities(self) -> frozenset[str]:
        return frozenset({CAPABILITY_IMMEDIATE_TRIGGER})

    # --------------------------------------------------------------- capture
    @property
    def is_capturing(self) -> bool:
        return self._capturing

    def start_capture(self, session, completed_handler=None) -> CaptureError:
        total = session.pre_trigger_samples + session.post_trigger_samples
        if not session.capture_channels or total <= 0 or total > self.buffer_size:
            return CaptureError.BAD_PARAMS
        self._capturing = True
        threading.Thread(target=self._capture, args=(session, total, completed_handler), daemon=True,
                         name="counter-capture").start()
        return CaptureError.NONE

    def _capture(self, session, total: int, completed_handler) -> None:
        numbers = np.arange(total, dtype=np.uint32)
        for channel in session.capture_channels:
            channel.samples = ((numbers >> channel.channel_number) & 1).astype(np.uint8)
        self._capturing = False
        self._raise_capture_completed(CaptureCompletedArgs(success=True, session=session), completed_handler)

    def stop_capture(self) -> bool:
        return False


def open_counter(rest: str) -> CounterDriver:
    """``counter:<channels>`` (``counter:8``)."""
    try:
        channels = int(rest or 8)
    except ValueError:
        raise DeviceConnectionError(f"counter:{rest}: write the number of channels, e.g. counter:8") from None
    if not 1 <= channels <= 24:
        raise DeviceConnectionError("A counter has 1 to 24 channels.")
    return CounterDriver(channels)


def find_counters() -> list[tuple[str, str]]:
    """The connected devices: (the rest of their address, a label). A real device looks for its USB
    ports here (``openscilab.driver.ports``)."""
    return [("8", "Counter (8 channels)")]


# what the plugin adds: the kind "counter"; process=True would read it in a device process of its
# own (for devices on USB that stream)
kinds.register("counter", open_counter, title="Counter", detect=find_counters, simulation="free")
