"""Arranging flows (lab/layout.py), the Arrange command, Auto-arrange and the placed nodes of the
templates and examples."""

from __future__ import annotations

import glob
import os

import pytest

from openscilab.lab import layout, yaml_io
from openscilab.lab.model import Flow

LIBRARY = os.path.join(os.path.dirname(__file__), "..", "examples", "library")
FLOW_FILES = sorted(glob.glob(os.path.join(LIBRARY, "*", "*", "flows", "*.flow.yaml")))


def _flow(*edges: str) -> Flow:
    flow = Flow("t")
    for edge in edges:
        source, target = (part.strip() for part in edge.split("->"))
        for ref in (source, target):
            node = ref.split(".")[0]
            if node not in flow.nodes:
                flow.add_node("control.counter", node)
        flow.connect(source, target)
    return flow


def _overlaps(positions: dict, height, width: float) -> list[tuple[str, str]]:
    found = []
    items = list(positions.items())
    for index, (first, (x1, y1)) in enumerate(items):
        for second, (x2, y2) in items[index + 1:]:
            if x1 < x2 + width and x2 < x1 + width and y1 < y2 + height(second) and y2 < y1 + height(first):
                found.append((first, second))
    return found


def test_nodes_follow_their_wires_from_left_to_right():
    flow = _flow("a.count -> b.reset", "b.count -> c.reset", "a.count -> c.increment")
    positions = layout.arrange(flow, lambda _node: 60.0, 190.0)
    assert positions["a"][0] < positions["b"][0] < positions["c"][0]


def test_a_feedback_wire_does_not_move_the_node_that_drives_it():
    # a sweep: step -> capture -> measurement -> back to the sweep's 'next'
    flow = _flow("sweep.count -> capture.reset", "capture.count -> mean.reset", "mean.count -> sweep.increment",
                 "sweep.count -> table.reset", "mean.count -> table.increment")
    positions = layout.arrange(flow, lambda _node: 60.0, 190.0)
    assert positions["sweep"][0] < positions["capture"][0] < positions["mean"][0] < positions["table"][0]


def test_nodes_of_a_column_do_not_overlap_with_their_real_heights():
    flow = _flow(*[f"source.count -> n{index}.reset" for index in range(5)])
    heights = {f"n{index}": 40.0 + 50 * index for index in range(5)}
    positions = layout.arrange(flow, lambda node: heights.get(node, 60.0), 190.0)
    assert _overlaps(positions, lambda node: heights.get(node, 60.0), 190.0) == []


def test_unwired_nodes_go_to_the_end_and_comments_below():
    flow = _flow("a.count -> b.reset")
    flow.add_node("report.write", "report")
    flow.add_node("structure.comment", "note")
    positions = layout.arrange(flow, lambda _node: 60.0, 190.0)
    assert positions["report"][0] == positions["b"][0]
    assert positions["note"][1] > max(positions[node][1] for node in ("a", "b", "report"))


@pytest.mark.parametrize("path", FLOW_FILES, ids=[os.path.relpath(path, LIBRARY) for path in FLOW_FILES])
def test_every_node_of_an_example_is_placed_and_none_overlap(path, shell):
    from openscilab.ui.documents.flow import open_flow_file

    flow = yaml_io.load(path)
    assert all(node.position is not None for node in flow.nodes.values()), path
    document = open_flow_file(path)
    try:
        # (a group is a frame around nodes: it overlaps them on purpose)
        positions = {node_id: node.position for node_id, node in flow.nodes.items() if node.type != "structure.group"}
        assert _overlaps(positions, document.scene.node_height, 190.0) == []
    finally:
        document.deleteLater()


def test_arrange_places_every_node_in_one_undo_step(shell):
    from openscilab.ui.documents.flow import FlowDocument

    flow = _flow("a.count -> b.reset", "b.count -> c.reset")
    for node in flow.nodes.values():
        node.position = (0.0, 0.0)  # all on one spot
    document = FlowDocument(flow)
    shell.add_document(document)
    document.arrange()
    xs = [document.flow.nodes[node].position[0] for node in "abc"]
    assert xs == sorted(xs) and len(set(xs)) == 3
    assert document.undo.count() == 1
    document.undo.undo()
    assert document.flow.nodes["a"].position == (0.0, 0.0)


def test_wires_run_straight_where_they_can():
    """A chain of nodes wired port to port lines up: the wires between them are level."""
    flow = _flow("a.count -> b.reset", "b.count -> c.reset")

    def port(node, name, is_input):
        return {"reset": 40.0, "count": 40.0}.get(name, 13.0)

    positions = layout.arrange(flow, lambda _node: 100.0, 190.0, port)
    assert positions["a"][1] == positions["b"][1] == positions["c"][1]


def test_a_long_wire_keeps_the_nodes_it_passes_out_of_its_way():
    # a -> b -> c -> d and a -> d: the long wire gets waypoints; b and c are ordered beside them
    flow = _flow("a.count -> b.reset", "b.count -> c.reset", "c.count -> d.reset", "a.count -> d.increment",
                 "x.count -> b.increment")
    positions = layout.arrange(flow, lambda _node: 60.0, 190.0)
    assert positions["a"][0] < positions["b"][0] < positions["c"][0] < positions["d"][0]
    assert _overlaps(positions, lambda _node: 60.0, 190.0) == []


def test_the_order_with_fewer_crossings_wins():
    # two sources each wired to the target on the far side: the ordering untangles them
    flow = _flow("s1.count -> t2.reset", "s2.count -> t1.reset", "t1.count -> end.reset", "t2.count -> end.increment")
    positions = layout.arrange(flow, lambda _node: 60.0, 190.0)
    upper_source = min(("s1", "s2"), key=lambda node: positions[node][1])
    upper_target = min(("t1", "t2"), key=lambda node: positions[node][1])
    assert {"s1": "t2", "s2": "t1"}[upper_source] == upper_target


def test_auto_arrange_places_new_nodes_and_wires(shell):
    from openscilab.core import preferences

    document = shell.new_flow()
    assert not document.auto_arrange
    document.set_auto_arrange(True)
    assert preferences.get("flow.auto_arrange") is True
    document.add_node("control.timer", (900, 900), "timer")
    document.add_node("control.counter", (0, 0), "counter")
    document.connect_ports("timer.tick", "counter.in")
    from PySide6.QtWidgets import QApplication

    for _ in range(10):
        QApplication.processEvents()
    timer, counter = document.flow.nodes["timer"].position, document.flow.nodes["counter"].position
    assert timer[0] < counter[0]  # along the wire, left to right
    assert document.undo.undoText() == "Arrange"
    # a node moved by hand stays where it was put
    document._edit("Move", lambda flow: setattr(flow.nodes["counter"], "position", (1000.0, 40.0)))
    for _ in range(10):
        QApplication.processEvents()
    assert document.flow.nodes["counter"].position == (1000.0, 40.0)
    document.set_auto_arrange(False)
