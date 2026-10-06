"""The node library: measurement, signal processing, decoders, control, data, views, structure."""

from __future__ import annotations

import csv
import os

import numpy as np
import pytest

from openscilab.core import signals
from openscilab.core.signals import Analog, Digital, Scalar, TimeBase
from openscilab.lab import Flow, NodeRuntime, Out, Param, node, yaml_io
from openscilab.lab.engine import Engine
from openscilab.lab.nodes import dsp
from openscilab.lab.nodes.registry import Registry, collect

ROOT = os.path.join(os.path.dirname(__file__), "..")
DEMO = os.path.join(ROOT, "examples", "demo.lac")


@pytest.fixture
def registry():
    return node_registry()


def node_registry() -> Registry:
    """The built-in nodes and a source that sends the values of its parameter 'values'."""
    registry = Registry().copy()

    @node("test.source", outputs=[Out("out")], params=[Param("values", "any", [])], register=False)
    class Source(NodeRuntime):
        async def run(self):
            for value in self.params["values"]:
                await self.ctx.send("out", value)
                await self.ctx.sleep(0.001)

    @node("test.events", outputs=[Out("out", "Event")], params=[Param("data", "list", [])], register=False)
    class Events(NodeRuntime):
        async def run(self):
            for item in self.params["data"]:
                await self.ctx.sleep(0.01)
                self.ctx.emit("out", signals.Event(times=[self.ctx.now()], data=[item]))

    for spec in collect(locals()):
        registry.add(spec)
    return registry


def run_node(registry, type_name: str, inputs: dict, outputs=("out",), **params):
    """Send ``inputs[port] = [values]`` into one node; returns ``{output: [values]}``."""
    flow = Flow("x")
    flow.add_node(type_name, "node", **params)
    for index, (port, values) in enumerate(inputs.items()):
        source = flow.add_node("test.source", f"source{index}", values=values)
        flow.connect(f"{source.id}.out", f"node.{port}")
    result_values = {name: [] for name in outputs}
    engine = Engine(flow, registry=registry, mode="virtual")
    engine.subscribe(lambda event: event.kind == "value" and event.node == "node" and event.port in result_values
                     and result_values[event.port].append(event.value))
    result = engine.run(timeout=20)
    assert result.ok, result.error
    return result_values


def square(frequency: float, rate: float, count: int, duty: float = 0.5) -> Digital:
    phase = (np.arange(count) * frequency / rate) % 1.0
    return Digital(name="sq", values=(phase < duty).astype(np.uint8), time=TimeBase.uniform(rate))


def sine(frequency: float, rate: float, count: int, amplitude: float = 1.0, offset: float = 0.0) -> Analog:
    t = np.arange(count) / rate
    return Analog(name="sine", values=offset + amplitude * np.sin(2 * np.pi * frequency * t), time=TimeBase.uniform(rate))


# ----------------------------------------------------------------- measurement
def test_digital_measurements(registry):
    signal = square(1000, 100_000, 10_000, duty=0.25)
    assert run_node(registry, "measure.frequency", {"in": [signal]})["out"][0].value == pytest.approx(1000)
    period = run_node(registry, "measure.period", {"in": [signal]})["out"][0]
    assert period.value == pytest.approx(1e-3) and period.unit == "s"
    width = run_node(registry, "measure.pulse_width", {"in": [signal]}, level="high", statistic="max")["out"][0]
    assert width.value == pytest.approx(0.25e-3)
    assert run_node(registry, "measure.duty", {"in": [signal]})["out"][0].value == pytest.approx(0.25)
    counts = run_node(registry, "measure.count", {"in": [signal, signal]})["out"]
    assert [count.value for count in counts] == [99, 198]  # rising edges (100 periods, it starts high), totalled


def test_analog_measurements(registry):
    wave = sine(50, 10_000, 2000, amplitude=2.0, offset=1.0)
    values = {name: run_node(registry, f"measure.{name}", {"in": [wave]})["out"][0] for name in
              ("min", "max", "mean", "rms", "peak_to_peak")}
    assert values["min"].value == pytest.approx(-1.0, abs=1e-3)
    assert values["max"].value == pytest.approx(3.0, abs=1e-3)
    assert values["mean"].value == pytest.approx(1.0, abs=1e-6) and values["mean"].unit == "V"
    assert values["rms"].value == pytest.approx(np.sqrt(1 + 2), abs=1e-3)
    assert values["peak_to_peak"].value == pytest.approx(4.0, abs=1e-3)


