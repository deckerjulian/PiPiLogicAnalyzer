"""Scrolling and zooming with the mouse wheel, the trackpad and the keyboard."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QAction, QKeySequence, QWheelEvent
from PySide6.QtWidgets import QApplication

from pipilogicanalyzer.driver.models import AnalyzerChannel, CaptureSession
from pipilogicanalyzer.ui.main_window import MainWindow

SAMPLES = 20_000


def many_channels(count: int = 20) -> CaptureSession:
    session = CaptureSession(frequency=1_000_000, pre_trigger_samples=100, post_trigger_samples=SAMPLES - 100)
    session.capture_channels = [
        AnalyzerChannel(
            channel_number=index,
            channel_name=f"CH{index + 1}",
            samples=((np.arange(SAMPLES) // (index + 2)) % 2).astype(np.uint8),
        )
        for index in range(count)
    ]
    return session


@pytest.fixture
def window():
    application = QApplication.instance() or QApplication([])
    main = MainWindow()
    main.resize(1200, 600)
    main.show()
    main.load_session(many_channels())
    main.model.set_view(5_000, 1_000)
    application.processEvents()
    yield main
    main.close()


def wheel(widget, pixel=(0, 0), angle=(0, 0), phase=Qt.NoScrollPhase, modifiers=Qt.NoModifier, x=400.0):
    position = QPointF(x, 20.0)
    return QWheelEvent(
        position, widget.mapToGlobal(position), QPoint(*pixel), QPoint(*angle),
        Qt.NoButton, modifiers, phase, False,
    )


def test_the_mouse_wheel_zooms_at_the_pointer(window):
    viewer = window.sample_viewer
    anchor_before = viewer.navigator.sample_at(400)

    viewer.wheelEvent(wheel(viewer, angle=(0, 120)))

    assert window.model.visible_samples < 1_000
    assert abs(viewer.navigator.sample_at(400) - anchor_before) <= 2

    viewer.wheelEvent(wheel(viewer, angle=(0, -120)))
    viewer.wheelEvent(wheel(viewer, angle=(0, -120)))
    assert window.model.visible_samples > 1_000


def test_the_trackpad_scrolls_horizontally(window):
    viewer = window.sample_viewer
    per_pixel = viewer.navigator.samples_per_pixel()

    viewer.wheelEvent(wheel(viewer, pixel=(-120, 0), phase=Qt.ScrollUpdate))  # swipe to the left

    assert window.model.visible_samples == 1_000  # no zoom
    assert abs(window.model.first_sample - (5_000 + 120 * per_pixel)) <= 1

    for _ in range(10):  # small movements add up, even zoomed in far
        viewer.wheelEvent(wheel(viewer, pixel=(1, 0), phase=Qt.ScrollUpdate))
    assert window.model.first_sample < 5_000 + 120 * per_pixel


def test_the_trackpad_scrolls_the_channels_vertically(window):
    bar = window.scroll_area.verticalScrollBar()
    assert bar.maximum() > 0

    window.sample_viewer.wheelEvent(wheel(window.sample_viewer, pixel=(0, -80), phase=Qt.ScrollUpdate))

    assert bar.value() == 80
    assert window.model.first_sample == 5_000 and window.model.visible_samples == 1_000


def test_horizontal_wheels_and_ctrl_scroll(window):
    viewer = window.sample_viewer

    viewer.wheelEvent(wheel(viewer, angle=(-120, 0)))  # tilt wheel right / Shift + wheel on macOS
    assert window.model.first_sample == 5_100

    # Ctrl + wheel up moves forward in time, as before.
    viewer.wheelEvent(wheel(viewer, angle=(0, 120), modifiers=Qt.ControlModifier))
    assert window.model.first_sample == 5_200


def test_the_ruler_and_the_annotations_navigate_the_same_way(window):
    window.sample_marker.wheelEvent(wheel(window.sample_marker, angle=(0, 120)))
    assert window.model.visible_samples < 1_000

    annotations = window.annotation_viewer
    annotations.resize(1000, 30)
    first = window.model.first_sample
    annotations.wheelEvent(wheel(annotations, pixel=(-50, 0), phase=Qt.ScrollUpdate))
    assert window.model.first_sample > first


def action_for(window, key: str) -> QAction:
    sequence = QKeySequence(key)
    return next(action for action in window.findChildren(QAction) if sequence in action.shortcuts())


def test_arrow_and_plus_minus_keys(window):
    action_for(window, "Right").trigger()
    assert window.model.first_sample == 5_100
    action_for(window, "Left").trigger()
    assert window.model.first_sample == 5_000
    assert action_for(window, "Ctrl+Left") is action_for(window, "Left")

    action_for(window, "+").trigger()
    assert window.model.visible_samples == 500
    action_for(window, "-").trigger()
    assert window.model.visible_samples == 1_000
    assert action_for(window, "=") is action_for(window, "+")
