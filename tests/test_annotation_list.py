"""The list of an annotation row with the other rows of its decoder and the details of an entry."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from pipilogicanalyzer.core import capture_io
from pipilogicanalyzer.core.profiles import read_profiles_file
from pipilogicanalyzer.sigrok.engine import AnnotationSegment
from pipilogicanalyzer.sigrok.provider import SigrokProvider
from pipilogicanalyzer.ui.dialogs.annotation_list import COLUMNS, AnnotationListWindow, RelatedRow
from pipilogicanalyzer.ui.view_model import CaptureViewModel

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEMO = os.path.join(ROOT, "examples", "c64-demo.lac")
PROFILE = os.path.join(ROOT, "examples", "c64-expansion-port-profile.json")


@pytest.fixture(scope="module")
def application():
    return QApplication.instance() or QApplication([])


def segment(first: int, last: int, text: str) -> AnnotationSegment:
    return AnnotationSegment(type_id=0, first_sample=first, last_sample=last, values=[text])


def test_entries_of_another_row_during_an_entry():
    cycles = RelatedRow("Bus cycles", [segment(0, 10, "a"), segment(10, 20, "b"), segment(20, 30, "c")])
    assert [entry.values[0] for entry in cycles.during(segment(10, 30, "LDA"))] == ["b", "c"]
    assert [entry.values[0] for entry in cycles.during(segment(15, 15, "IRQ"))] == ["b"]
    region = RelatedRow("Memory region", [segment(0, 1000, "KERNAL")])
    assert region.during(segment(500, 510, "JSR"))[0].values[0] == "KERNAL"


@pytest.mark.skipif(not os.path.isfile(DEMO), reason="the demo capture is missing")
def test_the_disassembly_lists_the_bytes_read_and_the_details(application):
    from pipilogicanalyzer.sigrok.engine import DecoderRegistry

    registry = DecoderRegistry()
    registry.load()
    capture = capture_io.load_capture(DEMO)
    provider = SigrokProvider(registry)
    provider.load_configuration(read_profiles_file(PROFILE)[0].decoder_configuration)
    groups = provider.run(capture.session)
    (group,) = [item for item in groups if item.instance.decoder_id == "c64bus"]
    model = CaptureViewModel()
    model.set_session(capture.session)
    model.set_annotation_groups(groups)

    disassembly = next(row for row in group.annotations if row.name == "Disassembly")
    window = AnnotationListWindow(model, group, disassembly)
    try:
        columns = window.list_model.columns
        assert "Bus cycles" in columns and "Memory region" in columns
        cycles = columns.index("Bus cycles")
        instruction = next(
            row for row in range(window.list_model.rowCount())
            if len(window.list_model.related_entries(row, columns.index("Bus cycles") - len(COLUMNS))) >= 2
        )
        # The opcode and operand bytes read during the instruction
        assert all(len(byte) == 2 for byte in window.list_model.text(instruction, cycles).split())

        window.select_segment(window.list_model.segments[instruction])
        details = window.details.toPlainText()
        assert "Bus cycles:" in details and window.list_model.text(instruction, len(COLUMNS) - 1) in details
        assert "Channels read" in details

        # The filter also searches the other rows, the copy has their columns
        byte = window.list_model.text(instruction, cycles).split()[0]
        window.filter_edit.setText(f"= ${byte}")
        assert 0 < window.proxy.rowCount() < window.list_model.rowCount()
        assert "Bus cycles" in window.copy_entries().splitlines()[0]
        window._toggle_related("Bus cycles", False)
        assert window.table.isColumnHidden(cycles)
    finally:
        window.close()
