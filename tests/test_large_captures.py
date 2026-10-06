"""A data view with a large capture stays quick: what does not change is not computed again,
and rows with far more entries than pixels are drawn per pixel, not per entry."""

from __future__ import annotations

import time

import numpy as np
from PySide6.QtCore import QRectF
from PySide6.QtWidgets import QApplication

from openscilab.core.analysis import ChannelTransitions
from openscilab.core.statistics import channel_statistics
from openscilab.driver.models import AnalyzerChannel, BusDefinition, CaptureSession
from openscilab.sigrok.engine import Annotation, AnnotationSegment
from openscilab.sigrok.links import linked_segments, segment_index
from openscilab.sigrok.provider import AnnotationGroup, DecoderInstance
from openscilab.ui.dialogs.annotation_list import RelatedRow
from openscilab.ui.documents import data_edits
from openscilab.ui.documents.waveform import pattern_points
from openscilab.ui.widgets import annotation_viewer, bus_viewer


def session(samples: int = 200_000, channels: int = 8) -> CaptureSession:
    capture = CaptureSession(frequency=1_000_000, pre_trigger_samples=0, post_trigger_samples=samples)
    base = np.arange(samples, dtype=np.uint32)
    capture.capture_channels = [
        AnalyzerChannel(channel_number=number, channel_name=f"CH{number + 1}",
                        samples=((base >> (number + 1)) & 1).astype(np.uint8))
        for number in range(channels)]
    return capture


def loaded(make_dataview, capture=None):
    view = make_dataview()
    view.load_session(capture or session())
    QApplication.processEvents()
    return view


# ------------------------------------------------------------- the edge index
def test_only_changed_channels_are_indexed_again(make_dataview, monkeypatch):
    view = loaded(make_dataview)
    model = view.model
    before = list(model.transitions)
    built = []
    create = ChannelTransitions.__init__
    monkeypatch.setattr(ChannelTransitions, "__init__",
                        lambda self, *args, **kwargs: (built.append(1), create(self, *args, **kwargs))[1])

    channel = model.channels[2]
    assert model.move_channel(channel, 5)  # another order: the same indexes, moved
    assert built == [] and model.transitions_for(channel) is before[2]
    assert [model.transitions_for(c) for c in model.channels] == [before[i] for i in (0, 1, 3, 4, 5, 2, 6, 7)]

    channel.channel_name = "renamed"
    model.notify_channels_changed()
    view.undo.undo()  # the name
    view.undo.undo()  # the order
    assert built == []

    replaced = model.channels[0]
    replaced.samples = np.zeros(model.sample_count, np.uint8)  # an edit of one channel
    model.rebuild_transitions()
    assert len(built) == 1 and model.transitions_for(replaced).edge_count == 0
    assert model.transitions_for(model.channels[1]) is before[1]

    view.load_session(session())  # another capture: all of it
    assert len(built) == 1 + 8


def test_statistics_from_the_index_are_the_same():
    rng = np.random.default_rng(5)
    for _ in range(40):
        count = int(rng.integers(2, 3000))
        samples = (np.cumsum(rng.random(count) < 0.1) % 2).astype(np.uint8)
        index = ChannelTransitions(samples, 1000)
        start = int(rng.integers(0, count))
        end = int(rng.integers(start, count + 1))
        assert channel_statistics(samples, 1000, start, end, transitions=index) == \
            channel_statistics(samples, 1000, start, end)


def test_hidden_statistics_wait_until_they_are_looked_at(make_dataview, monkeypatch):
    from openscilab.ui.panels import measure_panel

    view = loaded(make_dataview)
    panel = view.measure_panel
    assert isinstance(panel, measure_panel.MeasurePanel)
    view.panels_dock.setVisible(True)
    tabs = view.side_tabs
    measure = next(index for index in range(tabs.count()) if tabs.widget(index).isAncestorOf(panel))
    tabs.setCurrentIndex((measure + 1) % tabs.count())  # behind another tab
    QApplication.processEvents()
    assert not panel.isVisible()
    panel._timer.stop()
    panel._stale = False
    view.model.notify_capture_changed()
    assert not panel._timer.isActive() and panel._stale  # nothing is measured for a hidden panel
    view.show_panel(panel)  # its tab comes to the front
    QApplication.processEvents()
    assert not panel._stale  # measured now


