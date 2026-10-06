"""What a running flow sends to the window: collected and delivered a few times a second, shown
by charts that cope with any number of points and with values that are no numbers."""

from __future__ import annotations

import math
import time

import numpy as np
from PySide6.QtCore import QPointF, QRectF
from PySide6.QtWidgets import QApplication

from openscilab.core import signals
from openscilab.lab import Flow
from openscilab.lab.panel_model import PanelWidget, display_number
from openscilab.ui.documents.chart import ChartDocument, ChartWidget, ValueDocument, nice_ticks
from openscilab.ui.flow.runner import FlowRunner
from openscilab.ui.panel.widgets import ChartItem, LedItem, NumberItem
from openscilab.ui.widgets.plot_lines import screen_points
from openscilab.ui.widgets.plot_navigation import wide_enough


def wait_for(condition, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if condition():
            return True
        time.sleep(0.005)
    return condition()


def fast_flow(count: int = 20_000) -> Flow:
    flow = Flow("fast")
    flow.add_node("control.timer", "timer", interval="1 ms", count=count)
    flow.add_node("dsp.math", "double", expression="a * 2")
    flow.add_node("view.strip_chart", "chart", window="1 s")
    flow.connect("timer.index", "double.a")
    flow.connect("double.out", "chart.in")
    return flow


# ------------------------------------------------------------------ the runner
def test_values_arrive_in_batches_and_completely_at_the_end(qtbot):
    runner = FlowRunner()
    deliveries, values, shown, states = [], [], [], {}
    runner.values.connect(lambda node, port, batch: (node, port) == ("double", "out")
                          and (deliveries.append(len(batch)), values.extend(batch)))
    runner.node_state.connect(lambda node, state, _message: states.__setitem__(node, state))
    runner.start(fast_flow(), mode="virtual")
    runner.views.shown.connect(lambda kind, node, value, options: shown.append(kind))
    assert wait_for(lambda: runner.result is not None, 30)
    # 20 000 values did not become 20 000 signals, and the last ones are there with the result
    assert len(deliveries) < 2000 and values[-1].value == 39998.0
    assert states == {"timer": "done", "double": "done", "chart": "done"}
    assert shown and len(shown) < 2000 and set(shown) == {"strip_chart"}
    assert runner.result.ok


def test_a_closed_document_hears_nothing_of_its_run(shell):
    document = shell.new_flow(fast_flow(200_000))
    document.fast_box.setChecked(True)
    assert document.start_run()
    engine = document.runner.engine
    shell.area.close_document(document)  # while it runs
    assert wait_for(lambda: engine.state in ("stopped", "finished"), 10)
    QApplication.processEvents()  # nothing raises for the deleted document


def test_wires_take_batches(shell):
    document = shell.new_flow(fast_flow(10))
    wire = next(iter(document.scene.wires.values()))
    document.scene.show_values("timer", "index", [1.0, 2.0, 3.0])
    assert list(wire.history) == [1.0, 2.0, 3.0] and wire.label == "3"


# ------------------------------------------------------------------- the shell
def test_values_do_not_rebuild_the_menus(shell, monkeypatch):
    chart = ChartDocument("strip_chart", "Chart")
    shell.add_document(chart)
    rebuilt = []
    monkeypatch.setattr(shell, "_on_active_changed", lambda document: rebuilt.append(document))
    shell.area.active_changed.disconnect()
    shell.area.active_changed.connect(shell._on_active_changed)
    changed = []
    chart.document_changed.connect(lambda: changed.append(1))
    for index in range(50):
        chart.show_value({"in": ([0.0, float(index)], [1.0, 2.0])}, {"title": "Chart"})
    assert changed == [] and rebuilt == []
    chart.show_value({"in": ([0.0], [1.0])}, {"title": "Renamed"})
    assert changed == [1] and chart.title == "Renamed"
    assert rebuilt == []  # the title changed: the tab follows, the menus stay


def test_an_edit_updates_the_actions_without_new_menus(shell, make_dataview):
    from tests.test_data_editing import session

    view = make_dataview()
    view.load_session(session())
    shell.area.activate(view)
    menus = list(shell._document_menu_actions)
    assert not shell.action_undo.isEnabled()
    view.delete_samples(0, 10)
    assert shell.action_undo.isEnabled()  # followed the document
    assert shell._document_menu_actions == menus  # the same menu actions, not removed and added again


def test_the_scope_keeps_the_view(shell):
    source = shell.new_flow(Flow("x"))
    capture = signals.Capture(name="stream", rate=1000.0, digital={"D0": np.zeros(5000, np.uint8)})
    view = shell.show_view(source, "scope", "scope", capture, {"title": "Scope"})
    view.model.set_view(1000, 500)
    longer = signals.Capture(name="stream", rate=1000.0, digital={"D0": np.zeros(9000, np.uint8)})
    assert shell.show_view(source, "scope", "scope", longer, {"title": "Scope"}) is view
    assert (view.model.first_sample, view.model.visible_samples) == (1000, 500)
    assert view.model.sample_count == 9000


# ---------------------------------------------------------------------- charts
def test_ticks_never_hang():
    started = time.monotonic()
    for low, high in ((100.0, 100.0 + 1e-14), (1e9, 1e9 + 1e-6), (-1e300, 1e300), (5.0, 5.0), (0.0, 1e-320)):
        ticks = nice_ticks(low, high)
        assert len(ticks) <= 101 and all(math.isfinite(tick) for tick in ticks)
    assert time.monotonic() - started < 1
    assert nice_ticks(0, 10) == [0, 2, 4, 6, 8, 10]
    assert nice_ticks(float("nan"), 1) == []


def test_the_zoom_limit_follows_the_size_of_the_values():
    assert wide_enough(0.0, 1e-9)  # a nanovolt around zero is a range
    assert not wide_enough(100.0, 100.0 + 1e-12)  # a picosecond at 100 s is not
    assert not wide_enough(1.0, 1.0)


def test_a_long_line_becomes_its_envelope():
    count = 200_000
    xs = np.arange(count) / 1000.0
    ys = np.sin(xs) * 3.0
    ys[123_456] = 50.0  # one spike
    area = QRectF(0, 0, 400, 200)
    points = screen_points(xs, ys, (0.0, float(xs[-1]), -60.0, 60.0), area)
    assert len(points) <= 2 * 401  # two points per pixel column
    top = min(point.y() for point in points)
    assert abs(top - (200 - (50.0 + 60.0) / 120.0 * 200)) < 0.01  # the spike is still there
    # few points, points that go back in x, and an XY chart are drawn as they are
    assert len(screen_points([0, 1, 2], [0, 1, 0], (0, 2, 0, 1), area)) == 3
    assert len(screen_points(xs[::-1], ys, (0.0, float(xs[-1]), -60.0, 60.0), area)) == count
    assert len(screen_points(xs, ys, (0.0, float(xs[-1]), -60.0, 60.0), area, envelope=False)) == count


def test_values_that_are_no_numbers_do_not_blank_a_chart(qtbot):
    chart = ChartWidget("strip_chart")
    qtbot.addWidget(chart)
    chart.resize(400, 300)
    chart.set_series({"a": ([0.0, 1.0, 2.0, 3.0], [1.0, float("nan"), 3.0, float("inf")])})
    assert chart.automatic_bounds()[:2] == (0.0, 2.0)
    assert not chart.grab().isNull()

    item = ChartItem(PanelWidget("chart", "chart"))
    qtbot.addWidget(item)
    item.resize(300, 200)
    for index, value in enumerate([1.0, float("nan"), 2.0, 3.0]):
        item.show_value((float(index), value))
    assert not item.grab().isNull()
    points = screen_points([p[0] for p in item.plot.points], [p[1] for p in item.plot.points],
                           (0.0, 3.0, 1.0, 3.0), QRectF(0, 0, 100, 100))
    assert len(points) == 3 and all(isinstance(point, QPointF) and math.isfinite(point.y()) for point in points)


def test_displays_show_blocks_of_samples(qtbot):
    analog = signals.Analog(name="A0", unit="V", values=np.array([0.5, 1.5, 2.5]))
    high = signals.Digital(name="D2", values=np.array([0, 0, 1], np.uint8))
    low = signals.Digital(name="D2", values=np.array([1, 1, 0], np.uint8))
    assert display_number(analog) == 2.5 and display_number(high) == 1.0
    assert display_number(signals.Analog(name="A0", values=np.zeros(0))) is None

    number = NumberItem(PanelWidget("n", "number"))
    led = LedItem(PanelWidget("l", "led"))
    for item in (number, led):
        qtbot.addWidget(item)
    number.show_value(analog)
    assert number.value_label.text().startswith("2.5")
    led.show_value(high)
    assert led.led.on
    led.show_value(low)
    assert not led.led.on  # it was "on" for any block that had samples


def test_values_without_a_time_do_not_pile_up(qtbot):
    item = ChartItem(PanelWidget("chart", "chart"))
    qtbot.addWidget(item)
    for value in (1.0, 2.0, 3.0):
        item.show_value(signals.Scalar(name="x", value=value))  # at = 0: no time given
        time.sleep(0.002)
    xs = [point[0] for point in item.plot.points]
    assert xs == sorted(xs) and len(set(xs)) == 3


def test_a_growing_table_adds_its_rows(qtbot, monkeypatch):
    document = ValueDocument("table", "Table")
    qtbot.addWidget(document)
    table = signals.Table(columns={"x": [1.0, 2.0], "y": [3.0, 4.0]})
    document.show_value(table, {})
    written = []
    set_item = document.table.setItem
    monkeypatch.setattr(document.table, "setItem", lambda row, column, item: (written.append((row, column)),
                                                                             set_item(row, column, item)))
    table.add_row({"x": 5.0, "y": 6.0})
    document.show_value(table, {})
    assert written == [(2, 0), (2, 1)]  # only the new row
    assert document.table.rowCount() == 3 and document.table.item(2, 1).text() == "6"
    document.show_value(signals.Table(columns={"a": [1.0]}), {})  # another table: all of it
    assert document.table.columnCount() == 1 and document.table.rowCount() == 1


# ------------------------------------------------------------ the flow editor
def test_inspectors_are_replaced_not_collected(shell):
    from openscilab.ui.flow.inspector import FlowInspector, NodeInspector

    document = shell.new_flow(fast_flow(10))
    for _round in range(10):
        document._selection_changed(["timer"])
        document._selection_changed([])
    QApplication.sendPostedEvents(None, 52)  # deferred deletes
    QApplication.processEvents()
    kept = document.findChildren(NodeInspector) + document.findChildren(FlowInspector)
    assert len(kept) <= 1  # there were twenty


def test_typing_the_description_keeps_the_field(shell):
    from openscilab.ui.flow.inspector import FlowInspector

    document = shell.new_flow(fast_flow(10))
    shell.area.activate(document)
    document._selection_changed([])
    inspector = document.inspector_widget()
    assert isinstance(inspector, FlowInspector)
    shell.show()
    inspector.description_edit.setFocus()
    if not inspector.has_focus():  # (no focus without an active window on this platform)
        inspector.has_focus = lambda: True
    document._edit("Description", lambda flow: setattr(flow, "description", "typed so far"))
    assert document.inspector_widget() is inspector  # not rebuilt under the cursor
    assert inspector.flow is document.flow


def test_ports_are_only_built_again_when_they_change(shell):
    document = shell.new_flow(fast_flow(10))
    item = document.scene.nodes["timer"]
    ports = dict(item.ports)
    document.set_param("timer", "interval", "5 ms")
    assert document.scene.nodes["timer"] is item and item.ports == ports
    assert all(now is before for now, before in zip(item.ports.values(), ports.values()))
    # the table gets a port per column: other ports, built again
    table = document.add_node("data.table", (0, 300))
    before = dict(document.scene.nodes[table].ports)
    document.set_param(table, "columns", ["a", "b"])
    assert set(document.scene.nodes[table].ports) != set(before)


def test_a_wire_makes_its_ports_connected(shell):
    document = shell.new_flow(fast_flow(10))
    scene = document.scene
    assert scene.port_connected("timer.index", False) and scene.port_connected("double.a", True)
    assert not scene.port_connected("timer.tick", False)
    document.connect_ports("timer.tick", "chart.in")
    assert scene.port_connected("timer.tick", False)


def test_search_popups_and_detached_windows_are_freed(shell):
    from openscilab.ui.flow.canvas import NodeSearchPopup
    from openscilab.ui.shell.documents import DetachedWindow

    document = shell.new_flow(fast_flow(10))
    for _round in range(5):
        popup = document.view.open_search()
        popup.close()
    for _round in range(3):
        shell.area.detach(document)
        shell.area.attach(document)
    QApplication.sendPostedEvents(None, 52)
    QApplication.processEvents()
    assert document.view.popup is None
    assert document.view.findChildren(NodeSearchPopup) == []
    assert shell.findChildren(DetachedWindow) == []
    assert document in shell.area.documents()


def test_a_subflow_document_whose_subflow_is_gone(shell, monkeypatch):
    from openscilab.ui import messages
    from openscilab.ui.documents.flow import FlowDocument

    document = shell.new_flow(fast_flow(10))
    document.scene.select_nodes(["double"])
    node_id = document.make_subflow("part")
    document.open_node(node_id)
    child = next(doc for doc in shell.documents() if isinstance(doc, FlowDocument) and doc.parent_document is document)
    document.undo.undo()  # the subflow is no longer part of the flow
    warned = []
    monkeypatch.setattr(messages, "warning", lambda *args, **kwargs: warned.append(args[2]))
    assert child._edit("Rename flow", lambda flow: setattr(flow, "name", "x")) is False
    assert warned and "no longer part" in warned[0]
