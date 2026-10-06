"""Faults the example library brought to light: edges between stream blocks, buffers of captures,
ports with dots, outputs a simulated capture sees in virtual time, texts of values."""

from __future__ import annotations

import numpy as np

from openscilab.core import signals
from openscilab.lab import yaml_io
from openscilab.lab.engine import Engine
from openscilab.lab.engine.runtime import summarize
from openscilab.lab.model import Edge, PortRef


def test_edges_between_the_blocks_of_a_stream_are_counted():
    flow = yaml_io.loads("""
flow: t
nodes:
  la: {type: device.instrument, address: sim:free}
  stream: {type: device.stream, channels: [D15], rate: 1 MHz, duration: 200 ms}
  count: {type: measure.count, edge: rising}
edges:
  - la.device -> stream.device
  - stream.D15 -> count.in
""")
    result = Engine(flow, mode="virtual").run(timeout=30)
    assert result.ok, result.error
    assert result.values[("count", "out")].value >= 199  # 200 ms of 1 kHz (it counted 180)


def test_separate_captures_do_not_make_an_edge_between_them():
    from openscilab.lab.nodes.measure import Count

    count = Count.__new__(Count)
    count.params = {"edge": "rising", "total": True}
    count.total, count.last = 0.0, {}
    count.p = lambda name, default=None: count.params.get(name, default)
    low = signals.Digital(values=[0, 0, 0], time=signals.TimeBase.uniform(1000.0, 0.0))
    high_later = signals.Digital(values=[1, 1, 1], time=signals.TimeBase.uniform(1000.0, 1.0))
    high_next = signals.Digital(values=[1, 1], time=signals.TimeBase.uniform(1000.0, 1.003))
    assert count.measure(low) == 0
    assert count.measure(high_later) == 0  # a second later: not the same line
    count.last = {"": (0, 1.003)}
    assert count.measure(high_next) == 1  # right after the block before: the edge between them


def test_the_buffer_joins_the_captures_of_a_stream():
    flow = yaml_io.loads("""
flow: t
nodes:
  la: {type: device.instrument, address: sim:free}
  stream: {type: device.stream, channels: [D15, D8], rate: 1 MHz, duration: 100 ms}
  buffer: {type: data.buffer, length: 30 ms}
edges:
  - la.device -> stream.device
  - stream.capture -> buffer.in
""")
    result = Engine(flow, mode="virtual").run(timeout=30)
    joined = result.values[("buffer", "out")]
    assert isinstance(joined, signals.Capture) and set(joined.channels) == {"D15", "D8"}
    assert joined.sample_count == 30_000 and abs(joined.start - 0.07) < 0.002  # the last 30 ms


def test_ports_with_dots_are_wired():
    edge = Edge.parse("capture.B2.GP12 -> f.in")
    assert edge.source == PortRef("capture", "B2.GP12") and str(edge) == "capture.B2.GP12 -> f.in"


def test_a_capture_in_virtual_time_sees_pulses_started_when_it_was_armed():
    flow = yaml_io.loads("""
flow: t
nodes:
  uno: {type: device.instrument, address: sim:uno}
  capture: {type: device.capture, channels: [D3], rate: 100 kHz, samples: 448, trigger: {edge: rising, source: D3}}
  pulse: {type: gpio.pulse, pin: D7, width: 1 ms}
edges:
  - uno.device -> capture.device
  - uno.device -> pulse.device
  - capture.armed -> pulse.trigger
""")
    result = Engine(flow, mode="virtual").run(timeout=30)
    # it searched the signal before the pulse came ("no trigger"), or missed the edge right where the
    # search began (the pulse starts after the same latency as the capture)
    assert result.ok, result.error
    assert np.count_nonzero(result.values[("capture", "D3")].values) > 0


def test_texts_of_values():
    assert summarize(signals.Event(times=[0.0], data=["capture-001.lac"])) == "capture-001.lac"
    assert summarize(signals.Event(times=[0.0, 1.0])) == "2 events"
    assert summarize(b"\x19\x00") == "19 00"
    assert summarize({"frequency": signals.Scalar(value=1000.0, unit="Hz"), "duty": 0.5}) == \
        "frequency: 1 kHz, duty: 0.5"
    assert summarize(signals.Scalar(value=0.5)) == "0.5"  # no "500 m" without a unit
