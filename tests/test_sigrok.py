"""Tests of the sigrokdecode compatible runtime and the decoder engine."""

from __future__ import annotations

import numpy as np
import pytest

from pipilogicanalyzer.driver.models import AnalyzerChannel, CaptureSession
from pipilogicanalyzer.sigrok import runtime
from pipilogicanalyzer.sigrok.engine import OptionType, run_decoder
from pipilogicanalyzer.sigrok.provider import DecoderInstance, SigrokProvider
from pipilogicanalyzer.sigrok.runtime import ConditionMatcher, DecodeContext, DecoderStop


@pytest.fixture
def clock_matcher() -> ConditionMatcher:
    # 0 1 0 1 0 1 0 1
    clock = np.array([0, 1, 0, 1, 0, 1, 0, 1], dtype=np.uint8)
    data = np.array([0, 0, 1, 1, 1, 1, 0, 0], dtype=np.uint8)
    return ConditionMatcher({0: clock, 1: data}, clock.size)


# ------------------------------------------------------------------ matching
def test_rising_edges(clock_matcher):
    assert clock_matcher.find(-1, [{0: "r"}])[0] == 1
    assert clock_matcher.find(1, [{0: "r"}])[0] == 3


def test_falling_edges(clock_matcher):
    assert clock_matcher.find(-1, [{0: "f"}])[0] == 2


def test_levels(clock_matcher):
    assert clock_matcher.find(-1, [{0: "h"}])[0] == 1
    assert clock_matcher.find(-1, [{0: "l"}])[0] == 0


def test_any_edge_and_stable(clock_matcher):
    assert clock_matcher.find(-1, [{0: "e"}])[0] == 1
    assert clock_matcher.find(-1, [{1: "s"}])[0] == 0


def test_first_sample_is_never_an_edge(clock_matcher):
    sample, _ = clock_matcher.find(-1, [{1: "e"}])
    assert sample == 2  # the data line changes at sample 2, not at 0


def test_conditions_are_anded_inside_a_dict(clock_matcher):
    sample, matched = clock_matcher.find(-1, [{0: "r", 1: "h"}])
    assert sample == 3
    assert matched == (True,)


def test_conditions_are_ored_between_dicts(clock_matcher):
    sample, matched = clock_matcher.find(-1, [{0: "f"}, {0: "r"}])
    assert sample == 1
    assert matched == (False, True)


def test_skip_advances_a_fixed_number_of_samples(clock_matcher):
    assert clock_matcher.find(0, [{"skip": 3}])[0] == 3
    assert clock_matcher.find(-1, [{"skip": 1}])[0] == 0


def test_conditions_on_unassigned_channels_never_match(clock_matcher):
    assert clock_matcher.find(-1, [{5: "r"}]) is None


def test_search_returns_none_at_the_end_of_the_capture(clock_matcher):
    assert clock_matcher.find(7, [{0: "r"}]) is None
    assert clock_matcher.find(7, [{"skip": 1}]) is None


def test_matching_across_chunks(monkeypatch):
    monkeypatch.setattr(runtime, "CHUNK_SIZE", 8)
    samples = np.zeros(100, dtype=np.uint8)
    samples[70:] = 1
    matcher = ConditionMatcher({0: samples}, samples.size)
    assert matcher.find(-1, [{0: "r"}])[0] == 70


def test_pins_report_unassigned_channels():
    matcher = ConditionMatcher({0: np.array([1, 0], dtype=np.uint8)}, 2)
    assert matcher.pins(0, 3) == (1, runtime.UNASSIGNED_PIN, runtime.UNASSIGNED_PIN)


def test_wait_without_conditions_advances_one_sample(clock_matcher):
    context = DecodeContext(clock_matcher, channel_count=2, sample_count=8, assigned_channels=[0, 1])
    pins, _matched, sample = context.wait(None)
    assert sample == 0
    assert pins == (0, 0)
    _pins, _matched, sample = context.wait(None)
    assert sample == 1


def test_wait_with_zero_skip_returns_the_current_sample(clock_matcher):
    context = DecodeContext(clock_matcher, channel_count=2, sample_count=8, assigned_channels=[0, 1])
    context.wait({0: "r"})
    _pins, _matched, sample = context.wait({"skip": 0})
    assert sample == 1  # unchanged


def test_wait_raises_at_the_end_of_the_capture(clock_matcher):
    context = DecodeContext(clock_matcher, channel_count=2, sample_count=8, assigned_channels=[0, 1])
    with pytest.raises(DecoderStop):
        for _ in range(20):
            context.wait(None)


# -------------------------------------------------------------------- engine
def test_registry_loads_the_fixture_decoder(decoder_registry):
    info = decoder_registry.get("testdec")
    assert info is not None
    assert info.longname == "Test decoder"
    assert info.is_base_decoder
    assert [channel.id for channel in info.channels] == ["clk", "aux"]
    assert [channel.required for channel in info.channels] == [True, False]
    assert not decoder_registry.load_errors


