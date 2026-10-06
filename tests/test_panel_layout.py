"""Arranging a panel with the mouse - events through the widgets, as the user makes them - and
zooming charts."""

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
    """The page of the first tab in the editor."""
    from PySide6.QtWidgets import QTabWidget

    content = document.edit_area.widget()
    return content.currentWidget() if isinstance(content, QTabWidget) else content


def mouse(target, kind, point, buttons=Qt.LeftButton, modifiers=Qt.NoModifier):
    """A mouse event at ``point`` (pixels of ``target``)."""
    position = QPointF(point)
    button = Qt.LeftButton if kind != QEvent.MouseMove else Qt.NoButton
    QApplication.sendEvent(target, QMouseEvent(kind, position, target.mapToGlobal(position), button, buttons,
                                               modifiers))


def test_the_editor_shows_the_surface_of_the_panel(shell, flow_file):  # noqa: F811
    from openscilab.ui.documents.panel import ORIGIN, _EditPage

    document = editor(shell, flow_file)
    page = page_of(document)
    assert isinstance(page, _EditPage)
    assert page.minimumWidth() == document.panel.width + 2 * ORIGIN
    for widget in document.panel.widgets[:5]:  # (the first tab)
        assert document.items[widget.id].geometry() == page.page_rect(widget.rect)


def test_a_widget_is_dragged_with_the_mouse(shell, flow_file):  # noqa: F811
    document = editor(shell, flow_file)
    widget = document.panel.widgets[0]  # the input at 16, 16
    item = document.items[widget.id]
    middle = item.rect().center()
    mouse(item, QEvent.MouseButtonPress, middle)  # (the item does not take it: the editor does)
    assert document.selection == [widget.id]
    mouse(item, QEvent.MouseMove, middle + QPoint(203, 301))
    mouse(item, QEvent.MouseButtonRelease, middle + QPoint(203, 301), Qt.NoButton)
    assert document.panel.widget(widget.id).rect[:2] == (216, 320)  # (on the raster)
    document.undo.undo()
    assert document.panel.widget(widget.id).rect[:2] == (16, 16)


def test_a_widget_is_resized_at_a_handle(shell, flow_file):  # noqa: F811
    from openscilab.ui.documents.panel import _handles

    document = editor(shell, flow_file)
    widget = next(item for item in document.panel.widgets if item.kind == "button")
    document.select(widget.id)
    page = page_of(document)
    corner = _handles(page.page_rect(widget.rect))["right bottom"]
    mouse(page, QEvent.MouseButtonPress, corner)
    mouse(page, QEvent.MouseMove, corner + QPoint(98, 41))
    mouse(page, QEvent.MouseButtonRelease, corner + QPoint(98, 41), Qt.NoButton)
    x, y, width, height = document.panel.widget(widget.id).rect
    assert (x, y) == widget.rect[:2] and (width, height) == (256, 104)


def test_a_widget_is_dropped_from_the_palette(shell, flow_file):  # noqa: F811
    from PySide6.QtCore import QMimeData
    from PySide6.QtGui import QDragMoveEvent, QDropEvent

    from openscilab.ui.documents.panel import ORIGIN, PANEL_WIDGET_MIME

    document = editor(shell, flow_file)
    page = page_of(document)
    data = QMimeData()
    data.setData(PANEL_WIDGET_MIME, b"led")
    point = QPointF(ORIGIN + 600 + 16, ORIGIN + 300 + 16)  # (the widget's corner a little above and left)
    # (Qt delivers drag events only while a drag runs: given to the page as the drag would give them)
    page.dragMoveEvent(QDragMoveEvent(point.toPoint(), Qt.CopyAction, data, Qt.LeftButton, Qt.NoModifier))
    # its top on the middle of the panel (600 high): it snaps there, the guide shows it
    assert page.ghost is not None and page.ghost.topLeft() == QPoint(ORIGIN + 600, ORIGIN + 300) and page.guides
    page.dropEvent(QDropEvent(point, Qt.CopyAction, data, Qt.LeftButton, Qt.NoModifier))
    QApplication.processEvents()
    added = document.panel.widgets[-1]
    assert added.kind == "led" and added.rect == (600, 300, 120, 80) and document.selection == [added.id]


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
    document.add_widget("number", "nowhere.out")
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
