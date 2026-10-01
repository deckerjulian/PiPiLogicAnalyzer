"""Condition events (core.conditions): edges, patterns, pulses and gaps, against a per-sample reference."""

from __future__ import annotations

import random

import numpy as np
import pytest

from pipilogicanalyzer.core import conditions
from pipilogicanalyzer.core.conditions import condition_events
from pipilogicanalyzer.driver.models import ConditionKind, EdgeKind, TriggerCondition

#: 1 GHz: one sample is one nanosecond
RATE = 1_000_000_000


def signal(*levels_and_lengths) -> np.ndarray:
    out = []
    for level, length in levels_and_lengths:
        out += [level] * length
    return np.array(out, dtype=np.uint8)


def random_channel(rng: random.Random, length: int, mean_run: int = 6) -> np.ndarray:
    out = []
    level = rng.randint(0, 1)
    while len(out) < length:
        out += [level] * rng.randint(1, 2 * mean_run)
        level ^= 1
    return np.array(out[:length], dtype=np.uint8)


# ----------------------------------------------------------------- reference
def _edges(x, lo, hi):
    return [p for p in range(max(lo, 1), hi) if x[p] != x[p - 1]]


def reference_events(channels, condition, start, end):
    """Per-sample implementation of the documented semantics."""
    kind = condition.kind
    if kind == ConditionKind.EDGE:
        x = channels[condition.channel]
        out = []
        for p in _edges(x, start, end):
            rising = x[p] == 1
            if condition.edge == EdgeKind.ANY or rising == (condition.edge == EdgeKind.RISING):
                out.append(p)
        return out
    if kind == ConditionKind.PATTERN:
        bits = [n for n in range(64) if condition.mask >> n & 1]

        def held(p):
            return all(channels[n][p] == (condition.value >> n & 1) for n in bits)

        return [p for p in range(start, end) if held(p) and (p == 0 or not held(p - 1))]
    x = channels[condition.channel]
    edges = _edges(x, 1, end)
    if kind == ConditionKind.PULSE:
        low = 1 if condition.min_ns is None else max(-(-condition.min_ns * RATE // 10**9), 1)
        high = None if condition.max_ns is None else condition.max_ns * RATE // 10**9
        out = []
        for s, e in zip(edges, edges[1:]):
            if s < start:
                continue
            level = x[s]
            if condition.edge == EdgeKind.RISING and level != 1:
                continue
            if condition.edge == EdgeKind.FALLING and level != 0:
                continue
            width = e - s
            if width >= low and (high is None or width <= high):
                out.append(e)
        return out
    m = max(-(-(condition.min_ns or 0) * RATE // 10**9), 1)
    out = []
    run = start
    later = [e for e in edges if e > start]
    for following in later + [None]:
        bound = end - 1 if following is None else following
        if run + m <= bound:
            out.append(run + m)
        if following is None:
            break
        run = following
    return out


def random_condition(rng: random.Random, channel_count: int) -> TriggerCondition:
    kind = rng.choice(list(ConditionKind))
    edge = rng.choice(list(EdgeKind))
    channel = rng.randrange(channel_count)
    if kind == ConditionKind.PATTERN:
        mask = rng.randrange(1, 1 << channel_count)
        return TriggerCondition(kind=kind, mask=mask, value=rng.randrange(1 << channel_count) & mask)
    if kind == ConditionKind.PULSE:
        low = rng.choice([None, rng.randint(1, 8)])
        high = rng.choice([None, rng.randint(low or 1, 14)])
        return TriggerCondition(kind=kind, channel=channel, edge=edge, min_ns=low, max_ns=high)
    if kind == ConditionKind.GAP:
        return TriggerCondition(kind=kind, channel=channel, min_ns=rng.randint(1, 15))
    return TriggerCondition(kind=kind, channel=channel, edge=edge)


# ---------------------------------------------------------------------- tests
def test_edges():
    x = signal((0, 3), (1, 2), (0, 4), (1, 1))
    ch = {0: x}
    rising = TriggerCondition(kind=ConditionKind.EDGE, channel=0, edge=EdgeKind.RISING)
    assert condition_events(ch, rising, RATE).tolist() == [3, 9]
    falling = TriggerCondition(kind=ConditionKind.EDGE, channel=0, edge=EdgeKind.FALLING)
    assert condition_events(ch, falling, RATE).tolist() == [5]
    both = TriggerCondition(kind=ConditionKind.EDGE, channel=0, edge=EdgeKind.ANY)
    assert condition_events(ch, both, RATE).tolist() == [3, 5, 9]
    # an edge at ``start`` is judged against the sample before it
    assert condition_events(ch, both, RATE, start=3).tolist() == [3, 5, 9]
    assert condition_events(ch, both, RATE, start=4, end=9).tolist() == [5]


def test_pattern_reported_where_it_becomes_true():
    a = signal((0, 2), (1, 6), (0, 2))
    b = signal((1, 4), (0, 6))
    cond = TriggerCondition(kind=ConditionKind.PATTERN, mask=0b11, value=0b01)
    assert condition_events({0: a, 1: b}, cond, RATE).tolist() == [4]
    # true at sample 0 counts at the start of the capture, not when searching from inside it
    cond = TriggerCondition(kind=ConditionKind.PATTERN, mask=0b10, value=0b10)
    assert condition_events({0: a, 1: b}, cond, RATE).tolist() == [0]
    assert condition_events({0: a, 1: b}, cond, RATE, start=1).tolist() == []


def test_pulses_need_their_start_and_end():
    x = signal((1, 3), (0, 2), (1, 5), (0, 1), (1, 2))  # high pulse at start has no start edge
    ch = {0: x}
    high = TriggerCondition(kind=ConditionKind.PULSE, channel=0, edge=EdgeKind.RISING)
    assert condition_events(ch, high, RATE).tolist() == [10]  # the last high run never ends
    low = TriggerCondition(kind=ConditionKind.PULSE, channel=0, edge=EdgeKind.FALLING)
    assert condition_events(ch, low, RATE).tolist() == [5, 11]
    sized = TriggerCondition(kind=ConditionKind.PULSE, channel=0, edge=EdgeKind.ANY, min_ns=2, max_ns=4)
    assert condition_events(ch, sized, RATE).tolist() == [5]
    assert condition_events(ch, sized, RATE, start=4).tolist() == []  # starts before ``start``
    any_pulse = TriggerCondition(kind=ConditionKind.PULSE, channel=0, edge=EdgeKind.ANY)
    assert condition_events(ch, any_pulse, RATE, end=11).tolist() == [5, 10]


def test_gap_counts_from_start_and_last_edge():
    x = signal((0, 10), (1, 3), (0, 8))
    gap = TriggerCondition(kind=ConditionKind.GAP, channel=0, min_ns=5)
    assert condition_events({0: x}, gap, RATE).tolist() == [5, 18]
    assert condition_events({0: x}, gap, RATE, start=4).tolist() == [9, 18]
    assert condition_events({0: x}, gap, RATE, start=6).tolist() == [18]  # 6..10 is too short
    # the quiet run must be over before ``end``
    assert condition_events({0: x}, gap, RATE, end=18).tolist() == [5]
    assert condition_events({0: x}, gap, RATE, end=19).tolist() == [5, 18]


def test_ns_rounding_uses_the_sample_rate():
    x = signal((0, 2), (1, 3), (0, 2), (1, 4), (0, 1))  # high pulses of 3 and 4 samples
    cond = TriggerCondition(kind=ConditionKind.PULSE, channel=0, edge=EdgeKind.RISING, min_ns=350)
    # at 10 MHz a sample is 100 ns: 350 ns needs 4 samples
    assert condition_events({0: x}, cond, 10_000_000).tolist() == [11]


@pytest.mark.parametrize("seed", range(40))
def test_random_against_reference(seed, monkeypatch):
    rng = random.Random(seed)
    monkeypatch.setattr(conditions, "EVENT_BLOCK", rng.choice([7, 64, 1 << 20]))
    monkeypatch.setattr(conditions, "GAP_SCAN_BLOCK", rng.choice([1, 3, 1024]))
    length = rng.randint(1, 400)
    channels = {n: random_channel(rng, length, rng.choice([2, 5, 12])) for n in range(3)}
    for _ in range(6):
        condition = random_condition(rng, 3)
        start = rng.randint(0, length)
        end = rng.choice([None, rng.randint(0, length)])
        expected = reference_events(channels, condition, start, length if end is None else end)
        assert condition_events(channels, condition, RATE, start, end).tolist() == expected, condition


def test_missing_channel():
    with pytest.raises(ValueError):
        condition_events({0: np.zeros(4, np.uint8)}, TriggerCondition(channel=3), RATE)
