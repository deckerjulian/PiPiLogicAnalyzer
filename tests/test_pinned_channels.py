"""Pinned channels stay above the scrolling waveform."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from pipilogicanalyzer.ui.main_window import MainWindow
from pipilogicanalyzer.ui.widgets.sample_viewer import MIN_CHANNEL_HEIGHT
from test_ui import make_session


@pytest.fixture
def window():
    QApplication.instance() or QApplication([])
    main = MainWindow()
    main.resize(1200, 800)
    main.load_session(make_session())
    yield main
    main.close()


def names(channels):
    return [channel.channel_name for channel in channels]


def test_a_pinned_channel_moves_to_the_fixed_area(window):
    model = window.model
    first, second = model.channels[:2]

    window.channel_viewer._toggle_pin(second)

    assert names(model.section_channels("pinned")) == ["CH2"]
    assert names(window.sample_viewer._channels()) == ["CH1", "CH3", "CH4"]
    assert names(window.pinned_sample_viewer._channels()) == ["CH2"]
    assert [row.channel for row in window.pinned_channel_viewer.visible_rows()] == [second]
    assert second not in [row.channel for row in window.channel_viewer.visible_rows()]
    assert not window.pinned_area.isHidden() and window.pinned_area.height() == MIN_CHANNEL_HEIGHT
    assert window.pinned_sample_viewer.grab() is not None

    window.pinned_channel_viewer._toggle_pin(first)
    assert names(model.section_channels("pinned")) == ["CH1", "CH2"]
    assert window.pinned_area.height() == 2 * MIN_CHANNEL_HEIGHT

    model.unpin_all()
    assert model.section_channels("pinned") == []
    assert window.pinned_area.isHidden()
    assert names(window.sample_viewer._channels()) == ["CH1", "CH2", "CH3", "CH4"]


def test_hidden_channels_are_not_shown_while_pinned(window):
    model = window.model
    channel = model.channels[0]
    model.set_pinned(channel, True)
    channel.hidden = True
    model.notify_channels_changed()
    assert model.section_channels("pinned") == []
    assert window.pinned_area.isHidden()


def test_a_new_capture_starts_without_pins(window):
    window.model.set_pinned(window.model.channels[0], True)
    window.load_session(make_session())
    assert window.model.section_channels("pinned") == []
