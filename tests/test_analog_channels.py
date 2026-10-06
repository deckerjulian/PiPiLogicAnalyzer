"""Analog channels: model, files, the analyzer view, measurements, derived digital channels."""

from __future__ import annotations

import os
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from openscilab.core import capture_io, sigrok_session
from openscilab.driver.models import AnalogChannel, AnalyzerChannel, CaptureSession, TriggerType
from openscilab.driver.simulated import open_simulated


def analog_session(count: int = 2000, rate: int = 100_000) -> CaptureSession:
    t = np.arange(count) / rate
    session = CaptureSession(frequency=rate, pre_trigger_samples=100, post_trigger_samples=count - 100)
    session.capture_channels = [AnalyzerChannel(channel_number=0, channel_name="D0",
                                                samples=(np.sin(2 * np.pi * 1000 * t) > 0).astype(np.uint8))]
    session.analog_channels = [
        AnalogChannel.from_volts(1.5 + np.sin(2 * np.pi * 1000 * t), channel_number=0, channel_name="CH1"),
        AnalogChannel.from_volts(np.linspace(0, 3.3, count), channel_number=1, channel_name="Ramp"),
    ]
    return session


# ---------------------------------------------------------------------- model
def test_raw_samples_scale_to_volts():
    channel = AnalogChannel.from_volts([-1.0, 0.0, 2.5], channel_name="x", low=-5, high=5)
    assert channel.raw.dtype == np.int16
    assert channel.volts() == pytest.approx([-1.0, 0.0, 2.5], abs=2e-4)
    assert channel.display_name == "x" and AnalogChannel(channel_number=2).display_name == "Analog 3"
    assert channel.clone(with_samples=False).raw is None


def test_a_session_of_analog_channels_only_has_their_length():
    session = CaptureSession(frequency=1000)
    session.analog_channels = [AnalogChannel.from_volts(np.zeros(50))]
    assert session.sample_count() == 50
    session.analog_channels[0].rate = 500  # its own rate: 50 samples are 100 of the session
    assert session.sample_count() == 100
    clone = session.clone()
    assert clone.analog_channels[0] is not session.analog_channels[0] and len(clone.analog_channels[0].raw) == 50


# ---------------------------------------------------------------------- files
def test_lac_round_trip_and_old_files(tmp_path):
    session = analog_session()
    session.state_times = np.arange(2000, dtype=float) * 3.5
    path = tmp_path / "a.lac"
    capture_io.save_capture(str(path), session)
    loaded = capture_io.load_capture(str(path)).session
    assert [channel.channel_name for channel in loaded.analog_channels] == ["CH1", "Ramp"]
    assert np.array_equal(loaded.analog_channels[0].raw, session.analog_channels[0].raw)
    assert loaded.analog_channels[0].scale == session.analog_channels[0].scale
    assert loaded.state_times[-1] == pytest.approx(1999 * 3.5)
    # files of earlier versions (without the keys) load as before
    demo = capture_io.load_capture(os.path.join(os.path.dirname(__file__), "..", "examples", "demo.lac")).session
    assert demo.analog_channels == [] and demo.state_times is None and demo.sample_count() == 1380


def test_sigrok_session_with_analog_channels(tmp_path):
    session = analog_session()
    path = tmp_path / "a.sr"
    sigrok_session.save_session(str(path), session)
    import zipfile

    with zipfile.ZipFile(path) as archive:
        metadata = archive.read("metadata").decode()
        assert "total analog=2" in metadata and "analog2=CH1" in metadata and "analog3=Ramp" in metadata
        assert "analog-1-2-1" in archive.namelist()
    loaded = sigrok_session.load_session(str(path))
    assert [channel.channel_name for channel in loaded.analog_channels] == ["CH1", "Ramp"]
    assert loaded.analog_channels[1].volts() == pytest.approx(session.analog_channels[1].volts(), abs=1e-3)
    assert len(loaded.capture_channels) == 1


def test_csv_in_volts_and_vcd_real(tmp_path):
    session = analog_session(200)
    capture_io.export_csv(str(tmp_path / "a.csv"), session, include_time=True)
    lines = (tmp_path / "a.csv").read_text().splitlines()
    assert lines[0] == "Time,D0,CH1 [V],Ramp [V]"
    assert float(lines[1].split(",")[2]) == pytest.approx(1.5, abs=1e-3)
    capture_io.export_vcd(str(tmp_path / "a.vcd"), session)
    vcd = (tmp_path / "a.vcd").read_text()
    assert "$var real 64" in vcd and "\nr1.5" in vcd


# ----------------------------------------------------------------- simulator
def test_the_simulated_oscilloscope_captures_analog_channels():
    driver = open_simulated("dho924s", fast=True).simulated_driver
    assert driver.analog_channel_count == 4 and driver.analog_channel_names() == ["CH1", "CH2", "CH3", "CH4"]
    assert driver.memory_depth(16, 4) == 10_000_000 and driver.memory_depth(16, 0) == 31_250_000
    session = CaptureSession(frequency=1_000_000, pre_trigger_samples=0, post_trigger_samples=5000,
                             trigger_type=TriggerType.IMMEDIATE)
    session.analog_channels = [AnalogChannel(channel_number=2), AnalogChannel(channel_number=3)]
    done = threading.Event()
    assert driver.start_capture(session, lambda args: done.set()).name == "NONE"
    assert done.wait(10)
    triangle = session.analog_channels[0].volts()
    assert session.analog_channels[0].channel_name == "CH3"
    assert triangle.min() == pytest.approx(-1.0, abs=0.05) and triangle.max() == pytest.approx(2.0, abs=0.05)
    assert session.analog_channels[0].scale == pytest.approx(10 / 4096)  # 12 bit over ±5 V


