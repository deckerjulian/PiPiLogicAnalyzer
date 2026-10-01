# Copyright (C) 2026 Julian Decker
#
# Part of PiPiLogicAnalyzer.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""State analysis of a timing capture.

A channel of the capture is used as the clock: every edge of it yields one
state, the levels of the other channels at that edge (optionally delayed). The
result is a new capture with one sample per state, plus the original position
of every state so the views can map between both.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ..driver.models import AnalyzerChannel, CaptureSession, EdgeKind
from .statistics import clock_edge_positions


@dataclass
class StateCapture:
    """States sampled from a timing capture."""

    session: CaptureSession
    #: Original sample (clock edge) of every state
    positions: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int64))
    #: Samples between a clock edge and the sample point of its state
    delay: int = 0

    @property
    def state_count(self) -> int:
        return len(self.positions)

    def state_at(self, sample: int) -> int:
        """Index of the last state at or before the original ``sample`` (-1: none)."""
        return int(np.searchsorted(self.positions, sample, side="right")) - 1

    def sample_of(self, state: int) -> int:
        """Original sample of ``state`` (clipped to the states)."""
        if not len(self.positions):
            return 0
        return int(self.positions[min(max(int(state), 0), len(self.positions) - 1)])


def resample_on_clock(
    session: CaptureSession,
    clock_channel: int,
    edge: EdgeKind = EdgeKind.RISING,
    delay: int = 0,
    qualifier_mask: int = 0,
    qualifier_value: int = 0,
    keep_clock: bool = False,
) -> StateCapture:
    """States of ``session`` clocked by the edges of the channel numbered ``clock_channel``.

    Each state holds the level of every other channel (and the clock with
    ``keep_clock``) at ``edge sample + delay``, clamped to the capture. States at
    which the channels of ``qualifier_mask`` (bit ``n``: channel number ``n``) do
    not have the levels of ``qualifier_value`` are dropped; missing channels read 0.
    Raises ``ValueError`` when the session has no such clock channel.
    """
    by_number = {c.channel_number: c for c in session.capture_channels if c.samples is not None}
    clock = by_number.get(clock_channel)
    if clock is None:
        raise ValueError(f"No samples of the clock channel {clock_channel + 1}")
    count = session.sample_count()
    edges = clock_edge_positions(clock.samples, edge)
    points = np.clip(edges + int(delay), 0, max(count - 1, 0))

    keep = np.ones(len(edges), dtype=bool)
    for number in range(64):
        if not qualifier_mask >> number & 1:
            continue
        channel = by_number.get(number)
        levels = np.asarray(channel.samples[points]) != 0 if channel is not None else np.zeros(len(points), bool)
        keep &= levels == bool(qualifier_value >> number & 1)

    if len(edges) >= 2:
        rate = (len(edges) - 1) * session.frequency / float(edges[-1] - edges[0])
    else:
        rate = session.frequency
    positions, points = edges[keep], points[keep]

    state = session.clone_settings()
    state.frequency = max(int(round(rate)), 1)
    state.capture_channels = []
    for channel in session.capture_channels:
        if channel.samples is None or (channel.channel_number == clock_channel and not keep_clock):
            continue
        state.capture_channels.append(
            AnalyzerChannel(
                channel_number=channel.channel_number,
                channel_name=channel.channel_name,
                channel_color=channel.channel_color,
                hidden=channel.hidden,
                samples=np.ascontiguousarray(channel.samples[points], dtype=np.uint8),
            )
        )
    state.pre_trigger_samples = int(np.searchsorted(positions, session.pre_trigger_samples, side="left"))
    state.post_trigger_samples = len(positions) - state.pre_trigger_samples
    state.loop_count = 0
    state.bursts = None
    state.clock_channel = clock_channel
    state.clock_edge = edge
    return StateCapture(session=state, positions=positions.astype(np.int64), delay=int(delay))


#: Read points tried by :func:`suggest_sampling`, relative to the clock edge
SUGGESTED_DELAYS = (-2, -1, 0, 1)
#: Clock edges looked at by :func:`suggest_sampling` (the first ones of the capture)
SUGGESTION_EDGES = 50_000


@dataclass
class SamplingSuggestion:
    edge: EdgeKind
    delay: int
    #: Changes of the other channels next to the read point, per tried (edge, delay)
    unstable: dict[tuple[EdgeKind, int], int]


def suggest_sampling(session: CaptureSession, clock_channel: int) -> SamplingSuggestion:
    """The clock edge and read offset at which the other channels are most stable.

    A device latches its inputs where they are stable: the 6510 at the falling edge of PHI2,
    while during the low phase the VIC drives the bus. For every candidate the number of other
    channels changing right before or after the read point is counted; the fewest win (ties:
    the falling edge, then reading one sample before the edge, as the setup time asks).
    Channels changing more often than the clock are left out.
    Raises ``ValueError`` when the session has no such clock channel.
    """
    by_number = {c.channel_number: c for c in session.capture_channels if c.samples is not None}
    clock = by_number.get(clock_channel)
    if clock is None:
        raise ValueError(f"No samples of the clock channel {clock_channel + 1}")
    count = session.sample_count()
    # Lines faster than the clock (another clock, such as the dot clock of a C64) are no data
    clock_edges = max(len(clock_edge_positions(clock.samples, EdgeKind.ANY)), 1)
    others = [
        channel.samples
        for number, channel in by_number.items()
        if number != clock_channel and len(clock_edge_positions(channel.samples, EdgeKind.ANY)) <= clock_edges
    ]
    unstable: dict[tuple[EdgeKind, int], int] = {}
    for edge in (EdgeKind.FALLING, EdgeKind.RISING):
        edges = clock_edge_positions(clock.samples, edge)[:SUGGESTION_EDGES]
        for delay in SUGGESTED_DELAYS:
            points = edges + delay
            points = points[(points >= 1) & (points < count - 1)]
            changes = 0
            for samples in others:
                level = np.asarray(samples[points])
                changes += int(np.count_nonzero((level != samples[points - 1]) | (level != samples[points + 1])))
            unstable[(edge, delay)] = changes
    preference = {EdgeKind.FALLING: 0, EdgeKind.RISING: 1}
    edge, delay = min(unstable, key=lambda key: (unstable[key], preference[key[0]], abs(key[1] + 1)))
    return SamplingSuggestion(edge, delay, unstable)
