"""Tests for the sample analysis helpers and the capture files."""

from __future__ import annotations

import json
import os

import numpy as np
import pytest

from pipilogicanalyzer.core import capture_io
from pipilogicanalyzer.core.analysis import ChannelTransitions, measure_channel
from pipilogicanalyzer.core.formatting import to_large_frequency, to_small_time
from pipilogicanalyzer.core.regions import SampleRegion
from pipilogicanalyzer.driver.models import AnalyzerChannel, CaptureSession, TriggerType


def square_wave(period: int, count: int) -> np.ndarray:
    return ((np.arange(count) // (period // 2)) % 2).astype(np.uint8)


def make_session(samples: int = 64) -> CaptureSession:
    session = CaptureSession(
        frequency=1_000_000, pre_trigger_samples=16, post_trigger_samples=samples - 16
    )
    session.capture_channels = [
        AnalyzerChannel(channel_number=0, channel_name="clk", samples=square_wave(8, samples)),
        AnalyzerChannel(channel_number=3, channel_name="data", samples=square_wave(16, samples)),
    ]
    return session


# ---------------------------------------------------------------- transitions
def test_transitions_index_the_runs():
    transitions = ChannelTransitions(np.array([0, 0, 1, 1, 1, 0], dtype=np.uint8), 1000)
    assert list(transitions.starts) == [0, 2, 5]
    assert list(transitions.values) == [0, 1, 0]
    assert transitions.edge_count == 2


def test_interval_at_returns_the_enclosing_run():
    transitions = ChannelTransitions(np.array([0, 0, 1, 1, 1, 0], dtype=np.uint8), 1000)
    interval = transitions.interval_at(3)
    assert (interval.start, interval.end, interval.value) == (2, 5, True)
    assert interval.duration == pytest.approx(0.003)
    assert transitions.interval_at(99) is None


def test_runs_in_range_includes_the_run_already_active():
    transitions = ChannelTransitions(square_wave(8, 64), 1000)
    starts, values = transitions.runs_in_range(10, 20)
    assert starts[0] <= 10
    assert starts[-1] <= 20
    assert len(starts) == len(values)


def test_empty_channel_is_handled():
    transitions = ChannelTransitions(None, 1000)
    assert len(transitions) == 0
    assert transitions.interval_at(0) is None
    assert transitions.runs_in_range(0, 10)[0].size == 0


# --------------------------------------------------------------- measurements
def test_measure_channel_of_a_square_wave():
    measures = measure_channel(square_wave(8, 80), 1_000_000, "clk")
    assert measures.positive_pulses == 10
    assert measures.negative_pulses == 10
    assert measures.average_positive_duration == pytest.approx(4e-6)
    assert measures.average_frequency == pytest.approx(125_000)
    assert measures.predicted_frequency == pytest.approx(125_000)
    assert measures.duty_cycle == pytest.approx(50.0)


def test_measure_channel_without_samples():
    measures = measure_channel(None, 1_000_000)
    assert measures.positive_pulses == 0
    assert measures.average_frequency == 0


def test_measure_is_robust_against_a_single_outlier():
    samples = np.concatenate([square_wave(8, 80), np.ones(500, dtype=np.uint8)])
    measures = measure_channel(samples, 1_000_000)
    # The mode ignores the 500 sample long pulse the average is dragged by.
    assert measures.predicted_positive_duration == pytest.approx(4e-6)
    assert measures.average_positive_duration > measures.predicted_positive_duration


# --------------------------------------------------------------------- format
def test_time_formatting():
    assert to_small_time(0.000000123) == "123 ns"
    assert to_small_time(0.0000123) == "12.3 µs"
    assert to_small_time(0.0123) == "12.3 ms"
    assert to_small_time(1.5) == "1.5 s"


def test_frequency_formatting():
    assert to_large_frequency(125_000) == "125 kHz"
    assert to_large_frequency(2_500_000) == "2.5 MHz"
    assert to_large_frequency(50) == "50 Hz"
    assert to_large_frequency(1_000_000_000) == "1 GHz"


# ----------------------------------------------------------------- capture io
def test_capture_roundtrip(tmp_path):
    session = make_session()
    regions = [SampleRegion(first_sample=4, last_sample=20, region_name="frame")]
    path = os.path.join(tmp_path, "capture.lac")

    capture_io.save_capture(path, session, regions)
    loaded = capture_io.load_capture(path)

    assert loaded.session.frequency == session.frequency
    assert loaded.session.pre_trigger_samples == session.pre_trigger_samples
    assert loaded.session.trigger_type is TriggerType.EDGE
    assert [c.channel_number for c in loaded.session.capture_channels] == [0, 3]
    assert np.array_equal(
        loaded.session.capture_channels[0].samples, session.capture_channels[0].samples
    )
    assert loaded.regions[0].region_name == "frame"
    assert loaded.regions[0].sample_count == 16


def test_capture_file_is_compatible_with_the_csharp_format(tmp_path):
    path = os.path.join(tmp_path, "capture.lac")
    capture_io.save_capture(path, make_session())

    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)

    assert set(data) == {"Settings", "Samples", "SelectedRegions"}
    settings = data["Settings"]
    for key in (
        "Frequency",
        "PreTriggerSamples",
        "PostTriggerSamples",
        "TotalSamples",
        "LoopCount",
        "MeasureBursts",
        "CaptureChannels",
        "TriggerType",
        "TriggerChannel",
        "TriggerInverted",
        "TriggerBitCount",
        "TriggerPattern",
    ):
        assert key in settings
    assert settings["CaptureChannels"][0]["TextualChannelNumber"] == "Channel 1"


def test_legacy_packed_samples_are_unpacked(tmp_path):
    path = os.path.join(tmp_path, "old.lac")
    payload = {
        "Settings": {
            "Frequency": 1000,
            "PreTriggerSamples": 1,
            "PostTriggerSamples": 3,
            "TriggerType": 0,
            "CaptureChannels": [
                {"ChannelNumber": 0, "ChannelName": "a"},
                {"ChannelNumber": 1, "ChannelName": "b"},
            ],
        },
        "Samples": [0b01, 0b11, 0b10, 0b00],
        "SelectedRegions": [],
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)

    loaded = capture_io.load_capture(path)
    assert np.array_equal(loaded.session.capture_channels[0].samples, [1, 1, 0, 0])
    assert np.array_equal(loaded.session.capture_channels[1].samples, [0, 1, 1, 0])


def test_compressed_captures_roundtrip(tmp_path):
    path = os.path.join(tmp_path, "capture.lac.gz")
    capture_io.save_capture(path, make_session())
    loaded = capture_io.load_capture(path)
    assert loaded.session.sample_count() == 64


def test_loading_a_non_capture_file_raises(tmp_path):
    path = os.path.join(tmp_path, "bad.lac")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"nothing": True}, handle)
    with pytest.raises(ValueError):
        capture_io.load_capture(path)


