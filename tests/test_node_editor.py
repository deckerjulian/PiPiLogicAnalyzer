"""A node's parameters in a window of their own (a double-click), big text fields for mappings and
long lists, and YAML read as version 1.2 (``on``/``off`` stay words: the state machine)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLineEdit

from openscilab.lab import yaml_io
from openscilab.ui.flow.inspector import YamlEdit, to_block_text

MACHINE = {"red": {"enter": {"red": 1, "green": 0}, "on": [{"after": "1 s", "to": "green"}]},
           "green": {"enter": {"red": 0, "green": 1}, "on": [{"after": "1 s", "to": "off"}]},
           "off": {"enter": {"red": 0, "green": 0}}}


def settle(times: int = 10) -> None:
    for _ in range(times):
        QApplication.processEvents()


def test_yaml_keeps_on_and_off_as_words():
    assert yaml_io.safe_load("{on: 1, off: 2, yes: 3, a: true, b: False}") == {
        "on": 1, "off": 2, "yes": 3, "a": True, "b": False}
    flow = yaml_io.loads("flow: t\nnodes:\n  m: {type: control.state_machine, initial: red, states: "
                         "{red: {on: [{after: 1 s, to: 'off'}]}, off: {}}}\nedges: []\n")
    assert flow.nodes["m"].params["states"] == {"red": {"on": [{"after": "1 s", "to": "off"}]}, "off": {}}
    again = yaml_io.loads(yaml_io.dumps(flow))  # (written so that YAML 1.1 reads it the same way too)
    assert again.nodes["m"].params == flow.nodes["m"].params
    import yaml

    assert yaml.safe_load(yaml_io.dumps(flow))["nodes"]["m"]["states"]["red"]["on"]


def test_the_state_machine_runs_through_its_states_and_says_what_is_wrong():
    from openscilab.lab.engine import Engine
    from openscilab.lab.model import Flow

    flow = Flow("t")
    flow.add_node("control.state_machine", "light", states=MACHINE, initial="red")
    states = []
    engine = Engine(flow, mode="virtual")
    engine.subscribe(lambda event: event.kind == "value" and event.port == "state" and states.append(event.value))
    assert engine.run(timeout=10).ok and states == ["red", "green", "off"]
    broken = Flow("t")
    broken.add_node("control.state_machine", "light", states={"red": {True: [{"after": "1 s", "to": "red"}]}},
                    initial="red")
    result = Engine(broken, mode="virtual").run(timeout=10)
    assert not result.ok and "'enter' and 'on'" in result.error


def test_mappings_get_a_big_text_field(shell):
    document = shell.new_flow()
    document.add_node("control.state_machine", (0, 0), "light", states=MACHINE, initial="red")
    document.scene.select_nodes(["light"])
    settle()
    inspector = document.inspector_widget()
    states = inspector.editors["states"]
    assert isinstance(states, YamlEdit) and isinstance(inspector.editors["initial"], QLineEdit)
    assert states.toPlainText() == to_block_text(MACHINE) and "\n" in states.toPlainText()
    states.setPlainText(states.toPlainText().replace("1 s", "2 s"))
    states.commit()
    assert document.flow.nodes["light"].params["states"]["red"]["on"][0]["after"] == "2 s"


def test_a_double_click_opens_the_editor_window(shell):
    document = shell.new_flow()
    document.add_node("control.state_machine", (0, 0), "light", states=MACHINE, initial="red")
    document.scene.node_double_clicked("light")
    settle()
    editor = document._node_editors["light"]
    assert editor.isVisible() and "State machine" in editor.windowTitle()
    states = editor.inspector.editors["states"]
    assert isinstance(states, YamlEdit) and states.minimumHeight() > 150  # (bigger than in the inspector)
    editor.inspector.editors["initial"].setText("green")
    editor.inspector.editors["initial"].editingFinished.emit()
    assert document.flow.nodes["light"].params["initial"] == "green"
    # it follows a rename and closes with its node
    document.rename_node("light", "lamp")
    settle()
    assert document._node_editors["lamp"] is editor and "lamp" in editor.windowTitle()
    document.delete_items(["lamp"], [])
    settle()
    assert "lamp" not in document._node_editors and not editor.isVisible()