def test_setup_and_hold(registry):
    clock = square(1000, 100_000, 1000)
    data = Digital(name="d", values=np.roll(square(500, 100_000, 1000).values, 10), time=TimeBase.uniform(100_000))
    result = run_node(registry, "measure.setup_hold", {"clock": [clock], "data": [data]}, outputs=("setup", "hold"))
    assert result["setup"][-1].value > 0 and result["hold"][-1].value > 0


# --------------------------------------------------------- signal processing
def test_threshold_debounce_and_resample(registry):
    analog = Analog(values=[0, 2, 3.3, 1.7, 1.0, 0], time=TimeBase.uniform(10))
    digital = run_node(registry, "dsp.threshold", {"in": [analog]}, threshold="1.65 V", hysteresis="0.4 V")["out"][0]
    assert list(digital.values) == [0, 1, 1, 1, 0, 0]

    bouncy = Digital(values=[0] * 20 + [1, 0, 1, 0, 1] + [1] * 30 + [0, 1] + [1] * 20, time=TimeBase.uniform(1000))
    clean = run_node(registry, "dsp.debounce", {"in": [bouncy]}, time="5 ms")["out"][0]
    assert np.count_nonzero(np.diff(clean.values.astype(int))) == 1

    resampled = run_node(registry, "dsp.resample", {"in": [Analog(values=[0.0, 1.0, 2.0, 3.0], time=TimeBase.uniform(1))]},
                         rate="2 Hz")["out"][0]
    assert resampled.rate == 2 and list(resampled.values) == pytest.approx([0, 0.5, 1, 1.5, 2, 2.5, 3, 3])


def test_fir_filter_continues_across_blocks(registry):
    rate = 10_000
    noisy = sine(50, rate, 4000).values + 0.5 * np.sin(2 * np.pi * 3000 * np.arange(4000) / rate)
    whole = Analog(values=noisy, time=TimeBase.uniform(rate))
    blocks = [Analog(values=noisy[i:i + 1000], time=TimeBase.uniform(rate, i / rate)) for i in range(0, 4000, 1000)]
    once = run_node(registry, "dsp.filter", {"in": [whole]}, kind="lowpass", cutoff="500 Hz")["out"][0]
    parts = run_node(registry, "dsp.filter", {"in": blocks}, kind="lowpass", cutoff="500 Hz")["out"]
    joined = np.concatenate([part.values for part in parts])
    assert np.allclose(joined, once.values)
    # the 3 kHz part is gone, the 50 Hz part stays (delayed by the filter)
    assert np.std(once.values[500:]) == pytest.approx(np.sqrt(0.5), rel=0.05)
    taps = dsp.fir_taps("highpass", 1000, rate, 51)
    assert abs(taps.sum()) < 1e-6


@pytest.mark.skipif(not dsp.HAS_SCIPY, reason="scipy is not installed")
def test_iir_filter_with_scipy(registry):  # pragma: no cover - depends on scipy
    wave = sine(50, 10_000, 4000)
    result = run_node(registry, "dsp.filter", {"in": [wave]}, method="iir", cutoff="200 Hz")["out"][0]
    assert len(result) == 4000


def test_iir_filter_without_scipy_is_explained(registry, monkeypatch):
    monkeypatch.setattr(dsp, "HAS_SCIPY", False)
    flow = Flow("x")
    flow.add_node("test.source", "source", values=[sine(50, 1000, 100)])
    flow.add_node("dsp.filter", "filter", method="iir")
    flow.connect("source.out", "filter.in")
    result = Engine(flow, registry=registry, mode="virtual").run(timeout=20)
    assert result.state == "error" and "need scipy" in result.error


