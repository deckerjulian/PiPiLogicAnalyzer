"""The time of samples: arrival envelope, start bounds, loopback latency, time scales, sync edges and
their correction, the timing of an acquisition and the latencies kept per instrument."""

from __future__ import annotations

import math
import random

import numpy as np
import pytest

from openscilab.core import preferences, timing
from openscilab_device.timing import (
    ArrivalClock,
    Latency,
    Loopback,
    fit_line,
    local_offset,
    sync_intervals,
    timescale_now,
)

RATE = 10_000.0


def usb_stream(seconds: float, start: float = 100.0, drift: float = 0.0, latency: float = 0.0015,
               frame: float = 0.001, block: int = 100, seed: int = 1):
    """Arrivals of a USB device: sample k is taken at start + k / (rate (1 + drift)); a block leaves at
    the next USB frame after its last sample and arrives ``latency`` plus some jitter later."""
    rng = random.Random(seed)
    period = 1.0 / (RATE * (1.0 + drift))
    arrivals = []
    for first in range(0, int(seconds * RATE), block):
        last = first + block - 1
        taken = start + last * period
        sent = math.ceil(taken / frame) * frame
        arrivals.append((last, sent + latency + rng.expovariate(1 / 0.0008)))
    return arrivals, period


def test_the_envelope_drops_the_jitter_and_keeps_the_shortest_latency():
    arrivals, _period = usb_stream(2.0)
    clock = ArrivalClock(RATE)
    for last, arrived in arrivals:
        clock.add(last, arrived)
    error = clock.envelope(0) - 100.0
    assert 0.0015 <= error <= 0.0015 + 0.0011  # the latency, at most a frame more, no jitter
    assert clock.jitter() > 0.0003
    estimate = clock.estimate(0)
    assert estimate.method == "arrival" and math.isinf(estimate.uncertainty)


def test_the_start_command_bounds_it_from_below():
    arrivals, _period = usb_stream(1.0)
    clock = ArrivalClock(RATE)
    clock.start(100.0 - 0.002)  # the start command, 2 ms before the first sample
    for last, arrived in arrivals:
        clock.add(last, arrived)
    estimate = clock.estimate(0)
    assert estimate.method == "bounds"
    assert abs(estimate.time - 100.0) <= estimate.uncertainty
    assert estimate.uncertainty < 0.003


def test_the_drift_of_the_sample_clock_is_fitted():
    arrivals, period = usb_stream(30.0, drift=40e-6)
    clock = ArrivalClock(RATE)
    for last, arrived in arrivals:
        clock.add(last, arrived)
    assert clock.drift == pytest.approx(40e-6, abs=8e-6)  # 1 ms frames over 30 s
    last = arrivals[-1][0]
    assert clock.envelope(last) - (100.0 + last * period) == pytest.approx(0.002, abs=0.0012)


def test_a_loopback_measures_the_latency():
    # output latency 1 ms (a USB command), input latency 1.5 ms: the loop is 2.5 ms
    arrivals, _period = usb_stream(2.0, latency=0.0015)
    clock = ArrivalClock(RATE)
    for last, arrived in arrivals:
        clock.add(last, arrived)
    loop = Loopback()
    rng = random.Random(3)
    for command in np.arange(100.2, 101.8, 0.1):
        loop.command(command)
        edge_time = command + 0.001 + rng.uniform(0, 0.0005)
        loop.edge(math.ceil((edge_time - 100.0) * RATE))
    latency = loop.latency(clock)
    assert latency.source == "loopback"
    true = clock.envelope(0) - 100.0  # what the envelope is late
    assert abs(latency.value - true) <= latency.uncertainty
    corrected = clock.estimate(0, latency)
    assert abs(corrected.time - 100.0) <= latency.uncertainty and corrected.method == "latency"
    with pytest.raises(ValueError):
        Loopback().latency(clock)


def test_fit_line_and_sync_intervals():
    assert fit_line([(0.0, 1.0), (1.0, 3.0), (2.0, 5.0)]) == pytest.approx((1.0, 2.0, 0.0))
    first = [value for value, _ in zip(sync_intervals(5), range(20))]
    assert first == [value for value, _ in zip(sync_intervals(5), range(20))]
    assert all(0.02 <= value <= 0.06 for value in first) and len(set(first)) == 20


