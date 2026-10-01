"""Software triggers through the scripting API."""

from __future__ import annotations

import numpy as np
import pytest

from pipilogicanalyzer import api
from pipilogicanalyzer.driver.models import ConditionKind, EdgeKind, TriggerCondition, TriggerStage, TriggerType

from test_software_trigger import FakeStreamDriver


def signals(count: int = 50_000) -> dict[int, np.ndarray]:
    data = {0: np.zeros(count, np.uint8), 1: np.zeros(count, np.uint8)}
    data[0][20_000:20_010] = 1  # a 10 µs high pulse at 1 MHz
    data[1][30_000:] = 1
    return data


def test_an_edge_found_in_the_stream():
    device = api.Device(FakeStreamDriver(signals()))
    capture = device.capture(
        channels=[0, 1], rate=1_000_000, samples=2_000, pre_trigger=500,
        trigger=api.Edge(1, rising=True), mode="stream", software_trigger=True, timeout=5,
    )
    assert capture.session.software_trigger and capture.session.pre_trigger_samples == 500
    channel = capture.samples(1)
    assert channel[499] == 0 and channel[500] == 1


def test_a_sequence_on_a_device_without_sequences():
    device = api.Device(FakeStreamDriver(signals()))
    sequence = api.Sequence([
        TriggerStage(TriggerCondition(ConditionKind.PULSE, 0, EdgeKind.RISING, min_ns=5_000, max_ns=20_000)),
        TriggerStage(TriggerCondition(ConditionKind.EDGE, 1, EdgeKind.RISING)),
    ])
    with pytest.raises(ValueError):
        device.build_session(channels=[0, 1], rate=1_000_000, samples=1_000, trigger=sequence, mode="stream")
    capture = device.capture(
        channels=[0, 1], rate=1_000_000, samples=1_000, pre_trigger=100, trigger=sequence,
        mode="stream", software_trigger=True, timeout=5,
    )
    assert capture.session.trigger_type == TriggerType.SEQUENCE
    assert capture.samples(1)[100] == 1 and capture.samples(1)[99] == 0


def test_software_triggers_need_a_stream():
    device = api.Device(FakeStreamDriver(signals()))
    with pytest.raises(ValueError, match="stream"):
        device.build_session(channels=[0], trigger=api.Edge(0), software_trigger=True)
