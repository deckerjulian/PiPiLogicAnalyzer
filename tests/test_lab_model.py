"""The flow model and its YAML files."""

from __future__ import annotations

import os

import pytest

from openscilab.core import units
from openscilab.lab import yaml_io
from openscilab.lab.model import Edge, Flow, FlowError, PortRef
from openscilab.lab.nodes.registry import (
    In,
    Out,
    Param,
    Registry,
    RegistryError,
    default_registry,
    node,
)

ROOT = os.path.join(os.path.dirname(__file__), "..")
COUNTER = os.path.join(ROOT, "examples", "flows", "counter.flow.yaml")

KENNLINIE = """\
flow: PWM-Kennlinie
description: |
  Sweeps a value and collects it.
  Two lines.
settings:
  seed: 7
nodes:
  sim: {type: device.instrument, address: sim:free}
  sweep: {type: control.sweep, start: 0, stop: 100, step: 5, dwell: 200 ms, at: [10, 20]}
  cap:
    type: device.capture
    channels: [D0]
    rate: 1 MHz
    duration: 50 ms
    trigger: {edge: rising, source: D0}
  table: {type: data.table, columns: [x, n]}
edges:
  - sim.device -> cap.device
  - sweep.value -> table.x
  - sweep.step -> cap.arm
  - cap.done -> table.n
"""


# ---------------------------------------------------------------------- units
@pytest.mark.parametrize("text, unit, value", [
    ("200 ms", "s", 0.2), ("1 kHz", "Hz", 1000.0), ("4 MHz", "Hz", 4e6), ("3.3 V", "V", 3.3),
    ("50 %", "", 0.5), ("2 min", "s", 120.0), (5, "s", 5.0), ("250us", "s", 250e-6), ("10M", "", 10e6),
])
def test_quantities(text, unit, value):
    assert units.parse(text, unit) == pytest.approx(value)


def test_bad_quantities():
    with pytest.raises(units.UnitError):
        units.parse("1 kHz", "s")
    with pytest.raises(units.UnitError):
        units.parse("fast")
    assert units.format_quantity(0.0002, "s") == "200 µs"
    assert units.format_quantity(1000, "Hz") == "1 kHz"


# ----------------------------------------------------------------------- yaml
def test_yaml_roundtrip_is_byte_identical():
    for text in (KENNLINIE, open(COUNTER, encoding="utf-8").read()):
        assert yaml_io.dumps(yaml_io.loads(text)) == text


def test_yaml_is_read_into_the_model():
    flow = yaml_io.loads(KENNLINIE)
    assert flow.name == "PWM-Kennlinie" and flow.settings == {"seed": 7}
    assert flow.description == "Sweeps a value and collects it.\nTwo lines.\n"
    assert flow.devices == {"sim": "sim:free"}
    assert list(flow.nodes) == ["sim", "sweep", "cap", "table"]
    assert flow.device_of("cap") == "sim"
    assert flow.nodes["sweep"].params == {"start": 0, "stop": 100, "step": 5, "dwell": "200 ms"}
    assert flow.nodes["sweep"].position == (10.0, 20.0)
    assert flow.edges[2] == Edge(PortRef("sweep", "step"), PortRef("cap", "arm"))
    assert flow.validate() == []


def test_a_long_node_is_written_as_a_block_and_reads_back():
    flow = Flow("x")
    flow.add_node("control.python", "code", code="async def run(ctx):\n    ctx.emit('out', 1)\n")
    text = yaml_io.dumps(flow)
    assert "  code:\n    type: control.python\n    code: |\n      async def run(ctx):" in text
    assert yaml_io.dumps(yaml_io.loads(text)) == text


@pytest.mark.parametrize("text, message", [
    ("nodes: [a]", "mapping"),
    ("nodes: {a: {start: 1}}", "no type"),
    ("edges: [a -> b]", "not a port"),
    ("edges: [a.b]", "not an edge"),
    ("strange: 1", "unknown section"),
    ("devices: {sim: 'sim:free'}", "devices are nodes"),
    ("nodes: {a: {type: x, at: 3}}", "[x, y]"),
    (": [", "not valid YAML"),
])
def test_broken_files_are_reported(text, message):
    with pytest.raises(FlowError, match=message):
        yaml_io.loads(text)


def test_save_and_load(tmp_path):
    flow = yaml_io.loads(KENNLINIE)
    path = tmp_path / "flows" / "k.flow.yaml"
    yaml_io.save(flow, str(path))
    assert path.read_text() == KENNLINIE
    assert yaml_io.load(str(path)).name == "PWM-Kennlinie"


