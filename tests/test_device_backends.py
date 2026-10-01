"""A device added later: a driver and a backend are all the application needs."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from typing import Optional

import pytest
from PySide6.QtWidgets import QApplication

from pipilogicanalyzer.core import device_info, settings
from pipilogicanalyzer.driver.base import AnalyzerDriverBase, CaptureError
from pipilogicanalyzer.driver.emulated import EmulatedAnalyzerDriver
from pipilogicanalyzer.ui import devices
from pipilogicanalyzer.ui.dialogs.capture_dialog import (
    CaptureDialog,
    all_capture_settings_files,
    capture_settings_file,
)
from pipilogicanalyzer.ui.dialogs.device_dialogs import DeviceInfoDialog


class MinimalDriver(AnalyzerDriverBase):
    """Only what a driver has to implement."""

    def __init__(self) -> None:
        super().__init__()
        self.sessions = []

    @property
    def driver_id(self) -> str:
        return "minimal"

    @property
    def device_version(self) -> Optional[str]:
        return "Minimal analyzer"

    @property
    def max_frequency(self) -> int:
        return 10_000_000

    @property
    def channel_count(self) -> int:
        return 8

    @property
    def buffer_size(self) -> int:
        return 65536

    @property
    def is_capturing(self) -> bool:
        return False

    def start_capture(self, session, completed_handler=None) -> CaptureError:
        self.sessions.append(session)
        return CaptureError.NONE

    def stop_capture(self) -> bool:
        return True


class MinimalBackend(devices.DeviceBackend):
    id = "minimal"

    def detected(self):
        return [devices.DeviceEntry(self.id, "usb", "1-2", "Minimal analyzer on 1-2")]

    def connect(self, entry, parent):
        assert entry.value == "1-2"
        return MinimalDriver()


@pytest.fixture(scope="module")
def application():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def minimal_backend(monkeypatch):
    monkeypatch.setattr(devices, "_added", [])
    backend = MinimalBackend()
    devices.register_backend(backend)
    return backend


def test_the_built_in_devices_have_backends():
    assert [backend.id for backend in devices.backends()][:2] == ["pico", "dslogic"]


def test_a_registered_device_is_listed_and_connected(application, minimal_backend, monkeypatch):
    from pipilogicanalyzer.ui.devices import pico as pico_devices
    from pipilogicanalyzer.ui.main_window import MainWindow

    monkeypatch.setattr(pico_devices.detector, "detect", lambda: [])
    monkeypatch.setattr(pico_devices.firmware_images, "find_boot_drives", lambda: [])
    monkeypatch.setattr(pico_devices.detector, "detect_foreign_picos", lambda: [])
    window = MainWindow()
    try:
        window.refresh_ports()
        assert window.port_combo.itemText(0) == "Minimal analyzer on 1-2"
        window.port_combo.setCurrentIndex(window._port_index(devices.DeviceEntry("minimal", "usb", "1-2")))
        window.connect_device()

        assert isinstance(window.driver, MinimalDriver)
        assert window.capture_button.isEnabled() and window.action_device_info.isEnabled()
        # Functions the device does not offer stay off
        assert not window.action_bootloader.isEnabled() and not window.action_board_test.isEnabled()
        assert not window.action_network_settings.isEnabled()
        assert window.firmware_notice.isHidden()
    finally:
        window.close()


def test_the_dialogs_work_with_the_defaults_of_the_base_class(application):
    driver = MinimalDriver()

    dialog = CaptureDialog(driver)
    assert dialog.settings_file == capture_settings_file(driver) == "capture-settings-minimal.json"
    assert dialog.blast_box.isHidden() and dialog.trigger_group.isVisibleTo(dialog)
    dialog.close()

    sections = dict(device_info.describe_device(driver))
    assert dict(sections["Device"])["Identification"] == "Minimal analyzer"
    assert dict(sections["Capture"])["Channels"] == "8"

    info = DeviceInfoDialog(driver)
    assert info.self_test is None and info.tabs.count() == 2  # no self-test tab
    info.close()


def test_a_loaded_profile_reaches_every_kind_of_device():
    settings.persist_settings("capture-settings-minimal.json", {})
    files = all_capture_settings_files()
    assert "capture-settings-minimal.json" in files
    assert "capture-settings-serial.json" in files and "capture-settings-dslogic.json" in files
    assert capture_settings_file(None) == "capture-settings-serial.json"


def test_features_of_the_built_in_drivers():
    emulated = EmulatedAnalyzerDriver(1)
    assert not emulated.is_hardware and emulated.board_count == 1
    assert not emulated.supports_bootloader and emulated.driver_id == "emulated"
