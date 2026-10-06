"""Trigger sequences (core.trigger_engine): batch search, incremental matcher and validation."""

from __future__ import annotations

import random
import time

import numpy as np
import pytest

from openscilab.core import conditions
from openscilab.core.trigger_engine import (
    SequenceMatcher,
    find_sequence,
    session_trigger_sequence,
    validate_sequence,
)
from openscilab.driver.models import (
    AnalyzerChannel,
    CaptureSession,
    ConditionKind,
    EdgeKind,
    TriggerCondition,
    TriggerSequence,
    TriggerStage,
    TriggerType,
)

from test_conditions import RATE, random_channel, random_condition, reference_events, signal


def edge(channel, kind=EdgeKind.RISING):
    return TriggerCondition(kind=ConditionKind.EDGE, channel=channel, edge=kind)


def reference_sequence(channels, sequence, start, end, limit=None):
    """Straightforward implementation of the documented semantics (per-sample events)."""
    results = []
    stage, origin, lo, deadline = 0, start, start, None
    while limit is None or len(results) < limit:
        current = sequence.stages[stage]
        events = [p for p in reference_events(channels, current.condition, origin, end) if p >= lo]
        if len(events) < current.count:
            if deadline is not None and deadline < end:
                stage, origin, lo, deadline = 0, deadline + 1, deadline + 1, None
                continue
            break
        position = events[current.count - 1]
        if deadline is not None and position > deadline:
            stage, origin, lo, deadline = 0, deadline + 1, deadline + 1, None
            continue
        if stage == len(sequence.stages) - 1:
            results.append(position)
            stage = 0
            deadline = None
        else:
            stage += 1
            within = sequence.stages[stage].within_ns
            deadline = None if within is None else position + within * RATE // 10**9
        origin, lo = position, position + 1
    return results


def random_sequence(rng: random.Random, channel_count: int) -> TriggerSequence:
    stages = []
    for index in range(rng.randint(1, 3)):
        within = rng.choice([None, None, rng.randint(1, 60)]) if index else None
        stages.append(TriggerStage(random_condition(rng, channel_count), rng.choice([1, 1, 2, 3]), within))
    return TriggerSequence(stages)


def feed_in_chunks(rng, channels, sequence, length, growing):
    matcher = SequenceMatcher(sequence, RATE, list(channels))
    position = 0
    while position < length:
        step = rng.choice([1, 2, 3, rng.randint(1, 50)])
        new_end = min(position + step, length)
        if growing:
            # views of the arrays filled so far, as the stream drivers report them
            matcher.feed({n: values[:new_end] for n, values in channels.items()}, 0)
        else:
            # only the new samples, sometimes overlapping the old ones
            back = rng.randint(0, min(position, 3))
            matcher.feed({n: values[position - back:new_end] for n, values in channels.items()}, position - back)
        position = new_end
        if matcher.trigger is not None:
            break
    return matcher.trigger


# ---------------------------------------------------------------------- tests
def test_two_stage_sequence():
    a = signal((0, 5), (1, 5), (0, 10))
    b = signal((0, 3), (1, 1), (0, 8), (1, 2), (0, 6))
    sequence = TriggerSequence([TriggerStage(edge(0)), TriggerStage(edge(1))])
    # b rises at 3 (before a) and 12 (after a rose at 5)
    assert find_sequence({0: a, 1: b}, sequence, RATE).tolist() == [12]


def test_within_restarts_after_the_time_limit():
    a = signal((0, 2), (1, 1), (0, 2), (1, 1), (0, 20))  # rises at 2 and 5
    b = signal((0, 12), (1, 2), (0, 12))  # rises at 12
    stages = [TriggerStage(edge(0)), TriggerStage(edge(1), within_ns=8)]
    # after the edge at 2 the limit ends at 10; the restart searches from 11, missing the edge at 5
    assert find_sequence({0: a, 1: b}, TriggerSequence(stages), RATE).tolist() == []
    stages[1].within_ns = 10
    assert find_sequence({0: a, 1: b}, TriggerSequence(stages), RATE).tolist() == [12]


