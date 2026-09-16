"""The capture widgets must survive degenerate captures without raising."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from pipilogicanalyzer.driver.models import AnalyzerChannel, CaptureSession
from pipilogicanalyzer.sigrok.engine import Annotation, AnnotationSegment
from pipilogicanalyzer.sigrok.provider import AnnotationGroup, DecoderInstance
from pipilogicanalyzer.ui.main_window import MainWindow

ZOOM_LEVELS = (1, 2, 4, 7, 50, 1_000_000)


@pytest.fixture(scope="session")
def application():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(application):
    main = MainWindow()
    main.resize(1000, 700)
    yield main
    main.close()


def build_session(samples: int, channels: int, constant=None) -> CaptureSession:
    session = CaptureSession(
        frequency=1000,
        pre_trigger_samples=min(2, samples),
        post_trigger_samples=max(samples - 2, 0),
    )
    session.capture_channels = [
        AnalyzerChannel(
            channel_number=index,
            samples=np.full(samples, constant, dtype=np.uint8)
            if constant is not None
            else ((np.arange(samples) // max(2**index, 1)) % 2).astype(np.uint8),
        )
        for index in range(channels)
    ]
    return session


@pytest.mark.parametrize(
    "session_factory",
    [
        pytest.param(lambda: build_session(1, 2), id="single-sample"),
        pytest.param(lambda: build_session(2, 3), id="two-samples"),
        pytest.param(lambda: build_session(50, 2, constant=1), id="always-high"),
        pytest.param(lambda: build_session(50, 2, constant=0), id="always-low"),
        pytest.param(lambda: build_session(5000, 1), id="one-channel"),
        pytest.param(lambda: build_session(300, 24), id="all-channels"),
    ],
)
def test_widgets_paint_degenerate_captures(window, session_factory):
    session = session_factory()
    window.load_session(session)

    for visible in ZOOM_LEVELS:
        window.model.set_view(0, visible)
        window.sample_viewer.repaint()
        window.sample_marker.repaint()
        window.previewer.invalidate()
        window.previewer.repaint()


def test_widgets_paint_without_samples(window):
    session = CaptureSession(frequency=1000)
    session.capture_channels = [AnalyzerChannel(channel_number=0, samples=None)]
    window.load_session(session)

    window.sample_viewer.repaint()
    window.previewer.invalidate()
    window.previewer.repaint()


def test_widgets_paint_without_a_capture(window):
    window.model.set_session(None)
    window.sample_viewer.repaint()
    window.sample_marker.repaint()
    window.previewer.invalidate()
    window.previewer.repaint()
    window.annotation_viewer.repaint()


def test_hiding_every_channel_is_safe(window):
    session = build_session(200, 4)
    window.load_session(session)

    for channel in session.capture_channels:
        channel.hidden = True
    window.model.notify_channels_changed()
    window.sample_viewer.repaint()

    window.channel_viewer.show_all_channels()
    window.sample_viewer.repaint()
    assert all(not channel.hidden for channel in session.capture_channels)


def test_annotations_with_degenerate_segments(window):
    window.load_session(build_session(100, 2))
    annotation = Annotation(
        name="weird",
        segments=[
            AnnotationSegment(0, 10, 10, ["zero width"]),
            AnnotationSegment(1, 20, 5, ["reversed"]),
            AnnotationSegment(2, 0, 10_000_000, ["huge"]),
            AnnotationSegment(3, -5, 3, [""]),
        ],
    )
    group = AnnotationGroup(
        instance=DecoderInstance(decoder_id="x"),
        decoder_name="X",
        color_index=0,
        annotations=[annotation],
    )
    window.model.set_annotation_groups([group])

    for visible in (4, 50, 100):
        window.model.set_view(0, visible)
        window.annotation_viewer.repaint()
