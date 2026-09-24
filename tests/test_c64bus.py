"""The c64bus decoder fits the C64 expansion port profile."""

from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

from pipilogicanalyzer.core.profiles import read_profiles_file
from pipilogicanalyzer.driver.models import AnalyzerChannel, CaptureSession
from pipilogicanalyzer.sigrok.composition import compose
from pipilogicanalyzer.sigrok.provider import DecoderInstance, SigrokProvider

PROJECT_DIRECTORY = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROFILE = os.path.join(PROJECT_DIRECTORY, "examples", "c64-expansion-port-profile.json")

pytestmark = pytest.mark.skipif(
    not os.path.isfile(os.path.join(PROJECT_DIRECTORY, "decoders", "c64bus", "pd.py")),
    reason="the c64bus decoder is not installed in ./decoders",
)

#: (address, data, R/W, /ROML) per cycle, 20 samples per cycle.
CYCLES = [(0xFFFC, 0xE2, 1, 1), (0x8009, 0x4C, 1, 0), (0xD020, 0x06, 0, 1)]
HALF = 10


@pytest.fixture(scope="module")
def registry():
    from pipilogicanalyzer.sigrok.engine import DecoderRegistry

    registry = DecoderRegistry()
    registry.load()
    return registry


def master_slave_profile():
    return read_profiles_file(PROFILE)[0]


def decode_cycles(registry, options=None, modify=None):
    """Decode ``CYCLES`` with the profile; returns (session, c64bus group, all groups)."""
    profile = master_slave_profile()
    channels = profile.capture_settings.capture_channels
    index = {channel.channel_name: position for position, channel in enumerate(channels)}

    # PHI2 is high in the second half and falls two samples before the bus changes (hold time).
    count = (len(CYCLES) + 1) * 2 * HALF
    samples = {position: np.ones(count, dtype=np.uint8) for position in range(len(channels))}
    samples[index["Φ2 (E)"]][:] = 0
    # The rising edge after the last cycle closes it.
    samples[index["Φ2 (E)"]][len(CYCLES) * 2 * HALF + HALF:] = 1
    for number, (address, data, rw, roml) in enumerate(CYCLES):
        start = number * 2 * HALF
        cycle = slice(start, start + 2 * HALF)
        samples[index["Φ2 (E)"]][start + HALF:start + 2 * HALF - 2] = 1
        samples[index["R/W (5)"]][cycle] = rw
        samples[index["/ROML (11)"]][cycle] = roml
        samples[index["/IRQ (4)"]][cycle] = 0 if number == 1 else 1
        for bit, pin in enumerate("YXWVUTSRPNMLKJHF"):
            samples[index[f"A{bit} ({pin})"]][cycle] = (address >> bit) & 1
        for bit, pin in enumerate((21, 20, 19, 18, 17, 16, 15, 14)):
            samples[index[f"D{bit} ({pin})"]][cycle] = (data >> bit) & 1
    if modify is not None:
        modify(samples, index)

    session = CaptureSession(frequency=profile.capture_settings.frequency, pre_trigger_samples=0,
                             post_trigger_samples=count)
    session.capture_channels = [
        AnalyzerChannel(channel_number=channel.channel_number, channel_name=channel.channel_name,
                        samples=samples[position])
        for position, channel in enumerate(channels)
    ]

    provider = SigrokProvider(registry)
    provider.load_configuration(profile.decoder_configuration)
    instance = next(item for item in provider.instances if item.decoder_id == "c64bus")
    instance.options.update(options or {})
    groups = provider.run(session)
    (group,) = [group for group in groups if group.instance.decoder_id == "c64bus"]
    assert group.error is None, group.error
    return session, group, groups


def segments_of(group, annotation_id):
    names = [annotation[0] for annotation in group.info.annotations]
    return [
        segment
        for row in group.annotations
        for segment in sorted(row.segments, key=lambda item: item.first_sample)
        if segment.type_id == names.index(annotation_id)
    ]


def test_channels_are_assigned_automatically_by_name(registry):
    from pipilogicanalyzer.ui.widgets.decoder_manager import DecoderManager

    profile = master_slave_profile()
    info = registry.get("c64bus")
    instance = DecoderInstance(decoder_id="c64bus")
    fake_manager = SimpleNamespace(model=SimpleNamespace(channels=profile.capture_settings.capture_channels))
    DecoderManager._auto_assign_channels(fake_manager, info, instance)

    # Every decoder channel finds its profile channel, none falls back to "first free".
    assert len(instance.channel_map) == len(info.channels)
    stored = next(item for item in profile.decoder_configuration if item["decoder_id"] == "c64bus")
    assert instance.channel_map == {int(key): value for key, value in stored["channel_map"].items()}