def test_count_and_restart_after_completion():
    clock = np.tile(np.array([0, 1], dtype=np.uint8), 10)  # rises at 1, 3, 5, ...
    sequence = TriggerSequence([TriggerStage(edge(0), count=3)])
    assert find_sequence({0: clock}, sequence, RATE).tolist() == [5, 11, 17]
    assert find_sequence({0: clock}, sequence, RATE, limit=2).tolist() == [5, 11]
    assert find_sequence({0: clock}, sequence, RATE, start=4).tolist() == [9, 15]


def test_pulse_then_gap():
    data = signal((0, 4), (1, 3), (0, 2), (1, 1), (0, 30))
    sequence = TriggerSequence([
        TriggerStage(TriggerCondition(kind=ConditionKind.PULSE, channel=0, edge=EdgeKind.RISING, min_ns=3)),
        TriggerStage(TriggerCondition(kind=ConditionKind.GAP, channel=0, min_ns=10)),
    ])
    # pulse 4..7 ends at 7; the gap counts from 7, the edge at 9 restarts it, quiet from 10: 20
    assert find_sequence({0: data}, sequence, RATE).tolist()[:1] == [20]


@pytest.mark.parametrize("seed", range(150))
def test_random_sequences_match_reference_and_any_chunking(seed, monkeypatch):
    rng = random.Random(seed)
    monkeypatch.setattr(conditions, "GAP_SCAN_BLOCK", rng.choice([1, 2, 1024]))
    channel_count = rng.randint(1, 3)
    length = rng.randint(1, 600)
    channels = {n: random_channel(rng, length, rng.choice([2, 4, 10])) for n in range(channel_count)}
    sequence = random_sequence(rng, channel_count)
    expected = reference_sequence(channels, sequence, 0, length)
    assert find_sequence(channels, sequence, RATE).tolist() == expected, sequence.describe()
    first = expected[0] if expected else None
    for growing in (True, False):
        assert feed_in_chunks(rng, channels, sequence, length, growing) == first, sequence.describe()
    start = rng.randint(0, length)
    assert find_sequence(channels, sequence, RATE, start=start).tolist() == reference_sequence(
        channels, sequence, start, length
    )


@pytest.mark.parametrize("seed", range(20))
def test_small_blocks_give_the_same_result(seed, monkeypatch):
    rng = random.Random(1000 + seed)
    channels = {n: random_channel(rng, 500, 4) for n in range(2)}
    sequence = random_sequence(rng, 2)
    whole = find_sequence(channels, sequence, RATE).tolist()
    monkeypatch.setattr(conditions, "EVENT_BLOCK", rng.randint(1, 40))
    monkeypatch.setattr("openscilab.core.trigger_engine.EVENT_BLOCK", conditions.EVENT_BLOCK)
    assert find_sequence(channels, sequence, RATE).tolist() == whole


def test_matcher_keeps_the_trigger_and_restarts_after_lost_samples():
    sequence = TriggerSequence([TriggerStage(edge(0)), TriggerStage(edge(1))])
    matcher = SequenceMatcher(sequence, RATE, [0, 1])
    zeros = np.zeros(10, dtype=np.uint8)
    rise = signal((0, 5), (1, 5))
    assert matcher.feed({0: rise, 1: zeros}, 0) is None  # stage 1 done at 5
    # samples 10..19 were lost: the sequence starts again at 20
    assert matcher.feed({0: zeros, 1: rise}, 20) is None
    assert matcher.restarts == 1
    assert matcher.feed({0: rise, 1: zeros}, 30) is None
    assert matcher.feed({0: zeros, 1: rise}, 40) == 45
    assert matcher.feed({0: rise, 1: rise}, 50) == 45


def test_matcher_rejects_invalid_sequences():
    with pytest.raises(ValueError):
        SequenceMatcher(TriggerSequence([TriggerStage(edge(4))]), RATE, [0, 1])


