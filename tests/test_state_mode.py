"""State analysis of a timing capture."""

from __future__ import annotations

import os
import time

import numpy as np
import pytest

from openscilab.core.state_mode import resample_on_clock
from openscilab.driver.models import AnalyzerChannel, BusDefinition, CaptureSession, EdgeKind

CLOCK = [0, 0, 1, 1, 0, 0, 1, 1, 0, 0, 1, 1]  # rising at 2, 6, 10; falling at 4, 8
DATA = [0, 0, 1, 0, 0, 0, 0, 1, 1, 0, 0, 1]
GATE = [1, 1, 1, 1, 1, 1, 0, 0, 1, 1, 1, 1]


def make_session(**kwargs) -> CaptureSession:
    session = CaptureSession(
        frequency=1000,
        pre_trigger_samples=5,
        post_trigger_samples=7,
        capture_channels=[
            AnalyzerChannel(channel_number=n, channel_name=name, samples=np.array(s, dtype=np.uint8))
            for n, name, s in ((0, "CLK", CLOCK), (1, "D", DATA), (3, "G", GATE))
        ],
        buses=[BusDefinition(name="B", channels=[1, 3])],
        **kwargs,
    )
    return session


def channel(state, number):
    return next(c for c in state.session.capture_channels if c.channel_number == number)


def test_rising_edges():
    state = resample_on_clock(make_session(), 0)
    assert state.positions.tolist() == [2, 6, 10]
    assert [c.channel_number for c in state.session.capture_channels] == [1, 3]
    assert channel(state, 1).samples.tolist() == [1, 0, 0]
    assert channel(state, 1).channel_name == "D"
    assert channel(state, 3).samples.tolist() == [1, 0, 1]
    assert state.session.frequency == 250  # one edge every 4 samples at 1 kHz
    assert state.session.pre_trigger_samples == 1  # first state at/after sample 5 is #1
    assert state.session.post_trigger_samples == 2
    assert state.session.sample_count() == 3
    assert state.session.buses[0].channels == [1, 3]
    assert state.session.clock_channel == 0
    assert state.state_count == 3
    assert state.state_at(7) == 1 and state.state_at(1) == -1
    assert state.sample_of(5) == 10


def test_falling_any_and_keep_clock():
    state = resample_on_clock(make_session(), 0, edge=EdgeKind.FALLING, keep_clock=True)
    assert state.positions.tolist() == [4, 8]
    assert channel(state, 0).samples.tolist() == [0, 0]
    assert channel(state, 1).samples.tolist() == [0, 1]
    state = resample_on_clock(make_session(), 0, edge=EdgeKind.ANY)
    assert state.positions.tolist() == [2, 4, 6, 8, 10]


def test_delay_clamped():
    state = resample_on_clock(make_session(), 0, delay=1)
    # sample points 3, 7, 11
    assert channel(state, 1).samples.tolist() == [0, 1, 1]
    assert state.positions.tolist() == [2, 6, 10]
    assert state.delay == 1
    state = resample_on_clock(make_session(), 0, delay=5)
    # points 7, 11, 15 -> clamped to 11
    assert channel(state, 1).samples.tolist() == [1, 1, 1]
    state = resample_on_clock(make_session(), 0, delay=-10)
    assert channel(state, 1).samples.tolist() == [0, 0, 0]


def test_qualifier():
    state = resample_on_clock(make_session(), 0, qualifier_mask=1 << 3, qualifier_value=1 << 3)
    assert state.positions.tolist() == [2, 10]
    assert channel(state, 1).samples.tolist() == [1, 0]
    state = resample_on_clock(make_session(), 0, qualifier_mask=1 << 3, qualifier_value=0)
    assert state.positions.tolist() == [6]
    # a missing qualifier channel reads 0
    state = resample_on_clock(make_session(), 0, qualifier_mask=1 << 9, qualifier_value=1 << 9)
    assert state.state_count == 0
    assert state.session.pre_trigger_samples == 0


def test_original_untouched_and_errors():
    session = make_session()
    resample_on_clock(session, 0)
    assert session.capture_channels[1].samples.tolist() == DATA
    with pytest.raises(ValueError):
        resample_on_clock(session, 7)