# ------------------------------------------------------------------ type check
def test_type_errors_propose_a_conversion():
    flow = Flow("x")
    flow.add_node("device.instrument", "sim", address="sim:free")
    flow.add_node("device.capture", "cap", channels=["D0"], samples=10)
    flow.connect("sim.device", "cap.device")
    flow.add_node("control.sweep", "sweep")
    flow.connect("cap.D0", "sweep.next")  # Digital into Any: fine
    flow.add_node("data.table", "table")
    assert flow.validate() == []

    registry = default_registry.copy()

    @node("test.analog_in", inputs=[In("a", "Analog", optional=False)], register=False)
    def analog_in(a):
        return a

    registry.add(analog_in.node_spec)
    flow.add_node("test.analog_in", "meter")
    problems = flow.validate(registry)
    assert [problem.text for problem in problems] == ["the input 'a' is not wired"]

    flow.connect("cap.D0", "meter.a")
    problems = flow.validate(registry)
    assert len(problems) == 1 and problems[0].text == "Digital does not fit Analog"
    assert problems[0].suggestion == "convert.to_analog"
    assert flow.can_connect("cap.D0", "meter.a", registry) == (False, "convert.to_analog")
    assert flow.can_connect("cap.capture", "table.in", registry) == (True, "")


def test_unknown_types_ports_devices_and_double_wires():
    flow = Flow("x")
    flow.add_node("device.instrument", "sim", address="sim:free")
    flow.add_node("no.such", "a")
    flow.add_node("device.capture", "cap", samples=10)
    flow.add_node("data.table", "table")
    flow.connect("sim.device", "table.in")  # a device is no value
    flow.add_node("control.sweep", "sweep")
    flow.add_node("control.timer", "timer")
    flow.connect("sweep.nothing", "cap.arm")
    flow.connect("sweep.value", "cap.arm")
    flow.connect("timer.tick", "cap.arm")
    flow.connect("ghost.out", "cap.arm")
    texts = [str(problem) for problem in flow.validate()]
    assert "a: unknown node type 'no.such' (a node of a plugin needs its plugin: Help → Plugins)" in texts
    assert "cap: the input 'device' is not wired" in texts
    assert any("Device does not fit Any" in text for text in texts)
    assert any("sweep has no output 'nothing'" in text for text in texts)
    assert any("has several wires" in text for text in texts)
    assert any("no node 'ghost'" in text for text in texts)


def test_editing_the_model():
    flow = Flow("x")
    first = flow.add_node("control.timer")
    second = flow.add_node("control.timer")
    assert (first.id, second.id) == ("timer", "timer2")
    flow.connect("timer.tick", "timer2.nothing")
    flow.rename_node("timer", "clock")
    assert str(flow.edges[0]) == "clock.tick -> timer2.nothing"
    with pytest.raises(FlowError):
        flow.rename_node("clock", "timer2")
    with pytest.raises(FlowError):
        flow.add_node("control.timer", "1bad")
    flow.remove_node("timer2")
    assert flow.edges == [] and list(flow.nodes) == ["clock"]


# -------------------------------------------------------------------- subflows
SUBFLOW = """\
flow: Outer
nodes:
  timer: {type: control.timer, interval: 1 ms, count: 3}
  inner: {type: subflow.collect, table.columns: [n]}
edges:
  - timer.index -> inner.value
subflows:
  collect:
    flow: Collect
    inputs:
      value: table.n
    outputs:
      rows: table.table
    nodes:
      table: {type: data.table, columns: [x]}
    edges: []
"""


def test_subflows_are_typed_and_expanded():
    flow = yaml_io.loads(SUBFLOW)
    assert yaml_io.dumps(flow) == SUBFLOW
    spec = flow.spec(flow.nodes["inner"])
    assert [port.name for port in spec.inputs] == ["value"]
    assert [(port.name, port.type) for port in spec.outputs] == [("rows", "Table")]
    assert flow.validate() == []

    expanded = flow.expanded()
    assert set(expanded.nodes) == {"timer", "inner/table"}
    assert expanded.nodes["inner/table"].params == {"columns": ["n"]}  # set by the subflow node
    assert [str(edge) for edge in expanded.edges] == ["timer.index -> inner/table.n"]

    flow.nodes["inner"].type = "subflow.missing"
    assert any("unknown subflow" in str(problem) for problem in flow.validate())


# -------------------------------------------------------------------- registry
def test_registry_groups_and_custom_nodes(tmp_path):
    registry = Registry()
    assert {"device.capture", "device.stream", "view.scope", "control.sweep", "control.timer", "data.table",
            "data.file"} <= set(registry.types())
    assert "control" in registry.groups()
    with pytest.raises(RegistryError):
        registry.get("nothing")

    module = tmp_path / "mine.py"
    module.write_text(
        "from openscilab.lab import node, In, Out\n"
        "@node('my.double', inputs=[In('x', 'Scalar', optional=False)], outputs=[Out('y', 'Scalar')])\n"
        "def double(x):\n    return 2 * x\n"
        "@node('my.twice')\n"
        "def twice(a, b=1):\n    return a * b\n"
    )
    added = registry.load_module(str(module))
    assert [spec.type for spec in added] == ["my.double", "my.twice"]
    assert registry.get("my.double").inputs[0].type == "Scalar"
    twice = registry.get("my.twice")
    assert [(port.name, port.optional) for port in twice.inputs] == [("a", False), ("b", True)]
    assert "my.double" not in default_registry


def test_param_specs_are_checked():
    with pytest.raises(RegistryError):
        Param("x", "weird")
    assert Out("y").type == "Any"
