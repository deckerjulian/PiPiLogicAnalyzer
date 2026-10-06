"""What a flow leaves behind when it does not end on its own: an error, Ctrl+C or Stop still
write what was measured; subflows with required inputs run."""

from __future__ import annotations

import os
import signal
import sys
import threading
import time

import pytest

from openscilab.lab import Flow, In, Out, yaml_io
from openscilab.lab.engine import Engine, NodeError, NodeRuntime
from openscilab.lab.nodes.registry import Registry, collect, node


@pytest.fixture
def registry():
    registry = Registry().copy()

    @node("test.fail_after", inputs=[In("in")], register=False)
    class FailAfter(NodeRuntime):
        async def setup(self):
            self.seen = 0

        async def on_input(self, port, value):
            self.seen += 1
            if self.seen == 3:
                raise NodeError("broken on purpose")

    @node("test.no_setup", inputs=[In("in")], register=False)
    class NoSetup(NodeRuntime):
        async def setup(self):
            raise NodeError("cannot start")

    @node("test.finishes", inputs=[In("in")], register=False)
    class Finishes(NodeRuntime):
        async def setup(self):
            self.ctx.engine.finished = getattr(self.ctx.engine, "finished", [])

        async def finish(self):
            self.ctx.engine.finished.append(self.node.id)

    @node("test.double", inputs=[In("x", "Scalar", optional=False)], outputs=[Out("y", "Scalar")], register=False)
    def double(x):
        return 2 * x

    for spec in collect(locals()):
        registry.add(spec)
    return registry


def logger_flow(tmp_path, interval="1 s") -> Flow:
    flow = Flow("x")
    flow.add_node("control.timer", "timer", interval=interval, count=0)
    flow.add_node("data.logger", "log", path=str(tmp_path / "log.csv"), flush=1000)
    flow.connect("timer.index", "log.in")
    return flow


def rows(path) -> int:
    with open(path, encoding="utf-8") as handle:
        return len(handle.read().splitlines()) - 1


def test_an_error_still_writes_the_log_and_the_report(registry, tmp_path):
    flow = logger_flow(tmp_path)
    flow.add_node("test.fail_after", "bad")
    flow.add_node("report.write", "report", path=str(tmp_path / "report.html"))
    flow.connect("timer.index", "bad.in")
    result = Engine(flow, registry=registry, mode="virtual").run(timeout=20)
    assert result.state == "error" and result.error == "bad: broken on purpose"
    assert rows(tmp_path / "log.csv") >= 2  # buffered rows (flush: 1000) are not lost
    report = (tmp_path / "report.html").read_text(encoding="utf-8")
    assert "The flow ended with an error" in report and "broken on purpose" in report


def test_nodes_that_never_started_do_not_finish(registry):
    flow = Flow("x")
    flow.add_node("control.timer", "timer", interval="1 s", count=2)
    flow.add_node("test.finishes", "first")
    flow.add_node("test.no_setup", "bad")
    flow.add_node("test.finishes", "late")
    for target in ("first", "bad", "late"):
        flow.connect("timer.index", f"{target}.in")
    engine = Engine(flow, registry=registry, mode="virtual")
    result = engine.run(timeout=20)
    assert result.error == "bad: cannot start"
    assert engine.finished == ["first"]  # "late" was never set up
    assert result.node_states["late"] != "error"


def test_stop_before_the_flow_runs_is_not_lost(registry, tmp_path):
    engine = Engine(logger_flow(tmp_path, interval="10 ms"), registry=registry, mode="real")
    engine.stop()  # the user pressed Stop before the engine thread got to run
    started = time.monotonic()
    result = engine.run(timeout=20)
    assert result.state == "stopped" and time.monotonic() - started < 2


@pytest.mark.skipif(sys.platform == "win32", reason="signals for event loops need a POSIX system")
def test_ctrl_c_ends_the_flow_and_keeps_the_rows(registry, tmp_path):
    engine = Engine(logger_flow(tmp_path, interval="10 ms"), registry=registry, mode="real")
    threading.Timer(0.3, lambda: os.kill(os.getpid(), signal.SIGINT)).start()
    before = signal.getsignal(signal.SIGINT)
    result = engine.run(timeout=20)
    assert signal.getsignal(signal.SIGINT) in (before, signal.default_int_handler)
    assert result.state == "stopped"
    assert rows(tmp_path / "log.csv") >= 5


