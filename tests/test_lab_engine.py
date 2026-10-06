"""The engine: data and control plane, virtual time, breakpoints, stopping."""

from __future__ import annotations

import threading
import time

import pytest

from openscilab.core import signals
from openscilab.lab import Flow, FlowError, In, Out, Param, node, yaml_io
from openscilab.lab.engine import Engine, NodeError, NodeRuntime
from openscilab.lab.engine import engine as engine_module
from openscilab.lab.nodes.registry import Registry, collect


@pytest.fixture
def registry():
    """The built-in nodes and a few test nodes."""
    registry = Registry().copy()

    @node("test.collect", inputs=[In("in", multiple=True)], params=[Param("delay", "quantity", 0, "s")], register=False)
    class Collect(NodeRuntime):
        async def setup(self):
            self.items = []
            self.ctx.engine.collected = getattr(self.ctx.engine, "collected", {})
            self.ctx.engine.collected[self.node.id] = self.items

        async def on_input(self, port, value):
            delay = self.q("delay") or 0
            if delay:
                await self.ctx.sleep(delay)
            self.items.append((round(self.ctx.now(), 9), value))

    @node("test.burst", outputs=[Out("out", "Scalar")], params=[Param("count", "int", 200)], register=False)
    class Burst(NodeRuntime):
        async def run(self):
            for index in range(int(self.p("count"))):
                await self.ctx.send("out", float(index))

    @node("test.fail", inputs=[In("in")], register=False)
    class Fail(NodeRuntime):
        async def on_input(self, port, value):
            raise NodeError("broken on purpose")

    @node("test.noise", outputs=[Out("out", "Scalar")], params=[Param("count", "int", 5)], register=False)
    class Noise(NodeRuntime):
        async def run(self):
            for _ in range(int(self.p("count"))):
                self.ctx.emit("out", float(self.ctx.rng.normal()))
                await self.ctx.sleep(0.1)

    @node("test.double", inputs=[In("x", "Scalar", optional=False)], outputs=[Out("y", "Scalar")], register=False)
    def double(x):
        return 2 * x

    @node("test.waiter", outputs=[Out("out", "Scalar")], register=False)
    async def waiter(ctx):
        await ctx.sleep(3600)
        ctx.emit("out", ctx.now())

    for spec in collect(locals()):
        registry.add(spec)
    return registry


def run(flow: Flow, registry, **options):
    engine = Engine(flow, registry=registry, mode=options.pop("mode", "virtual"), **options)
    return engine, engine.run(timeout=20)


# ----------------------------------------------------------------- data plane
def test_timer_values_flow_through_a_function_node(registry):
    flow = Flow("x")
    flow.add_node("control.timer", "timer", interval="10 ms", count=4)
    flow.add_node("test.double", "double")
    flow.add_node("test.collect", "out")
    flow.connect("timer.index", "double.x")
    flow.connect("double.y", "out.in")

    engine, result = run(flow, registry)

    assert result.ok and result.state == "finished"
    assert engine.collected["out"] == [(0.0, 0.0), (0.01, 2.0), (0.02, 4.0), (0.03, 6.0)]
    assert result.time == pytest.approx(0.03)
    assert result.value("double", "y") == 6.0
    assert result.node_states == {"timer": "done", "double": "done", "out": "done"}


def test_back_pressure_holds_a_fast_source(registry, monkeypatch):
    monkeypatch.setattr(engine_module, "HIGH_WATER", 4)
    monkeypatch.setattr(engine_module, "LOW_WATER", 1)
    flow = Flow("x")
    flow.add_node("test.burst", "burst", count=50)
    flow.add_node("test.collect", "slow", delay="1 ms")
    flow.connect("burst.out", "slow.in")
    queued = []

    engine = Engine(flow, registry=registry, mode="virtual")
    original = engine._emit

    def emit(node_id, port, value):
        original(node_id, port, value)
        queued.append(len(engine._mailboxes["slow"].items))

    engine._emit = emit
    result = engine.run(timeout=20)

    assert result.ok
    assert [value for _time, value in engine.collected["slow"]] == [float(index) for index in range(50)]
    assert max(queued) <= 5  # the source waited instead of filling the mailbox
    assert result.time == pytest.approx(0.05)


