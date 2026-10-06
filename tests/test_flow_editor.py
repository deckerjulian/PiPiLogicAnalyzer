"""The flow document: graph, YAML and Python views, editing with undo, running in the editor."""

from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QMimeData, QPointF, Qt
from PySide6.QtGui import QDropEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from openscilab.lab import In, Out, node, yaml_io
from openscilab.lab.nodes.registry import Registry
from openscilab.ui.documents.dataview import DataView
from openscilab.ui.documents.flow import FlowDocument
from openscilab.ui.flow.canvas import NODE_MIME
from openscilab.ui.flow.inspector import FlowInspector, NodeInspector

ROOT = os.path.join(os.path.dirname(__file__), "..")
COUNTER = os.path.join(ROOT, "examples", "flows", "counter.flow.yaml")


def wait_for(condition, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if condition():
            return True
        time.sleep(0.005)
    return condition()


@pytest.fixture
def typed_registry():
    registry = Registry().copy()

    @node("test.meter", inputs=[In("volts", "Analog", optional=False)], outputs=[Out("out", "Scalar")], register=False)
    def meter(volts):
        return float(volts.values.mean())

    registry.add(meter.node_spec)
    return registry


def edges(document: FlowDocument) -> list[str]:
    return [str(edge) for edge in document.flow.edges]


# ------------------------------------------------------------------- editing
def test_place_connect_and_undo(shell):
    document = shell.new_flow()
    assert shell.active_document() is document
    assert "Flow" in [action.text().replace("&", "") for action in shell.menuBar().actions()]

    timer = document.add_node("control.timer", (0, 0), interval="10 ms", count=3)
    table = document.add_node("data.table", (300, 0))
    assert (timer, table) == ("timer", "table")
    assert set(document.scene.nodes) == {"timer", "table"}
    assert document.dirty

    assert document.scene.finish_wire("timer.tick", "table.in")
    assert edges(document) == ["timer.tick -> table.in"]
    assert len(document.scene.wires) == 1

    document.undo.undo()
    assert edges(document) == [] and len(document.scene.wires) == 0
    document.undo.undo()
    assert list(document.flow.nodes) == ["timer"]
    document.undo.redo()
    document.undo.redo()
    assert edges(document) == ["timer.tick -> table.in"]
    assert shell.action_undo.isEnabled()


def test_dragging_a_wire_between_ports(shell):
    document = shell.new_flow()
    document.add_node("control.timer", (0, 0))
    document.add_node("data.table", (300, 0))
    QApplication.processEvents()
    source = document.scene.nodes["timer"].port("tick", False)
    target = document.scene.nodes["table"].port("in", True)
    document.scene.begin_wire(source)

    class Release:
        def __init__(self, position):
            self.position = position

        def scenePos(self):  # noqa: N802
            return self.position

        def accept(self):
            pass

    document.scene.mouseReleaseEvent(Release(target.scene_center()))
    assert edges(document) == ["timer.tick -> table.in"]


def test_incompatible_wires_are_refused_with_a_conversion(shell, typed_registry, monkeypatch):
    document = FlowDocument(registry=typed_registry, hub=shell.hub)
    shell.add_document(document)
    document.add_node("device.instrument", (-300, 0), "sim", address="sim:free")
    document.add_node("device.capture", (0, 0), channels=["D0"], samples=100)
    document.add_node("test.meter", (400, 0))
    assert not document.scene.finish_wire("capture.D0", "meter.volts")
    assert edges(document) == ["sim.device -> capture.device"]  # wired to the only device at once
    # said above the graph, with the conversion one click away (no dialog)
    assert "Digital does not fit Analog" in document.run_banner.label.text()
    assert document.run_banner.button.text() == "Insert convert.to_analog"
    assert not document.scene.finish_wire("sim.device", "meter.volts")  # a device is no value
    assert "the ports do not fit" in document.run_banner.label.text()

    assert not document.scene.finish_wire("capture.D0", "meter.volts")
    document.run_banner.button.click()
    assert edges(document) == ["sim.device -> capture.device", "capture.D0 -> to_analog.in",
                               "to_analog.out -> meter.volts"]
    assert document.flow.nodes["to_analog"].type == "convert.to_analog"
    assert document.flow.validate(typed_registry) == []


def test_delete_with_the_keyboard_and_alignment(shell):
    document = shell.new_flow()
    for index in range(3):
        document.add_node("control.timer", (index * 300, index * 37))
    document.scene.select_nodes(["timer", "timer2", "timer3"])
    document.align("top")
    assert {node.position[1] for node in document.flow.nodes.values()} == {0}
    document.align("left")
    assert {node.position[0] for node in document.flow.nodes.values()} == {0}

    document.scene.select_nodes(["timer2"])
    document.view.setFocus()
    QTest.keyClick(document.view.viewport(), Qt.Key_Delete)
    document.view.keyPressEvent(_key(Qt.Key_Delete))
    assert "timer2" not in document.flow.nodes


def _key(key):
    from PySide6.QtGui import QKeyEvent

    return QKeyEvent(QKeyEvent.KeyPress, key, Qt.NoModifier)


def test_typing_on_the_canvas_finds_a_node(shell):
    document = shell.new_flow()
    popup = document.view.open_search("swe", QPointF(40, 80))
    assert popup.results.currentItem().data(Qt.UserRole) == "control.sweep"
    popup.accept_current()
    assert document.flow.nodes["sweep"].type == "control.sweep"
    assert document.flow.nodes["sweep"].position == (40, 80)


def test_drop_from_the_palette_and_double_click(shell):
    document = shell.new_flow()
    assert shell.nodes_dock.isVisible()  # (beside a flow that is edited)
    palette = shell.nodes_section
    assert "device.capture" in palette.types()
    palette.search.setText("timer")
    assert palette.types()[0] == "control.timer"

    mime = QMimeData()
    mime.setData(NODE_MIME, b"control.timer")
    event = QDropEvent(QPointF(100, 100), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
    document.view.dropEvent(event)
    assert "timer" in document.flow.nodes

    group = palette.tree.topLevelItem(0)
    # a double-click emits both signals; it adds one node
    palette.tree.itemDoubleClicked.emit(group.child(0), 0)
    palette.tree.itemActivated.emit(group.child(0), 0)
    assert "timer2" in document.flow.nodes and "timer3" not in document.flow.nodes


# ---------------------------------------------------------------- inspector
def test_the_inspector_edits_parameters_and_names(shell):
    document = shell.new_flow()
    document.add_node("control.timer", (0, 0))
    document.scene.select_nodes(["timer"])
    inspector = shell.inspector.content
    assert isinstance(inspector, NodeInspector) and inspector.node.id == "timer"

    editor = inspector.editors["interval"]
    editor.setText("250 ms")
    editor.editingFinished.emit()
    assert document.flow.nodes["timer"].params["interval"] == "250 ms"
    editor = shell.inspector.content.editors["interval"]
    editor.setText("fast")  # not a quantity
    editor.editingFinished.emit()
    assert document.flow.nodes["timer"].params["interval"] == "250 ms"
    assert "not a quantity" in shell.inspector.content.error_label.text()

    shell.inspector.content.editors["count"].setText("5")
    shell.inspector.content.editors["count"].editingFinished.emit()
    assert document.flow.nodes["timer"].params["count"] == 5

    shell.inspector.content.name_edit.setText("clock")
    shell.inspector.content.name_edit.editingFinished.emit()
    assert list(document.flow.nodes) == ["clock"]
    # consecutive edits of one parameter are one undo step
    document.set_param("clock", "count", 6)
    document.set_param("clock", "count", 7)
    document.undo.undo()
    assert document.flow.nodes["clock"].params["count"] == 5

    document.scene.select_nodes([])
    assert isinstance(shell.inspector.content, FlowInspector)
    flow_inspector = shell.inspector.content
    flow_inspector.name_edit.setText("Renamed")
    flow_inspector.name_edit.editingFinished.emit()
    assert document.flow.name == "Renamed"


# ---------------------------------------------------------------- YAML/Python
def test_the_yaml_view_is_the_saved_file(shell, tmp_path):
    document = shell.open_file(COUNTER)
    assert isinstance(document, FlowDocument)
    text = open(COUNTER, encoding="utf-8").read()
    document.set_view("YAML")
    assert document.yaml_edit.toPlainText() == text
    assert not document.dirty

    path = tmp_path / "copy.flow.yaml"
    document._write(str(path))
    assert path.read_text() == text

    document.set_view("Python")
    assert "sim = f.device('sim', 'sim:free')" in document.python_edit.toPlainText()
    assert document.python_edit.isReadOnly()


def test_editing_the_yaml_changes_the_model_with_undo(shell):
    document = shell.open_file(COUNTER)
    document.set_view("YAML")
    document.yaml_edit.setPlainText(document.yaml_edit.toPlainText().replace("samples: 20000", "samples: 5000"))
    assert document.apply_yaml()
    assert document.flow.nodes["cap"].params["samples"] == 5000
    assert document.dirty
    document.yaml_edit.setPlainText("nodes: [broken")
    assert not document.apply_yaml()
    assert "YAML" in document.yaml_status.text()
    assert document.flow.nodes["cap"].params["samples"] == 5000
    document.undo.undo()
    assert document.flow.nodes["cap"].params["samples"] == 20000
    assert not document.dirty


# ------------------------------------------------------------------- running
def test_rebuild_the_counter_flow_and_run_it(shell, tmp_path):
    from openscilab.ui.flow.canvas import encode_node

    document = shell.new_flow()
    document._edit("setup", lambda flow: setattr(flow, "name", "Counter"))
    # the simulator from the palette's devices, then a capture: wired to it at once
    presets = {preset.title: preset for preset in shell.nodes_section.presets}
    preset = presets["Simulation: free"]
    shell._add_node_to_active_flow(encode_node("device.instrument", preset.node_id, {"address": preset.address}))
    assert document.flow.devices == {"free": "sim:free"}
    document.add_node("device.capture", (0, 0), "cap", channels=["D0", "D1", "D2", "D3", "D8"],
                      rate="4 MHz", samples=20000, trigger={"edge": "rising", "source": "D8"})
    assert edges(document) == ["free.device -> cap.device"]
    document.add_node("view.scope", (300, 0), "scope", title="Counter")
    document.add_node("data.file", (300, 150), "file", path=str(tmp_path / "counter.lac"))
    document.scene.finish_wire("cap.capture", "scope.in")
    document.scene.finish_wire("cap.capture", "file.in")
    assert document.flow.validate() == []

    document.fast_box.setChecked(True)
    shell.area.activate(document)
    assert shell.run_button.isEnabled()
    shell.run_button.click()
    assert wait_for(lambda: document.runner.result is not None)
    assert document.runner.result.state == "finished"
    assert (tmp_path / "counter.lac").exists()
    assert {item.state for item in document.scene.nodes.values()} == {"done"}
    assert "Capture" in document.scene.wires[document.flow.edges[1]].label
    scopes = [doc for doc in shell.documents() if isinstance(doc, DataView)]
    assert len(scopes) == 1 and scopes[0].title == "Counter" and scopes[0].model.sample_count == 20000
    assert any("finished" in shell.console.execution.item(row).text()
               for row in range(shell.console.execution.count()))


def test_breakpoints_pause_and_step_in_the_graph(shell):
    document = shell.new_flow()
    document.add_node("control.timer", (0, 0), interval="10 ms", count=3)
    document.add_node("data.table", (300, 0))
    document.scene.finish_wire("timer.tick", "table.in")
    document.toggle_breakpoint("table")
    assert document.scene.nodes["table"].breakpoint
    document.fast_box.setChecked(True)
    assert document.start_run()
    assert wait_for(lambda: document.runner.state == "paused")
    assert document.action_step.isEnabled() and document.action_run.text() == "Continue"
    document.step_run()
    assert wait_for(lambda: document.runner.state == "paused" and len(document.runner.engine.values) > 0)
    document.set_breakpoint("table", False)
    document.start_run()  # continue
    assert wait_for(lambda: document.runner.result is not None)
    assert document.runner.result.ok
    assert len(document.runner.result.value("table", "table")) == 3


def test_errors_show_at_the_node(shell, typed_registry):
    document = shell.new_flow()
    document.add_node("data.file", (0, 0), path="")
    problems = []
    document.problems_changed.connect(problems.append)
    document.report_problems()
    assert problems and "missing" in str(problems[-1][0])
    assert shell.console.problems.problems()


# ---------------------------------------------------------------- subflows
def test_make_and_open_a_subflow(shell):
    document = shell.new_flow()
    document.add_node("control.timer", (0, 0), interval="1 ms", count=2)
    document.add_node("data.table", (300, 0))
    document.add_node("data.table", (600, 0), "sink")
    document.scene.finish_wire("timer.index", "table.in")
    document.scene.finish_wire("table.table", "sink.in")
    document.scene.select_nodes(["table"])
    node_id = document.make_subflow("collect")
    assert document.flow.nodes[node_id].type == "subflow.collect"
    assert document.flow.validate() == []
    assert edges(document) == [f"timer.index -> {node_id}.table_in", f"{node_id}.table_table -> sink.in"]

    document.open_node(node_id)
    child = shell.active_document()
    assert isinstance(child, FlowDocument) and child.parent_document is document
    assert child.title.endswith("› collect")
    child.add_node("structure.comment", (0, 100), text="inside")
    assert "comment" in document.flow.subflows["collect"].nodes
    child.undo.undo()  # one undo stack for the flow and its subflows
    assert "comment" not in document.flow.subflows["collect"].nodes
    document.fast_box.setChecked(True)
    assert document.start_run()
    assert wait_for(lambda: document.runner.result is not None)
    assert document.runner.result.ok


def test_comments_and_groups_are_part_of_the_file(shell):
    document = shell.new_flow()
    document.add_node("control.timer", (0, 0))
    document.scene.select_nodes(["timer"])
    group = document.group_selection()
    document.add_node("structure.comment", (0, 200), text="Remember the probe")
    text = yaml_io.dumps(document.flow)
    assert "type: structure.group" in text and "Remember the probe" in text
    assert document.flow.nodes[group].params["size"][0] > 190
    assert document.flow.validate() == []


def test_an_open_device_becomes_a_node_and_is_used_as_it_is(shell):
    from openscilab.driver.simulated import open_simulated
    from openscilab.lab.engine import Engine

    instrument = open_simulated("uno")
    shell.hub.add(instrument)
    document = shell.new_flow()
    assert any(preset.address == "sim:uno" and preset.kind == "open" for preset in shell.nodes_section.presets)
    node_id = shell.add_instrument_to_flow(instrument)
    assert node_id == "uno" and document.flow.devices == {"uno": "sim:uno"}
    document.add_node("gpio.write", (300, 0), "led", pin="D7", level=1)
    assert edges(document) == ["uno.device -> led.device"]
    engine = Engine(document.flow, hub=shell.hub)  # real time: the open instrument itself
    assert engine.run(timeout=10).ok
    assert engine.devices["uno"] is instrument and instrument.gpio.mode("D7") == "output"
    instrument.gpio.safe_all()