def test_validate_sequence():
    assert validate_sequence(None, [0]) is not None
    assert validate_sequence(TriggerSequence([]), [0]) is not None
    assert validate_sequence(TriggerSequence([TriggerStage(edge(0))]), [0]) is None
    assert "not captured" in validate_sequence(TriggerSequence([TriggerStage(edge(2))]), [0, 1])
    assert validate_sequence(TriggerSequence([TriggerStage(edge(0), count=0)]), [0]) is not None
    pattern = TriggerCondition(kind=ConditionKind.PATTERN, mask=0b101, value=1)
    assert "channel 3" in validate_sequence(TriggerSequence([TriggerStage(pattern)]), [0, 1])
    empty = TriggerCondition(kind=ConditionKind.PATTERN, mask=0)
    assert validate_sequence(TriggerSequence([TriggerStage(empty)]), [0]) is not None
    pulse = TriggerCondition(kind=ConditionKind.PULSE, channel=0, min_ns=50, max_ns=10)
    assert validate_sequence(TriggerSequence([TriggerStage(pulse)]), [0]) is not None
    short = TriggerCondition(kind=ConditionKind.PULSE, channel=0, max_ns=5)
    assert validate_sequence(TriggerSequence([TriggerStage(short)]), [0]) is None
    assert validate_sequence(TriggerSequence([TriggerStage(short)]), [0], frequency=1_000_000) is not None
    gap = TriggerCondition(kind=ConditionKind.GAP, channel=0)
    assert validate_sequence(TriggerSequence([TriggerStage(gap)]), [0]) is not None
    stages = [TriggerStage(edge(0)), TriggerStage(edge(0), within_ns=0)]
    assert validate_sequence(TriggerSequence(stages), [0]) is not None


def test_session_trigger_sequence():
    session = CaptureSession(trigger_type=TriggerType.EDGE, trigger_channel=2, trigger_inverted=True)
    session.capture_channels = [AnalyzerChannel(channel_number=n) for n in range(4)]
    condition = session_trigger_sequence(session).stages[0].condition
    assert (condition.kind, condition.channel, condition.edge) == (ConditionKind.EDGE, 2, EdgeKind.FALLING)
    session.trigger_type = TriggerType.COMPLEX
    session.trigger_bit_count = 2
    session.trigger_pattern = 0b10
    condition = session_trigger_sequence(session).stages[0].condition
    assert (condition.mask, condition.value) == (0b1100, 0b1000)
    session.trigger_type = TriggerType.IMMEDIATE
    assert session_trigger_sequence(session) is None


def test_large_capture_is_fast():
    count = 20_000_000
    rng = np.random.default_rng(1)
    # a clock of 100 samples per period, a data line changing about every 5000 samples and a
    # strobe pulsing rarely
    clock = ((np.arange(count) // 50) & 1).astype(np.uint8)
    data = (np.cumsum(rng.random(count) < 1 / 5000) & 1).astype(np.uint8)
    strobe = np.zeros(count, dtype=np.uint8)
    for position in rng.integers(0, count - 10, 200):
        strobe[position : position + 5] = 1
    channels = {0: clock, 1: data, 2: strobe}
    sequence = TriggerSequence([
        TriggerStage(TriggerCondition(kind=ConditionKind.PULSE, channel=2, edge=EdgeKind.RISING, min_ns=4, max_ns=6)),
        TriggerStage(edge(1, EdgeKind.ANY), within_ns=1_000_000),
        TriggerStage(edge(0), count=3),
    ])
    started = time.perf_counter()
    found = find_sequence(channels, sequence, 1_000_000_000)
    elapsed = time.perf_counter() - started
    assert len(found) > 0
    assert elapsed < 1.0, elapsed
    # and the matcher on 1 M sample chunks gives the first of them
    matcher = SequenceMatcher(sequence, 1_000_000_000)
    for first in range(0, count, 1_000_000):
        if matcher.feed({n: values[first:first + 1_000_000] for n, values in channels.items()}, first) is not None:
            break
    assert matcher.trigger == found[0]
