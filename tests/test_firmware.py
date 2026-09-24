"""Firmware images, bootloader drives, flashing and device information."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import struct
import time
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication

from pipilogicanalyzer.core import device_info, firmware
from pipilogicanalyzer.driver import detector, protocol
from pipilogicanalyzer.driver.analyzer import PiPiLogicAnalyzerDriver
from pipilogicanalyzer.driver.base import AnalyzerDriverType
from pipilogicanalyzer.driver.detector import DetectedDevice, ForeignPico, UsbPortInfo
from pipilogicanalyzer.driver.emulated import EmulatedAnalyzerDriver
from pipilogicanalyzer.ui.dialogs.device_dialogs import DeviceInfoDialog
from pipilogicanalyzer.ui.dialogs.firmware_dialog import ConnectedDevice, FirmwareDialog

from test_driver import FakeTransport


def make_uf2(path, families=(firmware.FAMILY_RP2040,), blocks=3, payload=256, content=b""):
    """A UF2 image; ``content`` is written into the payload, split across the blocks."""
    data = bytearray()
    for number in range(blocks):
        family = families[number % len(families)]
        block_content = content[number * payload : (number + 1) * payload]
        header = struct.pack(
            "<IIIIIIII",
            firmware.UF2_MAGIC_START0,
            firmware.UF2_MAGIC_START1,
            firmware.UF2_FLAG_FAMILY_ID,
            0x10000000 + number * payload,
            payload,
            number,
            blocks,
            family,
        )
        data += header + block_content.ljust(476, b"\0") + struct.pack("<I", firmware.UF2_MAGIC_END)
    path.write_bytes(bytes(data))
    return path


def make_drive(root, name, board_id):
    drive = root / name
    drive.mkdir()
    (drive / "INFO_UF2.TXT").write_text(
        f"UF2 Bootloader v3.0\nModel: Raspberry Pi RP2\nBoard-ID: {board_id}\n"
    )
    return drive


# ----------------------------------------------------------------- images
def test_uf2_image_is_read(tmp_path):
    path = make_uf2(
        tmp_path / "pipilogicanalyzer_6.5_BOARD_PICO_2_W_WIFI_Turbo.uf2",
        families=(firmware.FAMILY_RP2350_ARM_S, firmware.FAMILY_ABSOLUTE),
    )
    image = firmware.read_uf2(str(path))

    assert image.chip == "RP2350"
    assert image.board.name == "2_WIFI"
    assert image.turbo
    assert (image.block_count, image.data_size) == (3, 768)
    assert "Pico 2 W (WiFi) (Turbo)" in image.label


def test_longest_board_type_wins():
    assert firmware.board_from_text("PiPiLogicAnalyzer_BOARD_PICO.uf2").name == "PICO"
    assert firmware.board_from_text("PiPiLogicAnalyzer_BOARD_PICO_W.uf2").name == "W"
    assert firmware.board_from_text("blink.uf2") is None


@pytest.mark.parametrize("content", [bytes(512), bytes(100), b""])
def test_invalid_uf2_files_are_rejected(tmp_path, content):
    path = tmp_path / "broken.uf2"
    path.write_bytes(content)
    with pytest.raises(ValueError):
        firmware.read_uf2(str(path))


def test_find_images_skips_invalid_files(tmp_path):
    make_uf2(tmp_path / "PiPiLogicAnalyzer_BOARD_PICO.uf2")
    (tmp_path / "broken.uf2").write_bytes(bytes(512))
    (tmp_path / "notes.txt").write_text("x")
    images = firmware.find_images([str(tmp_path), str(tmp_path / "missing")])
    assert [image.file_name for image in images] == ["PiPiLogicAnalyzer_BOARD_PICO.uf2"]


def test_compatibility(tmp_path):
    image = firmware.read_uf2(str(make_uf2(tmp_path / "a.uf2")))
    assert firmware.is_compatible(image, "RP2040") is True
    assert firmware.is_compatible(image, "RP2350") is False
    assert firmware.is_compatible(image, None) is None


# ---------------------------------------------------------------- versions
def test_device_versions_are_parsed():
    identity = firmware.parse_device_version("LOGIC_ANALYZER_2_WIFI_V6_5")
    assert (identity.board_name, identity.major, identity.minor) == ("2_WIFI", 6, 5)
    assert identity.board.chip == "RP2350" and identity.board.wifi

    old = firmware.parse_device_version("LOGIC_ANALYZER_V6_0")
    assert old.board_name == "" and old.board is None
    assert firmware.parse_device_version("MULTI_ANALYZER") is None


# --------------------------------------------------------- bootloader drives
def test_boot_drives_are_found(tmp_path):
    make_drive(tmp_path, "RPI-RP2", "RPI-RP2")
    make_drive(tmp_path, "RP2350", "RP2350")
    make_drive(tmp_path, "FEATHERBOOT", "SAMD21G18A-Feather-v0")
    (tmp_path / "USB STICK").mkdir()

    drives = firmware.find_boot_drives([str(path) for path in sorted(tmp_path.iterdir())])

    assert [(drive.name, drive.chip) for drive in drives] == [("RP2350", "RP2350"), ("RPI-RP2", "RP2040")]
    assert drives[1].bootloader == "v3.0"


def test_flashing_copies_the_image(tmp_path):
    image = make_uf2(tmp_path / "PiPiLogicAnalyzer_BOARD_PICO.uf2")
    drive = firmware.boot_drive_at(str(make_drive(tmp_path, "RPI-RP2", "RPI-RP2")))

    target = firmware.flash_image(str(image), drive)

    assert os.path.basename(target) == "FIRMWARE.UF2"
    assert open(target, "rb").read() == image.read_bytes()


# ------------------------------------------------------------------ detector
def fake_port(**values):
    defaults = dict(
        device="/dev/port", vid=None, pid=None, serial_number=None, location=None,
        hwid="", manufacturer=None, product=None, description="n/a",
    )
    defaults.update(values)
    return SimpleNamespace(**defaults)


def test_foreign_raspberry_pi_boards_are_detected(monkeypatch):
    ports = [
        fake_port(device="/dev/analyzer", vid=detector.VID, pid=detector.PID),
        fake_port(device="/dev/micropython", vid=detector.RASPBERRY_PI_VID, pid=0x0005, serial_number="E66"),
        fake_port(device="/dev/other", vid=detector.RASPBERRY_PI_VID, pid=0x1234, product="Custom"),
        fake_port(device="/dev/ftdi", vid=0x0403, pid=0x6001),
    ]
    monkeypatch.setattr(detector.list_ports, "comports", lambda: ports)

    boards = detector.detect_foreign_picos()

    assert [(board.port_name, board.description) for board in boards] == [
        ("/dev/micropython", "MicroPython"),
        ("/dev/other", "Custom"),
    ]
    details = detector.port_details("/dev/micropython")
    assert (details.vid, details.pid, details.serial_number) == (detector.RASPBERRY_PI_VID, 5, "E66")
    assert detector.port_details("/dev/missing") is None


def test_port_labels_tell_identical_boards_apart(monkeypatch):
    ports = [
        fake_port(device="/dev/b", vid=detector.VID, pid=detector.PID, serial_number="E6614C", location="1-2"),
        fake_port(device="/dev/a", vid=detector.VID, pid=detector.PID, serial_number="E66138", location="1-1"),
        fake_port(device="/dev/micropython", vid=detector.RASPBERRY_PI_VID, pid=0x0005),
        fake_port(device="/dev/ttyS0"),
    ]
    monkeypatch.setattr(detector.list_ports, "comports", lambda: ports)

    labels = [info.label for info in detector.list_port_infos()]

    assert labels == [
        "/dev/a (PiPiLogicAnalyzer, S/N E66138)",
        "/dev/b (PiPiLogicAnalyzer, S/N E6614C)",
        "/dev/micropython (MicroPython)",
        "/dev/ttyS0",
    ]


def test_multi_connect_dialog_shows_serial_numbers(application, monkeypatch):
    from pipilogicanalyzer.ui.dialogs.device_dialogs import MultiConnectDialog

    ports = [
        fake_port(device="/dev/a", vid=detector.VID, pid=detector.PID, serial_number="E66138", location="1-1"),
        fake_port(device="/dev/b", vid=detector.VID, pid=detector.PID, serial_number="E6614C", location="1-2"),
    ]
    monkeypatch.setattr(detector.list_ports, "comports", lambda: ports)
    dialog = MultiConnectDialog(detector.list_port_infos())

    table = dialog.port_table
    assert [[table.item(row, column).text() for column in range(4)] for row in range(2)] == [
        ["/dev/a", "PiPiLogicAnalyzer", "E66138", "1-1"],
        ["/dev/b", "PiPiLogicAnalyzer", "E6614C", "1-2"],
    ]
    assert dialog.combos[0].itemText(2) == "/dev/b (PiPiLogicAnalyzer, S/N E6614C)"

    dialog.combos[0].setCurrentIndex(2)
    dialog.combos[1].setCurrentIndex(1)
    dialog._accept()
    assert dialog.connection_strings == ["/dev/b", "/dev/a"]

    # Plain port names still work
    plain = MultiConnectDialog(["/dev/x"])
    assert plain.combos[0].itemText(1) == "/dev/x"


def test_device_list_only_offers_analyzers(application, monkeypatch):
    from pipilogicanalyzer.ui import main_window as main_window_module
    from pipilogicanalyzer.ui.main_window import MainWindow

    ports = [
        fake_port(device="/dev/analyzer", vid=detector.VID, pid=detector.PID, serial_number="E66138"),
        fake_port(device="/dev/micropython", vid=detector.RASPBERRY_PI_VID, pid=0x0005),
        fake_port(device="/dev/ttyS0"),
    ]
    monkeypatch.setattr(detector.list_ports, "comports", lambda: ports)
    monkeypatch.setattr(main_window_module.firmware_images, "find_boot_drives", lambda: [])
    window = MainWindow()
    try:
        window.refresh_ports()
        combo = window.port_combo
        items = [combo.itemData(index) for index in range(combo.count()) if combo.itemText(index)]
        assert items == [
            ("autodetect", None),
            ("serial", "/dev/analyzer"),
            ("network", None),
            ("multi", None),
        ]
    finally:
        window.close()


def test_multi_compose_dialog_shows_serial_numbers(application):
    from pipilogicanalyzer.ui.dialogs.device_dialogs import MultiComposeDialog

    devices = [
        DetectedDevice(port_name="/dev/a", serial_number="E66138", parent_id="1-1"),
        DetectedDevice(port_name="/dev/b", serial_number="E6614C", parent_id="1-2"),
    ]
    dialog = MultiComposeDialog(devices)

    assert [dialog.table.item(0, column).text() for column in range(3)] == ["/dev/a", "E66138", "1-1"]

    dialog.combos[0].setCurrentIndex(1)
    dialog.combos[1].setCurrentIndex(0)
    dialog._accept()
    assert [device.serial_number for device in dialog.ordered_devices] == ["E6614C", "E66138"]


# ------------------------------------------------------------ device details
@pytest.fixture
def extended_device(monkeypatch):
    transport = FakeTransport()
    transport._responses[0] = "LOGIC_ANALYZER_PICO_V6_5"
    monkeypatch.setattr(
        "pipilogicanalyzer.driver.analyzer.SerialTransport", lambda *args, **kwargs: transport
    )
    driver = PiPiLogicAnalyzerDriver("/dev/fake")
    for line in (
        "CAPS:SELFTEST,SIMULATION,DEVICEINFO",
        "INFO:BOARD:PICO",
        "INFO:CHIP:RP2040",
        "INFO:CHIP_REVISION:B2",
        "INFO:UNIQUE_ID:E6614103E7654321",
        "INFO:FLASH_SIZE:2097152",
        "INFO:CLOCK:200000000",
        "INFO:TURBO:0",
        "INFO:BUILD_DATE:Sep 14 2026 10:00:00",
        "INFO_END",
    ):
        transport.queue_response(line)
    driver.test_transport = transport  # type: ignore[attr-defined]
    return driver


def test_device_details_are_read(extended_device):
    details = extended_device.device_details()
    assert details["UNIQUE_ID"] == "E6614103E7654321"
    assert details["BUILD_DATE"] == "Sep 14 2026 10:00:00"
    assert bytes(extended_device.test_transport.written).endswith(
        protocol.command_packet(protocol.CMD_DEVICE_INFO)
    )


def test_device_description_of_an_extended_board(extended_device, monkeypatch):
    monkeypatch.setattr(
        detector,
        "port_details",
        lambda port: UsbPortInfo(port, 0x1209, 0x3020, "Dr. Gusman", "PiPiLogicAnalyzer", "E661", "1-1", None),
    )
    sections = dict(device_info.describe_device(extended_device))

    device = dict(sections["Device"])
    assert device["Board"] == "Raspberry Pi Pico"
    assert device["Build setting"] == "BOARD_PICO"
    assert device["Firmware version"] == "6.5"
    assert device["Microcontroller"] == "RP2040"
    assert "Board self-test" in device["Additional functions"]

    build = dict(sections["Firmware build"])
    assert build["Unique board ID"] == "E6614103E7654321"
    assert build["Flash size"] == "2 MB (2,097,152 bytes)"
    assert build["System clock"] == "200 MHz"
    assert build["Turbo mode (overclocked)"] == "No"

    connection = dict(sections["Connection"])
    assert connection["USB vendor:product"] == "1209:3020"
    assert connection["Product"] == "PiPiLogicAnalyzer"
    assert "Max. bursts" in dict(sections["Capture"])


def test_device_description_of_original_firmware(monkeypatch):
    transport = FakeTransport()
    transport.queue_response("ERR_UNKNOWN_MSG")
    monkeypatch.setattr(
        "pipilogicanalyzer.driver.analyzer.SerialTransport", lambda *args, **kwargs: transport
    )
    monkeypatch.setattr(detector, "port_details", lambda port: None)
    sections = dict(device_info.describe_device(PiPiLogicAnalyzerDriver("/dev/fake")))
    assert "Firmware build" not in sections
    assert dict(sections["Device"])["Firmware type"].startswith("Original or older")


@pytest.fixture(scope="module")
def application():
    return QApplication.instance() or QApplication([])


def test_device_info_dialog(application):
    dialog = DeviceInfoDialog(EmulatedAnalyzerDriver(1))
    titles = [dialog.tree.topLevelItem(index).text(0) for index in range(dialog.tree.topLevelItemCount())]
    assert titles == ["Device", "Connection", "Capture"]
    assert "[Capture]" in dialog.text() and "Channels: 24" in dialog.text()


# ------------------------------------------------------------ firmware dialog
def firmware_dialog(tmp_path, images, drives=(), foreign=(), analyzers=None, touched=None, **kwargs):
    analyzers = analyzers if analyzers is not None else []
    kwargs.setdefault("query_device", lambda port: (None, {}))  # never open a real port
    return FirmwareDialog(
        scan_boot_drives=lambda: list(drives),
        scan_foreign=lambda: list(foreign),
        scan_analyzers=lambda: list(analyzers),
        scan_images=lambda: list(images),
        touch_reset=lambda port: (touched.append(port) if touched is not None else None) or True,
        **kwargs,
    )


def test_flashing_needs_a_matching_image(application, tmp_path):
    drive = firmware.boot_drive_at(str(make_drive(tmp_path, "RPI-RP2", "RPI-RP2")))
    pico = firmware.read_uf2(str(make_uf2(tmp_path / "PiPiLogicAnalyzer_BOARD_PICO.uf2")))
    pico2 = firmware.read_uf2(
        str(make_uf2(tmp_path / "PiPiLogicAnalyzer_BOARD_PICO_2.uf2", families=(firmware.FAMILY_RP2350_ARM_S,)))
    )
    dialog = firmware_dialog(tmp_path, [pico, pico2], drives=[drive])

    assert dialog.targets.currentItem().text().startswith("Bootloader drive RPI-RP2 (RP2040)")
    dialog.images_box.setCurrentIndex(0)
    assert dialog.flash_button.isEnabled()
    assert not dialog.restart_button.isEnabled()

    dialog.images_box.setCurrentIndex(1)
    assert not dialog.flash_button.isEnabled()
    assert "built for the RP2350" in dialog.warning_label.text()
    dialog.done(0)


def test_flashing_waits_for_the_analyzer(application, tmp_path, monkeypatch):
    monkeypatch.setattr("pipilogicanalyzer.ui.messages.confirm", lambda *args, **kwargs: True)
    drive = firmware.boot_drive_at(str(make_drive(tmp_path, "RPI-RP2", "RPI-RP2")))
    image = firmware.read_uf2(str(make_uf2(tmp_path / "PiPiLogicAnalyzer_BOARD_PICO.uf2")))
    analyzers: list = []
    dialog = firmware_dialog(tmp_path, [image], drives=[drive], analyzers=analyzers)

    dialog.flash_selected()
    deadline = time.monotonic() + 5
    while "waiting" not in dialog.status_label.text() and time.monotonic() < deadline:
        application.processEvents()
        time.sleep(0.01)
    assert (tmp_path / "RPI-RP2" / "FIRMWARE.UF2").read_bytes() == open(image.path, "rb").read()

    analyzers.append(DetectedDevice(port_name="/dev/new-analyzer"))
    dialog.refresh_targets()
    assert dialog.new_port == "/dev/new-analyzer"
    assert "available on /dev/new-analyzer" in dialog.status_label.text()
    dialog.done(0)


def test_foreign_boards_are_restarted_by_the_1200_baud_touch(application, tmp_path):
    touched: list = []
    board = ForeignPico(port_name="/dev/micropython", pid=5, description="MicroPython")
    dialog = firmware_dialog(tmp_path, [], foreign=[board], touched=touched)

    assert "MicroPython on /dev/micropython" in dialog.targets.currentItem().text()
    assert dialog.restart_button.isEnabled() and not dialog.flash_button.isEnabled()
    dialog.restart_selected()
    assert touched == ["/dev/micropython"]
    dialog.done(0)


def test_connected_analyzer_is_restarted_through_the_callback(application, tmp_path):
    calls = []
    drives: list = []
    dialog = firmware_dialog(
        tmp_path, [], drives=drives,
        connected_devices=[ConnectedDevice("/dev/pico", "LOGIC_ANALYZER_PICO_V6_5", {"SDK": "2.1.0"})],
        restart_connected=lambda: calls.append(True) or True,
    )
    assert "firmware 6.5 · Raspberry Pi Pico · RP2040 · Pico SDK 2.1.0" in dialog.targets.currentItem().text()

    dialog.restart_selected()
    assert calls == [True]
    assert "Restarting" in dialog.status_label.text()

    # In bootloader mode the board reports nothing, the firmware it ran is still shown.
    drives.append(firmware.boot_drive_at(str(make_drive(tmp_path, "RPI-RP2", "RPI-RP2"))))
    dialog.refresh_targets()
    text = dialog.targets.currentItem().text()
    assert "UF2 bootloader v3.0" in text
    assert "before the restart: firmware 6.5 · Raspberry Pi Pico" in text
    dialog.done(0)


def wait_for_text(application, item_text, expected, timeout=5.0):
    deadline = time.monotonic() + timeout
    while expected not in item_text() and time.monotonic() < deadline:
        application.processEvents()
        time.sleep(0.01)
    return item_text()


def test_every_board_of_a_multi_device_set_shows_its_firmware(application, tmp_path):
    devices = [
        ConnectedDevice("/dev/master", "LOGIC_ANALYZER_2_W_V6_5", {"CHIP": "RP2350"}),
        ConnectedDevice("/dev/slave", "LOGIC_ANALYZER_V6_0"),
    ]
    dialog = firmware_dialog(tmp_path, [], connected_devices=devices, restart_connected=lambda: True)

    texts = [dialog.targets.item(row).text() for row in range(dialog.targets.count())]
    assert "firmware 6.5 · Raspberry Pi Pico 2 W (data over USB) · RP2350" in texts[0]
    assert "firmware 6.0 · board not reported" in texts[1]
    dialog.done(0)


def test_detected_analyzers_are_asked_for_their_firmware(application, tmp_path):
    queried: list = []

    def query(port):
        queried.append(port)
        if port == "/dev/busy":
            raise OSError("port in use")
        return "LOGIC_ANALYZER_PICO_2_V6_5", {"CHIP": "RP2350", "SDK": "2.1.1"}

    analyzers = [DetectedDevice(port_name="/dev/free"), DetectedDevice(port_name="/dev/busy")]
    dialog = firmware_dialog(tmp_path, [], analyzers=analyzers, query_device=query)
    row_text = lambda row: lambda: dialog.targets.item(row).text()  # noqa: E731

    assert "firmware 6.5 · Raspberry Pi Pico 2 · RP2350 · Pico SDK 2.1.1" in wait_for_text(
        application, row_text(0), "Pico SDK"
    )
    assert "could not be read" in wait_for_text(application, row_text(1), "could not be read")
    dialog.refresh_targets()
    assert sorted(queried) == ["/dev/busy", "/dev/free"]  # asked once per port
    dialog.done(0)


def test_version_7_firmware_is_accepted_with_the_v6_5_request():
    from pipilogicanalyzer.driver.base import parse_version

    version = parse_version("LOGIC_ANALYZER_PICO_V7_0")
    assert version.is_valid and (version.major, version.minor) == (7, 0)
    assert protocol.layout_for_version(version.major, version.minor) is protocol.LAYOUT_V6_5
    assert firmware.describe_firmware("LOGIC_ANALYZER_PICO_V7_0") == "firmware 7.0 · Raspberry Pi Pico · RP2040"


def test_firmware_descriptions():
    assert firmware.describe_firmware("LOGIC_ANALYZER_WIFI_V6_5") == (
        "firmware 6.5 · Raspberry Pi Pico W (WiFi) · RP2040"
    )
    assert firmware.describe_firmware("LOGIC_ANALYZER_V6_0", {"BOARD": "PICO"}) == "firmware 6.0 · PICO"
    assert firmware.describe_firmware("SOMETHING_ELSE") == "firmware SOMETHING_ELSE"
    assert firmware.describe_firmware(None) == "firmware version unknown"


# ---------------------------------------------------------------- main window
class OriginalFirmwareDriver(EmulatedAnalyzerDriver):
    @property
    def driver_type(self):
        return AnalyzerDriverType.SERIAL

    def capabilities(self):
        return frozenset()


def test_main_window_notices_boards_without_firmware(application, tmp_path, monkeypatch):
    from pipilogicanalyzer.ui import main_window as main_window_module
    from pipilogicanalyzer.ui.main_window import MainWindow

    drive = firmware.boot_drive_at(str(make_drive(tmp_path, "RPI-RP2", "RPI-RP2")))
    monkeypatch.setattr(main_window_module.firmware_images, "find_boot_drives", lambda: [drive])
    monkeypatch.setattr(main_window_module.detector, "detect_foreign_picos", lambda: [])
    window = MainWindow()
    try:
        window._check_for_new_boards()
        assert not window.firmware_notice.isHidden()
        assert "bootloader mode" in window.firmware_notice.text()

        window._check_connected_firmware(OriginalFirmwareDriver(1))
        assert window.firmware_notice_button.text() == "Update firmware..."
    finally:
        window.close()


def test_the_firmware_version_is_read_from_the_image(tmp_path):
    path = make_uf2(
        tmp_path / "PiPiLogicAnalyzer_BOARD_PICO_2.uf2",
        families=(firmware.FAMILY_RP2350_ARM_S,),
        content=b"\x00binary info\x00LOGIC_ANALYZER_PICO_2_V7_1\x00rest",
    )
    image = firmware.read_uf2(str(path))

    assert image.device_version == "LOGIC_ANALYZER_PICO_2_V7_1"
    assert image.version == "7.1" and image.version_label == "firmware 7.1"
    assert image.label == "Raspberry Pi Pico 2 - 7.1 PiPiLogicAnalyzer_BOARD_PICO_2.uf2"


def test_a_version_split_across_two_blocks_is_still_found(tmp_path):
    # 256 payload bytes per block: the string starts in the first block and ends in the second
    content = b"x" * 250 + b"LOGIC_ANALYZER_ZERO_V7_1" + b"\x00"
    path = make_uf2(tmp_path / "PiPiLogicAnalyzer_BOARD_ZERO.uf2", content=content)

    assert firmware.read_uf2(str(path)).version == "7.1"


def test_images_without_a_version_stay_usable(tmp_path):
    image = firmware.read_uf2(str(make_uf2(tmp_path / "blink.uf2", content=b"no version here")))

    assert image.device_version is None and image.version is None
    assert image.version_label == "unknown firmware version"
    assert image.label == "Unknown board - blink.uf2"


def test_a_two_digit_minor_version_is_read_completely(tmp_path):
    path = make_uf2(tmp_path / "PiPiLogicAnalyzer_BOARD_PICO.uf2", content=b"\x00LOGIC_ANALYZER_PICO_V7_10\x00")

    assert firmware.read_uf2(str(path)).version == "7.10"


def test_both_firmware_identities_are_understood(tmp_path):
    # The firmware of this project reports PIPI_LOGIC_ANALYZER_...; boards flashed with an
    # older version (or the original firmware) report LOGIC_ANALYZER_... and keep working.
    new = firmware.parse_device_version("PIPI_LOGIC_ANALYZER_PICO_2_V7_1")
    old = firmware.parse_device_version("LOGIC_ANALYZER_PICO_2_V7_0")
    assert (new.board_name, new.major, new.minor) == ("PICO_2", 7, 1)
    assert (old.board_name, old.major, old.minor) == ("PICO_2", 7, 0)
    assert firmware.describe_firmware("PIPI_LOGIC_ANALYZER_PICO_V7_1").startswith("firmware 7.1")

    path = make_uf2(tmp_path / "PiPiLogicAnalyzer_BOARD_PICO.uf2",
                    content=b"\x00PIPI_LOGIC_ANALYZER_PICO_V7_1\x00")
    assert firmware.read_uf2(str(path)).version == "7.1"