# -------------------------------------------------------------- control plane
def test_a_sweep_waits_for_next_and_dwells(registry):
    flow = Flow("x")
    flow.add_node("control.sweep", "sweep", start=0, stop=1, step=0.5, dwell="100 ms")
    flow.add_node("test.collect", "values")
    flow.add_node("test.collect", "ack", delay="30 ms")
    flow.connect("sweep.value", "values.in")
    flow.connect("sweep.step", "ack.in")
    flow.add_node("control.timer", "never", interval="1 s", count=0)  # endless: duration ends the flow
    flow.connect("never.tick", "values.in")

    engine, result = run(flow, registry, duration=0.5)

    times = [stamp for stamp, value in engine.collected["values"] if not isinstance(value, signals.Event)]
    assert times == [0.0, 0.1, 0.2]
    assert result.ok and result.time == pytest.approx(0.5)


def test_python_coroutine_nodes_sleep_in_virtual_time(registry):
    flow = Flow("x")
    flow.add_node("test.waiter", "wait")
    flow.add_node("test.collect", "out")
    flow.connect("wait.out", "out.in")
    started = time.monotonic()
    engine, result = run(flow, registry)
    assert engine.collected["out"] == [(3600.0, 3600.0)]
    assert time.monotonic() - started < 2


def test_virtual_time_is_deterministic(registry):
    flow = Flow("x", settings={"seed": 3})
    flow.add_node("test.noise", "noise", count=5)
    flow.add_node("test.collect", "out")
    flow.connect("noise.out", "out.in")
    first = run(flow, registry)[0].collected["out"]
    second = run(flow, registry)[0].collected["out"]
    other = run(flow, registry, seed=4)[0].collected["out"]
    assert first == second
    assert first != other


def test_real_time_runs_as_long_as_the_flow(registry):
    flow = Flow("x")
    flow.add_node("control.timer", "timer", interval="50 ms", count=3)
    flow.add_node("test.collect", "out")
    flow.connect("timer.tick", "out.in")
    started = time.monotonic()
    engine, result = run(flow, registry, mode="real")
    elapsed = time.monotonic() - started
    assert result.ok and len(engine.collected["out"]) == 3
    assert 0.09 <= elapsed < 2


# --------------------------------------------------------------------- errors
def test_an_error_stops_the_flow_and_names_the_node(registry):
    flow = Flow("x")
    flow.add_node("control.timer", "timer", interval="1 s", count=0)
    flow.add_node("test.fail", "bad")
    flow.connect("timer.tick", "bad.in")
    engine, result = run(flow, registry)
    assert result.state == "error"
    assert result.error == "bad: broken on purpose"
    assert result.node_states["bad"] == "error"


def test_a_flow_with_errors_does_not_start(registry):
    flow = Flow("x")
    flow.add_node("test.double", "double")
    with pytest.raises(FlowError, match="not wired"):
        Engine(flow, registry=registry)


# --------------------------------------------------------- breakpoints, stop
def test_breakpoint_pauses_and_steps(registry):
    flow = Flow("x")
    flow.add_node("control.timer", "timer", interval="10 ms", count=3)
    flow.add_node("test.collect", "out")
    flow.connect("timer.tick", "out.in")
    engine = Engine(flow, registry=registry, mode="virtual", breakpoints=["out"])
    states = []
    paused = threading.Event()

    def listen(event):
        if event.kind == "flow":
            states.append((event.state, event.message))
            if event.state == "paused":
                paused.set()

    engine.subscribe(listen)
    results = []
    thread = threading.Thread(target=lambda: results.append(engine.run(timeout=20)))
    thread.start()
    assert paused.wait(5)
    assert engine.collected["out"] == []
    paused.clear()
    engine.step()  # one value, then paused again at the breakpoint
    assert paused.wait(5)
    assert len(engine.collected["out"]) == 1
    engine.set_breakpoint("out", False)
    engine.resume()
    thread.join(10)

    assert results and results[0].ok
    assert len(engine.collected["out"]) == 3
    assert ("paused", "breakpoint at out") in states


