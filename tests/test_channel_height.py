"""Channels can be made lower (or taller), so that more of them fit on the screen."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication

from pipilogicanalyzer.ui.view_model import (
    DEFAULT_CHANNEL_HEIGHT,
    LARGEST_CHANNEL_HEIGHT,
    SMALLEST_CHANNEL_HEIGHT,
    CaptureViewModel,
)

from test_widget_robustness import build_session


@pytest.fixture(scope="module")
def application():
    return QApplication.instance() or QApplication([])


def test_the_model_limits_the_channel_height():
    model = CaptureViewModel()
    heights = []
    model.channel_height_changed.connect(lambda: heights.append(model.channel_height))

    assert model.channel_height == DEFAULT_CHANNEL_HEIGHT
    model.zoom_channels(0.5)
    model.set_channel_height(1)
    model.zoom_channels(0.5)  # already the smallest height: no change
    model.set_channel_height(10_000)
    assert heights == [DEFAULT_CHANNEL_HEIGHT // 2, SMALLEST_CHANNEL_HEIGHT, LARGEST_CHANNEL_HEIGHT]


def alt_wheel(widget, notches: int) -> None:
    position = QPointF(40, 10)
    event = QWheelEvent(
        position, widget.mapToGlobal(position), QPoint(), QPoint(0, 120 * notches),
        Qt.NoButton, Qt.AltModifier, Qt.NoScrollPhase, False,
    )
    QApplication.sendEvent(widget, event)


def test_lower_channels_fit_more_channels_on_the_screen(application):
    from pipilogicanalyzer.ui.main_window import MainWindow

    window = MainWindow()
    try:
        window.model.set_channel_height(DEFAULT_CHANNEL_HEIGHT)
        window.resize(1200, 800)
        window.load_session(build_session(1000, 24))
        window.show()
        application.processEvents()
        waveform, names = window.sample_viewer, window.channel_viewer
        assert waveform.minimumHeight() == names.minimumHeight() == 24 * DEFAULT_CHANNEL_HEIGHT

        window.model.set_channel_height(20)
        application.processEvents()
        assert waveform.minimumHeight() == names.minimumHeight() == 24 * 20
        assert window.channel_height_slider.value() == 20
        assert window.channel_height_label.text() == "20 px"

        # Low rows show the name in the label and stay aligned with the waveform
        rows = names.visible_rows()
        assert rows[0].name_edit.isHidden()
        assert all(abs(row.y() - index * waveform.channel_height()) <= 1 for index, row in enumerate(rows))
        assert abs(rows[0].height() - waveform.channel_height()) <= 1

        window.model.set_pinned(window.model.channels[0], True)
        assert window.pinned_area.height() == 20
        window.model.unpin_all()

        # Alt + wheel over the waveform or the channel names
        alt_wheel(waveform, 1)
        assert window.model.channel_height == 25
        alt_wheel(names, -1)
        assert window.model.channel_height == 20

        window.channel_height_slider.setValue(60)
        application.processEvents()
        assert window.model.channel_height == 60 and not rows[0].name_edit.isHidden()
    finally:
        window.model.set_channel_height(DEFAULT_CHANNEL_HEIGHT)
        window.close()
