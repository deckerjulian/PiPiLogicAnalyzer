"""Arranging a panel with the mouse, and zooming charts."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent, QNativeGestureEvent, QPointingDevice, QWheelEvent
from PySide6.QtWidgets import QApplication, QComboBox

from tests.test_panel import flow_file, gain_panel  # noqa: F401 - the fixture


def editor(shell, flow_file):  # noqa: F811
    document = shell.new_panel(gain_panel(), flow_path=None)
    document.set_flow_file(flow_file)
    document.set_view("Edit")
    document.resize(1000, 700)
    QApplication.processEvents()
    return document


def page_of(document):
    return document._grid_page("")


def test_the_editor_shows_free_cells(shell, flow_file):  # noqa: F811
    document = editor(shell, flow_file)
    page = page_of(document)
    assert page is not None and page.rows >= 6  # three rows of widgets and three free ones
    assert page.cell_at(page.grid.cellRect(4, 2).center()) == (4, 2)


def test_a_widget_moves_to_a_free_cell_and_not_onto_another(shell, flow_file):  # noqa: F811
    document = editor(shell, flow_file)
    page = page_of(document)
    widget = document.panel.widgets[0]  # the input at row 0, column 0
    document.begin_drag(widget.id, "move")
    document.drag_to(page.mapToGlobal(page.grid.cellRect(1, 1).center()))  # the LED is there
    assert document._drag_target is None and page.target is not None and page.target[1] is False
    document.drag_to(page.mapToGlobal(page.grid.cellRect(4, 3).center()))
    assert document.end_drag()
    moved = document.panel.widget(widget.id)
    assert (moved.row, moved.column) == (4, 3)
    document.undo.undo()
    assert (document.panel.widget(widget.id).row, document.panel.widget(widget.id).column) == (0, 0)


def test_a_widget_is_resized_at_its_corner(shell, flow_file):  # noqa: F811
    document = editor(shell, flow_file)
    page = page_of(document)
    widget = next(item for item in document.panel.widgets if item.kind == "button")  # row 2, column 0
    document.begin_drag(widget.id, "resize")
    document.drag_to(page.mapToGlobal(page.grid.cellRect(3, 2).center()))
    assert document.end_drag()
    resized = document.panel.widget(widget.id)
    assert (resized.rows, resized.columns) == (2, 3)


def test_a_widget_is_dropped_from_the_palette(shell, flow_file):  # noqa: F811
    document = editor(shell, flow_file)
    added = document.drop_new("led", "", (4, 2))
    assert added is not None and (added.row, added.column) == (4, 2)
    assert document.drop_new("led", "", (0, 0)) is None  # taken
    assert "taken" in document.flow_label.text()


def test_ports_fit_the_widget_and_tabs_are_chosen(shell, flow_file):  # noqa: F811
    document = editor(shell, flow_file)
    led = next(item for item in document.panel.widgets if item.kind == "led")
    document.select(led.id)
    inspector = document.inspector_widget()
    tab = inspector.findChild(QComboBox, "setting-tab")
    assert "More" in [tab.itemText(index) for index in range(tab.count())]
    assert all(port.split(".")[1] != "a" for port in document.ports("display", "led") if port.startswith("gain."))


def test_edit_and_operate_are_toggles(shell, flow_file):  # noqa: F811
    document = editor(shell, flow_file)
    assert [action.text() for action in document.view_actions] == ["Edit", "Operate"]
    document.view_actions[1].trigger()
    assert document.operating and document.view_actions[1].isChecked() and not document.view_actions[0].isChecked()


def test_a_binding_that_does_not_work_is_marked(shell, flow_file):  # noqa: F811
    document = editor(shell, flow_file)
    document.add_widget("number", "nowhere.out", row=5, column=0)
    QApplication.processEvents()
    item = document.items[document.panel.widgets[-1].id]
    assert "no port nowhere.out" in item.toolTip()


# ------------------------------------------------------------------- charts
def chart():
    from openscilab.ui.documents.chart import ChartWidget

    widget = ChartWidget("xy")
    widget.resize(600, 400)
    widget.set_series({"a": ([0.0, 1.0, 2.0, 3.0, 4.0], [0.0, 1.0, 4.0, 9.0, 16.0])})
    widget.show()
    QApplication.processEvents()
    widget.grab()  # paints: the range is known
    return widget


def test_a_chart_zooms_moves_and_goes_back_to_automatic():
    widget = chart()
    automatic = widget.automatic_bounds()
    center = widget.plot_area().center()
    widget.wheelEvent(QWheelEvent(center, widget.mapToGlobal(center), QPoint(0, 0), QPoint(0, 120), Qt.NoButton,
                                  Qt.NoModifier, Qt.NoScrollPhase, False))
    x0, x1, y0, y1 = widget.bounds()
    assert x1 - x0 < automatic[1] - automatic[0] and y1 - y0 < automatic[3] - automatic[2]
    widget.grab()
    before = widget.bounds()
    widget.wheelEvent(QWheelEvent(center, widget.mapToGlobal(center), QPoint(40, 0), QPoint(320, 0), Qt.NoButton,
                                  Qt.NoModifier, Qt.ScrollUpdate, False))
    assert widget.bounds()[0] < before[0]  # the fingers moved right: earlier values
    position = QPointF(center)
    pinch = QNativeGestureEvent(Qt.SmartZoomNativeGesture, QPointingDevice.primaryPointingDevice(), 2, position,
                                position, position, 0.0, QPointF(0, 0))
    assert widget.event(pinch)
    assert widget.navigator.automatic and widget.bounds() == automatic


def test_a_chart_is_dragged_and_reset_by_double_click():
    widget = chart()
    start = widget.plot_area().center()
    for kind, point, buttons in ((QEvent.MouseButtonPress, start, Qt.LeftButton),
                                 (QEvent.MouseMove, start + QPointF(50, 0), Qt.LeftButton),
                                 (QEvent.MouseButtonRelease, start + QPointF(50, 0), Qt.NoButton)):
        event = QMouseEvent(kind, point, widget.mapToGlobal(point), Qt.LeftButton, buttons, Qt.NoModifier)
        QApplication.sendEvent(widget, event)
    assert not widget.navigator.automatic
    event = QMouseEvent(QEvent.MouseButtonDblClick, start, widget.mapToGlobal(start), Qt.LeftButton, Qt.LeftButton,
                        Qt.NoModifier)
    QApplication.sendEvent(widget, event)
    assert widget.navigator.automatic
