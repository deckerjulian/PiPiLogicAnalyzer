# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""A monitor recorded as a slow capture: one sample per report of the monitor."""

from __future__ import annotations

import threading
from typing import Optional

import numpy as np

from ..driver.models import AnalogChannel, AnalyzerChannel, CaptureSession
from .instrument import MonitorState

#: Scale of the recorded voltages: ±10 V in ``int16``
VOLT_SCALE = 10.0 / 32767


class MonitorRecording:
    """Collects :class:`MonitorState` reports (from any thread) into a :class:`CaptureSession`.

    The session's rate is the monitor's rate; a report is one sample of every pin.
    """

    def __init__(self, rate: float, pins: list[str], analog: tuple[str, ...] = ()) -> None:
        self.rate = max(int(round(rate)), 1)
        self.pins = list(pins)
        self.analog = tuple(analog)
        self._lock = threading.Lock()
        self._digital: list[list[int]] = [[] for _ in self.pins]
        self._volts: list[list[float]] = [[] for _ in self.analog]
        self.times: list[float] = []

    def add(self, state: MonitorState) -> None:
        with self._lock:
            self.times.append(state.time)
            for index, pin in enumerate(self.pins):
                self._digital[index].append(int(state.digital.get(pin, 0)))
            for index, pin in enumerate(self.analog):
                self._volts[index].append(float(state.analog.get(pin, 0.0)))

    @property
    def count(self) -> int:
        with self._lock:
            return len(self.times)

    def sample_at(self, time_: float) -> int:
        """The sample recorded closest to the instrument time ``time_`` (seconds)."""
        with self._lock:
            if not self.times:
                return 0
            return int(min(np.searchsorted(self.times, time_), len(self.times) - 1))

    def session(self, into: Optional[CaptureSession] = None) -> CaptureSession:
        """The recording so far; ``into`` refreshes the channels of an earlier result in place."""
        with self._lock:
            count = len(self.times)
            digital = [np.asarray(values, dtype=np.uint8) for values in self._digital]
            volts = [np.asarray(values, dtype=np.float64) for values in self._volts]
        session = into
        if session is None:
            session = CaptureSession(frequency=self.rate, pre_trigger_samples=0, post_trigger_samples=count)
            session.capture_channels = [AnalyzerChannel(channel_number=index, channel_name=name)
                                        for index, name in enumerate(self.pins)]
            session.analog_channels = [AnalogChannel(channel_number=index, channel_name=name, scale=VOLT_SCALE)
                                       for index, name in enumerate(self.analog)]
        session.post_trigger_samples = count
        for channel, samples in zip(session.capture_channels, digital):
            channel.samples = samples
        for channel, values in zip(session.analog_channels, volts):
            channel.raw = channel.to_raw(values)
            channel.length = None
        return session