SUBFLOWS = """\
flow: Outer
nodes:
  timer: {type: control.timer, interval: 1 ms, count: 3}
  inner: {type: subflow.twice}
edges:
  - timer.index -> inner.value
subflows:
  twice:
    flow: Twice
    inputs:
      value: nested.value
    outputs:
      result: nested.result
    nodes:
      nested: {type: subflow.double}
    edges: []
  double:
    flow: Double
    inputs:
      value: double.x
    outputs:
      result: double.y
    nodes:
      double: {type: test.double}
    edges: []
"""


def test_subflows_with_required_inputs_run(registry):
    flow = yaml_io.loads(SUBFLOWS)
    # the required input of test.double is wired through the exposed input; the subflow "twice"
    # uses "double", which the flow around it defines
    assert [str(problem) for problem in flow.validate(registry)] == []
    result = Engine(flow, registry=registry, mode="virtual").run(timeout=20)
    assert result.ok
    assert result.values[("inner/nested/double", "y")] == 4.0


def test_a_subflow_input_that_is_not_exposed_is_still_a_problem(registry):
    flow = yaml_io.loads(SUBFLOWS)
    del flow.subflows["double"].inputs["value"]
    problems = [str(problem) for problem in flow.validate(registry)]
    assert any("is not wired" in problem and "double" in problem for problem in problems)


# ------------------------------------------------------- flows that run long
@pytest.fixture
def more(registry):
    """Further test nodes."""
    from openscilab.core import signals
    from openscilab.lab import Param

    @node("test.times", inputs=[In("in", multiple=True)], register=False)
    class Times(NodeRuntime):
        async def setup(self):
            self.ctx.engine.times = []

        async def on_input(self, port, value):
            self.ctx.engine.times.append(self.ctx.now())

    @node("test.echo_twice", inputs=[In("in")], outputs=[Out("out")], params=[Param("first", "bool", False)],
          register=False)
    class EchoTwice(NodeRuntime):
        async def run(self):
            if self.p("first"):
                await self.ctx.send("out", 1.0)

        async def on_input(self, port, value):
            await self.ctx.send("out", value)
            await self.ctx.send("out", value)

    @node("test.named", outputs=[Out("out")], params=[Param("name", "str", "x"), Param("count", "int", 3)],
          register=False)
    class Named(NodeRuntime):
        async def run(self):
            for index in range(int(self.p("count"))):
                await self.ctx.send("out", signals.Scalar(name=str(self.p("name")), value=float(index),
                                                          at=self.ctx.now()))
                await self.ctx.sleep(0.01)

    @node("test.stream", register=False)
    class Stream(NodeRuntime):
        async def run(self):
            queue = self.ctx.external_queue()  # a stream that never ends on its own
            await queue.get()

    @node("test.device_call", register=False)
    class DeviceCall(NodeRuntime):
        async def run(self):
            device = self.ctx.engine.test_device
            self.ctx.engine.called_in = await self.ctx.device_call(device, threading.get_ident)

    for spec in collect(locals()):
        registry.add(spec)
    return registry


def test_views_keep_only_what_they_show_now(more):
    flow = Flow("x")
    flow.add_node("control.timer", "timer", interval="1 ms", count=3000)
    flow.add_node("view.strip_chart", "chart", window="100 ms")
    flow.add_node("report.section", "section")
    flow.add_node("report.check", "check", low=1.0)
    flow.connect("timer.index", "chart.in")
    flow.connect("timer.index", "section.in")
    flow.connect("timer.index", "check.in")
    engine = Engine(flow, registry=more, mode="virtual")
    result = engine.run(timeout=30)
    assert result.ok
    items = engine.views.items
    assert [item[0] for item in items if item[0] != "check"] == ["strip_chart", "report"]  # one each
    assert len([item for item in items if item[0] == "check"]) == 3000  # every verdict
    assert [bool(item[2]) for item in items if item[0] == "check"][:3] == [False, True, True]
    assert len(engine.views.last("chart")["in"][0]) <= 102  # the window, not the whole run
    from openscilab.lab.report import report_html

    text = report_html("x", engine.views)
    assert text.count("<h2>Results</h2>") == 1 and "<li>2999</li>" in text


