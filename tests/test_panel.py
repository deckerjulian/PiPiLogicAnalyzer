"""Panels: the model and its file, binding widgets to ports, values into the flow and back,
editing with undo, operating full screen, the project data and the template."""

from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QWidget

from openscilab.lab import panel_model, yaml_io
from openscilab.lab.panel_model import Panel, PanelError, PanelWidget

FLOW = """
flow: Gain
nodes:
  gain: {type: dsp.math, expression: "a * b"}
  sweep: {type: control.sweep, start: 1, stop: 3, step: 1}
  over: {type: control.compare, op: ">", value: 5}
edges:
  - gain.out -> over.a
"""


def wait_for(condition, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if condition():
            return True
        time.sleep(0.005)
    return condition()


@pytest.fixture
def flow_file(tmp_path):
    path = tmp_path / "flows" / "gain.flow.yaml"
    path.parent.mkdir()
    path.write_text(FLOW)
    return str(path)


def gain_panel() -> Panel:
    panel = Panel(name="Gain", flow="../flows/gain.flow.yaml")
    panel.add("input", "gain.a", title="a", options={"value": 0})
    panel.add("slider", "gain.b", title="b", options={"min": 0, "max": 10, "value": 2})
    panel.add("number", "gain.out", title="a × b", x=16, y=112)
    panel.add("led", "over.result", title="> 5", x=192, y=112)
    panel.add("button", "sweep.trigger", title="Sweep", x=16, y=208)
    panel.add("number", "sweep.value", title="Step", tab="More")
    return panel


# --------------------------------------------------------------------- model
def test_the_model_round_trips_and_finds_problems(flow_file):
    panel = gain_panel()
    text = panel_model.dumps(panel)
    again = panel_model.loads(text)
    assert panel_model.dumps(again) == text
    assert again.widget("slider").option("max") == 10 and again.tab_names() == ["", "More"]
    assert again.bindings() == ["gain.a", "gain.b", "sweep.trigger"]
    assert [widget.rect for widget in again.widgets[:2]] == [(16, 16, 272, 72), (304, 16, 240, 72)]  # (free places)
    assert again.widget("number2").rect == (16, 16, 160, 80) and (again.width, again.height) == (960, 600)
    flow = yaml_io.loads(FLOW)
    assert panel.problems(flow) == []
    panel.add("number", "gain.a")  # an input on a display
    panel.add("led", "nothing.out").x = panel.width
    found = panel.problems(flow)
    assert any("no output" in problem for problem in found) and any("outside the panel" in problem for problem in found)
    assert any("no node 'nothing'" in problem for problem in found)
    with pytest.raises(PanelError, match="unknown widget kind"):
        PanelWidget("x", "dial")
    with pytest.raises(PanelError, match="unknown setting"):
        panel_model.loads("widgets: {x: {kind: led, size: 3}}")
    with pytest.raises(PanelError, match="unknown setting columns"):
        panel_model.loads("columns: 4\nwidgets: {}")


def test_control_values():
    switch = PanelWidget("s", "switch")
    assert panel_model.control_value(switch, True).value is True
    slider = PanelWidget("v", "slider", options={"unit": "V"})
    assert panel_model.control_value(slider, 2.5).unit == "V"
    assert panel_model.control_value(PanelWidget("b", "button", title="Go"), True).data == ["Go"]
    assert panel_model.display_number(True) == 1.0 and panel_model.display_number("x") is None


# ------------------------------------------------------------------ document
def test_values_go_into_the_flow_and_back(shell, tmp_path, flow_file):
    path = tmp_path / "panels" / "gain.panel.yaml"
    path.parent.mkdir()
    panel_model.save(gain_panel(), str(path))
    document = shell.open_file(str(path))
    assert document.document_kind == "panel" and document.flow() is not None
    assert document.ports("display")[:1] == ["gain.out"]
    assert document.start_run()
    assert document.operating and wait_for(lambda: document.runner.state == "running")
    items = document.items
    items["input"].spin.setValue(4)
    items["input"].set_button.click()  # a = 4, b = 2 (the slider's start value)
    assert wait_for(lambda: items["number"].value_label.text() == "8")
    assert wait_for(lambda: items["led"].led.on)
    items["slider"].set_value(1.0)  # b = 1: 4
    assert wait_for(lambda: items["number"].value_label.text() == "4")
    assert wait_for(lambda: not items["led"].led.on)
    items["button"].button.click()
    assert wait_for(lambda: items["number2"].value_label.text() == "3")  # the sweep ran on the press
    assert not document.grab().isNull()
    document.stop_run()
    assert wait_for(lambda: not document.runner.running)


def test_editing_with_undo(shell, flow_file, monkeypatch):
    from PySide6.QtWidgets import QFileDialog

    document = shell.new_panel(flow_path=flow_file)
    assert document.current_view() == "Edit" and document.flow() is not None
    widget = document.add_widget("number")
    assert widget.bind == "" and document.selected == widget.id  # several outputs: the inspector binds it
    document.update_widget(widget.id, bind="gain.out", width=320)
    assert document.panel.widget(widget.id).bind == "gain.out"
    inspector = document.inspector_widget()
    assert inspector is not None and inspector.findChild(QWidget, "setting-width").value() == 320
    document.move_selected(8, 8)
    document.move_selected(1, 0)  # (moves in a row: one undo step)
    assert document.panel.widget(widget.id).rect == (25, 24, 320, 80)
    document.undo.undo()
    assert document.panel.widget(widget.id).rect == (16, 16, 320, 80)
    document.undo.undo()
    document.undo.undo()
    assert document.panel.widgets == [] and not document.dirty
    document.undo.redo()
    assert document.panel.widgets and document.dirty
    # a click on a widget in the editor selects it
    document.add_widget("led", "over.result")
    document.select(None)
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    item = document.items["led"]
    QTest.mouseClick(item, Qt.LeftButton, pos=QPoint(5, 5))
    assert document.selected == "led"
    document.remove_selected()
    assert "led" not in {widget.id for widget in document.panel.widgets}
    target = os.path.join(os.path.dirname(os.path.dirname(flow_file)), "panels", "new")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args, **kwargs: (target, ""))
    assert document.save() and document.path.endswith("new.panel.yaml")
    assert panel_model.load(document.path).flow == os.path.join("..", "flows", "gain.flow.yaml")


