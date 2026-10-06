"""Decoding in the decoder process: the same results as in a thread, cancelling, a process that
died, and the threads that record and synchronise keep running meanwhile. The vectorized parts of
the sync path: hysteresis of an analog sync input, pairing of edges."""

from __future__ import annotations

import os
import random
import sys
import threading
import time

import numpy as np
import pytest

from openscilab.core.capture_io import load_capture
from openscilab.core.profiles import read_profiles_file
from openscilab.driver.remote.server import hysteresis
from openscilab.lab.nodes.remote import pair_edges
from openscilab.sigrok import worker
from openscilab.sigrok.provider import DecoderInstance, SigrokProvider

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import benchmark


def segments(groups) -> list:
    return [(group.decoder_name, group.error, [(row.name, list(row.segments)) for row in group.annotations])
            for group in groups]


@pytest.fixture(scope="module")
def service():
    decoders = worker.DecodeService(workers=1)
    yield decoders
    decoders.close()


def test_the_process_decodes_what_a_thread_decodes(service):
    session = benchmark.uart_session(200_000)
    provider = benchmark.uart_provider()
    assert segments(service.run(provider, session)) == segments(provider.run(session))
    # two decoders stacked, 39 channels
    capture = load_capture(os.path.join(ROOT, "examples", "c64-demo.lac"))
    provider = SigrokProvider()
    provider.load_configuration(read_profiles_file(os.path.join(ROOT, "examples", "profiles",
                                                                "c64-expansion-port.json"))[0].decoder_configuration)
    groups = service.run(provider, capture.session)
    assert segments(groups) == segments(provider.run(capture.session))
    assert all(group.instance in provider.instances and group.info is not None for group in groups)


def test_results_name_the_instances_of_a_frozen_provider(service):
    provider = benchmark.uart_provider()
    frozen = provider.frozen()
    groups = service.run(frozen, benchmark.uart_session(50_000))
    assert groups[0].instance is provider.instances[0]


def test_a_missing_decoder_and_a_disabled_one(service):
    provider = SigrokProvider()
    provider.instances = [DecoderInstance("no_such_decoder", "X", {0: 0}),
                          DecoderInstance("uart", "off", {0: 0}, {"baudrate": 115200}, enabled=False)]
    groups = service.run(provider, benchmark.uart_session(10_000))
    assert len(groups) == 1 and "not available" in groups[0].error


def test_cancelling_ends_the_decode_early(service):
    session = benchmark.uart_session(4_000_000)
    provider = benchmark.uart_provider()
    service.run(provider, benchmark.uart_session(1000))  # (warm)
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    start = time.monotonic()
    service.run(provider, session, cancelled=cancel.is_set)
    assert time.monotonic() - start < 2.5  # the whole decode takes about 6 s


def test_a_process_that_died_is_started_again(service):
    provider = benchmark.uart_provider()
    service.run(provider, benchmark.uart_session(1000))
    for running in service._workers:
        running.process.kill()
        running.process.join(5)
    groups = service.run(provider, benchmark.uart_session(20_000))
    assert groups and groups[0].error is None


def test_decoding_does_not_stall_the_other_threads(service):
    session = benchmark.uart_session(1_000_000)
    provider = benchmark.uart_provider()
    service.run(provider, benchmark.uart_session(1000))  # (warm)
    result = benchmark.stalls(lambda: service.run(provider, session))
    # a thread that wants to run every millisecond: in a thread of this process it waited up to
    # about 100 ms; with the decoder process as long as when nothing else runs (a loaded test machine
    # gets some room)
    assert result["probe late, 99 % (ms)"] < 15


# ------------------------------------------------------------------- sync path
def test_hysteresis_of_an_analog_sync_input():
    rng = np.random.default_rng(3)
    noise = rng.normal(0, 0.05, 5000)
    assert hysteresis(noise, float(noise.min()), float(noise.max())) is None  # only noise: no level yet
    signal = np.concatenate([np.zeros(1000), np.full(1000, 3.3), np.zeros(1000)]) + rng.normal(0, 0.05, 3000)
    levels = hysteresis(signal, 0.0, 3.3)
    assert list(np.nonzero(np.diff(levels))[0] + 1) == [1000, 2000]
    assert list(hysteresis(np.full(10, 1.6), 0.0, 3.3, previous=1)) == [1] * 10  # in the band: it keeps


def test_pairing_finds_the_nearest_edge_of_the_same_level():
    rng = random.Random(2)
    local = np.cumsum([rng.uniform(0.02, 0.06) for _ in range(200)])
    local_edges = [(float(at), index % 2) for index, at in enumerate(local)]
    device = [(float(at) + 0.0007, index % 2, ("edge", index)) for index, at in enumerate(local)]
    pairs: dict = {}
    pair_edges(local_edges, device, lambda at: at, 0.008, pairs)
    assert len(pairs) == 200
    assert all(pairs[round(at, 9)][1] == ("edge", index) for index, (at, _level) in enumerate(local_edges))
    # the wrong level is never taken; an edge far away not at all
    pairs = {}
    pair_edges([(float(local[0]), 1)], device[:1], lambda at: at, 0.008, pairs)
    assert pairs == {}
    pair_edges([(float(local[0]) + 1.0, 0)], device, lambda at: at, 0.008, pairs)
    assert pairs == {}