def test_the_log_of_a_long_run_is_bounded(more, monkeypatch):
    from openscilab.lab.engine import engine as engine_module

    monkeypatch.setattr(engine_module, "LOG_LINES", 50)
    flow = Flow("x")
    flow.add_node("control.sequence", "sequence", steps=[{"log": "again"}, {"wait": "1 ms"}], repeat=500)
    result = Engine(flow, registry=more, mode="virtual").run(timeout=30)
    assert result.ok and len(result.log) == 50


def test_a_timer_does_not_drift(more):
    flow = Flow("x")
    flow.add_node("control.timer", "timer", interval="0.1 s", count=50)
    flow.add_node("test.times", "times")
    flow.connect("timer.tick", "times.in")
    engine = Engine(flow, registry=more, mode="virtual")
    assert engine.run(timeout=20).ok
    # the n-th tick at exactly n * interval (adding the interval tick after tick drifts)
    assert engine.times == [index * 0.1 for index in range(50)]


def test_an_endless_timer_without_an_interval_is_refused(more):
    flow = Flow("x")
    flow.add_node("control.timer", "timer", interval="0 s", count=0)
    result = Engine(flow, registry=more, mode="virtual").run(timeout=20)
    assert result.state == "error" and "needs an interval" in result.error


def test_a_sequence_that_never_waits_can_be_stopped(more):
    flow = Flow("x")
    flow.add_node("control.sequence", "sequence", steps=[{"log": "again"}], repeat=0)
    engine = Engine(flow, registry=more, mode="virtual")
    threading.Timer(0.2, engine.stop).start()
    started = time.monotonic()
    result = engine.run(timeout=20)
    assert result.state == "stopped" and time.monotonic() - started < 5


def test_nodes_stuck_on_each_other_are_an_error(more, monkeypatch):
    from openscilab.lab.engine import engine as engine_module

    monkeypatch.setattr(engine_module, "HIGH_WATER", 4)
    monkeypatch.setattr(engine_module, "LOW_WATER", 1)
    flow = Flow("x")
    flow.add_node("test.echo_twice", "a", first=True)
    flow.add_node("test.echo_twice", "b")
    flow.connect("a.out", "b.in")
    flow.connect("b.out", "a.in")  # every value comes back twice: both fill up and wait for the other
    result = Engine(flow, registry=more, mode="virtual").run(timeout=20)
    assert result.state == "error" and result.error.startswith("deadlock: a, b")


def test_a_real_time_flow_ends_at_its_duration_with_an_open_stream(more):
    flow = Flow("x")
    flow.add_node("test.stream", "stream")
    started = time.monotonic()
    result = Engine(flow, registry=more, mode="real", duration=0.2).run(timeout=20)
    assert result.state == "finished" and 0.19 <= time.monotonic() - started < 2


def test_the_logger_names_its_sources_and_logs_table_rows_once(more, tmp_path):
    import csv

    flow = Flow("x")
    flow.add_node("test.named", "volts", name="A0")
    flow.add_node("test.named", "level", name="D2")
    flow.add_node("data.logger", "log", path=str(tmp_path / "log.csv"))
    flow.connect("volts.out", "log.in")
    flow.connect("level.out", "log.in")
    flow.add_node("control.timer", "timer", interval="1 ms", count=5)
    flow.add_node("data.table", "table")
    flow.add_node("data.logger", "rows", path=str(tmp_path / "rows.csv"))
    flow.connect("timer.index", "table.in")
    flow.connect("table.table", "rows.in")
    assert Engine(flow, registry=more, mode="virtual").run(timeout=20).ok
    logged = list(csv.reader(open(tmp_path / "log.csv", encoding="utf-8")))
    assert sorted({row[1] for row in logged[1:]}) == ["A0", "D2"]  # not "log" for every row
    table_rows = list(csv.reader(open(tmp_path / "rows.csv", encoding="utf-8")))
    assert [row[2] for row in table_rows[1:]] == ["0.0", "1.0", "2.0", "3.0", "4.0"]  # each row once


def test_real_devices_are_called_in_a_thread(more):
    flow = Flow("x")
    flow.add_node("test.device_call", "call")

    class Device:  # no simulator: nothing says it answers at once
        capture = None

    real = Engine(flow, registry=more, mode="real")
    real.test_device = Device()
    assert real.run(timeout=20).ok and real.called_in != threading.get_ident()

    # in virtual time everything is called directly: the run stays deterministic
    virtual = Engine(flow, registry=more, mode="virtual")
    virtual.test_device = Device()
    assert virtual.run(timeout=20).ok and virtual.called_in == threading.get_ident()

    from openscilab.driver.simulated import open_simulated

    simulated = Engine(flow, registry=more, mode="real")
    simulated.test_device = open_simulated("uno")
    assert simulated.run(timeout=20).ok and simulated.called_in == threading.get_ident()


