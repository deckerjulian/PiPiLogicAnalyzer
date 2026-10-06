"""Fixed mistakes of the flow and panel editors (found in the UI review)."""

from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog, QSpinBox

from openscilab.lab import Flow, yaml_io
from openscilab.lab.model import PortRef
from openscilab.ui import messages


def wait_for(condition, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def test_a_new_device_node_has_the_default_address(shell):
    document = shell.new_flow()
    node_id = document.add_node("device.instrument", (0, 0))
    assert document.flow.nodes[node_id].params["address"] == "sim:free"
    assert document.flow.devices == {node_id: "sim:free"}
    preset = document.add_node("device.instrument", (0, 200), "uno", address="sim:uno")
    assert document.flow.nodes[preset].params["address"] == "sim:uno"


def test_a_click_on_a_node_changes_nothing_and_two_moves_are_two_steps(shell):
    flow = Flow("Plain")
    flow.add_node("control.timer", "timer")  # no stored position, as in hand-written files
    document = shell.new_flow(flow)
    document.undo.setClean()
    QApplication.processEvents()
    item = document.scene.nodes["timer"]
    point = document.view.mapFromScene(item.sceneBoundingRect().center())
    QTest.mouseClick(document.view.viewport(), Qt.LeftButton, pos=point)
    assert not document.dirty
    document.positions_changed({"timer": (40.0, 0.0)})
    document.positions_changed({"timer": (80.0, 0.0)})
    document.undo.undo()
    assert document.flow.nodes["timer"].position == (40.0, 0.0)


def test_saving_a_python_flow_asks_for_a_yaml_file(shell, tmp_path, monkeypatch):
    script = tmp_path / "mine.py"
    script.write_text("# my own script\nfrom openscilab.lab import flow, nodes as n\n\n"
                      "with flow('mine') as f:\n    n.control.timer(id='t')\n\nfor _ in range(3):\n    pass\n")
    original = script.read_text()
    document = shell.open_file(str(script))
    document.add_node("data.table", (300, 0))
    asked = []
    monkeypatch.setattr(QFileDialog, "getSaveFileName",
                        lambda parent, title, start, *args: asked.append(start) or (str(tmp_path / "mine.flow.yaml"), ""))
    assert document.save()
    assert asked[0].endswith("mine.flow.yaml") and script.read_text() == original
    assert document.path.endswith("mine.flow.yaml") and "table" in yaml_io.load(document.path).nodes


def test_broken_yaml_is_not_dropped_when_leaving_the_view(shell, monkeypatch):
    document = shell.new_flow()
    document.add_node("control.timer", (0, 0))
    document.set_view("YAML")
    document.yaml_edit.setPlainText("nodes: [broken")
    choices = []
    monkeypatch.setattr(messages, "choose", lambda *args, **kwargs: choices.append(args[3]) or 0)
    document.set_view("Graph")
    assert document.current_view() == "YAML" and document.yaml_edit.toPlainText() == "nodes: [broken"
    monkeypatch.setattr(messages, "choose", lambda *args, **kwargs: 1)  # discard
    document.set_view("Graph")
    assert document.current_view() == "Graph" and "timer" in document.flow.nodes


def test_yaml_edits_of_one_visit_are_one_undo_step(shell):
    document = shell.new_flow()
    document.add_node("control.timer", (0, 0), interval="1 ms")
    steps = document.undo.count()
    document.set_view("YAML")
    for interval in ("2 ms", "3 ms", "4 ms"):
        document.yaml_edit.setPlainText(yaml_io.dumps(document.flow).replace(
            document.flow.nodes["timer"].params["interval"], interval))
        assert document.apply_yaml()
    assert document.undo.count() == steps + 1


def test_errors_leave_the_tooltip_when_the_node_is_idle_again(shell):
    document = shell.new_flow()
    document.add_node("control.timer", (0, 0))
    item = document.scene.nodes["timer"]
    normal = item.toolTip()
    item.set_state("error", "boom")
    assert item.toolTip().startswith("Error: boom")
    document.scene.reset_states()
    assert item.toolTip() == normal


def test_the_run_has_ended_when_its_result_is_there(shell):
    document = shell.new_flow()
    document.add_node("control.timer", (0, 0), interval="1 ms", count=1)
    document.fast_box.setChecked(True)
    assert document.start_run()
    assert wait_for(lambda: document.runner.result is not None)
    assert not document.runner.running and document.action_run.isEnabled()


def test_renaming_keeps_the_ports_of_a_subflow():
    flow = Flow("inner")
    flow.add_node("data.table", "table")
    flow.inputs["values"] = PortRef("table", "in")
    flow.outputs["result"] = PortRef("table", "table")
    flow.rename_node("table", "collect")
    assert flow.inputs["values"] == PortRef("collect", "in") and flow.outputs["result"].node == "collect"


def test_the_palette_shows_the_nodes_of_the_active_flow(shell):
    from openscilab.lab.nodes.registry import Registry

    document = shell.new_flow()
    document.registry = Registry()  # as a project with its own nodes has
    shell._on_active_changed(document)
    assert shell.nodes_section.registry is document.registry


# ------------------------------------------------------------------ panels
def flow_file(tmp_path) -> str:
    flow = Flow("gain")
    flow.add_node("control.number", "gain")
    path = str(tmp_path / "gain.flow.yaml")
    yaml_io.save(flow, path)
    return path


def test_the_panel_inspector_keeps_the_cursor_and_merges_steps(shell, tmp_path):
    document = shell.new_panel(flow_path=flow_file(tmp_path))
    widget = document.add_widget("number", "gain.out")
    inspector = document.inspector_widget()
    spin = inspector.findChild(QSpinBox, "setting-columns")
    steps = document.undo.count()
    spin.setValue(spin.value() + 1)
    spin.setValue(spin.value() + 1)
    assert document.inspector_widget() is inspector  # not rebuilt under the cursor
    assert document.undo.count() == steps + 1
    document.undo.undo()
    assert document.panel.widget(widget.id).columns == widget.columns


def test_the_panel_uses_the_current_flow(shell, tmp_path):
    path = flow_file(tmp_path)
    document = shell.new_panel(flow_path=path)
    assert set(document.flow().nodes) == {"gain"}
    flow = yaml_io.load(path)
    flow.add_node("data.table", "table")
    yaml_io.save(flow, path)
    os.utime(path, (time.time() + 5, time.time() + 5))
    assert "table" in document.flow().nodes  # the file changed: read again
    editor = shell.open_file(path)
    editor.add_node("data.table", (0, 200), "unsaved")
    assert "unsaved" in document.flow().nodes  # the open flow with its unsaved changes
    shell.area.activate(document)
    document.add_widget("number", "gain.out")
    document.add_widget("number", "gain.out")
    document.undo.undo()
    shell._on_active_changed(document)
    assert shell.action_redo.isEnabled()
