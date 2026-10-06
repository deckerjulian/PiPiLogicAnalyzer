# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Trigger sequences evaluated by the application (software trigger).

A :class:`~openscilab.driver.models.TriggerSequence` is a list of stages; each waits for
``count`` events of its condition (see :mod:`.conditions` for when a condition is recognised).
The semantics, shared by :func:`find_sequence` and :class:`SequenceMatcher`:

* The first stage searches from ``start``: its events may lie at ``start`` or later.
* A stage entered after the previous one completed at sample ``t`` needs events strictly after
  ``t``; its pulses must start at ``t`` or later and its gaps count from ``t``.
* A stage with ``within_ns`` (ignored on the first stage) must complete at the latest
  ``W = floor(within_ns * frequency / 1e9)`` samples after ``t``. Otherwise the sequence starts
  again at the first stage, searching from the sample after that time limit (``t + W + 1``),
  as a hardware sequencer would on its timeout (events of the first stage between ``t`` and the
  limit are not used again).
* When the last stage completes, the sequence triggers there and starts again at the first stage,
  searching strictly after the trigger (like a stage entered at the trigger).

Both run the same state machine over the per-condition event sources, so the incremental matcher
gives exactly the trigger :func:`find_sequence` finds, however the samples are cut into chunks.
Python loops only run per event and stage, never per sample.
"""

from __future__ import annotations

from typing import Iterable, Mapping, Optional

import numpy as np

from ..driver.models import (
    CaptureSession,
    ConditionKind,
    EdgeKind,
    TriggerCondition,
    TriggerSequence,
    TriggerStage,
    TriggerType,
)
from .conditions import (
    EVENT_BLOCK,
    ChunkEdges,
    ConditionSource,
    Cursor,
    channels_length,
    condition_channels,
    condition_events,
    make_source,
    ns_to_samples_ceil,
    ns_to_samples_floor,
)


def sequence_channels(sequence: TriggerSequence) -> list[int]:
    """Channel numbers the conditions of ``sequence`` look at (sorted)."""
    numbers: set[int] = set()
    for stage in sequence.stages:
        numbers.update(condition_channels(stage.condition))
    return sorted(numbers)


def validate_sequence(
    sequence: Optional[TriggerSequence],
    channel_numbers: Iterable[int],
    frequency: Optional[int] = None,
) -> Optional[str]:
    """Why ``sequence`` cannot be evaluated on ``channel_numbers``; ``None`` when it can.

    With ``frequency`` also times shorter than a sample are rejected.
    """
    if sequence is None or not sequence.stages:
        return "The trigger sequence has no stages."
    captured = set(channel_numbers)
    for index, stage in enumerate(sequence.stages):
        name = f"Stage {index + 1}"
        condition = stage.condition
        if stage.count < 1:
            return f"{name} has to occur at least once."
        if index and stage.within_ns is not None:
            if stage.within_ns <= 0:
                return f"The time limit of {name.lower()} has to be positive."
            if frequency and ns_to_samples_floor(stage.within_ns, frequency) < 1:
                return f"The time limit of {name.lower()} is shorter than a sample."
        if condition.kind == ConditionKind.PATTERN:
            if condition.mask <= 0:
                return f"The pattern of {name.lower()} has no channel."
            missing = [n for n in condition_channels(condition) if n not in captured]
            if missing:
                return f"{name} uses channel {missing[0] + 1}, which is not captured."
            continue
        if condition.channel not in captured:
            return f"{name} uses channel {condition.channel + 1}, which is not captured."
        if condition.kind == ConditionKind.PULSE:
            low, high = condition.min_ns, condition.max_ns
            if (low is not None and low < 0) or (high is not None and high < 0):
                return f"The pulse width of {name.lower()} cannot be negative."
            if low is not None and high is not None and low > high:
                return f"The shortest pulse of {name.lower()} is longer than the longest."
            if frequency and high is not None:
                shortest = max(ns_to_samples_ceil(low or 0, frequency), 1)
                if ns_to_samples_floor(high, frequency) < shortest:
                    return f"The pulse width of {name.lower()} is shorter than a sample."
        elif condition.kind == ConditionKind.GAP:
            if condition.min_ns is None or condition.min_ns <= 0:
                return f"The gap of {name.lower()} needs a duration."
    return None


def session_trigger_sequence(session: CaptureSession) -> Optional[TriggerSequence]:
    """The trigger of ``session`` as a sequence (a single stage for edge and pattern triggers).

    ``None`` for triggers without a condition (immediate, simulation).
    """
    trigger = session.trigger_type
    if trigger == TriggerType.SEQUENCE:
        return session.trigger_sequence
    if trigger in (TriggerType.EDGE, TriggerType.EDGE_OUT, TriggerType.BLAST):
        edge = EdgeKind.FALLING if session.trigger_inverted else EdgeKind.RISING
        condition = TriggerCondition(kind=ConditionKind.EDGE, channel=session.trigger_channel, edge=edge)
        return TriggerSequence([TriggerStage(condition)])
    if trigger in (TriggerType.COMPLEX, TriggerType.FAST):
        bits = max(int(session.trigger_bit_count), 0)
        ones = (1 << bits) - 1
        condition = TriggerCondition(
            kind=ConditionKind.PATTERN,
            mask=ones << session.trigger_channel,
            value=(session.trigger_pattern & ones) << session.trigger_channel,
        )
        return TriggerSequence([TriggerStage(condition)])
    return None


class _SequenceState:
    """The state machine of a sequence over the event sources of its stages."""

    def __init__(self, sequence: TriggerSequence, frequency: int, start: int = 0) -> None:
        self.stages: list[TriggerStage] = list(sequence.stages)
        self.sources: list[ConditionSource] = [make_source(stage.condition, frequency) for stage in self.stages]
        self.within: list[Optional[int]] = [
            None if index == 0 or stage.within_ns is None else ns_to_samples_floor(stage.within_ns, frequency)
            for index, stage in enumerate(self.stages)
        ]
        self.stage = 0
        self.found = 0
        self.cursor: Cursor = 0
        #: Last sample the current stage may complete at (``None``: no limit)
        self.deadline: Optional[int] = None
        self._restart(start)

    def _restart(self, position: int) -> None:
        self.stage = 0
        self.found = 0
        self.deadline = None
        self.cursor = self.sources[0].begin(position, position)

    def _enter(self, stage: int, completed: int) -> None:
        self.stage = stage
        self.found = 0
        self.cursor = self.sources[stage].begin(completed, completed + 1)
        within = self.within[stage]
        self.deadline = None if within is None else completed + within

    def extend(self, chunk: ChunkEdges) -> None:
        for source in self.sources:
            source.extend(chunk)

    def advance(self, horizon: int, limit: Optional[int], results: list[int]) -> None:
        """Runs the sequence over the events before ``horizon`` (every sample before it was fed),
        appending the trigger positions to ``results`` until it holds ``limit`` of them."""
        last = len(self.stages) - 1
        while limit is None or len(results) < limit:
            source = self.sources[self.stage]
            position, cursor = source.next(self.cursor, horizon)
            if position is None:
                self.cursor = cursor
                if self.deadline is not None and self.deadline < horizon:
                    self._restart(self.deadline + 1)  # time limit over without the stage
                    continue
                break
            if self.deadline is not None and position > self.deadline:
                self._restart(self.deadline + 1)
                continue
            self.cursor = cursor
            self.found += 1
            if self.found < self.stages[self.stage].count:
                continue
            if self.stage == last:
                results.append(position)
                self._enter(0, position)
            else:
                self._enter(self.stage + 1, position)
        # Only the current stage can use events found so far: the others start after them.
        for index, source in enumerate(self.sources):
            if index == self.stage:
                source.prune(self.cursor)
            else:
                source.clear()


def find_sequence(
    channels: Mapping[int, np.ndarray],
    sequence: TriggerSequence,
    frequency: int,
    start: int = 0,
    end: Optional[int] = None,
    limit: Optional[int] = None,
) -> np.ndarray:
    """Every sample in ``[start, end)`` where ``sequence`` completes, scanning forward.

    After a completion the sequence starts again at its first stage (see the module
    documentation for the exact semantics). ``limit``: stop after that many completions.
    """
    numbers = sequence_channels(sequence)
    if not sequence.stages:
        return np.zeros(0, dtype=np.int64)
    length = channels_length(channels, numbers) if numbers else min(
        (len(values) for values in channels.values()), default=0
    )
    end = length if end is None else min(int(end), length)
    start = max(int(start), 0)
    results: list[int] = []
    if start >= end or (limit is not None and limit <= 0):
        return np.zeros(0, dtype=np.int64)
    if len(sequence.stages) == 1 and sequence.stages[0].condition.kind != ConditionKind.GAP:
        # Events of one condition do not depend on where the search restarts (pulses do not
        # overlap): every ``count``-th event completes the sequence. Gaps restart their timing.
        stage = sequence.stages[0]
        events = condition_events(channels, stage.condition, frequency, start, end)
        return np.ascontiguousarray(events[max(stage.count, 1) - 1 :: max(stage.count, 1)][:limit])
    state = _SequenceState(sequence, frequency, start)
    previous: dict[int, int] = {}
    for block_start in range(max(start - 1, 0), end, EVENT_BLOCK):
        block_end = min(block_start + EVENT_BLOCK, end)
        chunk = ChunkEdges(
            {number: channels[number][block_start:block_end] for number in numbers},
            block_start,
            block_end - block_start,
            previous,
        )
        state.extend(chunk)
        previous = chunk.last_values()
        state.advance(block_end, limit, results)
        if limit is not None and len(results) >= limit:
            break
    return np.asarray(results, dtype=np.int64)


class SequenceMatcher:
    """Evaluates a sequence on samples arriving in chunks (a stream).

    :meth:`feed` takes the samples of the channels at a stream position; parts already fed are
    skipped, so growing views of the same arrays can be passed again and again. Once the sequence
    completes, :attr:`trigger` holds the stream position and stays.

    When samples are missing between two chunks (a ring buffer dropped them before they were fed)
    the sequence starts again at the first stage at the new chunk, which then begins the
    observation (its first sample is no edge); :attr:`restarts` counts these.
    """

    def __init__(
        self,
        sequence: TriggerSequence,
        frequency: int,
        channel_numbers: Optional[Iterable[int]] = None,
    ) -> None:
        if not sequence.stages:
            raise ValueError("The trigger sequence has no stages.")
        self.sequence = sequence
        self.frequency = int(frequency)
        self.channel_numbers = sequence_channels(sequence)
        if channel_numbers is not None:
            error = validate_sequence(sequence, channel_numbers)
            if error:
                raise ValueError(error)
        self.trigger: Optional[int] = None
        self.restarts = 0
        #: Stream position after the last sample fed (``None``: nothing fed yet)
        self.position: Optional[int] = None
        self._state: Optional[_SequenceState] = None
        self._previous: dict[int, int] = {}

    def feed(self, chunk: Mapping[int, np.ndarray], first_sample: int = 0) -> Optional[int]:
        """Adds the samples of ``chunk`` (``chunk[n][0]`` at stream position ``first_sample``);
        returns the trigger position once the sequence completed, else ``None``."""
        if self.trigger is not None:
            return self.trigger
        numbers = self.channel_numbers
        if numbers:
            count = channels_length(chunk, numbers)
        else:
            count = min((len(values) for values in chunk.values()), default=0)
        first_sample = int(first_sample)
        end = first_sample + count
        if self.position is None or first_sample > self.position:
            if self.position is not None:
                self.restarts += 1
            self._state = _SequenceState(self.sequence, self.frequency, first_sample)
            self._previous = {}
            self.position = first_sample
        begin = self.position
        if end <= begin:
            return None
        offset = begin - first_sample
        part = ChunkEdges(
            {number: chunk[number][offset:count] for number in numbers}, begin, end - begin, self._previous
        )
        assert self._state is not None
        self._state.extend(part)
        self._previous = part.last_values()
        self.position = end
        results: list[int] = []
        self._state.advance(end, 1, results)
        if results:
            self.trigger = results[0]
        return self.trigger