def test_option_types_are_detected(decoder_registry):
    options = {option.id: option for option in decoder_registry.get("testdec").options}
    assert options["label"].option_type is OptionType.STRING
    assert options["skip"].option_type is OptionType.INTEGER
    assert options["mode"].option_type is OptionType.LIST
    assert options["mode"].values == ("all", "first")


def test_running_a_decoder_produces_annotations(decoder_registry):
    info = decoder_registry.get("testdec")
    clock = np.array([0, 1, 0, 1, 0, 1, 0, 0], dtype=np.uint8)

    run = run_decoder(info, {0: clock}, clock.size, 1_000_000)

    assert run.error is None
    assert len(run.annotations) == 1
    annotation = run.annotations[0]
    assert annotation.name == "Edges"
    assert [segment.first_sample for segment in annotation.segments] == [1, 3, 5]
    assert annotation.segments[0].values == ["edge 1"]
    assert len(run.python_output) == 3


def test_decoder_options_are_applied(decoder_registry):
    info = decoder_registry.get("testdec")
    clock = np.array([0, 1, 0, 1], dtype=np.uint8)

    run = run_decoder(info, {0: clock}, clock.size, 1_000, options={"label": "tick"})
    assert run.annotations[0].segments[0].values == ["tick 1"]


def test_list_options_stop_the_decoder_early(decoder_registry):
    info = decoder_registry.get("testdec")
    clock = np.array([0, 1, 0, 1, 0, 1], dtype=np.uint8)

    run = run_decoder(info, {0: clock}, clock.size, 1_000, options={"mode": "first"})
    rows = {annotation.name: annotation for annotation in run.annotations}
    assert len(rows["Edges"].segments) == 1
    assert rows["Infos"].segments[0].values == ["first only"]


def test_missing_required_channel_yields_no_annotations(decoder_registry):
    info = decoder_registry.get("testdec")
    run = run_decoder(info, {}, 10, 1_000)
    assert run.annotations == []
    assert run.error is None


# ------------------------------------------------------------------ provider
def make_session(samples: np.ndarray) -> CaptureSession:
    session = CaptureSession(frequency=1_000, pre_trigger_samples=0, post_trigger_samples=samples.size)
    session.capture_channels = [
        AnalyzerChannel(channel_number=0, channel_name="clk", samples=samples)
    ]
    return session


def test_provider_runs_the_configured_instances(decoder_registry):
    provider = SigrokProvider(decoder_registry)
    provider.add_instance(
        DecoderInstance(decoder_id="testdec", label="Test", channel_map={0: 0}, color_index=1)
    )

    session = make_session(np.array([0, 1, 0, 1], dtype=np.uint8))
    groups = provider.run(session)

    assert len(groups) == 1
    assert groups[0].decoder_name == "Test"
    assert groups[0].error is None
    assert groups[0].row_count == 1


def test_provider_reports_unknown_decoders(decoder_registry):
    provider = SigrokProvider(decoder_registry)
    provider.add_instance(DecoderInstance(decoder_id="nope"))
    groups = provider.run(make_session(np.array([0, 1], dtype=np.uint8)))
    assert "not available" in groups[0].error


def test_disabled_instances_are_skipped(decoder_registry):
    provider = SigrokProvider(decoder_registry)
    provider.add_instance(
        DecoderInstance(decoder_id="testdec", channel_map={0: 0}, enabled=False)
    )
    assert provider.run(make_session(np.array([0, 1], dtype=np.uint8))) == []


def test_missing_channels_are_reported(decoder_registry):
    provider = SigrokProvider(decoder_registry)
    instance = provider.add_instance(DecoderInstance(decoder_id="testdec"))
    assert provider.missing_channels(instance) == ["CLK"]


def test_instances_serialise_with_their_parent(decoder_registry):
    provider = SigrokProvider(decoder_registry)
    parent = provider.add_instance(DecoderInstance(decoder_id="testdec", channel_map={0: 0}))
    provider.add_instance(DecoderInstance(decoder_id="testdec", parent=parent))

    data = provider.to_list()
    assert data[1]["parent"] == 0

    restored = SigrokProvider(decoder_registry)
    restored.from_list(data)
    assert restored.instances[1].parent is restored.instances[0]


def test_removing_a_decoder_removes_the_stacked_ones(decoder_registry):
    provider = SigrokProvider(decoder_registry)
    parent = provider.add_instance(DecoderInstance(decoder_id="testdec"))
    provider.add_instance(DecoderInstance(decoder_id="testdec", parent=parent))

    provider.remove_instance(parent)
    assert provider.instances == []
