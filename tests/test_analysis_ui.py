"""The analysis tools of the main window: cursors, search, buses, listing, markers, charts,
comparison, state analysis, the toolbar capture settings and trigger sequences."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from typing import Optional

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from openscilab.core import capture_io, settings
from openscilab.driver.base import (
    ACQUISITION_BUFFER,
    ACQUISITION_STREAM,
    AnalyzerDriverBase,
    CaptureError,
)
from openscilab.driver.models import (
    AnalyzerChannel,
    BusDefinition,
    CaptureSession,
    ConditionKind,
    EdgeKind,
    TriggerCondition,
    TriggerSequence,
    TriggerStage,
    TriggerType,
)


@pytest.fixture(scope="module")
def application():
    return QApplication.instance() or QApplication([])


def make_session(samples: int = 2000) -> CaptureSession:
    session = CaptureSession(frequency=1_000_000, pre_trigger_samples=100, post_trigger_samples=samples - 100)
    t = np.arange(samples)
    session.capture_channels = [
        AnalyzerChannel(channel_number=index, channel_name=f"CH{index + 1}", samples=((t >> (index + 1)) & 1).astype(np.uint8))
        for index in range(4)
    ]
    return session


@pytest.fixture
def window(application, make_dataview):
    main = make_dataview()
    main.resize(1400, 900)
    main.load_session(make_session())
    yield main
    main.close()


class StreamingDriver(AnalyzerDriverBase):
    """A device with buffer and stream acquisition that records the sessions it is given."""

    def __init__(self) -> None:
        super().__init__()
        self.sessions: list[CaptureSession] = []

    @property
    def driver_id(self) -> str:
        return "fake-stream"

    @property
    def device_version(self) -> Optional[str]:
        return "Fake streaming analyzer"

    @property
    def max_frequency(self) -> int:
        return 100_000_000

    @property
    def channel_count(self) -> int:
        return 16

    @property
    def buffer_size(self) -> int:
        return 4 << 20

    @property
    def is_capturing(self) -> bool:
        return False

    def acquisition_modes(self) -> tuple[str, ...]:
        return (ACQUISITION_BUFFER, ACQUISITION_STREAM)

    def start_capture(self, session, completed_handler=None) -> CaptureError:
        self.sessions.append(session)
        return CaptureError.NONE

    def stop_capture(self) -> bool:
        return True


# ---------------------------------------------------------------- cursors
def test_cursors_measure_the_time_between_them(window):
    model = window.model
    model.set_cursor("A", 100)
    model.set_cursor("B", 350)
    panel = window.measure_panel
    assert panel.cursor_values["delta"].text() == "250 µs"
    assert panel.cursor_values["frequency"].text() == "4 kHz"
    assert panel.cursor_values["samples"].text() == "250"
    # Channel 1 toggles every 2 samples: 125 edges between the cursors
    assert panel.levels_table.item(0, 3).text() == "125"
    panel.zoom_to_cursors()
    assert model.first_sample <= 100 and model.first_sample + model.visible_samples >= 350


def test_statistics_of_the_whole_capture(window):
    panel = window.measure_panel
    panel.refresh_statistics()
    assert panel.statistics_table.item(0, 1).text() == "250 kHz"
    assert panel.statistics_table.item(0, 2).text() == "50.0 %"


def test_cursors_and_markers_reset_with_a_new_capture(window):
    window.model.set_cursor("A", 10)
    window.model.add_bookmark(20, "Start")
    window.load_session(make_session(500))
    assert window.model.cursor("A") is None and not window.model.bookmarks


# ----------------------------------------------------------------- search
def test_search_finds_edges_and_steps_through_them(window):
    panel = window.search_panel
    panel.kind_combo.setCurrentIndex(0)
    panel.edge_channel.setCurrentIndex(panel.edge_channel.findData(1))
    panel.edge_kind.setCurrentIndex(0)
    panel.search()
    hits = window.model.search_hits
    # Channel 2 has a period of 8 samples and rises first at sample 4
    assert len(hits) == 2000 // 8 and int(hits.starts[0]) == 4
    current = hits.current
    panel.step(1)
    assert window.model.search_hits.current == current + 1


def test_search_for_pulse_widths_and_bus_values(window):
    panel = window.search_panel
    panel.kind_combo.setCurrentIndex(2)
    panel.pulse_channel.setCurrentIndex(panel.pulse_channel.findData(2))
    panel.pulse_min.setText("8 µs")
    panel.search()
    assert len(window.model.search_hits) > 0

    window.model.set_buses([BusDefinition("LOW", [0, 1], symbols={3: "BOTH"})])
    panel.kind_combo.setCurrentIndex(4)
    panel.bus_value.setText("BOTH")
    panel.search()
    starts = window.model.search_hits.starts
    assert len(starts) and all(int(window.model.bus_values(window.model.buses[0])[s]) == 3 for s in starts[:10])


def test_an_invalid_search_reports_the_problem(window):
    panel = window.search_panel
    panel.kind_combo.setCurrentIndex(1)
    panel.pattern_edit.setText("12")
    panel.search()
    assert "0, 1 and X" in panel.status.text()


# ------------------------------------------------------- buses and listing
def test_buses_are_drawn_and_listed(window, application):
    window.model.set_buses([BusDefinition("NIBBLE", [0, 1, 2, 3])])
    assert window.bus_viewer.height() > 0
    window.listing_panel.rebuild()
    listing = window.listing_panel.table_model
    assert listing.columns[3] == "NIBBLE"
    assert listing.data(listing.index(1, 3)) == "0x1"


def test_the_bus_dialog_reads_a_symbol_table(application):
    from openscilab.ui.dialogs.bus_dialog import BusDialog

    dialog = BusDialog(make_session().capture_channels)
    for row in range(2):
        dialog.channel_list.item(row).setCheckState(dialog.channel_list.item(row).checkState().Checked)
    dialog.symbols_edit.setPlainText("IDLE = 0\n0x3 BUSY")
    dialog._accept()
    assert dialog.bus.channels == [0, 1] and dialog.bus.symbols == {0: "IDLE", 3: "BUSY"}


# ---------------------------------------------------------------- markers
def test_markers_are_listed_and_removed(window):
    model = window.model
    model.set_cursor("A", 300)
    window.markers_panel.add_marker()
    assert [bookmark.sample for bookmark in model.bookmarks] == [300]
    markers = window.markers_panel.tree.topLevelItem(0)
    markers.child(0).setSelected(True)
    window.markers_panel.remove_selected()
    assert not model.bookmarks


# ------------------------------------------------- charts, compare, state
def test_charts_show_values_and_histograms(window):
    from openscilab.ui.dialogs.chart_dialog import ChartDialog

    window.model.set_buses([BusDefinition("NIBBLE", [0, 1, 2, 3])])
    dialog = ChartDialog(window.model, window)
    dialog.tabs.setCurrentIndex(0)
    assert dialog.chart.line is not None
    dialog.tabs.setCurrentIndex(1)
    assert dialog.histogram.bars is not None
    assert "pulses" in dialog.summary.text()
    dialog.close()


def test_compare_marks_the_differences(window, tmp_path):
    from openscilab.ui.dialogs.compare_dialog import CompareDialog

    reference = make_session()
    reference.capture_channels[3].samples = reference.capture_channels[3].samples.copy()
    reference.capture_channels[3].samples[500:520] ^= 1
    path = tmp_path / "reference.lac"
    capture_io.save_capture(str(path), reference)

    dialog = CompareDialog(window.model, window)
    dialog.set_reference_file(str(path))
    dialog.auto_box.setChecked(False)
    dialog.tolerance_box.setValue(0)
    dialog.compare()
    assert dialog.result.differences[3] == [(500, 520)]
    assert int(window.model.search_hits.starts[0]) == 500
    dialog._mark_regions()
    assert len(window.model.regions) == 1


def test_state_analysis_resamples_on_a_clock(window, monkeypatch):
    from openscilab.ui.dialogs import state_dialog

    class Accepting(state_dialog.StateDialog):
        def exec(self):
            self.clock_combo.setCurrentIndex(0)
            self._accept()
            return self.result_capture is not None

    monkeypatch.setattr(state_dialog, "StateDialog", Accepting)
    timing = window.model.session
    window.state_analysis()
    session = window.model.session
    assert session.clock_channel is None
    # Every channel keeps its place, so the decoders still find theirs
    assert session.channel_numbers == timing.channel_numbers
    # The other channels change with the rising edges of channel 1 (period 4): one state per
    # falling edge, the suggested read point
    assert window.model.sample_count == 2000 // 4 - 1
    assert window.action_back_to_timing.isEnabled()

    window.back_to_timing()
    assert window.model.session is timing and not window.action_back_to_timing.isEnabled()


# --------------------------------------------------- toolbar capture settings
def test_the_toolbar_starts_a_capture_with_its_settings(window, monkeypatch):
    """The quick settings of the device card capture into the data view."""
    from openscilab.core.hub import Hub
    from openscilab.core.instrument import Instrument
    from openscilab.ui.documents.device import DeviceDocument

    driver = StreamingDriver()
    instrument = Instrument.from_driver(driver, name="fake")
    hub = Hub()
    hub.add(instrument)
    card = DeviceDocument(instrument, hub)
    window.use_instrument(instrument)
    bar = window.capture_controls.quick_capture  # the data view captures
    bar.rate_combo.setCurrentIndex(bar.rate_combo.findData(10_000_000))
    bar.samples_combo.setCurrentIndex(bar.samples_combo.findData(100_000))
    stored = capture_io.session_from_dict(settings.get_settings("capture-settings-fake-stream.json"))
    assert stored.frequency == 10_000_000
    assert stored.pre_trigger_samples + stored.post_trigger_samples == 100_000

    assert window.capture()
    started = driver.sessions[-1]
    assert started.frequency == 10_000_000 and len(started.capture_channels) == 16
    assert window._running_capture is started  # its result goes to the data view
    window.detach_source(window.source)
    card.shutdown()


# ------------------------------------------------------- trigger sequences
def test_the_capture_dialog_builds_a_software_sequence(application):
    from openscilab.ui.dialogs.capture_dialog import CaptureDialog

    dialog = CaptureDialog(StreamingDriver(), persist=False)
    assert not dialog.sequence_radio.isHidden() and not dialog.software_box.isHidden()
    dialog.sequence_radio.setChecked(True)
    assert dialog.software_box.isChecked()
    stage = dialog.sequence_editor.stages[0]
    stage.kind_combo.setCurrentIndex(stage.kind_combo.findData(ConditionKind.PULSE))
    stage.min_edit.setText("1 µs")
    dialog.sequence_editor.add_stage(
        TriggerStage(TriggerCondition(ConditionKind.EDGE, 2, EdgeKind.FALLING), count=3, within_ns=5000)
    )
    session = dialog.build_session()
    assert session is not None
    assert session.trigger_type == TriggerType.SEQUENCE and session.software_trigger
    assert session.acquisition_mode == ACQUISITION_STREAM
    stages = session.trigger_sequence.stages
    assert stages[0].condition.kind == ConditionKind.PULSE and stages[0].condition.min_ns == 1000
    assert stages[1].count == 3 and stages[1].within_ns == 5000

    reopened = CaptureDialog(StreamingDriver(), persist=False)
    reopened.apply_session(session)
    assert reopened.sequence_radio.isChecked() and len(reopened.sequence_editor.stages) == 2
    assert isinstance(reopened.build_session().trigger_sequence, TriggerSequence)


def test_a_device_without_streams_or_sequences_offers_none(application):
    from openscilab.driver.emulated import EmulatedAnalyzerDriver
    from openscilab.ui.dialogs.capture_dialog import CaptureDialog

    class Buffered(EmulatedAnalyzerDriver):
        @property
        def is_hardware(self) -> bool:
            return True

    dialog = CaptureDialog(Buffered(1), persist=False)
    assert dialog.sequence_radio.isHidden() and dialog.software_box.isHidden()


# ---------------------------------------------------------------- exports
def test_sigrok_sessions_are_exported_and_opened(window, tmp_path, monkeypatch):
    from openscilab.ui.documents import dataview as module

    path = str(tmp_path / "capture.sr")
    monkeypatch.setattr(module.QFileDialog, "getSaveFileName", lambda *args, **kwargs: (path, ""))
    window.export_capture("sr")
    original = [channel.samples.copy() for channel in window.model.session.capture_channels]
    window.open_capture_file(path)
    loaded = window.model.session
    assert loaded.frequency == 1_000_000
    assert all(np.array_equal(a, b.samples) for a, b in zip(original, loaded.capture_channels))