def test_fft_derivative_integral_average_and_math(registry):
    rate = 8000
    spectrum = run_node(registry, "dsp.fft", {"in": [sine(1000, rate, 800)]})["out"][0]
    peak = spectrum.columns["frequency"][int(np.argmax(spectrum.columns["magnitude"]))]
    assert peak == pytest.approx(1000, abs=rate / 800)

    ramp = Analog(values=np.arange(10, dtype=float) * 2, time=TimeBase.uniform(10))
    assert run_node(registry, "dsp.derivative", {"in": [ramp]})["out"][0].values == pytest.approx([20.0] * 10)
    ones = Analog(values=np.ones(10), time=TimeBase.uniform(10))
    following = Analog(values=np.ones(10), time=TimeBase.uniform(10, 1.0))  # the next block of a stream
    integral = run_node(registry, "dsp.integral", {"in": [ones, following]})["out"]
    # 0 at the first sample, up to the last one at 0.9 s; on across the blocks without a gap
    assert integral[0].values[0] == 0.0 and integral[0].values[-1] == pytest.approx(0.9)
    assert integral[1].values[0] == pytest.approx(1.0) and integral[1].values[-1] == pytest.approx(1.9)

    noisy = [Analog(values=np.full(4, value), time=TimeBase.uniform(10)) for value in (1.0, 3.0, 5.0)]
    averaged = run_node(registry, "dsp.average", {"in": noisy}, count=2)["out"]
    assert list(averaged[-1].values) == [4.0] * 4
    moving = run_node(registry, "dsp.average", {"in": [Analog(values=[0, 0, 3, 3], time=TimeBase.uniform(1))]},
                      mode="moving", count=3)["out"][0]
    assert list(moving.values) == pytest.approx([0, 0, 1, 2])
    assert moving.time.start == pytest.approx(-1.0)  # without delay: the mean of 3 samples belongs to the middle one

    result = run_node(registry, "dsp.math", {"a": [ramp], "b": [Scalar(value=1.0)]}, expression="a / 2 + b")["out"]
    assert list(result[-1].values) == pytest.approx(np.arange(10) + 1)
    flow_error = Flow("x")
    flow_error.add_node("test.source", "s", values=[1.0])
    flow_error.add_node("dsp.math", "m", expression="a +")
    flow_error.connect("s.out", "m.a")
    assert "not valid" in Engine(flow_error, registry=registry, mode="virtual").run(timeout=20).error


# -------------------------------------------------------------------- decoders
def test_every_sigrok_decoder_is_a_node(registry):
    types = registry.types()
    for decoder in ("decode.uart", "decode.i2c", "decode.spi", "decode.c64bus"):
        assert decoder in types
    uart = registry.get("decode.uart")
    assert uart.param("baudrate").default == 115200
    assert uart.param("parity").choices[0] == "none"
    assert [port.name for port in uart.outputs] == ["events", "table", "text"]


def test_decoders_against_the_demo_capture(registry):
    flow = Flow("x")
    flow.add_node("data.file_read", "file", path=DEMO)
    flow.add_node("decode.uart", "uart", channels={"rx": "RX"}, baudrate=115200, format="ascii")
    flow.add_node("decode.i2c", "i2c")  # channels found by name (SCL, SDA)
    flow.connect("file.capture", "uart.in")
    flow.connect("file.capture", "i2c.in")
    result = Engine(flow, registry=registry, mode="virtual").run(timeout=30)
    assert result.ok, result.error
    assert result.value("uart", "text") == "LogicAnalyzer"  # what the RX line of the demo holds
    i2c_rows = set(result.value("i2c", "table").columns["row"])
    assert "Address/data" in i2c_rows or any("ddress" in row for row in i2c_rows)
    events = result.value("uart", "events")
    assert events.data[0]["row"] in ("RX bits", "RX data", "RX warnings")


def test_the_uart_check_passes():
    flow = yaml_io.load(os.path.join(ROOT, "examples", "flows", "uart_check.flow.yaml"))
    engine = Engine(flow, mode="virtual")
    result = engine.run(timeout=30)
    assert result.ok, result.error
    assert result.value("check", "result").value is True
    assert engine.views.last("result") is True
    assert len(result.value("table", "table")) > 10


# --------------------------------------------------------------------- control
def test_compare_limit_and_counter(registry):
    results = run_node(registry, "control.compare", {"a": [1.0, 2.0, 3.0]}, outputs=("result",), op=">=", value=2)
    assert [item.value for item in results["result"]] == [False, True, True]
    within = run_node(registry, "control.compare", {"a": [Scalar(value=3.31, unit="V")]}, outputs=("result",),
                      op="within", value="3.3 V", tolerance=0.02)
    assert within["result"][0].value is True
    text = run_node(registry, "control.compare", {"a": ["hello world"]}, outputs=("result",), op="contains", value="world")
    assert text["result"][0].value is True

    limits = run_node(registry, "control.limit", {"in": [0.5, 5.0]}, outputs=("ok", "out"), low=0, high=3)
    assert [item.value for item in limits["ok"]] == [True, False] and limits["out"][1].value == 3

    counts = run_node(registry, "control.counter", {"in": [1, 2, signals.Event(times=[0, 1], data=[1, 2])]},
                      outputs=("count",))
    assert counts["count"] == [1.0, 2.0, 4.0]