# ------------------------------------------------------------------- analyzer
def test_the_analyzer_draws_and_measures_analog_tracks(make_dataview, monkeypatch):
    window = make_dataview()
    session = analog_session()
    window.load_session(session)
    QApplication.processEvents()
    viewer = window.analog_viewer
    assert viewer.isVisible() and viewer.height() == 2 * window.model.analog_height
    for visible in (40, 400, 2000):  # points, line, envelope
        window.model.set_view(0, visible)
        assert not viewer.grab().isNull()

    low, high = window.model.analog_range(session.analog_channels[0])
    assert low < 0.5 and high > 2.5
    assert viewer.value_at(session.analog_channels[1], 1000) == pytest.approx(3.3 * 1000 / 1999, abs=1e-3)

    window.model.set_cursor("A", 0)
    window.model.set_cursor("B", 1999)
    panel = window.measure_panel
    panel.range_combo.setCurrentIndex(0)  # between the cursors
    panel.refresh_statistics()
    row = [panel.analog_table.item(0, column).text() for column in range(8)]
    assert row[0] == "CH1" and row[1].startswith("1.5")
    assert row[3].startswith("500") and row[4].startswith("2.5")  # min 0.5 V, max 2.5 V
    assert row[7].startswith("2")  # peak to peak 2 V


def test_a_digital_channel_from_a_threshold(make_dataview):
    window = make_dataview()
    window.load_session(analog_session())
    derived = window.add_derived_channel(window.model.session.analog_channels[1], 1.65, 0.1)
    assert derived.channel_name.startswith("Ramp > 1.65")
    assert window.model.session.capture_channels[-1] is derived
    assert derived.samples[0] == 0 and derived.samples[-1] == 1
    assert window.model.transitions_for(derived) is not None
    assert window.dirty


def test_the_capture_dialog_offers_analog_inputs_and_their_depth(application):
    from openscilab.ui.dialogs.capture_dialog import CaptureDialog

    driver = open_simulated("dho924s", fast=True).capture.driver
    dialog = CaptureDialog(driver)
    try:
        assert [box.text() for box in dialog.analog_boxes] == ["CH1", "CH2", "CH3", "CH4"]
        dialog.immediate_radio.setChecked(True)
        assert dialog.limits.max_post_samples == 31_250_000
        for box in dialog.analog_boxes:
            box.setChecked(True)
        assert dialog.limits.max_post_samples == 10_000_000  # the depth follows the analog channels
        assert "4 analog" in dialog.summary_label.text()
        session = dialog.build_session()
        assert [channel.channel_name for channel in session.analog_channels] == ["CH1", "CH2", "CH3", "CH4"]
    finally:
        dialog.close()


def test_clock_inputs_are_the_pins_that_can_clock(application):
    from openscilab.ui.dialogs.capture_dialog import CaptureDialog

    driver = open_simulated("free", fast=True).capture.driver
    dialog = CaptureDialog(driver)
    try:
        numbers = [dialog.clock_channel_box.itemData(index) for index in range(dialog.clock_channel_box.count())]
        assert numbers == [8, 10, 13, 15]
        dialog.acquisition_box.setCurrentIndex(dialog.acquisition_box.findData("stream"))
        assert dialog.clock_box.isEnabled()  # STREAM_STATE
    finally:
        dialog.close()


@pytest.fixture
def application():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def streaming_scope(tmp_path, monkeypatch):
    """A profile with analog inputs that streams (in the settings folder, where profiles are found)."""
    import json

    from openscilab.core import settings

    folder = os.path.join(settings.settings_directory(), "sim")
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "streamscope.json"), "w") as handle:
        json.dump({"digital": ["D0"], "analog": ["A0"], "max_rate": 1_000_000, "memory_depth": 100_000,
                   "stream_bandwidth": 1_000_000, "adc_bits": 10, "analog_range": [0, 5],
                   "capabilities": ["IMMEDIATE_TRIGGER", "CONTINUOUS_STREAM"],
                   "circuit": {"sources": {"A0": {"type": "sine", "frequency": "50 Hz", "amplitude": "2 V",
                                                  "offset": "2.5 V"},
                                           "D0": {"type": "square", "frequency": "100 Hz"}}}}, handle)
    return "streamscope"


def test_analog_channels_live_and_the_roll_mode(make_dataview, streaming_scope):
    import time as clock

    instrument = open_simulated(streaming_scope)
    window = make_dataview()
    window.use_instrument(instrument)
    session = CaptureSession(frequency=10_000, pre_trigger_samples=0, post_trigger_samples=5000,
                             trigger_type=TriggerType.IMMEDIATE, acquisition_mode="stream")
    session.capture_channels = [AnalyzerChannel(channel_number=0, channel_name="D0")]
    session.analog_channels = [AnalogChannel(channel_number=0)]
    window._begin_capture(session)
    deadline = clock.monotonic() + 10
    while not window.model.is_live and clock.monotonic() < deadline:
        QApplication.processEvents()
        clock.sleep(0.01)
    assert window.model.is_live
    assert not window.follow_button.isHidden() and window.follow_button.isChecked()
    live = window.model.session
    assert live.analog_channels and live.analog_channels[0].raw is not None
    window.follow_button.setChecked(False)  # hold the view still
    held = window.model.first_sample
    clock.sleep(0.15)
    for _ in range(20):
        QApplication.processEvents()
    assert window.model.first_sample == held
    window.follow_button.setChecked(True)
    while window.driver.is_capturing and clock.monotonic() < deadline:
        QApplication.processEvents()
        clock.sleep(0.01)
    for _ in range(20):
        QApplication.processEvents()
    done = window.model.session
    assert done.analog_channels[0].volts().max() == pytest.approx(4.5, abs=0.05)
    assert len(done.analog_channels[0].raw) == 5000