# ---------------------------------------------------------------------- buses
def test_bus_values_follow_the_arrays_of_their_channels(make_dataview):
    capture = session()
    capture.buses = [BusDefinition(name="Low", channels=[0, 1, 2, 3]), BusDefinition(name="High", channels=[4, 5])]
    view = loaded(make_dataview, capture)
    model = view.model
    low, high = model.buses
    low_values, high_values = model.bus_values(low), model.bus_values(high)
    model.notify_capture_changed()  # nothing of the samples changed
    assert model.bus_values(low) is low_values
    model.channels[0].samples = np.ones(model.sample_count, np.uint8)  # a channel of "Low" edited
    model.notify_capture_changed()
    assert model.bus_values(high) is high_values
    changed = model.bus_values(low)
    assert changed is not low_values and int(changed[0]) & 1 == 1
    starts, values = model.bus_run_index(low)
    assert starts[0] == 0 and np.array_equal(values, changed[starts])


def test_dense_bus_rows_look_the_same(make_dataview, monkeypatch):
    capture = session(samples=400_000, channels=8)
    capture.buses = [BusDefinition(name="Bus", channels=list(range(8)))]
    view = loaded(make_dataview, capture)
    view.resize(1200, 700)
    for first, visible in ((0, 400_000), (1000, 150_000), (399_000, 5000), (10, 300)):
        view.model.set_view(first, visible)
        QApplication.processEvents()
        monkeypatch.setattr(bus_viewer, "DENSE_RUNS", 0)  # looked up per pixel column
        dense = view.bus_viewer.grab().toImage()
        monkeypatch.setattr(bus_viewer, "DENSE_RUNS", 10**9)  # every run looked at
        plain = view.bus_viewer.grab().toImage()
        assert dense == plain, (first, visible)


# ---------------------------------------------------------------- annotations
def annotation_row(count: int, length: int = 8, gap: int = 2, long_first: bool = False) -> Annotation:
    segments = [AnnotationSegment(type_id=index % 3, first_sample=index * (length + gap),
                                  last_sample=index * (length + gap) + length, values=[f"{index:02X}"])
                for index in range(count)]
    if long_first:
        segments.insert(0, AnnotationSegment(type_id=1, first_sample=0, last_sample=count * (length + gap),
                                             values=["frame"]))
    return Annotation(name="data", segments=segments) if "segments" in Annotation.__dataclass_fields__ \
        else Annotation("data", segments)


def group_of(*rows) -> AnnotationGroup:
    return AnnotationGroup(instance=DecoderInstance("test"), decoder_name="Test", color_index=0, annotations=list(rows))