# ---------------------------------------------------------- loading and saving
def test_text_parameters_survive_saving(tmp_path):
    for text in ("one\ntwo", "one\ntwo\n", "one\ntwo\n\n\n", " indented first\nline", "\nstarts empty", "a\n\n  b\n"):
        flow = Flow("x")
        flow.add_node("control.python", "code", code=text)
        again = yaml_io.loads(yaml_io.dumps(flow))
        assert again.nodes["code"].params["code"] == text, repr(text)
        assert yaml_io.dumps(again) == yaml_io.dumps(flow)


def test_parameters_may_have_any_name():
    flow = yaml_io.loads("flow: x\nnodes:\n  a: {type: control.timer, position: 3, node_id: n, type_name: t}\n")
    assert flow.nodes["a"].params == {"position": 3, "node_id": "n", "type_name": "t"}
    assert flow.nodes["a"].position is None
    from openscilab.lab import FlowError

    with pytest.raises(FlowError):
        yaml_io.loads("flow: x\nnodes:\n  a: {type: control.timer, at: [left, top]}\n")


def test_function_nodes_get_their_parameters(registry):
    from openscilab.lab import Param

    @node("test.scale", params=[Param("factor", "float", 3.0)], register=False)
    def scale(x, params):
        return x * params["factor"]

    registry.add(scale.node_spec)
    assert [port.name for port in scale.node_spec.inputs] == ["x"]  # "params" is no input to wire
    flow = Flow("x")
    flow.add_node("control.timer", "timer", interval="1 ms", count=3)
    flow.add_node("test.scale", "scale", factor=3.0)
    flow.connect("timer.index", "scale.x")
    assert flow.errors(registry) == []
    result = Engine(flow, registry=registry, mode="virtual").run(timeout=20)
    assert result.ok and result.values[("scale", "out")] == 6.0


def test_a_capture_slower_than_one_sample_a_second_is_refused(registry):
    flow = Flow("x")
    flow.add_node("device.instrument", "sim", address="sim:free")
    flow.add_node("device.capture", "capture", channels=["D0"], rate="0.2 Hz", samples=10)
    flow.connect("sim.device", "capture.device")
    result = Engine(flow, registry=registry, mode="virtual").run(timeout=20)
    assert result.state == "error" and "at least 1 Hz" in result.error


def test_browse_gives_the_path_the_flow_resolves(tmp_path):
    from openscilab.ui.flow.inspector import flow_path

    root, data = str(tmp_path), str(tmp_path / "data")
    engine = Engine(Flow("x"), mode="virtual", base_dir=root, data_dir=data)
    for chosen in (tmp_path / "data" / "log.csv", tmp_path / "data" / "sub" / "log.csv",
                   tmp_path / "waveforms" / "sine.wave.yaml", tmp_path / "top.csv"):
        stored = flow_path(str(chosen), data, root)
        assert not os.path.isabs(stored)
        assert os.path.normpath(engine.resolve_path(stored)) == str(chosen), stored
    outside = str(tmp_path.parent / "elsewhere.csv")
    assert flow_path(outside, data, root) == outside


# ------------------------------------------------- found by the second check
@pytest.mark.skipif(sys.platform == "win32", reason="signals for event loops need a POSIX system")
def test_a_flow_gives_the_interrupt_handler_back(registry, tmp_path):
    calls = []

    def mine(_number, _frame):
        calls.append(1)

    before = signal.signal(signal.SIGINT, mine)
    try:
        flow = Flow("x")
        flow.add_node("control.timer", "timer", interval="1 ms", count=3)
        assert Engine(flow, registry=registry, mode="virtual").run(timeout=20).ok
        assert signal.getsignal(signal.SIGINT) is mine  # (it was replaced by the default handler)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        Engine(flow, registry=registry, mode="virtual").run(timeout=20)
        assert signal.getsignal(signal.SIGINT) is signal.SIG_IGN
    finally:
        signal.signal(signal.SIGINT, before)
