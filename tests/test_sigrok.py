"""Tests of the sigrokdecode compatible runtime and the decoder engine."""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

import numpy as np
import pytest

from openscilab.driver.models import AnalyzerChannel, CaptureSession
from openscilab.sigrok import runtime
from openscilab.sigrok.engine import OptionType, run_decoder
from openscilab.sigrok.provider import DecoderInstance, SigrokProvider
from openscilab.sigrok.runtime import ConditionMatcher, DecodeContext, DecoderStop


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
    assert annotation.segments[0].values == ("edge 1",)
    assert len(run.python_output) == 3


def test_decoder_options_are_applied(decoder_registry):
    info = decoder_registry.get("testdec")
    clock = np.array([0, 1, 0, 1], dtype=np.uint8)

    run = run_decoder(info, {0: clock}, clock.size, 1_000, options={"label": "tick"})
    assert run.annotations[0].segments[0].values == ("tick 1",)


def test_list_options_stop_the_decoder_early(decoder_registry):
    info = decoder_registry.get("testdec")
    clock = np.array([0, 1, 0, 1, 0, 1], dtype=np.uint8)

    run = run_decoder(info, {0: clock}, clock.size, 1_000, options={"mode": "first"})
    rows = {annotation.name: annotation for annotation in run.annotations}
    assert len(rows["Edges"].segments) == 1
    assert rows["Infos"].segments[0].values == ("first only",)


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


# ------------------------------------------------------- the search of wait()
class ReferenceMatcher(ConditionMatcher):
    """The search as it was before the edge index: every condition as a mask over a window.
    Slow and simple – what the indexed search must agree with."""

    def find(
        self, current_sample: int, conditions: Sequence[Mapping[Any, Any]]
    ) -> Optional[tuple[int, tuple[bool, ...]]]:
        """Return the first sample after ``current_sample`` matching any condition."""
        start = max(current_sample + 1, 0)
        if start >= self.sample_count:
            return None

        handled, fast = self._pure_skip_match(current_sample, conditions)
        if handled:
            return fast

        position = start
        while position < self.sample_count:
            end = min(position + runtime.CHUNK_SIZE, self.sample_count)
            masks = [
                self._condition_mask(condition, position, end, current_sample)
                for condition in conditions
            ]

            best_index: Optional[int] = None
            for mask in masks:
                if mask is None:
                    continue
                hits = np.flatnonzero(mask)
                if hits.size:
                    candidate = int(hits[0])
                    if best_index is None or candidate < best_index:
                        best_index = candidate

            if best_index is not None:
                matched = tuple(
                    bool(mask[best_index]) if mask is not None else False for mask in masks
                )
                return position + best_index, matched

            position = end

        return None

    def _pure_skip_match(
        self, current_sample: int, conditions: Sequence[Mapping[Any, Any]]
    ) -> tuple[bool, Optional[tuple[int, tuple[bool, ...]]]]:
        """Fast path for ``wait()``/``wait({'skip': n})``.

        Advancing a fixed number of samples is by far the most common condition
        (every decoder that walks a clock uses it), so it is resolved with plain
        arithmetic instead of building sample masks.
        """
        targets: list[Optional[int]] = []
        for condition in conditions:
            if len(condition) != 1 or "skip" not in condition:
                return False, None
            target = current_sample + int(condition["skip"])
            targets.append(target if target > current_sample else None)

        valid = [target for target in targets if target is not None and target < self.sample_count]
        if not valid:
            return True, None

        best = min(valid)
        return True, (best, tuple(target == best for target in targets))

    def _condition_mask(
        self, condition: Mapping[Any, Any], begin: int, end: int, current_sample: int
    ) -> Optional[np.ndarray]:
        """Boolean mask of the samples in ``[begin, end)`` satisfying ``condition``."""
        length = end - begin
        mask = np.ones(length, dtype=bool)

        for key, value in condition.items():
            if key == "skip":
                target = current_sample + int(value)
                if target < begin or target >= end:
                    return None
                skip_mask = np.zeros(length, dtype=bool)
                skip_mask[target - begin] = True
                mask &= skip_mask
                continue

            channel = int(key)
            samples = self.channel_samples.get(channel)
            if samples is None:
                # Condition on a channel the user did not assign: never matches.
                return None

            current = samples[begin:end]
            if begin == 0:
                previous = np.concatenate((current[:1], current[:-1]))
            else:
                previous = samples[begin - 1 : end - 1]

            term = str(value).lower()
            if term == "l":
                mask &= current == 0
            elif term == "h":
                mask &= current == 1
            elif term == "r":
                mask &= (current == 1) & (previous == 0)
            elif term == "f":
                mask &= (current == 0) & (previous == 1)
            elif term == "e":
                mask &= current != previous
            elif term == "s":
                mask &= current == previous
            else:
                return None

            if not mask.any():
                return mask

        return mask