def test_capture_channel_0_is_assigned_by_id():
    from pipilogicanalyzer.driver.models import AnalyzerChannel
    from pipilogicanalyzer.ui.widgets.decoder_manager import DecoderManager

    # The id matches capture channel 0, the name matches nothing.
    info = SimpleNamespace(
        channels=[SimpleNamespace(id="cs", name="CS#", index=0)], required_channels=[]
    )
    channels = [AnalyzerChannel(channel_number=0, channel_name="CS")]
    instance = DecoderInstance(decoder_id="test")
    fake_manager = SimpleNamespace(model=SimpleNamespace(channels=channels))
    DecoderManager._auto_assign_channels(fake_manager, info, instance)

    assert instance.channel_map == {0: 0}


def test_bus_cycles_are_decoded_with_the_profile_configuration(registry):
    _session, group, _groups = decode_cycles(registry)

    def values(annotation_id):
        return [segment.values[0] for segment in segments_of(group, annotation_id)]

    assert values("read") == ["R $FFFC = $E2", "R $8009 = $4C"]
    assert values("write") == ["W $D020 = $06"]
    assert values("region") == ["Reset vector", "Cartridge ROM low (/ROML)", "VIC-II"]
    assert values("signal") == ["IRQ active"]
    assert values("unstable") == []
    # Three cycles are no program: the Disassembly row explains why it is empty.
    (hint,) = segments_of(group, "interrupt")
    assert hint.values[0].startswith("No disassembly: the program flow could not be followed")
    assert (hint.first_sample, hint.last_sample) == (10, 70)


def test_the_bus_is_read_at_the_last_sample_before_the_falling_edge(registry):
    # The cycle runs from the rising PHI2 edge (sample 10) to the next one (30); PHI2 is low
    # from sample 18 on, so the last sample with PHI2 high is 17.
    _session, group, _groups = decode_cycles(registry)
    first_read = segments_of(group, "read")[0]
    assert (first_read.first_sample, first_read.last_sample, first_read.sample_point) == (10, 30, 17)

    _session, group, _groups = decode_cycles(registry, options={"read": "at falling edge"})
    assert segments_of(group, "read")[0].sample_point == 18

    _session, group, _groups = decode_cycles(registry, options={"offset": -3})
    assert segments_of(group, "read")[0].sample_point == 14


def test_a_bus_changing_at_the_read_point_is_flagged(registry):
    def change_a0_at_the_edge(samples, index):
        samples[index["A0 (Y)"]][18:20] = 1

    _session, group, _groups = decode_cycles(registry, modify=change_a0_at_the_edge)

    reads = segments_of(group, "read")
    assert reads[0].values[:2] == ["R $FFFC = $E2 (unstable: A0)", "R $FFFC = $E2?"]
    assert reads[1].values[0] == "R $8009 = $4C"
    assert [segment.values[0] for segment in segments_of(group, "unstable")] == [
        "Bus changing at the read point: A0"
    ]


def test_composition_shows_how_address_and_data_are_built(registry):
    session, group, _groups = decode_cycles(registry)
    segment = segments_of(group, "read")[0]

    composition = compose(group.info, group.instance, session.capture_channels, segment)

    buses = {bus.name: bus for bus in composition.buses}
    assert set(buses) == {"A", "D"}  # /IO1 and /IO2 stay single lines
    assert buses["A"].value == 0xFFFC and buses["A"].hexadecimal == "$FFFC"
    assert buses["A"].describe() == "A15…A0 = 1111 1111 1111 1100 = $FFFC"
    assert buses["D"].describe() == "D7…D0 = 1110 0010 = $E2"

    labels = {bit.name: bit.label for bit in composition.bits}
    assert labels["A15"] == "A15 = 1 (+$8000)"
    assert labels["A0"] == "A0 = 0"
    assert labels["R/W"] == "R/W = 1"
    assert labels["Φ2"] == "Φ2 = 1 ~"  # the clock falls right after the read point
    assert "R/W = 1" in composition.describe()
    assert "Changing" not in composition.describe()


