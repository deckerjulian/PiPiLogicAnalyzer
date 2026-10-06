# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The most used capture settings in the toolbar: sample rate, length and the trigger.

They edit the stored capture settings of the connected kind of device, the same ones the capture
dialog starts from, so *Start* captures at once and the dialog shows what the toolbar set.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QPushButton, QWidget

from ...core import settings
from ...core.capture_io import session_from_dict, session_to_dict
from ...core.formatting import to_large_frequency, to_small_time
from ...driver.base import ACQUISITION_BUFFER, AnalyzerDriverBase
from ...driver.models import AnalyzerChannel, CaptureSession, EdgeKind, TriggerType
from ..dialogs.capture_dialog import capture_settings_file
from ..icons import set_icon

#: Sample counts offered for the length of a capture
SAMPLE_COUNTS = (
    1_000, 2_000, 5_000, 10_000, 20_000, 50_000, 100_000, 200_000, 500_000,
    1_000_000, 2_000_000, 5_000_000, 10_000_000, 20_000_000, 50_000_000, 100_000_000,
    200_000_000, 500_000_000, 1_000_000_000,
)
#: Share of the samples before the trigger of a new capture
DEFAULT_PRE_TRIGGER_SHARE = 0.1
EDGE_SYMBOLS = {False: "↑", True: "↓"}