def test_time_scales():
    assert timescale_now("tai") - timescale_now("utc") == pytest.approx(37.0, abs=0.01)
    assert local_offset("utc") == pytest.approx(timescale_now("utc") - __import__("time").monotonic(), abs=0.01)
    with pytest.raises(ValueError):
        timescale_now("gps")


# --------------------------------------------------------------- openSciLab side
def test_edges_and_their_matching():
    rng = random.Random(1)
    times = np.cumsum([rng.uniform(0.02, 0.06) for _ in range(40)]) + 5.0
    rate = 100_000.0
    count = int((times[-1] - 5.0 + 0.1) * rate)
    t = 5.0 + np.arange(count) / rate
    levels = (np.searchsorted(times, t, side="right") % 2).astype(np.uint8)
    found, found_levels, _index = timing.edges(levels, 5.0, rate)
    assert len(found) == 40 and np.allclose(found, times, atol=1 / rate)
    assert list(found_levels[:2]) == [1, 0]
    analog = levels * 3.3 + 0.05
    assert len(timing.edges(analog.astype(float), 5.0, rate)[0]) == 40
    # the same edges seen by a clock 0.3 s late with 20 ppm drift
    other = (times - 5.0) * (1 + 20e-6) + 5.3
    pairs = timing.match(times, other, max_shift=1.0)
    assert len(pairs) == 40
    correction = timing.fit_correction(pairs)
    assert correction.apply(other[10]) == pytest.approx(times[10], abs=1e-6)
    shared = timing.fit_correction(pairs, fit_drift=False)
    assert shared.drift == 0.0 and shared.apply(other[0]) == pytest.approx(times[0], abs=2e-5)


def test_an_acquisition_and_its_methods(monkeypatch):
    acquisition = timing.Acquisition(RATE, commanded=99.998)
    assert acquisition.state().method == timing.METHOD_ARRIVAL
    arrivals, _period = usb_stream(1.0)
    for last, arrived in arrivals:
        acquisition.arrived(last, arrived)
    assert acquisition.raw(0).method == timing.METHOD_BOUNDS
    assert abs(acquisition.time_of(0) - 100.0) < 0.003
    # handed on blocks: the index of a time
    acquisition.given(acquisition.time_of(0), 0)
    acquisition.given(acquisition.time_of(5000), 5000)
    assert acquisition.index_of(acquisition.time_of(5000) + 0.01) == pytest.approx(5100)
    # a sync signal moves it
    acquisition.correct(timing.Correction(offset=-0.001, drift=0.0, at=100.0, uncertainty=2e-5, reference="la"))
    state = acquisition.state()
    assert state.method == timing.METHOD_SIGNAL and state.reference == "la" and state.uncertainty == 2e-5
    # a time scale needs this computer's clock to follow one
    stamped = timing.Acquisition(RATE, commanded=0.0)
    assert not stamped.stamped(timescale_now("utc"), "utc")
    monkeypatch.setitem(preferences._load(), "timing.host_clock", "ptp")
    assert timing.host_timescale_accuracy() == timing.HOST_ACCURACY["ptp"]
    now = __import__("time").monotonic()
    assert stamped.stamped(timescale_now("utc"), "utc", accuracy=1e-6)
    assert stamped.raw(0).time == pytest.approx(now, abs=0.01) and stamped.raw(0).method == timing.METHOD_TIMESCALE
    exact = timing.Acquisition(RATE, commanded=0.0, exact_start=7.0)
    assert exact.time_of(RATE) == 8.0 and exact.state().method == timing.METHOD_SIMULATED


def test_latencies_are_kept_per_instrument_and_settings():
    class Pico:
        kind, uri = "Pico", "pico:/dev/cu.usbmodem1"

    stream = timing.calibration_key(Pico(), "stream", 1e6)
    capture = timing.calibration_key(Pico(), "buffer", 1e6, 100_000)
    assert stream != capture and "100000" in capture
    assert timing.stored_latency(stream) is None
    timing.store_latency(stream, Latency(0.0012, 0.0003, "loopback"))
    assert timing.stored_latency(stream) == Latency(0.0012, 0.0003, "loopback")
    timing.store_latency(stream, None)
    assert timing.stored_latency(stream) is None
