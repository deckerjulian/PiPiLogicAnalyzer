"""The DSLogic in the capture dialog and in the main window."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from pipilogicanalyzer.driver.base import ACQUISITION_STREAM, AnalyzerDriverType
from pipilogicanalyzer.driver.dslogic import BitstreamMissingError, usb
from pipilogicanalyzer.driver.models import TriggerType
from pipilogicanalyzer.ui.dialogs.capture_dialog import CaptureDialog

from test_dslogic_driver import FakeDSLogic, info, open_driver


@pytest.fixture(scope="session")
def application():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def pro32(application):
    return open_driver(FakeDSLogic(info(0x002C, super_speed=True)))


def select(dialog, channels):
    for selector in dialog.channel_selectors:
        selector.enabled = selector.channel_number in channels


def test_dialog_offers_the_rates_of_the_selected_channels(pro32):
    dialog = CaptureDialog(pro32)
    try:
        assert not dialog.rate_box.isHidden() and dialog.frequency_box.isHidden()
        assert len(dialog.channel_selectors) == 32
        select(dialog, range(8))
        rates = [dialog.rate_box.itemData(i) for i in range(dialog.rate_box.count())]
        assert rates[-1] == 1_000_000_000
        select(dialog, range(32))
        rates = [dialog.rate_box.itemData(i) for i in range(dialog.rate_box.count())]
        assert rates[-1] == 250_000_000
        assert dialog.frequency_box.value() <= 250_000_000

        dialog.acquisition_box.setCurrentIndex(dialog.acquisition_box.findData(ACQUISITION_STREAM))
        rates = [dialog.rate_box.itemData(i) for i in range(dialog.rate_box.count())]
        assert rates[-1] == 50_000_000
    finally:
        dialog.close()


def test_dialog_trigger_options(pro32):
    dialog = CaptureDialog(pro32)
    try:
        box = dialog.trigger_channel_box
        values = [box.itemData(i) for i in range(box.count())]
        assert values == list(range(32))  # every channel, no external trigger input
        assert dialog.blast_box.isHidden() and dialog.burst_box.isHidden()
        assert not dialog.immediate_radio.isHidden()
        assert not dialog.threshold_box.isHidden()
    finally:
        dialog.close()


def test_immediate_stream_session(pro32):
    dialog = CaptureDialog(pro32)
    try:
        select(dialog, range(16))
        dialog.acquisition_box.setCurrentIndex(dialog.acquisition_box.findData(ACQUISITION_STREAM))
        dialog.rate_box.setCurrentIndex(dialog.rate_box.findData(10_000_000))
        dialog.immediate_radio.setChecked(True)
        dialog.threshold_box.setValue(1.8)
        dialog.post_samples_box.setValue(100_000)
        dialog._accept()
        session = dialog.selected_settings
        assert session is not None
        assert session.trigger_type == TriggerType.IMMEDIATE
        assert session.acquisition_mode == ACQUISITION_STREAM
        assert session.frequency == 10_000_000
        assert session.threshold_voltage == 1.8
        assert session.pre_trigger_samples == 0
        assert pro32.capture_setup(session) is not None

        # the settings come back the next time
        again = CaptureDialog(pro32)
        try:
            assert again.immediate_radio.isChecked()
            assert again.acquisition_box.currentData() == ACQUISITION_STREAM
            assert again.frequency_box.value() == 10_000_000
            assert again.threshold_box.value() == pytest.approx(1.8)
        finally:
            again.close()
    finally:
        dialog.close()


def test_edge_trigger_on_channel_32(pro32):
    dialog = CaptureDialog(pro32)
    try:
        select(dialog, [0, 31])
        dialog.edge_radio.setChecked(True)
        dialog.trigger_channel_box.setCurrentIndex(dialog.trigger_channel_box.findData(31))
        dialog._accept()
        session = dialog.selected_settings
        assert session.trigger_type == TriggerType.EDGE and session.trigger_channel == 31
        assert pro32.capture_setup(session).trigger.conditions == {31: "R"}
    finally:
        dialog.close()


def test_main_window_lists_and_connects_a_dslogic(application, monkeypatch):
    from pipilogicanalyzer.ui import main_window as module
    from pipilogicanalyzer.ui.main_window import MainWindow

    board = info(0x002A, super_speed=True)
    monkeypatch.setattr(usb, "list_devices", lambda: [board])
    monkeypatch.setattr(module.firmware_images, "find_boot_drives", lambda: [])
    device = FakeDSLogic(board)
    monkeypatch.setattr(
        module.dslogic_driver, "DSLogicDriver",
        lambda found: open_driver(device) if found is board else None,
    )
    window = MainWindow()
    try:
        window.refresh_ports()
        combo = window.port_combo
        index = window._port_index(("dslogic", board.location))
        assert index >= 0 and "U3Pro16" in combo.itemText(index)
        combo.setCurrentIndex(index)
        window.connect_device()
        assert window.driver is not None and window.driver.driver_type == AnalyzerDriverType.DSLOGIC
        assert window.capture_button.isEnabled()
        assert not window.action_bootloader.isEnabled()
        assert not window.action_board_test.isEnabled()
        window.disconnect_device()
        assert device.closed
    finally:
        window.close()


def test_missing_bitstream_offers_the_download(application, monkeypatch):
    from pipilogicanalyzer.ui import main_window as module
    from pipilogicanalyzer.ui.main_window import MainWindow

    board = info(0x0020)
    monkeypatch.setattr(usb, "list_devices", lambda: [board])
    monkeypatch.setattr(module.firmware_images, "find_boot_drives", lambda: [])
    attempts = []

    def driver_factory(found):
        attempts.append(found)
        if len(attempts) == 1:
            raise BitstreamMissingError("DSLogicPlus.bin", "DSLogic Plus")
        return open_driver(FakeDSLogic(found))

    downloads = []
    monkeypatch.setattr(module.dslogic_driver, "DSLogicDriver", driver_factory)
    monkeypatch.setattr(module.dslogic_resources, "download", lambda name: downloads.append(name))
    monkeypatch.setattr(module.messages, "choose", lambda *args, **kwargs: 0)
    window = MainWindow()
    try:
        window.refresh_ports()
        window.port_combo.setCurrentIndex(window._port_index(("dslogic", board.location)))
        window.connect_device()
        assert downloads == ["DSLogicPlus.bin"]
        assert len(attempts) == 2 and window.driver is not None
        window.disconnect_device()
    finally:
        window.close()