def test_dense_annotation_rows_are_drawn_as_blocks(make_dataview, monkeypatch):
    view = loaded(make_dataview)
    view.resize(1200, 700)
    row = annotation_row(20_000)  # 200 000 samples of entries
    view.model.set_annotation_groups([group_of(row)])
    view.model.set_view(0, 200_000)
    QApplication.processEvents()
    drawn = []
    draw = annotation_viewer.AnnotationViewer._draw_segment
    monkeypatch.setattr(annotation_viewer.AnnotationViewer, "_draw_segment",
                        lambda self, *args, **kwargs: (drawn.append(1), draw(self, *args, **kwargs))[1])
    image = view.annotation_viewer.grab().toImage()
    assert len(drawn) < 100  # not 20 000 shapes
    # the row is not empty: its blocks are painted
    background = image.pixel(annotation_viewer.NAME_COLUMN_WIDTH + 400, 2)
    assert any(image.pixel(annotation_viewer.NAME_COLUMN_WIDTH + x, annotation_viewer.ANNOTATION_HEIGHT // 2)
               != background for x in range(10, 900, 7))

    drawn.clear()
    view.model.set_view(1000, 400)  # zoomed in: every entry with its shape and text
    QApplication.processEvents()
    view.annotation_viewer.grab()
    assert 30 <= len(drawn) <= 100  # (about 40 entries in view, painted once or twice)


def test_a_long_entry_that_began_before_the_view_is_drawn(make_dataview, monkeypatch):
    view = loaded(make_dataview)
    view.resize(1200, 700)
    row = annotation_row(2000, long_first=True)
    view.model.set_annotation_groups([group_of(row)])
    view.model.set_view(15_000, 300)  # far inside the long entry, many short ones after its start
    QApplication.processEvents()
    drawn = []
    draw = annotation_viewer.AnnotationViewer._draw_segment
    monkeypatch.setattr(annotation_viewer.AnnotationViewer, "_draw_segment",
                        lambda self, painter, metrics, segment, *args, **kwargs: (
                            drawn.append(segment.values[0]), draw(self, painter, metrics, segment, *args, **kwargs))[1])
    view.annotation_viewer.grab()
    assert "frame" in drawn


def test_the_segment_index_and_the_linked_entries():
    row = annotation_row(5000)
    starts, ends, reach, types = segment_index(row)
    assert len(starts) == 5000 and np.all(np.diff(reach) >= 0) and types[4] == 1
    assert segment_index(row)[0] is starts  # built once

    frame = AnnotationSegment(type_id=0, first_sample=0, last_sample=50_000, values=["frame"])
    frames = Annotation("frames", [frame])
    group = group_of(frames, row)
    started = time.monotonic()
    for _ in range(200):
        links = linked_segments([group], frame)  # 5000 entries inside it: too many to list as parts
    assert time.monotonic() - started < 1.0
    assert [link for link in links if link.relation == "part"] == []
    short = row.segments[10]  # samples 100 to 108
    around = AnnotationSegment(type_id=0, first_sample=95, last_sample=125, values=["word"])
    words = Annotation("words", [around])
    links = linked_segments([group_of(words, row)], short)
    assert [link.relation for link in links if link.segment is around] == ["whole"]  # the entry around it
    links = linked_segments([group_of(words, row)], around)
    assert [link.segment.values[0] for link in links if link.relation == "part"] == ["0A", "0B"]


def test_entries_during_another_are_found_without_scanning_the_row():
    segments = annotation_row(20_000, long_first=True).segments
    related = RelatedRow("data", segments)
    probe = segments[15_000]
    found = related.during(probe)
    assert probe in found and segments[0] in found and len(found) <= 4
    started = time.monotonic()
    for segment in segments[1::10]:
        related.during(segment)
    assert time.monotonic() - started < 2.0


# -------------------------------------------------------------- other things
def test_undo_steps_follow_the_size_of_the_capture():
    assert data_edits.undo_limit(1000, 8) == data_edits.UNDO_STEPS
    assert data_edits.undo_limit(20_000_000, 24) == 4
    assert data_edits.undo_limit(100_000_000, 48) == 1


def test_a_long_pattern_is_drawn_from_its_runs():
    area = QRectF(0, 0, 500, 100)
    levels = (np.arange(2_000_000) // 100_000 % 2).astype(np.uint8)  # 20 runs
    points = pattern_points(levels, len(levels), area, 10, 90)
    assert len(points) == 40 and {point.y() for point in points} == {10.0, 90.0}
    busy = (np.arange(2_000_000) % 2).astype(np.uint8)  # a million runs: a bar per pixel column
    points = pattern_points(busy, len(busy), area, 10, 90)
    assert len(points) <= 2 * 501
    assert pattern_points(np.zeros(0, np.uint8), 0, area, 10, 90) == []


def test_the_real_time_view_of_states_keeps_regions_and_saves_the_states(make_dataview, tmp_path):
    from openscilab.core import capture_io
    from openscilab.core.regions import SampleRegion

    capture = session(samples=200, channels=2)
    capture.state_times = np.cumsum(np.full(200, 10.0))
    view = loaded(make_dataview, capture)
    view.model.add_region(SampleRegion(first_sample=10, last_sample=20, region_name="states 10-20"))
    view.model.add_bookmark(50, "state 50")
    view.state_time_button.setChecked(True)  # (switches the view)
    assert view.model.session is not capture and view.model.regions == []
    path = str(tmp_path / "states.lac")
    view._write_capture(path)  # while the times are shown: the file holds the states
    saved = capture_io.load_capture(path)
    assert saved.session.sample_count() == 200 and [region.region_name for region in saved.regions] == ["states 10-20"]
    assert saved.bookmarks == [(50, "state 50")]
    view.state_time_button.setChecked(False)
    assert view.model.session is capture
    assert [region.region_name for region in view.model.regions] == ["states 10-20"]
    assert [(bookmark.sample, bookmark.name) for bookmark in view.model.bookmarks] == [(50, "state 50")]
    assert not view.undo.canUndo()


def test_a_search_runs_in_the_background(make_dataview, monkeypatch):
    from openscilab.ui import background
    from openscilab.ui.panels.search_panel import SearchPanel

    view = loaded(make_dataview)
    panel = next(child for child in view.findChildren(SearchPanel))
    calls = []
    run = background.run
    monkeypatch.setattr(background, "run", lambda *args, **kwargs: (calls.append(args[1]), run(*args, **kwargs))[1])
    panel.search()
    assert calls == ["Searching..."] and len(view.model.search_hits) > 0