def test_a_sequence_with_steps_and_a_timeout(registry):
    flow = Flow("x")
    flow.add_node("control.sequence", "seq", steps=[
        {"set": "voltage", "value": "1.5 V"},
        {"wait": "100 ms"},
        {"emit": "trigger"},
        {"wait_for": "ack", "timeout": "1 s"},
        {"set": "voltage", "value": 0},
        {"wait_for": "ack", "timeout": "50 ms"},
    ])
    flow.add_node("test.events", "acks", data=["ok"])
    flow.add_node("data.table", "log")
    flow.connect("acks.out", "seq.ack")
    flow.connect("seq.voltage", "log.in")
    flow.connect("seq.timeout", "log.in")
    engine = Engine(flow, registry=registry, mode="virtual")
    events = []
    engine.subscribe(lambda event: event.kind == "value" and event.node == "seq" and events.append(
        (round(event.time, 6), event.port)))
    result = engine.run(timeout=20)
    assert result.ok, result.error
    assert ("voltage" in [port for _time, port in events]) and (0.1, "trigger") in events
    assert (0.15, "timeout") in [(time, port) for time, port in events]  # the second wait gave up after 50 ms
    assert result.value("seq", "voltage") == 0
    assert result.time == pytest.approx(0.15)


def test_a_state_machine(registry):
    flow = Flow("x")
    flow.add_node("control.state_machine", "machine", initial="idle", states={
        "idle": {"enter": {"lamp": 0}, "on": [{"input": "button", "when": "press", "to": "on"}]},
        "on": {"enter": {"lamp": 1}, "on": [{"after": "200 ms", "to": "off"}]},
        "off": {"enter": {"lamp": 0}},
    })
    flow.add_node("test.events", "button", data=["release", "press"])
    flow.connect("button.out", "machine.button")
    engine = Engine(flow, registry=registry, mode="virtual")
    states = []
    engine.subscribe(lambda event: event.kind == "value" and event.port == "state" and states.append(
        (round(event.time, 6), event.value)))
    result = engine.run(timeout=20)
    assert result.ok, result.error
    assert states == [(0.0, "idle"), (0.02, "on"), (0.22, "off")]
    assert result.value("machine", "lamp") == 0


def test_python_nodes(registry):
    flow = Flow("x")
    flow.add_node("control.python", "py", outputs=["square"], inputs=["x"], pulled=["x"], code=(
        "async def run(ctx):\n"
        "    for _ in range(3):\n"
        "        x = await ctx.receive('x')\n"
        "        ctx.emit('square', x * x)\n"
        "        await ctx.sleep(0.5)\n"
    ))
    flow.add_node("test.source", "numbers", values=[2, 3, 4])
    flow.connect("numbers.out", "py.x")
    engine = Engine(flow, registry=registry, mode="virtual")
    seen = []
    engine.subscribe(lambda event: event.kind == "value" and event.port == "square" and seen.append(event.value))
    result = engine.run(timeout=20)
    assert result.ok, result.error
    assert seen == [4, 9, 16] and result.time == pytest.approx(1.5)

    broken = Flow("x")
    broken.add_node("control.python", "py", code="def oops(:\n")
    result = Engine(broken, registry=registry, mode="virtual").run(timeout=20)
    assert result.state == "error" and "line 1" in result.error


# ------------------------------------------------------------------------ data
def test_files_are_read_written_and_logged(registry, tmp_path):
    flow = Flow("x")
    flow.add_node("data.file_read", "read", path=DEMO)
    flow.add_node("data.file", "write", path=str(tmp_path / "copy.sr"))
    flow.add_node("test.source", "values", values=[1.0, 2.0, Scalar(value=3.0, unit="V")])
    flow.add_node("data.logger", "logger", path=str(tmp_path / "log.csv"))
    flow.add_node("data.buffer", "buffer", count=2)
    flow.connect("read.capture", "write.in")
    flow.connect("values.out", "logger.in")
    flow.connect("values.out", "buffer.in")
    result = Engine(flow, registry=registry, mode="virtual").run(timeout=20)
    assert result.ok, result.error
    from openscilab.core import sigrok_session

    assert sigrok_session.load_session(str(tmp_path / "copy.sr")).total_samples == 1380
    rows = list(csv.reader(open(tmp_path / "log.csv")))
    assert rows[0] == ["time", "source", "value"] and [row[2] for row in rows[1:]] == ["1.0", "2.0", "3.0"]
    assert [row["value"] for row in result.value("buffer", "out").rows()] == [2.0, 3.0]

    csv_flow = Flow("x")
    csv_flow.add_node("data.file_read", "read", path=str(tmp_path / "log.csv"))
    result = Engine(csv_flow, registry=registry, mode="virtual").run(timeout=20)
    assert result.value("read", "table").columns["value"] == [1.0, 2.0, 3.0]