def test_operating_full_screen(shell, flow_file):
    document = shell.new_panel(Panel(name="Kiosk", flow=flow_file))
    document.add_widget("number", "gain.out")
    document.toggle_kiosk()
    assert document.kiosk is not None and document.operating
    assert document.kiosk.isFullScreen() or document.kiosk.windowState() & 0x4  # Qt.WindowFullScreen
    document.toggle_kiosk()
    assert document.kiosk is None and document.operate_area.widget() is not None


def test_the_curve_example_opens_its_panel_and_flow(shell, tmp_path):
    document = shell.open_example("05-measurement/06-characteristic-curve", str(tmp_path / "curve"))
    assert document is not None and document.document_kind == "panel"
    kinds = sorted(item.document_kind for item in shell.area.documents() if item.path)
    assert kinds == ["flow", "panel"]
    assert document.panel.problems(document.flow(), document.registry()) == []
    assert shell.data_folder(document) == str(tmp_path / "curve" / "data")


def test_the_project_data_list(shell, tmp_path):
    import numpy as np

    from openscilab.core import capture_io
    from openscilab.driver.models import AnalyzerChannel, CaptureSession

    root = tmp_path / "lab"
    (root / "data").mkdir(parents=True)
    (root / "project.yaml").write_text("project: Lab\n")
    for name, width in (("a.lac", 10), ("b.lac", 7)):
        session = CaptureSession(frequency=1000, pre_trigger_samples=0, post_trigger_samples=100)
        session.capture_channels = [AnalyzerChannel(channel_number=0, samples=(np.arange(100) // width) % 2)]
        capture_io.save_capture(str(root / "data" / name), session)
    (root / "data" / "log.csv").write_text("time,value\n0,1\n")
    (root / "data" / "report.html").write_text("<html></html>")
    section = shell.project_section
    section.set_data_folder(str(root / "data"))
    kinds = sorted(section.data.item(index).data(0x0101) for index in range(section.data.count()))
    assert kinds == ["capture", "capture", "report", "table"]
    dialog = shell.compare_captures(str(root / "data" / "a.lac"), str(root / "data" / "b.lac"))
    assert dialog is not None and dialog.result is not None and not dialog.result.identical
    dialog.close()
    exported = shell.export_capture_csv(str(root / "data" / "a.lac"), str(root / "data" / "a.csv"))
    with open(exported) as handle:
        assert handle.readline().startswith("Time")


def test_the_curve_in_the_operating_panel_in_virtual_time(shell, tmp_path):
    """Acceptance (Phase 0): the example *Characteristic curve* (a template before) with the simulated Uno and DHO924S,
    operated in its panel: Start sweeps, the scope measures, the slope is checked and reported."""
    document = shell.open_example("05-measurement/06-characteristic-curve", str(tmp_path / "curve"))
    document.fast_box.setChecked(True)
    assert document.start_run()
    assert wait_for(lambda: document.runner.state == "running")
    started = time.monotonic()
    document.items["start"].button.click()
    assert wait_for(lambda: document.items["passed"].led.on, 10)
    elapsed = time.monotonic() - started
    assert elapsed < 5  # in virtual time the 5.7 s of the sweep pass at once
    slope = float(document.items["slope"].value_label.text().split()[0])
    assert slope == pytest.approx(5.0, abs=0.25)
    assert document.items["scope"].plot.capture is not None
    assert wait_for(lambda: os.path.exists(tmp_path / "curve" / "data" / "curve.html"))
    document.stop_run()
    assert wait_for(lambda: not document.runner.running)


def test_the_editor_scrolls_over_widgets(shell, flow_file):
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtWidgets import QApplication

    document = shell.new_panel(flow_path=flow_file)
    for row in range(30):
        document.add_widget("number", "gain.out", y=row * 100)
    QApplication.processEvents()
    bar = document.edit_area.verticalScrollBar()
    assert bar.maximum() > 0
    item = document.items[document.panel.widgets[0].id]
    target = item.findChildren(QWidget)[0] if item.findChildren(QWidget) else item
    event = QWheelEvent(QPointF(5, 5), target.mapToGlobal(QPointF(5, 5)), QPoint(0, -40), QPoint(0, -320),
                        Qt.NoButton, Qt.NoModifier, Qt.ScrollUpdate, False)
    QApplication.sendEvent(target, event)
    assert bar.value() == 40


# ------------------------------------------------------- panels from a flow
def test_suggested_widgets_bind_to_ports_of_the_flow():
    flow = yaml_io.loads(FLOW)
    widgets = panel_model.suggest(flow)
    binds = {widget.bind: widget.kind for widget in widgets}
    assert binds["sweep.trigger"] == "button" and binds["over.result"] == "led" and binds["gain.out"] == "number"
    panel = Panel(widgets=widgets)
    assert panel.problems(flow) == []  # bound ports exist, all on the panel
    assert not any(panel_model._overlap(first.rect, second.rect) for first in widgets for second in widgets
                   if first is not second)
    assert {widget.bind for widget in widgets if widget.y == panel_model.MARGIN} == {"gain.a", "sweep.trigger"}


@pytest.mark.parametrize("key", ["05-measurement/06-characteristic-curve", "08-data/07-long-term-logger",
                                 "05-measurement/07-test-bench", "00-start/03-data-acquisition",
                                 "00-start/04-synchronized-instruments", "00-start/05-remote-measuring-device"])
def test_suggested_panels_have_no_problems(key):
    import glob

    from openscilab.lab.flow_files import load_flow

    root = os.path.join(os.path.dirname(__file__), "..", "examples", "library", key)
    flow = load_flow(glob.glob(os.path.join(root, "flows", "*.flow.yaml"))[0])
    widgets = panel_model.suggest(flow)
    assert 0 < len(widgets) <= panel_model.SUGGESTED_WIDGETS
    panel = Panel(widgets=widgets)
    panel.grow_to_fit()
    assert panel.problems(flow) == [] and panel.width == 960


def test_a_panel_for_the_unsaved_flow_of_the_graph(shell, tmp_path, monkeypatch):
    """The flow at the start is not saved: its Panel button makes a panel that works with it."""
    from openscilab.ui import messages
    from openscilab.ui.documents.panel import PanelDocument

    flow_document = shell.new_flow()
    for node_id, node in yaml_io.loads(FLOW).nodes.items():
        flow_document.add_node(node.type, (0, 0), node_id, **node.params)
    flow_document.connect_ports("gain.out", "over.a")
    flow_document.set_param("gain", "expression", "a * 2")  # (no slider for b in this panel)
    flow_document.action_panel.trigger()
    document = shell.active_document()
    assert isinstance(document, PanelDocument) and document.flow_document is flow_document
    assert document.flow() is flow_document.flow and document.flow_file() is None
    assert "gain.out" in document.ports("display")
    assert {widget.bind for widget in document.panel.widgets} >= {"sweep.trigger", "over.result", "gain.out"}
    flow_document.add_node("measure.mean", (300, 0), "mean")  # the panel sees edits of the flow at once
    assert "mean.out" in document.ports("display")
    flow_document.undo.undo()  # (its input is not wired: the flow could not run)

    lines = []
    document.execution_line.connect(lines.append)
    assert document.start_run()  # runs the flow of the document, writing into its scratch folder
    assert wait_for(lambda: document.runner.state == "running"), (document.runner.state, lines)
    document.items["gain_a"].spin.setValue(4)
    document.items["gain_a"].set_button.click()
    assert wait_for(lambda: document.items["gain_out"].value_label.text() == "8")
    assert wait_for(lambda: document.items["over_result"].led.on)
    document.stop_run()
    assert wait_for(lambda: not document.runner.running)

    # saving the panel saves the flow first, so the panel can name its file
    flow_path = str(tmp_path / "gain.flow.yaml")
    monkeypatch.setattr(messages, "choose", lambda *args, **kwargs: 0)
    monkeypatch.setattr(flow_document, "save", lambda: flow_document._write(flow_path))
    assert document._write(str(tmp_path / "gain.panel.yaml"))
    saved = panel_model.load(str(tmp_path / "gain.panel.yaml"))
    assert saved.flow == "gain.flow.yaml" and saved.widgets


# ------------------------------------------------------------- free layout
def test_widgets_snap_to_each_other_to_the_panel_and_to_the_raster():
    other = (16, 16, 160, 64)
    # the left edge comes within 6 px of the other's right edge, the top of its top: they meet
    rect, guides = panel_model.snap((181, 19, 160, 80), [other], (960, 600))
    assert rect == (176, 16, 160, 80) and ("x", 176.0, 16.0, 96.0) in guides and guides[-1][:2] == ("y", 16.0)
    # nothing near: on the raster of 8 px
    assert panel_model.snap((301, 203, 100, 50), [other], (960, 600))[0] == (304, 200, 100, 50)
    # the middle of the panel; within the panel
    assert panel_model.snap((427, 503, 100, 50), [], (960, 600))[0] == (430, 504, 100, 50)
    assert panel_model.snap((900, 580, 100, 50), [], (960, 600))[0] == (860, 550, 100, 50)
    # a resize moves only its edges; at least the smallest size
    rect, guides = panel_model.snap((300, 200, 100, 50), [other], (960, 600), "right bottom")
    assert rect == (300, 200, 100, 48)
    assert panel_model.snap((300, 200, 3, 50), [], (960, 600), "right")[0][2] == panel_model.MIN_SIZE[0]
    # free (Alt): where the mouse puts it
    assert panel_model.snap((181, 19, 160, 80), [other], (960, 600), threshold=0, grid=0)[0] == (181, 19, 160, 80)


def test_widgets_are_lined_up_and_distributed():
    rects = {"a": (10, 10, 50, 20), "b": (40, 50, 80, 30), "c": (200, 5, 20, 20)}
    assert panel_model.align(rects, "left") == {"a": (10, 10, 50, 20), "b": (10, 50, 80, 30), "c": (10, 5, 20, 20)}
    assert panel_model.align(rects, "right")["a"] == (170, 10, 50, 20)
    assert panel_model.align(rects, "middle")["b"] == (40, 28, 80, 30)
    spread = panel_model.distribute(rects, "x")
    assert spread["a"] == (10, 10, 50, 20) and spread["c"] == (200, 5, 20, 20) and spread["b"] == (90, 50, 80, 30)
    with pytest.raises(ValueError):
        panel_model.align(rects, "diagonal")


def test_the_editor_moves_resizes_and_selects_with_the_mouse(shell, flow_file):
    from PySide6.QtCore import QPoint, Qt

    from openscilab.ui.documents.panel import ORIGIN, _EditPage

    document = shell.new_panel(flow_path=flow_file)
    first = document.add_widget("number", "gain.out", x=16, y=16)
    second = document.add_widget("led", "over.result", x=400, y=200)
    page = document.edit_area.widget()
    assert isinstance(page, _EditPage)

    def at(x, y):  # a point of the panel on the page
        return QPoint(ORIGIN + x, ORIGIN + y)

    # drag the LED next to the number: it snaps to its edges, the guides show it, one undo step
    page.press(at(410, 210), second.id, Qt.NoModifier)
    page.drag_move(at(190, 30), Qt.LeftButton, Qt.NoModifier)
    assert page.live[second.id] == (176, 16, 120, 80) and page.guides
    page = document.edit_area.widget()
    page.release()
    assert document.panel.widget(second.id).rect == (176, 16, 120, 80) and document.undo.undoText() == "Move"
    page = document.edit_area.widget()
    assert not page.guides
    # the handle at its right edge makes it wider (on the raster); Alt: freely
    page.press(at(176 + 120, 56), None, Qt.NoModifier)
    page.drag_move(at(176 + 203, 56), Qt.LeftButton, Qt.NoModifier)
    assert page.live[second.id] == (176, 16, 200, 80)
    page.drag_move(at(176 + 203, 56), Qt.LeftButton, Qt.AltModifier)
    assert page.live[second.id] == (176, 16, 203, 80)
    page.release()
    assert document.panel.widget(second.id).rect == (176, 16, 203, 80)
    # a frame on the free surface selects what it touches; Shift and a click add one
    page = document.edit_area.widget()
    page.press(at(5, 300), None, Qt.NoModifier)
    page.drag_move(at(200, 60), Qt.LeftButton, Qt.NoModifier)
    page.release()
    assert document.selection == [first.id, second.id]
    page.press(at(20, 20), first.id, Qt.ShiftModifier)
    page.release()
    assert document.selection == [second.id]
    # the arrow keys move the selection (Shift: by the raster)
    from PySide6.QtTest import QTest

    page = document.edit_area.widget()
    page.setFocus()
    QTest.keyClick(page, Qt.Key_Right, Qt.ShiftModifier)
    QTest.keyClick(document.edit_area.widget(), Qt.Key_Down)
    assert document.panel.widget(second.id).rect == (184, 17, 203, 80)
    # the corner of the panel resizes it (not smaller than its widgets need)
    page = document.edit_area.widget()
    page.press(at(960, 600), None, Qt.NoModifier)
    page.drag_move(at(10, 10), Qt.LeftButton, Qt.NoModifier)
    page.release()
    assert (document.panel.width, document.panel.height) == (387, 120)  # (120: the smallest panel)


def test_several_widgets_are_arranged(shell, flow_file):
    document = shell.new_panel(flow_path=flow_file)
    ids = [document.add_widget("number", "gain.out", x=x, y=y).id for x, y in ((16, 16), (300, 40), (520, 24))]
    document.select_many(ids)
    from PySide6.QtWidgets import QLabel, QMenu

    assert any(label.text() == "<b>3</b> selected" for label in document.inspector_widget().findChildren(QLabel))

    menu = QMenu()
    document.fill_arrange_menu(menu)
    titles = [action.text() for action in menu.actions() if action.text()]
    assert titles[:3] == ["Align left edges", "Align centres", "Align right edges"]
    next(action for action in menu.actions() if action.text() == "Align top edges").trigger()
    assert {document.panel.widget(key).y for key in ids} == {16} and document.undo.undoText() == "Align"
    document.distribute_selected("x")
    assert [document.panel.widget(key).x for key in ids] == [16, 268, 520]
    document.select(ids[0])
    document.raise_selected()
    assert document.panel.widgets[-1].id == ids[0]
    document.select_many(ids[:2])
    document.remove_selected()
    assert [widget.id for widget in document.panel.widgets] == [ids[2]]


def test_a_widget_from_the_palette_lands_where_it_is_dropped(shell, flow_file):
    document = shell.new_panel(flow_path=flow_file)
    widget = document.drop_new("slider", "", (100, 200))
    assert widget.rect == (100, 200, 240, 72) and document.selection == [widget.id]
    hint = document.edit_area.widget().findChild(QWidget)  # (no hint on a panel with widgets)
    assert hint is not None


def test_the_operated_panel_scales_with_its_window(shell, flow_file):
    from openscilab.ui.documents.panel import MIN_SCALE, _RunPage

    document = shell.new_panel(Panel(name="Scaled", flow=flow_file, width=400, height=200))
    document.add_widget("number", "gain.out", x=0, y=0, width=200, height=100)
    document.set_view("Operate")
    page = document.operate_area.takeWidget()  # (sized here, not by the scroll area)
    assert isinstance(page, _RunPage)
    page.show()
    page.resize(800, 800)
    item = document.items["number"]
    assert item.geometry().getRect() == (0, 0, 400, 200)  # twice the size: the window is twice as wide
    page.resize(1000, 200)
    assert item.geometry().getRect() == (300, 0, 200, 100)  # the height limits it: in the middle
    page.resize(10, 10)
    assert page.scale() == MIN_SCALE
    page.close()
