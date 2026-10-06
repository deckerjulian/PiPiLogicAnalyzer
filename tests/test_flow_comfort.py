"""Comfort in the flow editor: wires that find their port, the clipboard, menus on wires and ports,
groups that carry their nodes, problems on the nodes, a helpful inspector."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication, QComboBox, QFileDialog, QLabel, QPushButton

from openscilab.ui.flow.inspector import NodeInspector


def flow_with_table(shell):
    document = shell.new_flow()
    document.add_node("control.timer", (0, 0), "timer", interval="1 ms", count=2)
    document.add_node("data.table", (400, 0), "table")
    QApplication.processEvents()
    return document


class Press:
    def __init__(self, position):
        self._position = QPointF(*position)

    def scenePos(self):  # noqa: N802 - Qt naming
        return self._position

    def accept(self):
        pass


# --------------------------------------------------------------------- wires
def test_dragging_a_wire_lights_up_the_ports_it_fits(shell):
    document = flow_with_table(shell)
    scene = document.scene
    start = scene.nodes["timer"].port("index", False)
    scene.begin_wire(start)
    target = scene.nodes["table"].port("in", True)
    assert target.highlight == "ok"
    assert all(port.highlight is None for port in scene.nodes["timer"].ports.values())  # its own node
    # dropped on the body of the table node: it ends at the port that fits
    body = scene.nodes["table"].sceneBoundingRect().center()
    scene.mouseReleaseEvent(Press((body.x(), body.y())))
    assert [str(edge) for edge in document.flow.edges] == ["timer.index -> table.in"]
    assert target.highlight is None


def test_a_wire_close_to_a_port_snaps_to_it(shell):
    document = flow_with_table(shell)
    scene = document.scene
    start = scene.nodes["timer"].port("index", False)
    near = scene.nodes["table"].port("in", True).scene_center() + QPointF(-15, 10)
    assert scene.target_near(start, near) is scene.nodes["table"].port("in", True)


def test_a_wire_dropped_on_the_canvas_offers_the_nodes_it_fits(shell):
    document = flow_with_table(shell)
    scene = document.scene
    scene.begin_wire(scene.nodes["timer"].port("index", False))
    scene.mouseReleaseEvent(Press((900, 600)))
    popup = document.view.popup
    assert popup is not None and popup.isVisible()
    types = [popup.results.item(row).data(Qt.UserRole) for row in range(popup.results.count())]
    assert "data.table" in types and "control.timer" not in types  # a timer has no input for it
    popup.results.setCurrentRow(types.index("data.table"))
    popup.accept_current()
    added = [node_id for node_id in document.flow.nodes if node_id not in ("timer", "table")]
    assert len(added) == 1 and f"timer.index -> {added[0]}.in" in [str(edge) for edge in document.flow.edges]


def test_a_node_goes_into_a_wire(shell):
    document = flow_with_table(shell)
    document.connect_ports("timer.index", "table.in")
    edge = document.flow.edges[0]
    document.insert_on_wire(edge, QPointF(200, 100))
    popup = document.view.popup
    types = [popup.results.item(row).data(Qt.UserRole) for row in range(popup.results.count())]
    assert types and "control.timer" not in types
    popup.results.setCurrentRow(0)
    popup.accept_current()
    inserted = [node_id for node_id in document.flow.nodes if node_id not in ("timer", "table")][0]
    wires = sorted(str(edge) for edge in document.flow.edges)
    assert any(wire.startswith("timer.index -> " + inserted) for wire in wires)
    assert any(wire.endswith("-> table.in") and wire.startswith(inserted) for wire in wires)


# ----------------------------------------------------------------- clipboard
def test_copy_paste_duplicate_and_select_all(shell):
    document = flow_with_table(shell)
    document.connect_ports("timer.index", "table.in")
    document.select_all()
    assert len(document.scene.selected_nodes()) == 2
    assert document.copy_selection()
    pasted = document.paste()
    assert sorted(pasted) == ["table2", "timer2"]
    assert "timer2.index -> table2.in" in [str(edge) for edge in document.flow.edges]
    assert document.flow.nodes["timer2"].position == (40.0, 40.0)
    assert sorted(item.node_id for item in document.scene.selected_nodes()) == ["table2", "timer2"]
    document.undo.undo()
    assert "timer2" not in document.flow.nodes
    document.scene.select_nodes(["table"])
    assert document.duplicate_selection() == ["table2"]
    document.scene.select_nodes(["table2"])
    assert document.cut_selection() and "table2" not in document.flow.nodes
    assert document.paste() == ["table2"]


def test_new_nodes_do_not_pile_up(shell):
    document = shell.new_flow()
    first = document.add_node("data.table", document._center())
    second = document.add_node("data.table", document._center())
    assert document.flow.nodes[first].position != document.flow.nodes[second].position


def test_tab_on_the_canvas_adds_a_node(shell):
    document = shell.new_flow()
    document.view.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Tab, Qt.NoModifier))
    assert document.view.popup is not None and document.view.popup.isVisible()
    assert "Add node..." in [action.text() for action in document.toolbar().actions()]


# -------------------------------------------------------------------- devices
def test_a_device_added_later_is_wired_and_says_so(shell):
    document = shell.new_flow()
    document.add_node("device.capture", (300, 0), "cap")
    messages = []
    document.message.connect(messages.append)
    device = document.add_node("device.instrument", (0, 0), "sim", address="sim:free")
    assert f"{device}.device -> cap.device" in [str(edge) for edge in document.flow.edges]
    assert messages and "cap.device" in messages[0]


# --------------------------------------------------------------------- groups
def test_a_group_carries_its_nodes_and_resizes(shell):
    document = flow_with_table(shell)
    document.add_node("structure.group", (-40, -40), "group", size=[700, 300])
    QApplication.processEvents()
    group = document.scene.nodes["group"]
    group._drag_start = group.pos()
    group._members = {item: item.pos() for item in document.scene.nodes.values() if item is not group}
    group.setPos(group.pos() + QPointF(80, 40))
    assert document.scene.nodes["table"].pos() == QPointF(480, 40)
    document.scene.items_moved(["timer", "table"])
    assert document.flow.nodes["table"].position == (480.0, 40.0)
    # the inside of a group is not the group: clicks there reach the canvas
    assert not group.shape().contains(QPointF(300, 150)) and group.shape().contains(QPointF(300, 5))
    document.set_param("group", "size", [800, 320])
    assert document.flow.nodes["group"].params["size"] == [800, 320]


# ------------------------------------------------------------------- problems
def test_problems_show_on_their_node(shell):
    document = shell.new_flow()
    document.add_node("device.capture", (0, 0), "cap")
    item = document.scene.nodes["cap"]
    assert item.problems and "Error" in item.toolTip()


# ------------------------------------------------------------------ inspector
def test_the_inspector_helps(shell, monkeypatch, tmp_path):
    document = shell.new_flow()
    document.add_node("device.instrument", (0, 0), "uno", address="sim:uno")
    document.add_node("gpio.write", (300, 0), "write")
    node = document.flow.nodes["write"]
    spec = document.flow.spec(node, document.registry)
    inspector = NodeInspector(document.flow, node, spec, problems=["pin: not set"], running=True,
                              suggestions=document.suggestions_for("write"), folder=str(tmp_path))
    texts = [label.text() for label in inspector.findChildren(QLabel)]
    assert any("pin: not set" in text for text in texts)
    assert any("next run" in text for text in texts)
    pin_editor = inspector.editors.get("pin")
    assert isinstance(pin_editor, QComboBox) and "D13" in [pin_editor.itemText(i) for i in range(pin_editor.count())]
    file_id = document.add_node("data.file", (0, 200), "file")
    file_node = document.flow.nodes[file_id]
    inspector = NodeInspector(document.flow, file_node, document.flow.spec(file_node, document.registry),
                              folder=str(tmp_path))
    changed = []
    inspector.param_changed.connect(lambda node_id, name, value: changed.append((name, value)))
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args, **kwargs: (str(tmp_path / "out.lac"), ""))
    inspector.editors["path"].findChild(QPushButton).click()
    assert changed == [("path", "out.lac")]