def test_hovering_an_annotation_marks_it_in_the_waveform(registry):
    from PySide6.QtCore import QPointF
    from PySide6.QtWidgets import QApplication

    from pipilogicanalyzer.ui.main_window import MainWindow
    from pipilogicanalyzer.ui.widgets.annotation_viewer import ANNOTATION_HEIGHT

    application = QApplication.instance() or QApplication([])  # noqa: F841
    session, group, groups = decode_cycles(registry)
    window = MainWindow()
    try:
        window.resize(1200, 800)
        window.load_session(session)
        window.model.set_view(0, len(CYCLES) * 2 * HALF + 2 * HALF)
        window.model.set_annotation_groups(groups)
        viewer = window.annotation_viewer
        viewer.resize(1000, viewer.height())

        segment = segments_of(group, "read")[0]
        row = next(index for index, entry in enumerate(viewer._rows()) if segment in entry[2].segments)
        position = QPointF(viewer._x_for(segment.first_sample + 3), row * ANNOTATION_HEIGHT + 5)

        hover = viewer.hover_at(position)
        assert hover is not None and hover.segment is segment
        assert "A15…A0 = 1111 1111 1111 1100 = $FFFC" in viewer.tooltip_text(hover)
        assert "Read at sample 17" in viewer.tooltip_text(hover)

        window.model.set_hover(hover)
        assert window.model.hover is hover
        window.sample_viewer.resize(1000, 2000)
        assert not window.sample_viewer.grab().isNull()
        assert not viewer.grab().isNull()

        assert viewer.hover_at(QPointF(10, 5)) is None  # the name column
        window.model.set_annotation_groups([])
        assert window.model.hover is None
    finally:
        window.close()


def test_an_annotation_row_opens_as_a_list(registry):
    from PySide6.QtCore import QEvent, QPoint, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from pipilogicanalyzer.ui.dialogs.annotation_list import TYPE_COLUMN, VALUE_COLUMN
    from pipilogicanalyzer.ui.main_window import MainWindow
    from pipilogicanalyzer.ui.view_model import AnnotationHover
    from pipilogicanalyzer.ui.widgets.annotation_viewer import ANNOTATION_HEIGHT

    application = QApplication.instance() or QApplication([])
    session, group, groups = decode_cycles(registry)
    window = MainWindow()
    try:
        window.resize(1200, 800)
        window.load_session(session)
        window.model.set_view(0, len(CYCLES) * 2 * HALF + 2 * HALF)
        window.model.set_annotation_groups(groups)
        viewer = window.annotation_viewer
        viewer.resize(1000, viewer.height())

        read = segments_of(group, "read")[0]
        write = segments_of(group, "write")[0]
        rows = viewer._rows()
        row = next(index for index, entry in enumerate(rows) if read in entry[2].segments)
        annotation = rows[row][2]

        # A click on the name column opens the list of the row
        QTest.mouseClick(viewer, Qt.LeftButton, Qt.NoModifier, QPoint(20, row * ANNOTATION_HEIGHT + 5))
        (lister,) = window._annotation_lists.values()
        assert lister.isVisible()
        assert lister.windowTitle() == f"{group.decoder_name}: {annotation.name}"
        model = lister.list_model
        assert model.rowCount() == len(annotation.segments)
        assert model.text(model.row_of(read), TYPE_COLUMN) == "Read cycle"
        assert "FFFC" in model.text(model.row_of(read), VALUE_COLUMN).upper()

        # Selecting an entry marks it in the waveform, hovering the waveform selects the entry
        lister.select_segment(read)
        assert window.model.hover.segment is read
        window.model.set_hover(AnnotationHover(group=group, segment=write))
        assert lister.current_segment() is write

        # Filter and copy
        lister.filter_edit.setText("write cycle")
        assert lister.proxy.rowCount() == 1
        copied = lister.copy_entries().splitlines()
        assert copied[0].split("\t")[0] == "Sample" and len(copied) == 2 and "D020" in copied[1].upper()
        assert application.clipboard().text() == "\n".join(copied)
        assert "D020" in lister.copy_entries(values_only=True).upper()
        lister.filter_edit.clear()

        # A double-click on an annotation reuses the window and selects the annotation
        x = int(viewer._x_for(read.first_sample + 3))
        QTest.mouseDClick(viewer, Qt.LeftButton, Qt.NoModifier, QPoint(x, row * ANNOTATION_HEIGHT + 5))
        assert list(window._annotation_lists.values()) == [lister]
        assert lister.current_segment() is read

        # Decoding again keeps the selection; without results the list is empty
        window.model.set_annotation_groups(groups)
        assert lister.current_segment() is read
        window.model.set_annotation_groups([])
        assert model.rowCount() == 0

        lister.close()
        QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        assert not window._annotation_lists
    finally:
        window.close()


def test_bus_cycles_keep_the_value_in_the_short_texts(registry):
    _session, group, _groups = decode_cycles(registry)
    assert segments_of(group, "read")[0].values == ["R $FFFC = $E2", "R $FFFC=$E2", "FFFC=E2", "E2", "R"]
    (write,) = segments_of(group, "write")
    assert write.values == ["W $D020 = $06", "W $D020=$06", "D020=06", "06", "W"]