# ------------------------------------------------------------------ exporters
def test_csv_export(tmp_path):
    path = os.path.join(tmp_path, "capture.csv")
    capture_io.export_csv(path, make_session())

    lines = open(path, encoding="utf-8").read().splitlines()
    assert lines[0] == "clk,data"
    assert len(lines) == 65
    assert lines[1] == "0,0"


def test_csv_export_with_time_column(tmp_path):
    path = os.path.join(tmp_path, "capture.csv")
    capture_io.export_csv(path, make_session(), include_time=True)
    lines = open(path, encoding="utf-8").read().splitlines()
    assert lines[0] == "Time,clk,data"
    # The first sample is 16 samples before the trigger, at 1MHz.
    assert lines[1].startswith("-1.6e-05")


def test_vcd_export(tmp_path):
    path = os.path.join(tmp_path, "capture.vcd")
    capture_io.export_vcd(path, make_session())
    content = open(path, encoding="utf-8").read()

    assert "$timescale 1 ns $end" in content
    assert "$var wire 1 ! clk $end" in content
    assert "$var wire 1 # data $end" in content
    assert "$dumpvars" in content
    # 8 samples per clock period at 1MHz -> an edge every 4000 ns.
    assert "#4000\n" in content


def test_export_without_samples_raises(tmp_path):
    session = CaptureSession()
    session.capture_channels = [AnalyzerChannel(channel_number=0)]
    with pytest.raises(ValueError):
        capture_io.export_csv(os.path.join(tmp_path, "x.csv"), session)
    with pytest.raises(ValueError):
        capture_io.export_vcd(os.path.join(tmp_path, "x.vcd"), session)


def test_vcd_times_do_not_drift_at_fractional_periods(tmp_path):
    session = make_session()
    session.frequency = 24_000_000  # 41.67 ns per sample
    path = os.path.join(tmp_path, "capture.vcd")
    capture_io.export_vcd(path, session)
    last = [line for line in open(path, encoding="utf-8").read().splitlines() if line.startswith("#")][-1]

    sample_count = min(len(channel.samples) for channel in session.capture_channels)
    assert int(last[1:]) == round(sample_count * 1e9 / 24_000_000)