def test_large_capture_is_fast():
    count = 10_000_000
    clock = (np.arange(count) // 5 % 2).astype(np.uint8)  # rising every 10 samples
    data = np.random.default_rng(6).integers(0, 2, count).astype(np.uint8)
    session = CaptureSession(
        frequency=100_000_000,
        pre_trigger_samples=count // 2,
        capture_channels=[
            AnalyzerChannel(channel_number=0, samples=clock),
            *(AnalyzerChannel(channel_number=n, samples=data) for n in range(1, 9)),
        ],
    )
    begin = time.perf_counter()
    state = resample_on_clock(session, 0, delay=2, qualifier_mask=0b10, qualifier_value=0b10)
    elapsed = time.perf_counter() - begin
    assert np.all(data[state.positions + 2] == 1)
    assert 0 < state.state_count < count // 10
    assert abs(state.session.frequency - 10_000_000) <= 1
    assert elapsed < 2.0


# ------------------------------------------------------------ suggested read point
def test_the_read_point_is_where_the_data_is_stable():
    """Data changing with the rising clock edge is read at the falling one."""
    from openscilab.core.state_mode import suggest_sampling

    clock = np.tile(np.array([0] * 10 + [1] * 10, np.uint8), 50)
    rng = np.random.default_rng(3)
    values = rng.integers(0, 2, size=(4, 50)).astype(np.uint8)
    # A new value from shortly before each rising edge (the VIC phase of a C64 ends there)
    data = [np.roll(np.repeat(row, 20), -12) for row in values]
    faster = (np.arange(1000) // 3 % 2).astype(np.uint8)  # a dot clock: left out
    session = CaptureSession(frequency=20_000_000)
    session.capture_channels = [AnalyzerChannel(channel_number=0, samples=clock)] + [
        AnalyzerChannel(channel_number=n + 1, samples=line) for n, line in enumerate([*data, faster])
    ]
    suggestion = suggest_sampling(session, 0)
    assert suggestion.edge == EdgeKind.FALLING and suggestion.unstable[(suggestion.edge, suggestion.delay)] == 0


DEMO = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples", "c64-demo.lac")


@pytest.mark.skipif(not os.path.isfile(DEMO), reason="the demo capture is missing")
def test_c64_states_match_the_bus_cycles_of_the_decoder():
    """Resampled on PHI2 as suggested, every state holds the address and data of its bus cycle."""
    import re

    from openscilab.core import capture_io
    from openscilab.core.profiles import read_profiles_file
    from openscilab.core.state_mode import suggest_sampling
    from openscilab.sigrok.engine import DecoderRegistry
    from openscilab.sigrok.provider import SigrokProvider

    session = capture_io.load_capture(DEMO).session
    registry = DecoderRegistry()
    registry.load()
    provider = SigrokProvider(registry)
    provider.load_configuration(read_profiles_file(DEMO.replace("c64-demo.lac", os.path.join("profiles", "c64-expansion-port.json")))[0].decoder_configuration)
    (group,) = [item for item in provider.run(session) if item.instance.decoder_id == "c64bus"]
    cycles = next(row for row in group.annotations if row.name == "Bus cycles").segments
    reference = []
    for segment in cycles:
        match = re.match(r"[RW] \$([0-9A-F]{4}) = \$([0-9A-F]{2})", segment.values[0])
        if match:
            reference.append((segment.sample_point, int(match.group(1), 16), int(match.group(2), 16)))

    by_name = {channel.channel_name.split(" ")[0]: channel.channel_number for channel in session.capture_channels}
    clock = next(number for name, number in by_name.items() if name.startswith("Φ2") or name.upper().startswith("PHI2"))
    suggestion = suggest_sampling(session, clock)
    assert suggestion.edge == EdgeKind.FALLING
    state = resample_on_clock(session, clock, suggestion.edge, suggestion.delay, keep_clock=True)
    levels = {channel.channel_name.split(" ")[0]: channel.samples for channel in state.session.capture_channels}

    def value(prefix: str, width: int, index: int) -> int:
        return sum(int(levels[f"{prefix}{bit}"][index]) << bit for bit in range(width))

    checked = 0
    for read_point, address, data in reference[:2000]:
        index = int(np.searchsorted(state.positions, read_point))
        assert abs(int(state.positions[index]) - read_point) <= 4
        assert (value("A", 16, index), value("D", 8, index)) == (address, data)
        checked += 1
    assert checked > 100
