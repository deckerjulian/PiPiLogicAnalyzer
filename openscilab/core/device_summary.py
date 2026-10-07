# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""A connected device in a few lines: what it does, how it is connected, the settings of its next
capture and how much of the device they use - for the device list of the sidebar.

*Load* is how close the settings come to what the device can: a capture into the memory of the
device uses part of that memory (its samples of the most it holds with these channels), a stream
part of the link (its rate of the highest rate the link carries for these channels); while a
capture arrives, how much of it arrived. A device in a process of its own adds what that process
takes of a processor core. Qt free.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from . import units
from .instrument import Instrument, InstrumentStatus

log = logging.getLogger(__name__)

#: the kinds of :attr:`DeviceSummary.level`: what colour the state has
LEVELS = ("idle", "armed", "busy", "error", "off")


@dataclass
class DeviceSummary:
    #: what the device does now, short (``Ready``, ``Armed``, ``Receiving 45 %``, ``Disconnected``)
    state: str
    #: one of :data:`LEVELS`
    level: str
    #: how it is connected (``sim:pico · simulated``)
    connection: str
    #: the settings of the next capture (``100 MHz · 32,768 samples · 24 ch · trigger: none``), or
    #: what the device offers besides captures (``GPIO · monitor 20/s · generator``)
    settings: str
    #: 0..1, ``None``: nothing to say
    load: Optional[float] = None
    #: what :attr:`load` is (``memory 25 %``, ``link 40 %``, ``received 45 %``)
    load_text: str = ""
    #: ``"memory"``, ``"link"`` (a stream close to the link's limit may overflow) or ``"received"``
    load_kind: str = ""
    #: the share of a processor core its device process takes (``None``: not in a process of its own)
    process_load: Optional[float] = None

    @property
    def load_line(self) -> str:
        parts = [self.load_text] if self.load_text else []
        if self.process_load is not None:
            parts.append(f"process {self.process_load * 100:.0f} % CPU")
        return " · ".join(parts)


def settings_line(session: Any) -> str:
    """The settings of a capture in a line: rate, samples, channels, trigger, mode."""
    parts = [units.format_quantity(session.frequency, "Hz"),
             f"{session.pre_trigger_samples + session.post_trigger_samples:,} samples",
             f"{len(session.capture_channels)} ch"]
    if session.analog_channels:
        parts.append(f"{len(session.analog_channels)} analog")
    parts.append(f"trigger: {session.trigger_type.label.lower()}")
    if getattr(session, "acquisition_mode", "") == "stream":
        parts.append("stream")
    return " · ".join(parts)


class LimitCache:
    """What a device can, asked once per set of channels and mode (a device in a process of its
    own answers through a pipe: not on every refresh of the list)."""

    def __init__(self) -> None:
        self._values: dict = {}

    def capacity(self, driver: Any, session: Any) -> Optional[tuple[str, float]]:
        """``("memory", most samples)`` of a capture into the device, ``("link", highest rate)``
        of a stream, for the channels of ``session``; ``None`` when the device cannot tell."""
        channels = tuple(sorted(channel.channel_number for channel in session.capture_channels))
        stream = getattr(session, "acquisition_mode", "") == "stream"
        key = (id(driver), channels, stream)
        if key not in self._values:
            try:
                if stream:
                    value: Optional[tuple[str, float]] = ("link", float(driver.max_frequency_for(list(channels),
                                                                                              "stream")))
                else:
                    value = ("memory", float(driver.get_limits(list(channels)).max_total_samples))
            except Exception:  # noqa: BLE001 - a device that cannot tell (or is gone) shows no load
                log.debug("The limits of %s cannot be read", getattr(driver, "device_version", "?"), exc_info=True)
                value = None
            self._values[key] = value
        return self._values[key]


def summarize(instrument: Instrument, session: Any = None, state: Optional[tuple[str, str]] = None,
              received: Optional[float] = None, limits: Optional[LimitCache] = None) -> DeviceSummary:
    """The summary of ``instrument``: ``session`` the settings of its next capture, ``state`` what
    its capture does (``CaptureController.state_text``), ``received`` the share of the running
    capture that arrived."""
    connection = instrument.uri or instrument.kind
    if instrument.status == InstrumentStatus.SIMULATED:
        connection += " · simulated"
    if instrument.status == InstrumentStatus.DISCONNECTED:
        return DeviceSummary("Disconnected", "off", connection, "")
    process = getattr(instrument, "process", None)
    process_load = getattr(process, "cpu_load", None) if process is not None else None
    if instrument.capture is None or session is None:
        return DeviceSummary(instrument.status.value.capitalize(), "idle", connection, _offers(instrument),
                             process_load=process_load)
    text, kind = state or ("Ready", "idle")
    summary = DeviceSummary(_short(text, kind), kind, connection, settings_line(session), process_load=process_load)
    if kind == "busy" and received is not None:
        summary.load, summary.load_text, summary.load_kind = received, f"received {received * 100:.0f} %", "received"
        return summary
    capacity = (limits or LimitCache()).capacity(instrument.capture.driver, session)
    if capacity is not None and capacity[1] > 0:
        what, most = capacity
        used = session.frequency if what == "link" else session.pre_trigger_samples + session.post_trigger_samples
        summary.load = min(used / most, 1.0)
        summary.load_text = f"{what} {used / most * 100:.0f} %"
        summary.load_kind = what
    return summary


def _short(text: str, kind: str) -> str:
    if kind == "armed":
        return "Armed"
    if kind == "error":
        return "Failed"
    if kind == "idle":
        return "Ready"
    return text  # (Receiving 45 %)


def _offers(instrument: Instrument) -> str:
    """What a device without captures offers (its facets), a running monitor with its rate."""
    parts = []
    for facet in instrument.facets():
        title = str(getattr(facet, "title", "") or type(facet).__name__)
        if title == "Monitor":
            try:
                if facet.running:
                    title = "Monitor running"
            except Exception:  # noqa: BLE001 - a device that cannot tell: its title alone
                log.debug("The monitor of %s cannot tell whether it runs", instrument.name, exc_info=True)
        if title not in parts:
            parts.append(title)
    return " · ".join(parts)
