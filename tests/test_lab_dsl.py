"""The Python DSL: the same flow as the YAML file, in both directions."""

from __future__ import annotations

import os

import pytest

from openscilab.lab import FlowError, flow, from_python, to_python, yaml_io
from openscilab.lab import nodes as n
from openscilab.lab.project import Project

ROOT = os.path.join(os.path.dirname(__file__), "..")
COUNTER = os.path.join(ROOT, "examples", "flows", "counter.flow.yaml")


def build_counter():
    with flow("Counter", description="Captures the counter of the simulator and saves it.") as f:
        sim = f.device("sim", "sim:free")
        cap = n.device.capture(sim, id="cap", channels=["D0", "D1", "D2", "D3", "D8"], rate="4 MHz",
                               samples=20000, trigger=n.edge("D8"))
        cap.capture >> n.view.scope(id="scope", title="Counter")
        cap.capture >> n.data.file(id="file", path="counter.lac")
    return f


def test_dsl_and_yaml_give_the_same_file():
    assert build_counter().to_yaml() == open(COUNTER, encoding="utf-8").read()


def test_yaml_to_python_and_back_is_identical():
    text = open(COUNTER, encoding="utf-8").read()
    script = to_python(yaml_io.loads(text))
    assert "sim = f.device('sim', 'sim:free')" in script and "sim.device >> cap.device" in script
    assert "cap.capture >> scope.in_" in script
    assert yaml_io.dumps(from_python(script)) == text


def test_positional_arguments_ports_and_short_names():
    with flow("Kennlinie", seed=2) as f:
        uno = f.device("sim:free")
        sweep = n.control.sweep(0, 100, step=5, dwell="200 ms")
        cap = n.capture(uno, channels=["D0"], rate="1 MHz", duration="50 ms", trigger=n.edge("D0"))
        sweep.step >> cap.arm
        table = n.data.table(columns=["x", "n"], x=sweep.value, n=cap.done)
        n.view.scope(cap)

    model = f.flow
    assert model.devices == {"free": "sim:free"} and model.settings == {"seed": 2}
    assert model.nodes["sweep"].params == {"start": 0, "stop": 100, "step": 5, "dwell": "200 ms"}
    assert "device" not in model.nodes["capture"].params
    assert [str(edge) for edge in model.edges] == [
        "free.device -> capture.device", "sweep.step -> capture.arm", "sweep.value -> table.x", "capture.done -> table.n", "capture.capture -> scope.in",
    ]
    assert table.id == "table"
    assert f.validate() == []
    # and back through Python
    assert yaml_io.dumps(from_python(f.to_python())) == f.to_yaml()


def test_node_to_node_wiring_and_keywords():
    with flow("x") as f:
        timer = n.control.timer(interval="1 ms", count=2)
        table = n.data.table()
        timer >> table
        n.data.file(in_=table.table, path="t.csv")
    assert [str(edge) for edge in f.flow.edges] == ["timer.tick -> table.in", "table.table -> file.in"]


def test_errors_of_the_dsl():
    with pytest.raises(FlowError):
        n.control.timer()  # outside a flow
    with flow("x"):
        with pytest.raises(FlowError):
            n.control.timer(1, 2, 3, 4)  # more positional arguments than parameters
    with pytest.raises(FlowError, match="defines no flow"):
        from_python("x = 1")


def test_subflows_in_the_dsl():
    with flow("Outer") as f:
        with f.subflow("collect") as s:
            table = n.data.table(columns=["x"])
            s.input("value", table.x)
            s.output("rows", table.table)
        timer = n.control.timer(interval="1 ms", count=3)
        inner = n.subflow.collect(id="inner")
        timer.index >> inner.value
    assert f.validate() == []
    assert yaml_io.dumps(from_python(f.to_python())) == f.to_yaml()
    result = f.run(fast=True)
    assert result.ok
    assert len(result.value("inner/table", "table")) == 3


def test_the_dsl_runs_a_flow(tmp_path):
    f = build_counter()
    result = f.run(fast=True, data_dir=str(tmp_path))
    assert result.ok, result.error
    assert (tmp_path / "counter.lac").exists()


# --------------------------------------------------------------------- projects
def test_a_project_with_own_nodes_and_devices(tmp_path):
    project = Project.create(str(tmp_path / "lab"), "Lab", devices={"sim": "sim:free"})
    assert sorted(os.listdir(project.root)) == ["data", "flows", "nodes", "panels", "project.yaml", "tests", "waveforms"]
    (tmp_path / "lab" / "nodes" / "scale.py").write_text(
        "from openscilab.lab import node, In, Out\n"
        "@node('my.scale', inputs=[In('x', 'Scalar', optional=False)], outputs=[Out('y', 'Scalar')])\n"
        "def scale(x):\n    return 10 * x\n"
    )
    with flow("Scaled") as f:
        timer = n.control.timer(interval="1 ms", count=3)
        timer.index >> n.data.table(id="raw")
    f.flow.add_node("my.scale", "scale")
    f.flow.add_node("data.file", "file", path="scaled.csv")
    f.flow.add_node("data.table", "table")
    f.flow.connect("timer.index", "scale.x")
    f.flow.connect("scale.y", "table.in")
    f.flow.connect("table.table", "file.in")
    f.flow.add_node("device.instrument", "sim")  # the project's device
    f.flow.add_node("device.capture", "cap", channels=["D15"], samples=100)
    f.flow.connect("sim.device", "cap.device")
    f.save(os.path.join(project.flows_dir, "scaled.flow.yaml"))

    reopened = Project.open(project.root)
    assert reopened.name == "Lab" and reopened.devices == {"sim": "sim:free"}
    loaded = reopened.load_flow("scaled")
    assert loaded.devices == {"sim": "sim:free"}  # taken from the project
    assert loaded.validate(reopened.registry()) == []
    assert Project.find(os.path.join(project.flows_dir, "scaled.flow.yaml")) == project.root

    from openscilab.lab.engine import Engine

    result = Engine(loaded, registry=reopened.registry(), mode="virtual", project=reopened).run(timeout=20)
    assert result.ok, result.error
    assert (tmp_path / "lab" / "data" / "scaled.csv").read_text().splitlines()[1:] == ["0.0,0.0", "0.001,10.0", "0.002,20.0"]


def test_the_example_script_is_the_example_flow(tmp_path):
    script = os.path.join(ROOT, "examples", "flows", "counter.py")
    with open(script, encoding="utf-8") as handle:
        flow_from_script = from_python(handle.read(), filename=script)
    assert yaml_io.dumps(flow_from_script) == open(COUNTER, encoding="utf-8").read()

    from openscilab import cli

    assert cli.main(["run", script, "--sim", "--fast", "--data-dir", str(tmp_path), "-q"]) == 0
    assert (tmp_path / "counter.lac").exists()