def test_the_logger_keeps_streams_on_disk(registry, tmp_path):
    flow = Flow("x")
    flow.add_node("device.instrument", "sim", address="sim:free")
    flow.add_node("device.stream", "stream", channels=["D15"], rate="100 kHz", duration="30 ms")
    flow.connect("sim.device", "stream.device")
    flow.add_node("data.logger", "logger", path=str(tmp_path / "long.lac"))
    flow.connect("stream.capture", "logger.in")
    result = Engine(flow, registry=registry, mode="virtual").run(timeout=20)
    assert result.ok, result.error
    from openscilab.core import capture_io

    assert capture_io.load_capture(str(tmp_path / "long.lac")).session.total_samples == 3000


def test_a_signal_buffer_keeps_the_latest_seconds(registry):
    blocks = [Analog(values=np.full(10, float(index)), time=TimeBase.uniform(10, index)) for index in range(5)]
    result = run_node(registry, "data.buffer", {"in": blocks}, length="2 s")["out"][-1]
    assert len(result) == 20 and list(result.values[:10]) == [3.0] * 10


# ------------------------------------------------------------------- views
def test_views_reach_the_view_sink(registry):
    flow = Flow("x")
    flow.add_node("control.sweep", "sweep", start=0, stop=2, step=1)
    flow.add_node("dsp.math", "square", expression="a * a")
    flow.connect("sweep.value", "square.a")
    for kind in ("number", "led", "log", "table", "strip_chart"):
        flow.add_node(f"view.{kind}", kind)
        flow.connect("square.out", f"{kind}.in")
    flow.add_node("view.xy", "xy")
    flow.connect("sweep.value", "xy.x")
    flow.connect("square.out", "xy.y")
    flow.add_node("test.source", "wave", values=[sine(100, 1000, 100)])
    flow.add_node("view.spectrum", "spectrum")
    flow.connect("wave.out", "spectrum.in")
    engine = Engine(flow, registry=registry, mode="virtual")
    assert engine.run(timeout=20).ok
    views = engine.views
    assert views.last("number") == 4.0
    assert views.last("led") is True
    assert len(views.last("log")) == 3
    assert views.last("xy").columns == {"x": [0.0, 1.0, 2.0], "y": [0.0, 1.0, 4.0]}
    assert views.last("strip_chart")["in"][1] == [0.0, 1.0, 4.0]
    assert "magnitude" in views.last("spectrum").columns


# ------------------------------------------------------------------ structure
def test_bundles(registry):
    flow = Flow("x")
    flow.add_node("test.source", "a", values=[1])
    flow.add_node("test.source", "b", values=["two"])
    flow.add_node("structure.bundle", "bundle", fields=["a", "b"])
    flow.add_node("structure.unbundle", "unbundle", fields=["a", "b"])
    flow.connect("a.out", "bundle.a")
    flow.connect("b.out", "bundle.b")
    flow.connect("bundle.out", "unbundle.in")
    result = Engine(flow, registry=registry, mode="virtual").run(timeout=20)
    assert result.ok
    assert result.value("bundle", "out") == {"a": 1, "b": "two"}
    assert result.value("unbundle", "b") == "two"


def test_the_example_project_with_its_own_nodes():
    from openscilab.lab.project import Project

    project = Project.open(os.path.join(ROOT, "examples", "project"))
    assert "example.bit_rate" in project.registry()
    flow = project.load_flow("bit_rate")
    engine = Engine(flow, registry=project.registry(), mode="virtual", project=project)
    result = engine.run(timeout=20)
    assert result.ok, result.error
    assert result.value("rate", "baud").value == pytest.approx(115200, rel=0.03)
    assert engine.views.last("led") is False