def samples_label(count: int) -> str:
    for factor, unit in ((1_000_000_000, "G"), (1_000_000, "M"), (1_000, "k")):
        if count >= factor and count % (factor // 1000 or 1) == 0:
            value = count / factor
            return f"{value:g} {unit}"
    return str(count)


def standard_rates(minimum: int, maximum: int) -> list[int]:
    """1-2-5 steps from ``minimum`` up to ``maximum`` (which is always offered)."""
    rates = []
    decade = 1
    while decade <= maximum:
        for step in (1, 2, 5):
            rate = step * decade
            if minimum <= rate < maximum:
                rates.append(rate)
        decade *= 10
    return rates + [maximum]


def trigger_summary(session: CaptureSession) -> str:
    kind = session.trigger_type
    channel = f"CH{session.trigger_channel + 1}"
    if kind == TriggerType.IMMEDIATE:
        text = "No trigger"
    elif kind in (TriggerType.EDGE, TriggerType.EDGE_OUT):
        text = f"Edge {EDGE_SYMBOLS[session.trigger_inverted]} {channel}"
    elif kind == TriggerType.BLAST:
        text = f"Blast {EDGE_SYMBOLS[session.trigger_inverted]} {channel}"
    elif kind == TriggerType.SEQUENCE:
        stages = len(session.trigger_sequence.stages) if session.trigger_sequence else 0
        text = f"Sequence, {stages} stage{'s' if stages != 1 else ''}"
    else:
        text = f"Pattern {session.trigger_description()} from {channel}"
    return text + (" (software)" if session.software_trigger else "")


def next_session(driver: AnalyzerDriverBase) -> CaptureSession:
    """The settings of the next capture with ``driver``: those stored for it, else all channels at the
    highest rate with a sensible length."""
    data = settings.get_settings(capture_settings_file(driver))
    session = None
    if data:
        try:
            session = session_from_dict(data)
        except (KeyError, TypeError, ValueError):
            session = None
    channels = list(range(driver.channel_count))
    if session is None or not session.capture_channels:
        session = CaptureSession()
        session.capture_channels = [AnalyzerChannel(channel_number=number) for number in channels]
        session.frequency = driver.max_frequency_for(channels, ACQUISITION_BUFFER)
        limits = driver.get_limits(channels, ACQUISITION_BUFFER)
        total = min(1_000_000, limits.max_total_samples)
        session.pre_trigger_samples = max(
            min(int(total * DEFAULT_PRE_TRIGGER_SHARE), limits.max_pre_samples), limits.min_pre_samples
        )
        session.post_trigger_samples = max(total - session.pre_trigger_samples, limits.min_post_samples)
        session.trigger_type = TriggerType.EDGE
        session.clock_edge = EdgeKind.RISING
        if getattr(driver, "is_simulator", False):
            # a simulator's first capture must show something: no waiting for an edge on CH1
            session.trigger_type = TriggerType.IMMEDIATE
            session.post_trigger_samples += session.pre_trigger_samples
            session.pre_trigger_samples = 0
    else:
        # Channels the device does not have (settings of another board)
        session.capture_channels = [
            channel for channel in session.capture_channels if channel.channel_number < driver.channel_count
        ] or [AnalyzerChannel(channel_number=0)]
    return session


class QuickCaptureBar(QWidget):
    """Rate, length and trigger of the next capture for the toolbar."""

    #: The full capture settings were asked for (the trigger button)
    settings_requested = Signal()
    changed = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.driver: Optional[AnalyzerDriverBase] = None
        self.session: Optional[CaptureSession] = None
        self._updating = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.rate_combo = QComboBox(self)
        self.rate_combo.setToolTip("Sample rate")
        self.rate_combo.setMinimumWidth(100)
        self.rate_combo.currentIndexChanged.connect(self._on_rate_changed)
        layout.addWidget(self.rate_combo)

        self.samples_combo = QComboBox(self)
        self.samples_combo.setToolTip("Samples per channel (pre- and post-trigger) and their duration")
        self.samples_combo.setMinimumWidth(120)
        self.samples_combo.currentIndexChanged.connect(self._on_samples_changed)
        layout.addWidget(self.samples_combo)

        self.trigger_button = QPushButton("Trigger", self)
        set_icon(self.trigger_button, "target")
        self.trigger_button.setToolTip("Channels, trigger and every other setting of the capture")
        self.trigger_button.clicked.connect(self.settings_requested.emit)
        layout.addWidget(self.trigger_button)

        self.set_driver(None)

    # ------------------------------------------------------------------ state
    def set_driver(self, driver: Optional[AnalyzerDriverBase]) -> None:
        # every device with digital channels (simulators too); a driver without them has nothing to set
        self.driver = driver if driver is not None and getattr(driver, "channel_count", 0) else None
        self.reload()

    def reload(self) -> None:
        """Reads the stored settings again (after the capture dialog changed them)."""
        self.session = self._stored_session() if self.driver is not None else None
        for widget in (self.rate_combo, self.samples_combo, self.trigger_button):
            widget.setEnabled(self.session is not None)
        self._fill()

    def capture_session(self) -> Optional[CaptureSession]:
        """The settings of the next capture; ``None`` without a device."""
        return self.session.clone_settings() if self.session is not None else None

    def _stored_session(self) -> CaptureSession:
        return next_session(self.driver)

    def _persist(self) -> None:
        if self.driver is not None and self.session is not None:
            settings.persist_settings(
                capture_settings_file(self.driver), session_to_dict(self.session.clone_settings(), include_samples=False)
            )
        self.changed.emit()

    def _channel_numbers(self) -> list[int]:
        return self.session.channel_numbers if self.session else [0]

    def _rates(self) -> list[int]:
        driver, session = self.driver, self.session
        channels = self._channel_numbers()
        fixed = driver.sample_rates(channels, session.acquisition_mode)
        if fixed:
            return sorted(set(fixed))
        maximum = driver.max_frequency_for(channels, session.acquisition_mode)
        return standard_rates(max(driver.min_frequency, 1), maximum)

    def _limits(self):
        session = self.session
        limits = self.driver.get_limits(
            self._channel_numbers(), session.acquisition_mode, to_disk=session.to_disk, continuous=session.continuous
        )
        if session.software_trigger:
            from ...driver.software_trigger import software_trigger_limits

            limits = software_trigger_limits(limits)
        return limits

    # ---------------------------------------------------------------- display
    def _fill(self) -> None:
        self._updating = True
        try:
            self.rate_combo.clear()
            self.samples_combo.clear()
            session = self.session
            if session is None:
                self.rate_combo.addItem("Rate")
                self.samples_combo.addItem("Samples")
                self.trigger_button.setText("Trigger")
                return

            blast = session.trigger_type == TriggerType.BLAST
            rates = [self.driver.blast_frequency] if blast else self._rates()
            if session.frequency not in rates and not blast:
                rates = sorted(set(rates + [session.frequency]))
            for rate in rates:
                self.rate_combo.addItem(to_large_frequency(rate), rate)
            self.rate_combo.setCurrentIndex(max(self.rate_combo.findData(self.driver.blast_frequency if blast else session.frequency), 0))
            self.rate_combo.setEnabled(not blast)

            total = session.pre_trigger_samples + session.post_trigger_samples
            maximum = self._limits().max_total_samples
            counts = [count for count in SAMPLE_COUNTS if count <= maximum]
            if total not in counts:
                counts = sorted(set(counts + [total]))
            frequency = max(session.frequency, 1)
            for count in counts:
                self.samples_combo.addItem(
                    f"{samples_label(count)} · {to_small_time(count / frequency)}", count
                )
            self.samples_combo.setCurrentIndex(max(self.samples_combo.findData(total), 0))
            if session.continuous:
                self.samples_combo.setToolTip("Samples kept of the endless stream (the latest ones)")

            self.trigger_button.setText(trigger_summary(session))
            count = len(session.capture_channels)
            self.trigger_button.setToolTip(
                f"{count} channel{'s' if count != 1 else ''}, {trigger_summary(session)}: click for the "
                "channels, the trigger and every other setting of the capture"
            )
        finally:
            self._updating = False

    # ---------------------------------------------------------------- changes
    def _on_rate_changed(self, index: int) -> None:
        if self._updating or self.session is None or index < 0:
            return
        self.session.frequency = int(self.rate_combo.itemData(index))
        self._persist()
        self._fill()

    def _on_samples_changed(self, index: int) -> None:
        if self._updating or self.session is None or index < 0:
            return
        total = int(self.samples_combo.itemData(index))
        limits = self._limits()
        session = self.session
        old_total = max(session.pre_trigger_samples + session.post_trigger_samples, 1)
        share = session.pre_trigger_samples / old_total
        pre = 0 if session.trigger_type == TriggerType.IMMEDIATE else int(total * share)
        session.pre_trigger_samples = min(max(pre, limits.min_pre_samples), limits.max_pre_samples)
        session.post_trigger_samples = max(total - session.pre_trigger_samples, limits.min_post_samples)
        self._persist()
        self._fill()