def test_stop_ends_an_endless_flow(registry):
    flow = Flow("x")
    flow.add_node("control.timer", "timer", interval="10 ms", count=0)
    flow.add_node("test.collect", "out")
    flow.connect("timer.tick", "out.in")
    engine = Engine(flow, registry=registry, mode="real")
    results = []
    thread = threading.Thread(target=lambda: results.append(engine.run(timeout=20)))
    thread.start()
    time.sleep(0.15)
    engine.stop()
    thread.join(5)
    assert results[0].state == "stopped"
    assert len(engine.collected["out"]) >= 3


def test_events_report_nodes_and_values(registry):
    flow = Flow("x")
    flow.add_node("control.timer", "timer", interval="1 ms", count=2)
    flow.add_node("test.collect", "out")
    flow.connect("timer.index", "out.in")
    engine = Engine(flow, registry=registry, mode="virtual")
    events = []
    engine.subscribe(events.append)
    engine.run(timeout=20)
    kinds = {event.kind for event in events}
    assert {"flow", "node", "value"} <= kinds
    assert [event.value for event in events if event.kind == "value" and event.port == "index"] == [0.0, 1.0]
    assert events[0].state == "running" and events[-1].state == "finished"


# --------------------------------------------------------- with the simulator
def test_the_counter_flow_writes_a_capture(tmp_path):
    from openscilab.core import capture_io

    flow = yaml_io.load("examples/flows/counter.flow.yaml")
    started = time.monotonic()
    engine = Engine(flow, mode="virtual", data_dir=str(tmp_path))
    result = engine.run(timeout=20)
    assert result.ok, result.error
    assert time.monotonic() - started < 1
    capture = capture_io.load_capture(str(tmp_path / "counter.lac"))
    session = capture.session
    assert session.frequency == 4_000_000 and session.total_samples == 20000
    names = [channel.channel_name for channel in session.capture_channels]
    assert names == ["D0", "D1", "D2", "D3", "D8"]
    clock = session.capture_channels[4].samples
    # Triggered on the rising edge of D8 (pre: 0): 1 MHz at 4 MHz is 1100 1100 ...
    assert list(clock[:8]) == [1, 1, 0, 0, 1, 1, 0, 0]
    assert engine.views.last("scope").sample_count == 20000


def test_a_stream_arrives_in_blocks(registry, tmp_path):
    flow = Flow("x")
    flow.add_node("device.instrument", "sim", address="sim:free")
    flow.add_node("device.stream", "stream", channels=["D15"], rate="100 kHz", duration="50 ms")
    flow.connect("sim.device", "stream.device")
    flow.add_node("test.collect", "blocks")
    flow.add_node("view.scope", "scope")
    flow.connect("stream.capture", "blocks.in")
    flow.connect("stream.capture", "scope.in")
    engine, result = run(flow, registry)
    assert result.ok, result.error
    blocks = [value for _time, value in engine.collected["blocks"]]
    assert len(blocks) == 5 and all(block.sample_count == 1000 for block in blocks)
    joined = engine.views.last("scope")
    assert joined.sample_count == 5000
    # 1 kHz square at 100 kHz: 50 samples high, 50 low
    assert list(joined.digital["D15"][:100]) == [1] * 50 + [0] * 50
    assert result.time == pytest.approx(0.05)


def test_unknown_devices_are_reported(registry):
    flow = Flow("x")
    flow.add_node("device.instrument", "sim", address="sim:nothing")
    flow.add_node("device.capture", "cap", samples=10)
    flow.connect("sim.device", "cap.device")
    engine, result = run(flow, registry)
    assert result.state == "error" and "unknown simulator profile" in result.error
    flow.nodes["sim"].params["address"] = ""  # no address and no project
    engine, result = run(flow, registry)
    assert result.state == "error" and "no address" in result.error
