"""Features taken over from LogicAnalyzer 6.5 (protocol, driver, dialogs)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import struct

import pytest
from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox

from pipilogicanalyzer.core.formatting import to_inferred_frequency
from pipilogicanalyzer.core.profiles import ProfileStore
from pipilogicanalyzer.core.settings import settings_directory
from pipilogicanalyzer.driver import protocol
from pipilogicanalyzer.driver.analyzer import PiPiLogicAnalyzerDriver
from pipilogicanalyzer.driver.base import CaptureError, CaptureMode
from pipilogicanalyzer.driver.models import AnalyzerChannel
from pipilogicanalyzer.sigrok import engine
from pipilogicanalyzer.ui.dialogs.capture_dialog import CaptureDialog

from test_capture_dialog import FakeDriver, select
from test_driver import FakeTransport, make_session


# ------------------------------------------------------------------- protocol
def test_v6_5_capture_request_layout():
    layout = protocol.LAYOUT_V6_5
    assert layout.size == 56

    raw = protocol.CaptureRequest(
        channels=list(range(32)),
        channel_count=32,
        frequency=100_000_000,
        pre_samples=512,
        post_samples=1024,
        loop_count=1000,
        measure=0,
        capture_mode=2,
    ).pack(layout)

    assert len(raw) == 56
    assert raw[6:38] == bytes(range(32))
    assert raw[38] == 32
    assert struct.unpack_from("<III", raw, 40) == (100_000_000, 512, 1024)
    assert struct.unpack_from("<H", raw, 52)[0] == 1000
    assert raw[54:56] == bytes([0, 2])


@pytest.mark.parametrize(
    "major, minor, size", [(6, 0, 48), (6, 4, 48), (6, 5, 56), (7, 0, 56)]
)
def test_request_layout_follows_the_firmware_version(major, minor, size):
    assert protocol.layout_for_version(major, minor).size == size


# --------------------------------------------------------------------- driver
@pytest.fixture
def driver_v65(monkeypatch):
    transport = FakeTransport(channels=32)
    transport._responses[0] = "LOGIC_ANALYZER_V6_5"
    monkeypatch.setattr(
        "pipilogicanalyzer.driver.analyzer.SerialTransport", lambda *args, **kwargs: transport
    )
    instance = PiPiLogicAnalyzerDriver("/dev/fake")
    instance.test_transport = transport  # type: ignore[attr-defined]
    return instance


@pytest.fixture
def driver_v60(monkeypatch):
    transport = FakeTransport()
    monkeypatch.setattr(
        "pipilogicanalyzer.driver.analyzer.SerialTransport", lambda *args, **kwargs: transport
    )
    return PiPiLogicAnalyzerDriver("/dev/fake")


def test_v6_0_devices_keep_the_old_request(driver_v60):
    assert driver_v60.request_layout is protocol.LAYOUT_V6_0
    assert driver_v60.max_loop_count == 254


def test_v6_5_devices_receive_the_32_channel_request(driver_v65, monkeypatch):
    assert driver_v65.channel_count == 32
    assert driver_v65.max_loop_count == 65534

    session = make_session(channels=2, pre=2, post=6)
    session.capture_channels.append(AnalyzerChannel(channel_number=31))
    session.loop_count = 1000
    driver_v65.test_transport.queue_response("CAPTURE_STARTED")
    monkeypatch.setattr(driver_v65, "_read_capture", lambda *args: None)

    assert driver_v65.start_capture(session) is CaptureError.NONE

    request = driver_v65.compose_request(session, CaptureMode.CHANNELS_24)
    expected = protocol.command_packet(
        protocol.CMD_START_CAPTURE, request.pack(protocol.LAYOUT_V6_5)
    )
    assert bytes(driver_v65.test_transport.written).endswith(expected)


def test_more_than_254_bursts_need_v6_5_firmware(driver_v60, driver_v65):
    session = make_session(channels=1, pre=2, post=6)
    session.loop_count = 1000
    requested = session.total_samples
    assert not driver_v60.validate_settings(session, requested)
    assert driver_v65.validate_settings(session, requested)


def test_burst_measurement_limits(driver_v65):
    session = make_session(channels=1, pre=100, post=200)
    session.measure_bursts = True

    session.loop_count = 253
    assert driver_v65.validate_settings(session, session.total_samples)

    session.loop_count = 254
    assert not driver_v65.validate_settings(session, session.total_samples)

    session.loop_count = 2
    session.post_trigger_samples = 50
    assert not driver_v65.validate_settings(session, session.total_samples)

    session.measure_bursts = False
    assert driver_v65.validate_settings(session, session.total_samples)


def test_burst_gaps_use_the_blast_frequency_as_tick(driver_v65):
    session = make_session(channels=1, pre=0, post=100)
    session.frequency = 1_000_000
    session.loop_count = 1
    # 200MHz blast frequency -> 5ns ticks; timestamps count downwards.
    ticks_per_burst = 100 * 1_000_000_000 // session.frequency // 5
    gap_ticks = 20_000
    first = 0x00FFFFFF - 1000
    second = first - ticks_per_burst
    third = second - ticks_per_burst - gap_ticks
    import numpy as np

    bursts = driver_v65._compute_bursts(session, np.array([first, second, third], dtype=np.uint64))
    assert bursts[1].burst_time_gap == gap_ticks * 5


# ----------------------------------------------------------------- formatting
def test_inferred_frequency_of_a_half_period():
    assert to_inferred_frequency(0.0005) == "1 kHz"
    assert to_inferred_frequency(0) == "-"


def test_blank_channel_names_fall_back_to_the_number():
    assert AnalyzerChannel(channel_number=4, channel_name="   ").display_name == "Channel 5"


# ------------------------------------------------------------- decoder paths
def test_project_decoders_are_preferred_over_the_settings_directory(tmp_path, monkeypatch):
    project_decoders = tmp_path / "project" / "decoders"
    project_decoders.mkdir(parents=True)
    settings_decoders = os.path.join(settings_directory(), "decoders")
    os.makedirs(settings_decoders)
    monkeypatch.setattr(engine, "PROJECT_DIRECTORY", str(tmp_path / "project"))
    monkeypatch.chdir(tmp_path)

    paths = engine.decoder_search_paths()
    assert paths.index(str(project_decoders)) < paths.index(settings_decoders)


def test_repository_decoders_are_found_from_any_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    expected = os.path.join(engine.PROJECT_DIRECTORY, "decoders")
    if not os.path.isdir(expected):
        pytest.skip("no decoders folder in the project")
    assert expected in engine.decoder_search_paths()


# ------------------------------------------------------------- capture dialog
@pytest.fixture(scope="module")
def application():
    return QApplication.instance() or QApplication([])


class FakeDriverV65(FakeDriver):
    @property
    def max_loop_count(self) -> int:
        return 65534


@pytest.fixture
def quiet(monkeypatch):
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args, **kwargs: warnings.append(args[2]))
    return warnings


def test_burst_count_follows_the_device(application, quiet):
    assert CaptureDialog(FakeDriver()).burst_count_box.maximum() == 255
    assert CaptureDialog(FakeDriverV65()).burst_count_box.maximum() == 65535


def test_measurement_is_disabled_for_too_many_bursts(application, quiet):
    dialog = CaptureDialog(FakeDriverV65())
    dialog.burst_box.setChecked(True)
    dialog.burst_count_box.setValue(10)
    dialog.measure_box.setChecked(True)
    assert dialog.measure_box.isEnabled()

    dialog.burst_count_box.setValue(300)
    assert not dialog.measure_box.isEnabled()
    assert not dialog.measure_box.isChecked()

    dialog.burst_count_box.setValue(254)
    assert dialog.measure_box.isEnabled()


def test_measurement_needs_enough_post_trigger_samples(application, quiet):
    dialog = CaptureDialog(FakeDriver())
    select(dialog, [0])
    dialog.burst_box.setChecked(True)
    dialog.burst_count_box.setValue(3)
    dialog.measure_box.setChecked(True)
    dialog.pre_samples_box.setValue(10)
    dialog.post_samples_box.setValue(50)

    dialog._accept()
    assert dialog.selected_settings is None
    assert "Post-trigger samples too low" in dialog.validation_label.text()

    dialog.post_samples_box.setValue(200)
    dialog._accept()
    assert dialog.selected_settings.measure_bursts
    assert dialog.selected_settings.loop_count == 2


def test_profile_saved_in_the_dialog_can_be_applied(application, quiet, monkeypatch):
    store = ProfileStore()
    dialog = CaptureDialog(FakeDriver(), profiles=store, decoder_configuration=[{"decoder_id": "x"}])
    select(dialog, [1, 2])
    dialog.channel_selectors[1].channel_name = "SCL"
    dialog.frequency_box.setValue(5_000_000)
    monkeypatch.setattr(QInputDialog, "getText", lambda *args, **kwargs: ("Bus", True))

    dialog.save_as_profile()

    assert ProfileStore().get("Bus").decoder_configuration == [{"decoder_id": "x"}]

    select(dialog, [7])
    dialog.frequency_box.setValue(1_000_000)
    dialog._rebuild_profiles_menu()
    (action,) = [action for action in dialog.profiles_menu.actions() if action.text() == "Bus"]
    action.trigger()

    assert dialog.enabled_channels() == [1, 2]
    assert dialog.channel_selectors[1].channel_name == "SCL"
    assert dialog.frequency_box.value() == 5_000_000


def test_preview_visibility_is_restored(application):
    from pipilogicanalyzer.ui.main_window import MainWindow

    first = MainWindow()
    first.action_toggle_preview.setChecked(False)
    first.close()

    second = MainWindow()
    try:
        assert not second.action_toggle_preview.isChecked()
        assert second.previewer.isHidden()
    finally:
        second.close()
