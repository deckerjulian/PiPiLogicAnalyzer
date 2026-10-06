"""Navigating the flow graph: two fingers move, pinch zooms, the wheel zooms at the pointer."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QKeyEvent, QNativeGestureEvent, QPointingDevice, QWheelEvent
from PySide6.QtWidgets import QApplication

from openscilab.ui.flow.canvas import MAX_ZOOM, MIN_ZOOM


@pytest.fixture
def view(shell):
    document = shell.new_flow()
    document.add_node("control.timer", (0, 0))
    document.add_node("data.table", (300, 0))
    document.add_node("data.table", (6000, 4000))  # far away: must still be reachable
    QApplication.processEvents()
    view = document.view
    view.actual_size()
    view.centerOn(0, 0)
    QApplication.processEvents()
    return view


def wheel(view, pixel=(0, 0), angle=(0, 0), phase=Qt.NoScrollPhase, modifiers=Qt.NoModifier, at=(200.0, 150.0)):
    position = QPointF(*at)
    return QWheelEvent(position, view.viewport().mapToGlobal(position), QPoint(*pixel), QPoint(*angle),
                       Qt.NoButton, modifiers, phase, False)


def gesture(kind, value=0.0, at=(200.0, 150.0)):
    position = QPointF(*at)
    return QNativeGestureEvent(kind, QPointingDevice.primaryPointingDevice(), 2, position, position, position,
                               value, QPointF(0, 0))


def scroll(view) -> tuple[int, int]:
    return view.horizontalScrollBar().value(), view.verticalScrollBar().value()


def test_two_fingers_move_the_canvas_without_zooming(view):
    before, zoom = scroll(view), view.zoom_level()
    view.wheelEvent(wheel(view, pixel=(30, -20), angle=(240, -160), phase=Qt.ScrollUpdate))
    assert view.zoom_level() == zoom
    assert scroll(view) == (before[0] - 30, before[1] + 20)  # the content follows the fingers
    view.wheelEvent(wheel(view, pixel=(-50, 0), angle=(-400, 0), phase=Qt.ScrollUpdate))  # sideways only
    assert view.zoom_level() == zoom and scroll(view)[0] == before[0] + 20


def test_pinch_zooms_at_the_fingers(view):
    anchor = view.mapToScene(QPoint(200, 150))
    assert view.viewportEvent(gesture(Qt.BeginNativeGesture))
    assert view.viewportEvent(gesture(Qt.ZoomNativeGesture, 0.5))
    assert view.zoom_level() == pytest.approx(1.5)
    after = view.mapToScene(QPoint(200, 150))
    assert abs(after.x() - anchor.x()) < 2 and abs(after.y() - anchor.y()) < 2
    view.viewportEvent(gesture(Qt.ZoomNativeGesture, -0.5))
    assert view.zoom_level() == pytest.approx(0.75)


def test_pinch_stops_at_the_limits_instead_of_skipping(view):
    for _ in range(20):
        view.viewportEvent(gesture(Qt.ZoomNativeGesture, 0.5))
    assert view.zoom_level() == pytest.approx(MAX_ZOOM)
    for _ in range(40):
        view.viewportEvent(gesture(Qt.ZoomNativeGesture, -0.5))
    assert view.zoom_level() == pytest.approx(MIN_ZOOM)


def test_smart_zoom_fits_and_goes_back(view):
    view.viewportEvent(gesture(Qt.SmartZoomNativeGesture))
    fitted = view.zoom_level()
    assert fitted < 1  # the far node is in view
    view.viewportEvent(gesture(Qt.SmartZoomNativeGesture))
    assert view.zoom_level() == pytest.approx(1.0)


def test_the_mouse_wheel_zooms_at_the_pointer_and_moves_with_modifiers(view):
    anchor = view.mapToScene(QPoint(200, 150))
    view.wheelEvent(wheel(view, angle=(0, 120)))
    assert view.zoom_level() == pytest.approx(1.15)
    after = view.mapToScene(QPoint(200, 150))
    assert abs(after.x() - anchor.x()) < 2 and abs(after.y() - anchor.y()) < 2
    zoom, before = view.zoom_level(), scroll(view)
    view.wheelEvent(wheel(view, angle=(0, -120), modifiers=Qt.ControlModifier))
    assert view.zoom_level() == zoom and scroll(view)[0] > before[0]
    view.wheelEvent(wheel(view, angle=(0, -120), modifiers=Qt.ShiftModifier))
    assert view.zoom_level() == zoom and scroll(view)[1] > before[1]


def test_cmd_and_two_fingers_zoom(view):
    view.wheelEvent(wheel(view, pixel=(0, 240), angle=(0, 1920), phase=Qt.ScrollUpdate, modifiers=Qt.ControlModifier))
    assert view.zoom_level() == pytest.approx(2.0)


def test_far_nodes_can_be_reached(view):
    node = view.flow_scene.nodes["table2"]
    view.centerOn(node)
    center = view.mapToScene(view.viewport().rect().center())
    assert abs(center.x() - node.sceneBoundingRect().center().x()) < 5


def test_keys_and_actions_zoom_and_the_view_survives_the_yaml_view(view, shell):
    document = shell.active_document()
    view.setFocus()
    view.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Plus, Qt.NoModifier, "+"))
    assert view.zoom_level() > 1 and view.popup is None or not view.popup.isVisible()
    document.action_zoom_out.trigger()
    document.action_zoom_out.trigger()
    zoom, position = view.zoom_level(), scroll(view)
    document.set_view("YAML")
    document.set_view("Graph")
    QApplication.processEvents()
    assert view.zoom_level() == zoom and scroll(view) == position
    assert {"Zoom in", "Zoom out", "Fit", "Actual size"} <= {action.text() for action in document.flow_menu.actions()}
    assert document.action_fit.shortcut().toString() == "Ctrl+0"
