"""Connected hardware: the image that fits a board, the scan, installing (bootloader, write, back)
and the overview document."""

from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QPushButton

from openscilab.core import firmware
from openscilab.driver.pico.detector import DetectedDevice, ForeignPico
from openscilab.ui.devices import hardware

PICO = firmware.board_by_name("PICO")
PICO_2 = firmware.board_by_name("PICO_2")


def image(board: str, turbo: bool = False, version: str = "7_2", family=None) -> firmware.Uf2Image:
    model = firmware.board_by_name(board)
    family = family or (firmware.FAMILY_RP2040 if model.chip == "RP2040" else firmware.FAMILY_RP2350_ARM_S)
    name = f"PiPiLogicAnalyzer_{model.board_type}{'_Turbo' if turbo else ''}.uf2"
    return firmware.Uf2Image(path=f"/images/{name}", families=frozenset({family}), block_count=10, data_size=2560,
                             board=model, turbo=turbo, device_version=f"PIPI_LOGIC_ANALYZER_{board}_V{version}")


IMAGES = [image("PICO", turbo=True), image("PICO"), image("W"), image("PICO_2"), image("2_W")]


def wait_for(condition, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return condition()


# ------------------------------------------------------------------- images
def test_the_image_that_fits_a_board():
    assert firmware.matching_image(IMAGES, PICO).file_name == "PiPiLogicAnalyzer_BOARD_PICO.uf2"  # not turbo
    assert firmware.matching_image(IMAGES, firmware.board_by_name("W")).file_name.endswith("BOARD_PICO_W.uf2")
    assert firmware.matching_image(IMAGES, None) is None
    by_chip = firmware.images_for(IMAGES, chip="RP2350")
    assert [item.board.name for item in by_chip] == ["PICO_2", "2_W"]  # the default board of the chip first
    assert firmware.images_for(IMAGES, chip="RP2040")[0].board.name == "PICO"


def test_the_state_of_the_firmware():
    assert firmware.firmware_state("PIPI_LOGIC_ANALYZER_PICO_V7_2", IMAGES)[0] == "current"
    state, update = firmware.firmware_state("PIPI_LOGIC_ANALYZER_PICO_V7_1", IMAGES)
    assert state == "update" and update.version == "7.2"
    assert firmware.firmware_state("LOGIC_ANALYZER_V6_5", IMAGES)[0] == "unknown"  # no board reported
    assert firmware.firmware_state("garbage", IMAGES) == ("unknown", None)


# --------------------------------------------------------------------- scan
def drive(path="/Volumes/RPI-RP2", chip="RP2040") -> firmware.BootDrive:
    return firmware.BootDrive(path=path, chip=chip, info=(("UF2 Bootloader", "v3.0"),))


def test_the_scan_lists_every_kind_of_hardware(application):
    from openscilab.ui.devices import DeviceEntry

    entries = hardware.scan(
        None, {"/dev/a": "PIPI_LOGIC_ANALYZER_PICO_V7_1"},
        find_drives=lambda: [drive()],
        detect=lambda: [DetectedDevice("/dev/a", "E661"), DetectedDevice("/dev/b")],
        detect_foreign=lambda: [ForeignPico("/dev/c", 0x0005, "MicroPython")],
        images=IMAGES,
        other_entries=lambda: [DeviceEntry("dslogic", "usb", "1-2", "DSLogic U3Pro16 on 1-2")])
    kinds = [(entry.kind, entry.connection.split(" ")[0]) for entry in entries]
    assert kinds == [("analyzer", "/dev/a"), ("analyzer", "/dev/b"), ("bootloader", "/Volumes/RPI-RP2"),
                     ("foreign", "/dev/c"), ("device", "dslogic:1-2")]
    first, second, boot, foreign, dslogic = entries
    assert first.state == "update" and first.board is PICO and first.image.version == "7.2"
    assert "firmware 7.1" in first.firmware and first.entry.value == "/dev/a"
    assert second.firmware == "reading..." and second.state == ""
    assert boot.state == "none" and boot.image.board is PICO and boot.can_install
    assert foreign.firmware == "MicroPython" and foreign.can_install and not dslogic.can_install


# ---------------------------------------------------------------- installing
class FakeBoard:
    """A board that goes into its bootloader on a touch and comes back after writing."""

    def __init__(self, in_bootloader: bool = False):
        self.mode = "bootloader" if in_bootloader else "running"
        self.written = []

    def find_drives(self):
        return [drive("/Volumes/NEW")] if self.mode == "bootloader" else []

    def touch(self, port):
        self.mode = "bootloader"
        return True

    def flash(self, path, target):
        self.written.append((path, target.path))
        self.mode = "running"

    def detect(self):
        return [DetectedDevice("/dev/a")] if self.mode == "running" else []


@pytest.fixture
def application():
    return QApplication.instance() or QApplication([])


def test_installing_restarts_writes_and_waits_for_the_board(application):
    board = FakeBoard()
    installer = hardware.FirmwareInstaller(find_drives=board.find_drives, touch_reset=board.touch,
                                           flash=board.flash, detect=board.detect)
    steps, results = [], []
    installer.progress.connect(steps.append)
    installer.finished.connect(lambda ok, message: results.append((ok, message)))
    entry = hardware.HardwareEntry("analyzer:/dev/a", hardware.ANALYZER, "Analyzer", "/dev/a", port="/dev/a",
                                   chip="RP2040")
    assert installer.install(entry, IMAGES[1])
    assert wait_for(lambda: results)
    assert results[0][0] and installer.new_port == "/dev/a"
    assert board.written == [("/images/PiPiLogicAnalyzer_BOARD_PICO.uf2", "/Volumes/NEW")]
    assert steps[0].startswith("Restarting") and any(step.startswith("Writing") for step in steps)


def test_a_wrong_chip_is_refused_and_a_board_in_bootloader_is_written_at_once(application):
    board = FakeBoard(in_bootloader=True)
    installer = hardware.FirmwareInstaller(find_drives=board.find_drives, touch_reset=board.touch,
                                           flash=board.flash, detect=board.detect)
    results = []
    installer.finished.connect(lambda ok, message: results.append((ok, message)))
    entry = hardware.HardwareEntry("boot", hardware.BOOTLOADER, "Board", "/Volumes/NEW", chip="RP2040",
                                   drive=drive("/Volumes/NEW"))
    assert not installer.install(entry, IMAGES[3])  # a Pico 2 image on an RP2040
    assert "RP2350" in results[0][1]
    assert installer.install(entry, IMAGES[1])
    assert wait_for(lambda: len(results) == 2) and results[1][0]


# ----------------------------------------------------------------- document
def test_the_overview_installs_the_fitting_image(shell, monkeypatch):
    from openscilab.ui import messages
    from openscilab.ui.documents.hardware import HardwareDocument

    board = FakeBoard()
    installer = hardware.FirmwareInstaller(find_drives=board.find_drives, touch_reset=board.touch,
                                           flash=board.flash, detect=board.detect)
    versions = {"/dev/a": "PIPI_LOGIC_ANALYZER_PICO_V7_1"}

    def scan(hub, known, images=None):
        return hardware.scan(hub, known, find_drives=board.find_drives, detect=board.detect,
                             detect_foreign=lambda: [], images=images, other_entries=lambda: [])

    document = HardwareDocument(shell.hub, scan=scan, query=lambda port: (versions[port], {}), installer=installer,
                                images=lambda: IMAGES)
    shell.add_document(document)
    assert wait_for(lambda: document.entries and document.entries[0].state == "update")
    assert document.table.item(0, 3).text() == "update to 7.2"
    asked = []
    monkeypatch.setattr(messages, "confirm", lambda *args, **kwargs: asked.append(args[2]) or True)
    button = document.table.cellWidget(0, 4).findChild(QPushButton, "install-analyzer:/dev/a")
    assert button.text() == "Update firmware"
    versions["/dev/a"] = "PIPI_LOGIC_ANALYZER_PICO_V7_2"  # what the board reports afterwards
    button.click()
    assert "firmware 7.2" in asked[0] and "Raspberry Pi Pico" in asked[0]
    assert wait_for(lambda: "installed" in document.status_label.text())
    assert board.written[0][0].endswith("BOARD_PICO.uf2")
    assert wait_for(lambda: document.entries and document.entries[0].state == "current")
    opened = []
    document.open_requested.connect(opened.append)
    document.table.cellWidget(0, 4).findChild(QPushButton).click()  # Open
    assert opened and opened[0].value == "/dev/a"


def test_the_shell_offers_the_connected_hardware(shell):
    from openscilab.ui.documents.hardware import HardwareDocument

    assert "Connected &hardware" in [action.text() for action in shell.devices_menu.actions()]
    shell.devices_section.hardware_button.click()
    document = shell.active_document()
    assert isinstance(document, HardwareDocument) and shell.show_hardware() is document


def test_the_overview_says_when_nothing_is_connected(shell):
    from openscilab.ui.documents.hardware import HardwareDocument

    document = HardwareDocument(shell.hub, scan=lambda hub, known, images=None: [], images=lambda: [])
    shell.add_document(document)
    assert "No board or device found" in document.table.item(0, 0).text()
    QApplication.processEvents()
    header = document.table.horizontalHeader()
    assert all(document.table.columnWidth(column) >= header.sectionSizeHint(column) for column in (0, 1, 3))
