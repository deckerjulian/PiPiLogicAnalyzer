"""Soak: streams of a simulated Pico for minutes - in this process, in a device process and in a
running flow - while the memory is watched: it must not grow (tracemalloc), the samples must keep
coming. Only on request (the suite leaves it out)::

    OPENSCILAB_SOAK_MINUTES=30 pytest -m soak tests/test_soak.py -s

The minutes are shared by the three parts; the workflow *Stability* runs it on request (Actions →
Stability → Run workflow). The lines it prints are the report."""

from __future__ import annotations

import os
import resource
import sys
import time
import tracemalloc

import pytest

pytestmark = pytest.mark.soak

MINUTES = float(os.environ.get("OPENSCILAB_SOAK_MINUTES", "30"))
#: the memory this process may grow while it streams (bytes, tracemalloc): a leak of a few bytes
#: per block adds up to more over minutes, a stable path to about nothing
ALLOWED_GROWTH = 16 * 1024 * 1024
CHANNELS = list(range(8))
RATE = "500k"
BLOCK = "0.25 s"


def seconds_of_part(parts: int = 3) -> float:
    return max(MINUTES * 60 / parts, 10)


def rss_mib() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return usage / (1024 * 1024 if sys.platform == "darwin" else 1024)


class Watch:
    """Memory of this process over a part: a line now and then, the growth at the end."""

    def __init__(self, what: str) -> None:
        self.what = what
        self.started = time.monotonic()
        self.reported = self.started
        self.count = 0
        tracemalloc.start()
        self.before = tracemalloc.take_snapshot()

    def rebase(self) -> None:
        """Measure from here: what a start allocates once (a stream's buffer) is no growth."""
        self.before = tracemalloc.take_snapshot()
        self.count = 0

    def tick(self, count: int = 1) -> None:
        self.count += count
        now = time.monotonic()
        if now - self.reported >= 30:
            self.reported = now
            current, peak = tracemalloc.get_traced_memory()
            print(f"{self.what}: {now - self.started:5.0f} s, {self.count} blocks, "
                  f"{current / 1e6:.1f} MB traced (peak {peak / 1e6:.1f}), rss {rss_mib():.0f} MiB", flush=True)

    def finish(self) -> int:
        after = tracemalloc.take_snapshot()
        tracemalloc.stop()
        growth = sum(stat.size_diff for stat in after.compare_to(self.before, "filename"))
        print(f"{self.what}: done after {time.monotonic() - self.started:.0f} s, {self.count} blocks, "
              f"memory grew by {growth / 1e6:+.2f} MB, rss {rss_mib():.0f} MiB", flush=True)
        if growth > ALLOWED_GROWTH:
            print("The biggest growths:")
            for stat in after.compare_to(self.before, "lineno")[:8]:
                print(f"  {stat.size_diff / 1e3:+.1f} kB  {stat.traceback.format()[-1].strip()}")
        return growth


def stream_for(device, seconds: float, what: str) -> None:
    for _ in range(10):  # (warm-up: caches, the first allocations, the last session the driver keeps)
        device.capture(channels=CHANNELS, rate=RATE, duration=BLOCK, mode="stream")
    expected = len(device.capture(channels=CHANNELS, rate=RATE, duration=BLOCK, mode="stream").samples(0))
    watch = Watch(what)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        capture = device.capture(channels=CHANNELS, rate=RATE, duration=BLOCK, mode="stream")
        assert len(capture.samples(0)) == expected and len(capture.samples(7)) == expected
        del capture
        watch.tick()
    assert watch.finish() < ALLOWED_GROWTH


def test_streams_in_this_process():
    from openscilab import api

    with api.open("sim:pico") as device:
        stream_for(device, seconds_of_part(), "stream in the process")


def test_streams_from_a_device_process():
    from openscilab import api
    from openscilab.core import shared_arrays

    with api.open("sim:pico", process=True) as device:
        stream_for(device, seconds_of_part(), "stream from a device process")
        assert device.driver.process.alive
    shared_arrays.clean_segments()  # (nothing of ours left behind: raises or logs nothing)


def test_a_flow_streams_for_the_time():
    from openscilab.lab import yaml_io
    from openscilab.lab.engine import Engine

    seconds = seconds_of_part()
    flow = yaml_io.loads(f"""
flow: Soak
nodes:
  pico: {{type: device.instrument, address: "sim:pico"}}
  stream: {{type: device.stream, channels: [0, 1, 2, 3], rate: 200 kHz, duration: {seconds:.0f} s}}
  line: {{type: convert.channel, channel: 0}}
  frequency: {{type: measure.frequency}}
  shown: {{type: view.number}}
edges:
  - pico.device -> stream.device
  - stream.capture -> line.in
  - line.out -> frequency.in
  - frequency.out -> shown.in
""")
    engine = Engine(flow)
    watch = Watch("flow with a stream")
    blocks = []

    def seen(event) -> None:
        if event.kind == "value" and event.node == "stream" and event.port == "capture":
            blocks.append(1)
            if len(blocks) == 200:
                watch.rebase()  # (the stream is running: its buffers are allocated)
            watch.tick()

    engine.subscribe(seen)
    result = engine.run(timeout=seconds + 60)
    assert result.ok, result.error
    assert len(blocks) > seconds * 2, len(blocks)  # (a block every quarter second at least)
    assert watch.finish() < ALLOWED_GROWTH
