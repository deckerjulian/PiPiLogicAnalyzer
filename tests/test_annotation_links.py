"""Annotations that belong together across rows, marked while one of them is hovered."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from dataclasses import dataclass

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from pipilogicanalyzer.sigrok.engine import Annotation, AnnotationSegment
from pipilogicanalyzer.sigrok.links import PART, WHOLE, linked_segments, read_parts


@dataclass
class Group:
    decoder_name: str
    annotations: list
    color_index: int = 0
    info: object = None
    instance: object = None
    error: object = None


def segment(first: int, last: int, text: str) -> AnnotationSegment:
    return AnnotationSegment(type_id=0, first_sample=first, last_sample=last, values=[text])


@pytest.fixture
def groups():
    instructions = Annotation("Disassembly", [segment(10, 40, "BNE $C003"), segment(40, 50, "INX")])
    cycles = Annotation(
        "Bus cycles", [segment(0, 10, "R0"), segment(10, 20, "R1"), segment(20, 30, "R2"),
                       segment(30, 40, "R3"), segment(40, 50, "R4")]
    )
    region = Annotation("Memory region", [segment(0, 10_000, "Free RAM")])
    other = Annotation("Data bus", [segment(10, 20, "D0"), segment(20, 30, "F5")])
    return [Group("C64 Bus", [cycles, region, instructions]), Group("6510 Bus", [other])]


def test_an_instruction_marks_its_bus_cycles(groups):
    instruction = groups[0].annotations[2].segments[0]
    links = linked_segments(groups, instruction)
    parts = {link.segment.values[0] for link in links if link.relation == PART}
    assert parts == {"R1", "R2", "R3", "D0", "F5"}
    # The memory region around everything is not marked
    assert not [link for link in links if link.relation == WHOLE]
    assert [part.values[0] for part in read_parts(links, groups[0])] == ["R1", "R2", "R3"]


def test_a_bus_cycle_marks_its_instruction(groups):
    cycle = groups[0].annotations[0].segments[2]
    links = linked_segments(groups, cycle)
    assert [link.segment.values[0] for link in links if link.relation == WHOLE] == ["BNE $C003"]
    assert [link.segment.values[0] for link in links if link.relation == PART] == ["F5"]
    assert read_parts(links, groups[0]) == []


def test_an_entry_described_by_another_row_is_still_a_value(groups):
    """A memory region as long as the bus cycle does not make the cycle an entry of parts."""
    groups[0].annotations[1].segments.insert(0, segment(20, 30, "I/O"))
    groups[0].annotations[1].segments.sort(key=lambda item: item.first_sample)
    cycle = groups[0].annotations[0].segments[2]
    assert read_parts(linked_segments(groups, cycle), groups[0]) == []


def test_the_waveform_shows_the_parts_of_a_hovered_instruction(groups):
    from pipilogicanalyzer.driver.models import AnalyzerChannel, CaptureSession
    from pipilogicanalyzer.ui.main_window import MainWindow
    from pipilogicanalyzer.ui.widgets.annotation_viewer import build_hover

    QApplication.instance() or QApplication([])
    session = CaptureSession(frequency=1_000_000, pre_trigger_samples=0, post_trigger_samples=100)
    session.capture_channels = [AnalyzerChannel(channel_number=0, samples=np.zeros(100, np.uint8))]
    window = MainWindow()
    try:
        window.resize(1200, 700)
        window.load_session(session)
        window.model.set_annotation_groups(groups)
        hover = build_hover(groups, window.model.channels, groups[0], groups[0].annotations[2].segments[0])
        assert len(hover.parts) == 3 and hover.composition is None
        assert id(groups[0].annotations[0].segments[1]) in hover.linked_ids
        window.model.set_hover(hover)
        window.sample_viewer.grab()
        window.annotation_viewer.grab()
        assert "R2" in window.annotation_viewer.tooltip_text(hover)
    finally:
        window.close()