def random_condition(rng, channels: int) -> dict:
    condition: dict = {}
    for _ in range(int(rng.integers(0, 4))):
        if rng.random() < 0.25:
            condition["skip"] = int(rng.integers(-3, 40))
        else:
            # channel ``channels`` is not assigned; "n" and "x" are terms that never match
            condition[int(rng.integers(0, channels + 1))] = str(rng.choice(list("lhrfesnRx")))
    return condition


@pytest.mark.parametrize("chunk", [8, 50, 65536])
def test_the_indexed_search_agrees_with_the_masks(monkeypatch, chunk):
    monkeypatch.setattr(runtime, "CHUNK_SIZE", chunk)
    rng = np.random.default_rng(chunk)
    for case in range(400):
        count = int(rng.integers(1, 400))
        # runs of random length; now and then a value that is neither 0 nor 1
        samples = {}
        for channel in range(3):
            edges = np.cumsum(rng.random(count) < rng.choice([0.02, 0.2, 0.6])) % 2
            if case % 7 == 0:
                edges = edges + (rng.random(count) < 0.05) * 2
            samples[channel] = edges.astype(np.uint8)
        new, old = ConditionMatcher(samples, count), ReferenceMatcher(samples, count)
        for _ in range(20):
            current = int(rng.integers(-1, count + 2))
            conditions = [random_condition(rng, 3) for _ in range(int(rng.integers(1, 4)))]
            assert new.find(current, conditions) == old.find(current, conditions), (current, conditions)


def test_single_conditions_do_not_build_masks(monkeypatch):
    """An edge far away costs a lookup, not masks over all the samples up to it."""
    samples = np.zeros(1_000_000, np.uint8)
    samples[900_000:] = 1
    matcher = ConditionMatcher({0: samples}, samples.size)

    def no_masks(*_args):
        raise AssertionError("searched with masks")

    monkeypatch.setattr(matcher, "_condition_mask", no_masks)
    assert matcher.find(0, [{0: "r"}]) == (900_000, (True,))
    assert matcher.find(0, [{0: "h"}]) == (900_000, (True,))
    assert matcher.find(0, [{0: "f"}]) is None
    assert matcher.find(10, [{0: "r"}, {"skip": 100}]) == (110, (False, True))
    assert matcher.find(899_990, [{0: "e"}, {"skip": 100}]) == (900_000, (True, False))
    assert matcher.find(899_999, [{"skip": 1, 0: "h"}]) == (900_000, (True,))


def test_conditions_on_several_channels_look_no_further_than_the_next_match(monkeypatch):
    clock = (np.arange(100_000) // 5 % 2).astype(np.uint8)
    data = np.zeros(100_000, np.uint8)
    matcher = ConditionMatcher({0: clock, 1: data}, clock.size)
    searched = []
    mask = matcher._condition_mask

    def counted(condition, begin, end, current):
        searched.append(end - begin)
        return mask(condition, begin, end, current)

    monkeypatch.setattr(matcher, "_condition_mask", counted)
    # as an I²C decoder waits: the next rising clock edge, or a start/stop (data edge while high)
    assert matcher.find(0, [{0: "r"}, {0: "h", 1: "f"}, {0: "h", 1: "r"}]) == (5, (True, False, False))
    assert sum(searched) <= 2 * 5  # up to the clock edge, not a window of 65536 samples


def test_the_index_of_a_capture_on_disk(tmp_path):
    samples = np.memmap(tmp_path / "channel.bin", dtype=np.uint8, mode="w+", shape=(50_000,))
    samples[20_000:30_000] = 1
    matcher = ConditionMatcher({0: samples}, samples.size)
    assert matcher.find(0, [{0: "r"}]) == (20_000, (True,))
    assert matcher.find(20_000, [{0: "f"}]) == (30_000, (True,))
