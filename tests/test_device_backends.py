"""A device added later: a driver and a backend are all the application needs."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from typing import Optional

import pytest
from PySide6.QtWidgets import QApplication

from openscilab.core import device_info, settings
from openscilab.driver.base import AnalyzerDriverBase, CaptureError
from openscilab.driver.emulated import EmulatedAnalyzerDriver
from openscilab.ui import devices
from openscilab.ui.dialogs.capture_dialog import (
    CaptureDialog,
    all_capture_settings_files,
    capture_settings_file,
)
from openscilab.ui.dialogs.device_dialogs import DeviceInfoDialog


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
    assert [backend.id for backend in devices.backends()][:3] == ["pico", "arduino", "dslogic"]


def test_a_registered_device_is_listed_and_connected(application, minimal_backend, monkeypatch, shell):
    from openscilab.ui.devices import pico as pico_devices

    monkeypatch.setattr(pico_devices.detector, "detect", lambda: [])
    monkeypatch.setattr(pico_devices.firmware_images, "find_boot_drives", lambda: [])
    monkeypatch.setattr(pico_devices.detector, "detect_foreign_picos", lambda: [])
    section = shell.devices_section
    section.refresh()
    assert section.list.item(0).text() == "Minimal analyzer on 1-2"
    section.list.itemActivated.emit(section.list.item(0))

    card = shell.active_document()
    assert isinstance(card.instrument.capture.driver, MinimalDriver)
    assert card.data_button.isEnabled() and card.action_settings.isVisible() and card.profiles_button.isEnabled()
    # Functions the device does not offer are not on its Details tab
    assert not card.action_bootloader.isVisible() and not card.action_self_test.isVisible()
    assert not card.action_network.isVisible() and not card.action_generated.isVisible()
    assert section.notice.isHidden()


def test_the_dialogs_work_with_the_defaults_of_the_base_class(application):
    driver = MinimalDriver()

    dialog = CaptureDialog(driver)
    assert dialog.settings_file == capture_settings_file(driver) == "capture-settings-minimal.json"
    # the trigger is a page of its own: shown when its page is
    assert dialog.blast_box.isHidden() and not dialog.trigger_group.isHidden()
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


# ------------------------------------------------------------------- the hub
class SecondBackend(MinimalBackend):
    id = "second"

    def detected(self):
        return [devices.DeviceEntry(self.id, "usb", "1-2", "Second analyzer on 1-2")]


@pytest.fixture
def no_picos(monkeypatch):
    from openscilab.ui.devices import pico as pico_devices

    monkeypatch.setattr(pico_devices.detector, "detect", lambda: [])
    monkeypatch.setattr(pico_devices.firmware_images, "find_boot_drives", lambda: [])
    monkeypatch.setattr(pico_devices.detector, "detect_foreign_picos", lambda: [])
    monkeypatch.setattr(devices, "_added", [])
    devices.register_backend(MinimalBackend())
    devices.register_backend(SecondBackend())


def test_a_backend_opens_an_instrument(no_picos, application):
    from openscilab.core.instrument import CaptureFacet

    instrument = MinimalBackend().open_instrument(devices.DeviceEntry("minimal", "usb", "1-2"), None)

    assert isinstance(instrument.capture, CaptureFacet)
    assert isinstance(instrument.capture.driver, MinimalDriver)
    assert instrument.uri == "minimal:1-2" and instrument.name == "Minimal analyzer"


def test_connecting_in_the_device_list_puts_the_device_into_the_hub(no_picos, shell):
    instrument = shell.connect_entry(devices.DeviceEntry("minimal", "usb", "1-2"))

    assert shell.hub.names() == ["Minimal analyzer"] and instrument is shell.hub.get("Minimal analyzer")
    assert shell.devices_label.text() == "1 device"
    view = shell.show_data(instrument)
    assert view.instrument is instrument and instrument.owner is view

    shell.device_card(instrument).disconnect_button.click()
    assert len(shell.hub) == 0 and view.driver is None and view.instrument is None


def test_two_devices_in_the_sidebar_each_with_a_device_card(no_picos, shell):
    from openscilab.ui.documents.device import DeviceDocument

    def available():
        return [section.list.item(row).text() for row in range(section.list.count())]

    shell.activity_bar.select("devices")
    section = shell.devices_section
    assert available()[:2] == ["Minimal analyzer on 1-2", "Second analyzer on 1-2"]

    section.list.itemActivated.emit(section.list.item(0))
    first = shell.hub.get("Minimal analyzer")
    second = shell.connect_entry(devices.DeviceEntry("second", "usb", "1-2", "Second"))
    section.refresh()

    assert shell.hub.names() == ["Minimal analyzer", "Minimal analyzer 2"]
    assert section.open_list.count() == 2
    assert "Minimal analyzer on 1-2" not in available()  # open devices leave the list
    cards = [document for document in shell.documents() if isinstance(document, DeviceDocument)]
    assert [card.instrument for card in cards] == [first, second]
    assert cards[0].pin_table.rowCount() == 8 and cards[0].pin_table.isColumnHidden(5)  # no GPIO
    assert "Capture" in cards[0].capabilities_label.text()
    chips = [instrument.name for instrument in shell.hub.instruments()]
    assert len(chips) == 2 and "Minimal analyzer 2" in chips[1]

    # Clicking an open instrument shows its card again; the context actions work.
    section.open_list.itemClicked.emit(section.open_list.item(0))
    assert shell.active_document() is cards[0]
    analyzer = shell.show_data(first)
    assert analyzer.instrument is first and analyzer.driver is first.capture.driver
    assert shell.show_data(first) is analyzer

    cards[1].disconnect_button.click()
    assert shell.hub.names() == ["Minimal analyzer"]
    assert not cards[1].disconnect_button.isEnabled()


def test_one_document_captures_with_an_instrument_at_a_time(no_picos, shell, make_dataview):
    instrument = shell.connect_entry(devices.DeviceEntry("minimal", "usb", "1-2"))
    first, second = make_dataview(), make_dataview()
    first.use_instrument(instrument)
    second.use_instrument(instrument)

    assert instrument.owner is second and second.driver is instrument.capture.driver
    assert first.instrument is None and first.driver is None
    # Closing the document leaves the device open in the hub.
    shell.area.close_document(second, force=True)
    assert instrument in shell.hub and instrument.owner is None


def test_a_pico_capture_runs_through_the_hub(shell, make_dataview, monkeypatch):
    import threading

    import numpy as np

    from openscilab.core.instrument import Instrument
    from openscilab.driver.base import CaptureMode
    from openscilab.driver.models import AnalyzerChannel, CaptureSession
    from openscilab.driver.pico.analyzer import PicoDriver
    from tests.test_driver import FakeTransport, build_capture_payload

    transport = FakeTransport()
    monkeypatch.setattr("openscilab.driver.pico.analyzer.SerialTransport", lambda *a, **k: transport)
    instrument = Instrument.from_driver(PicoDriver("/dev/fake"), uri="pico:/dev/fake")
    shell.hub.add(instrument)
    window = make_dataview()
    window.use_instrument(instrument)

    session = CaptureSession(frequency=1_000_000, pre_trigger_samples=2, post_trigger_samples=6)
    session.capture_channels = [AnalyzerChannel(channel_number=index) for index in range(4)]
    transport.queue_response("CAPTURE_STARTED")
    transport.queue_response("CAPTURE_DATA")
    transport.queue_data(build_capture_payload([0b0001, 0b0011, 0b0000, 0b1111] * 2, CaptureMode.CHANNELS_8))
    done = threading.Event()
    window.source.bridge.completed.connect(lambda _args: done.set())

    window._begin_capture(session)
    deadline = 200
    while not done.is_set() and deadline:
        QApplication.processEvents()
        done.wait(0.02)
        deadline -= 1
    QApplication.processEvents()

    assert done.is_set()
    assert window.model.sample_count == 8
    assert np.array_equal(window.model.session.capture_channels[1].samples, [0, 1, 0, 1, 0, 1, 0, 1])
    assert window.dirty
