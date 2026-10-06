# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Where a :class:`~openscilab.driver.models.TriggerCondition` is fulfilled.

:func:`condition_events` finds every position of a condition in a capture at once (the search
panel: "next edge", "pattern", "pulse 1..2 µs", "gap > 10 µs"); the trigger engine
(:mod:`.trigger_engine`) uses the same event sources incrementally on a stream.

An event is the sample at which the condition is *recognised*:

* ``EDGE``: the first sample with the new level (an edge needs the sample before it, so sample 0
  of a stream is never an edge);
* ``PATTERN``: the sample at which the masked channels reach the value (also sample 0 of a stream
  when the pattern holds there);
* ``PULSE``: the edge ending the pulse; the pulse starts with an edge, so a pulse already running
  where the observation starts is not reported. Its width is ``end - start`` samples;
* ``GAP``: the sample at which ``min_ns`` have passed without an edge, i.e. ``g + m`` for a quiet
  run starting at ``g`` that has no edge in ``(g, g + m)`` (``m`` samples of ``min_ns``). A run is
  reported once; the run in progress at the start of the search counts from the start.

Everything is vectorised with numpy; Python only loops over blocks of samples and over the events
asked for, never over samples. Samples are processed in blocks, so captures on disk are not loaded
at once.
"""

from __future__ import annotations

from typing import Mapping, Optional, Union

import numpy as np

from ..driver.models import ConditionKind, EdgeKind, TriggerCondition

NS_PER_SECOND = 1_000_000_000
#: Samples processed at once (bounds the temporary memory of large captures)
EVENT_BLOCK = 16 << 20
#: Runs examined at once by a gap search (doubled while nothing is found)
GAP_SCAN_BLOCK = 1024

_EMPTY = np.zeros(0, dtype=np.int64)


def ns_to_samples_ceil(ns: int, frequency: int) -> int:
    """Fewest samples lasting at least ``ns`` nanoseconds."""
    return -(-int(ns) * int(frequency) // NS_PER_SECOND)


def ns_to_samples_floor(ns: int, frequency: int) -> int:
    """Most samples lasting at most ``ns`` nanoseconds."""
    return int(ns) * int(frequency) // NS_PER_SECOND


def condition_channels(condition: TriggerCondition) -> list[int]:
    """Channel numbers a condition looks at."""
    if condition.kind == ConditionKind.PATTERN:
        mask = int(condition.mask)
        return [bit for bit in range(mask.bit_length()) if mask >> bit & 1]
    return [int(condition.channel)]


class ChunkEdges:
    """A chunk of samples at the stream position ``first`` and the edges of its channels.

    ``previous`` maps channel numbers to the sample before the chunk (missing: the chunk starts
    the observation, its first sample is no edge). The edges of a channel are computed once and
    shared by every condition looking at it.
    """

    def __init__(
        self,
        chunk: Mapping[int, np.ndarray],
        first: int,
        count: int,
        previous: Mapping[int, int],
    ) -> None:
        self.chunk = chunk
        self.first = int(first)
        self.count = int(count)
        self.previous = previous
        self._edges: dict[int, tuple[np.ndarray, np.ndarray]] = {}

    @property
    def end(self) -> int:
        return self.first + self.count

    def samples(self, channel: int) -> np.ndarray:
        return np.asarray(self.chunk[channel][: self.count], dtype=np.uint8)

    def edges(self, channel: int) -> tuple[np.ndarray, np.ndarray]:
        """``(positions, rising)``: stream positions of the edges and whether each rises."""
        cached = self._edges.get(channel)
        if cached is not None:
            return cached
        if self.count <= 0:
            result = (_EMPTY, np.zeros(0, dtype=bool))
        else:
            values = self.samples(channel)
            local = np.flatnonzero(values[1:] != values[:-1]) + 1
            previous = self.previous.get(channel)
            if previous is not None and bool(values[0]) != bool(previous):
                local = np.concatenate((np.zeros(1, dtype=local.dtype), local))
            result = (local.astype(np.int64) + self.first, values[local] != 0)
        self._edges[channel] = result
        return result

    def last_values(self) -> dict[int, int]:
        """The last sample of every channel (``previous`` of the next chunk)."""
        if self.count <= 0:
            return dict(self.previous)
        return {channel: int(values[self.count - 1]) for channel, values in self.chunk.items()}


#: Search position of a source (see :meth:`ConditionSource.begin`)
Cursor = Union[int, tuple[int, bool]]


class ConditionSource:
    """Events of one condition, collected chunk by chunk.

    The trigger engine searches them with a cursor: :meth:`begin` places it for a stage entered
    at ``origin`` (pulses must start there or later, gaps count from there) whose events must lie
    at ``lo`` or later; :meth:`next` returns the next event and the cursor after it. Every event
    before ``horizon`` (the end of the samples fed) is known, later ones are not.
    """

    def __init__(self, condition: TriggerCondition, frequency: int) -> None:
        self.condition = condition
        self.frequency = max(int(frequency), 1)

    def extend(self, chunk: ChunkEdges) -> None:
        raise NotImplementedError

    def begin(self, origin: int, lo: int) -> Cursor:
        raise NotImplementedError

    def next(self, cursor: Cursor, horizon: int) -> tuple[Optional[int], Cursor]:
        raise NotImplementedError

    def prune(self, cursor: Cursor) -> None:
        """Forgets what a search from ``cursor`` no longer needs."""
        raise NotImplementedError

    def clear(self) -> None:
        """Forgets the events found so far (a search will only start after them)."""
        raise NotImplementedError

    def events(self, origin: int, horizon: int) -> np.ndarray:
        """Every event of a search started at ``origin`` (``lo == origin``)."""
        raise NotImplementedError


class _PositionSource(ConditionSource):
    """EDGE and PATTERN: a sorted list of positions."""

    def __init__(self, condition: TriggerCondition, frequency: int) -> None:
        super().__init__(condition, frequency)
        self._positions = _EMPTY
        self._channels = condition_channels(condition)
        #: PATTERN: the pattern held at the last sample fed (``None``: nothing fed yet)
        self._held: Optional[bool] = None

    def extend(self, chunk: ChunkEdges) -> None:
        if chunk.count <= 0:
            return
        if self.condition.kind == ConditionKind.EDGE:
            positions, rising = chunk.edges(self._channels[0])
            if self.condition.edge == EdgeKind.RISING:
                positions = positions[rising]
            elif self.condition.edge == EdgeKind.FALLING:
                positions = positions[~rising]
        else:
            held = np.ones(chunk.count, dtype=bool)
            value = int(self.condition.value)
            for channel in self._channels:
                samples = chunk.samples(channel)
                if value >> channel & 1:
                    held &= samples != 0
                else:
                    held &= samples == 0
            local = np.flatnonzero(held[1:] & ~held[:-1]) + 1
            if held[0] and not self._held:
                local = np.concatenate((np.zeros(1, dtype=local.dtype), local))
            self._held = bool(held[-1])
            positions = local.astype(np.int64) + chunk.first
        if len(positions):
            self._positions = np.concatenate((self._positions, positions)) if len(self._positions) else positions

    def begin(self, origin: int, lo: int) -> Cursor:
        return int(lo)

    def next(self, cursor: Cursor, horizon: int) -> tuple[Optional[int], Cursor]:
        index = int(np.searchsorted(self._positions, cursor, side="left"))
        if index >= len(self._positions):
            return None, cursor
        position = int(self._positions[index])
        return position, position + 1

    def prune(self, cursor: Cursor) -> None:
        index = int(np.searchsorted(self._positions, cursor, side="left"))
        if index:
            self._positions = self._positions[index:]

    def clear(self) -> None:
        self._positions = _EMPTY

    def events(self, origin: int, horizon: int) -> np.ndarray:
        return self._positions[np.searchsorted(self._positions, origin, side="left") :]


class _PulseSource(ConditionSource):
    """PULSE: the qualifying pulses as sorted ``(start, end)`` arrays."""

    def __init__(self, condition: TriggerCondition, frequency: int) -> None:
        super().__init__(condition, frequency)
        self._starts = _EMPTY
        self._ends = _EMPTY
        #: Position of the last edge (start of the run in progress); -1: unknown
        self._run_start = -1
        self._min = 1 if condition.min_ns is None else max(ns_to_samples_ceil(condition.min_ns, frequency), 1)
        self._max = None if condition.max_ns is None else ns_to_samples_floor(condition.max_ns, frequency)

    def extend(self, chunk: ChunkEdges) -> None:
        positions, rising = chunk.edges(self.condition.channel)
        if not len(positions):
            return
        starts = np.concatenate((np.array([self._run_start], dtype=np.int64), positions[:-1]))
        self._run_start = int(positions[-1])
        widths = positions - starts
        valid = (starts >= 0) & (widths >= self._min)
        if self._max is not None:
            valid &= widths <= self._max
        # A falling edge ends a high pulse, a rising one a low pulse.
        if self.condition.edge == EdgeKind.RISING:
            valid &= ~rising
        elif self.condition.edge == EdgeKind.FALLING:
            valid &= rising
        if valid.any():
            self._starts = np.concatenate((self._starts, starts[valid]))
            self._ends = np.concatenate((self._ends, positions[valid]))

    def begin(self, origin: int, lo: int) -> Cursor:
        # A pulse ends after it starts, so starting at ``origin`` puts its end after ``lo - 1``.
        return int(origin)

    def next(self, cursor: Cursor, horizon: int) -> tuple[Optional[int], Cursor]:
        index = int(np.searchsorted(self._starts, cursor, side="left"))
        if index >= len(self._starts):
            return None, cursor
        end = int(self._ends[index])
        # Pulses of a channel do not overlap: the next one starts at this end or later.
        return end, end

    def prune(self, cursor: Cursor) -> None:
        index = int(np.searchsorted(self._starts, cursor, side="left"))
        if index:
            self._starts = self._starts[index:]
            self._ends = self._ends[index:]

    def clear(self) -> None:
        self._starts = self._ends = _EMPTY

    def events(self, origin: int, horizon: int) -> np.ndarray:
        return self._ends[np.searchsorted(self._starts, origin, side="left") :]


class _GapSource(ConditionSource):
    """GAP: the edges of the channel; quiet runs are judged when searched.

    The cursor ``(x, counted)`` stands for the runs starting at the edges after ``x``, and with
    ``counted`` also for a run counted from ``x`` itself (the stage start, or an edge).
    """

    def __init__(self, condition: TriggerCondition, frequency: int) -> None:
        super().__init__(condition, frequency)
        self._edges = _EMPTY
        self._length = max(ns_to_samples_ceil(condition.min_ns or 0, frequency), 1)

    def extend(self, chunk: ChunkEdges) -> None:
        positions, _ = chunk.edges(self.condition.channel)
        if len(positions):
            self._edges = np.concatenate((self._edges, positions)) if len(self._edges) else positions

    def begin(self, origin: int, lo: int) -> Cursor:
        return (int(origin), True)

    def next(self, cursor: Cursor, horizon: int) -> tuple[Optional[int], Cursor]:
        origin, counted = cursor  # type: ignore[misc]
        edges = self._edges
        index = int(np.searchsorted(edges, origin, side="right"))
        block = GAP_SCAN_BLOCK
        # Last sample known: a run without a following edge may end there.
        last = horizon - 1
        while True:
            segment = edges[index : index + block]
            following = int(edges[index + block]) if index + block < len(edges) else last
            if counted:
                starts = np.concatenate((np.array([origin], dtype=np.int64), segment))
                ends = np.concatenate((segment, np.array([following], dtype=np.int64)))
            else:
                starts = segment
                ends = np.concatenate((segment[1:], np.array([following], dtype=np.int64)))
            if not len(starts):
                return None, cursor
            valid = starts + self._length <= ends
            if valid.any():
                run = int(starts[int(np.argmax(valid))])
                return run + self._length, (run, False)
            index += block
            if index >= len(edges):
                # Only the run in progress is still open.
                return None, (int(starts[-1]), True)
            counted = False
            block *= 2

    def prune(self, cursor: Cursor) -> None:
        origin, _ = cursor  # type: ignore[misc]
        index = int(np.searchsorted(self._edges, origin, side="right"))
        if index:
            self._edges = self._edges[index:]

    def clear(self) -> None:
        self._edges = _EMPTY

    def events(self, origin: int, horizon: int) -> np.ndarray:
        later = self._edges[np.searchsorted(self._edges, origin, side="right") :]
        starts = np.concatenate((np.array([origin], dtype=np.int64), later))
        ends = np.concatenate((later, np.array([horizon - 1], dtype=np.int64)))
        return starts[starts + self._length <= ends] + self._length


def make_source(condition: TriggerCondition, frequency: int) -> ConditionSource:
    """The event source of ``condition`` at ``frequency`` samples per second."""
    if condition.kind in (ConditionKind.EDGE, ConditionKind.PATTERN):
        return _PositionSource(condition, frequency)
    if condition.kind == ConditionKind.PULSE:
        return _PulseSource(condition, frequency)
    if condition.kind == ConditionKind.GAP:
        return _GapSource(condition, frequency)
    raise ValueError(f"Unknown condition kind {condition.kind!r}")


def channels_length(channels: Mapping[int, np.ndarray], numbers: list[int]) -> int:
    """Samples every channel of ``numbers`` holds; ``ValueError`` when one is missing."""
    missing = [number for number in numbers if channels.get(number) is None]
    if missing:
        raise ValueError(f"Channel {missing[0] + 1} is not captured")
    return min((len(channels[number]) for number in numbers), default=0)


def condition_events(
    channels: Mapping[int, np.ndarray],
    condition: TriggerCondition,
    frequency: int,
    start: int = 0,
    end: Optional[int] = None,
) -> np.ndarray:
    """Sorted sample positions in ``[start, end)`` where ``condition`` is recognised.

    ``channels`` maps channel numbers to their samples. Pulses starting before ``start`` are not
    reported (nor ones not finished before ``end``); a gap in progress at ``start`` counts from
    ``start``. Edges and patterns at ``start`` are judged against the sample before it.
    """
    numbers = condition_channels(condition)
    if not numbers:  # a pattern without channels holds everywhere: it starts at sample 0
        return np.zeros(1 if start <= 0 and (end is None or end > 0) else 0, dtype=np.int64)
    length = channels_length(channels, numbers)
    end = length if end is None else min(int(end), length)
    start = max(int(start), 0)
    if start >= end:
        return _EMPTY
    source = make_source(condition, frequency)
    previous: dict[int, int] = {}
    for block_start in range(max(start - 1, 0), end, EVENT_BLOCK):
        block_end = min(block_start + EVENT_BLOCK, end)
        chunk = ChunkEdges(
            {number: channels[number][block_start:block_end] for number in numbers},
            block_start,
            block_end - block_start,
            previous,
        )
        source.extend(chunk)
        previous = chunk.last_values()
    events = source.events(start, end)
    return events[events < end]
