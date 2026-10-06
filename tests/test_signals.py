"""Typed signals: time bases, blocks, conversions."""

from __future__ import annotations

import numpy as np
import pytest

from openscilab.core import signals
from openscilab.core.signals import (
    Analog,
    Bool,
    Capture,
    Digital,
    Event,
    Scalar,
    SignalError,
    States,
    Table,
    TimeBase,
)
from openscilab.driver.models import AnalyzerChannel, CaptureSession


# ------------------------------------------------------------------ time base
def test_a_uniform_time_base_gives_times_and_indices():
    time = TimeBase.uniform(1000, start=0.5)
    assert np.allclose(time.times(3), [0.5, 0.501, 0.502])
    assert time.index_of(0.5015) == 1
    assert time.end(10) == pytest.approx(0.51)
    assert time.shifted(1).start == pytest.approx(1.5)


def test_time_stamps_and_unknown_time():
    time = TimeBase.stamped([0.0, 0.1, 0.35])
    assert not time.is_uniform and time.is_known
    assert time.index_of(0.2) == 1
    assert time.time_of(2) == pytest.approx(0.35)
    with pytest.raises(SignalError):
        TimeBase().times(2)
    with pytest.raises(SignalError):
        TimeBase(rate=-1)
    with pytest.raises(SignalError):
        TimeBase(rate=10, timestamps=np.zeros(2))


# --------------------------------------------------------------------- blocks
def test_blocks_continue_a_digital_stream():
    signal = Digital(name="D0", values=[0, 1, 1], time=TimeBase.uniform(100))
    signal.append(Digital(name="D0", values=[0, 0], time=TimeBase.uniform(100, start=0.03)))
    assert list(signal.values) == [0, 1, 1, 0, 0]
    assert signal.duration == pytest.approx(0.05)


def test_a_block_with_a_gap_or_another_rate_is_refused():
    signal = Analog(values=[1.0, 2.0], time=TimeBase.uniform(100))
    with pytest.raises(SignalError):
        signal.append(Analog(values=[3.0], time=TimeBase.uniform(100, start=0.5)))
    with pytest.raises(SignalError):
        signal.append(Analog(values=[3.0], time=TimeBase.uniform(200, start=0.02)))
    with pytest.raises(SignalError):
        signal.append(Analog(values=[3.0], unit="A", time=TimeBase.uniform(100, start=0.02)))
    with pytest.raises(SignalError):
        signal.append(Digital(values=[1], time=TimeBase.uniform(100, start=0.02)))


def test_time_stamped_blocks_concatenate():
    signal = Analog(values=[1.0], time=TimeBase.stamped([0.0]))
    signal.append(Analog(values=[2.0, 3.0], time=TimeBase.stamped([0.2, 0.7])))
    assert np.allclose(signal.times(), [0.0, 0.2, 0.7])


def test_events_states_tables_and_captures_take_blocks():
    events = Event(times=[0.1], data=["a"])
    events.append(Event(times=[0.2, 0.3], data=["b", "c"]))
    assert list(events) == [(0.1, "a"), (0.2, "b"), (0.3, "c")]
    with pytest.raises(SignalError):
        events.append(Event(times=[0.0], data=["late"]))

    states = States(values=[1, 2], times=[0.0, 0.1], channels=["A", "B"])
    states.append(States(values=[3], times=[0.2]))
    assert list(states.values) == [1, 2, 3]
    assert list(states.bit(0).values) == [1, 0, 1]
    assert states.bit(1).name == "B"

    table = Table.from_rows([{"x": 1, "y": 2}])
    table.append(Table.from_rows([{"x": 3, "z": 4}]))
    assert table.rows() == [{"x": 1, "y": 2, "z": None}, {"x": 3, "y": None, "z": 4}]

    capture = Capture(rate=10, digital={"D0": np.array([0, 1], np.uint8)})
    capture.append(Capture(rate=10, digital={"D0": np.array([1], np.uint8)}))
    assert list(capture.digital["D0"]) == [0, 1, 1]


# ---------------------------------------------------------------- conversions
def test_analog_to_digital_with_hysteresis():
    values = [0.0, 1.6, 1.8, 1.6, 1.4, 1.0, 1.6]
    analog = Analog(values=values, time=TimeBase.uniform(10))
    assert list(analog.to_digital(1.5).values) == [0, 1, 1, 1, 0, 0, 1]
    # With 0.4 V hysteresis: high above 1.7, low below 1.3.
    assert list(analog.to_digital(1.5, hysteresis=0.4).values) == [0, 0, 1, 1, 1, 0, 0]


def test_measurements_and_edges():
    analog = Analog(values=[1.0, 3.0], time=TimeBase.uniform(10))
    assert analog.mean().value == pytest.approx(2.0)
    assert analog.mean().unit == "V"
    assert analog.rms().value == pytest.approx(np.sqrt(5))
    digital = Digital(values=[0, 1, 1, 0, 1], time=TimeBase.uniform(10))
    edges = digital.edges()
    assert np.allclose(edges.times, [0.1, 0.3, 0.4]) and edges.data == [1, 0, 1]
    assert np.allclose(digital.edges("rising").times, [0.1, 0.4])
    assert digital.high_share().value == pytest.approx(0.6)
    assert list(digital.to_analog(0, 5).values) == [0, 5, 5, 0, 5]


def test_conversion_table_and_compatibility():
    assert signals.compatible("Digital", "Digital")
    assert signals.compatible("Analog", "Any")
    assert not signals.compatible("Analog", "Digital")
    found = signals.conversion("Analog", "Digital")
    assert found is not None and found.node == "dsp.threshold"
    assert signals.conversion("Table", "Bool") is None

    digital = signals.convert(Analog(values=[0.0, 3.3], time=TimeBase.uniform(1)), "Digital")
    assert isinstance(digital, Digital) and list(digital.values) == [0, 1]
    assert isinstance(signals.convert(Scalar(value=1.0), "Bool"), Bool)
    with pytest.raises(SignalError):
        signals.convert(Table(), "Bool")
    assert set(signals.SIGNAL_TYPES) == set(signals.TYPES)


def test_a_capture_session_becomes_a_capture_and_back():
    session = CaptureSession(frequency=1000, pre_trigger_samples=1, post_trigger_samples=3)
    session.capture_channels = [AnalyzerChannel(channel_number=0, channel_name="CLK", samples=np.array([0, 1, 0, 1], np.uint8))]
    capture = Capture.from_session(session)
    assert capture.channels == ["CLK"] and capture.rate == 1000 and capture.trigger == 1
    clock = capture.channel("CLK")
    assert isinstance(clock, Digital) and clock.rate == 1000
    assert capture.to_session() is session
    with pytest.raises(KeyError):
        capture.channel("missing")

    fresh = Capture(rate=50, digital={"A": np.array([1, 0], np.uint8)}, trigger=0)
    rebuilt = fresh.to_session()
    assert rebuilt.frequency == 50 and rebuilt.capture_channels[0].channel_name == "A"


def test_windows_and_values():
    analog = Analog(values=np.arange(10, dtype=float), time=TimeBase.uniform(10))
    window = analog.window(0.2, 0.5)
    assert list(window.values) == [2.0, 3.0, 4.0]
    assert window.time.start == pytest.approx(0.2)
    assert analog.value_at(0.75) == 7.0
    with pytest.raises(SignalError):
        analog.value_at(5.0)
