# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The hardware connected to this computer, with the firmware on it, and installing the right one.

:func:`scan` lists every board and device: analyzers with their firmware (open in the hub or
not), boards in bootloader mode, Raspberry Pi boards with other firmware, other devices (DSLogic,
...). :class:`FirmwareInstaller` installs the image that fits a board: it restarts the board into
its bootloader, waits for the drive, writes the image and waits until the board is back.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from PySide6.QtCore import QObject, QThread, QTimer, Signal

from ...core import firmware
from ...core.hub import Hub
from ...core.instrument import Instrument

ANALYZER = "analyzer"
BOOTLOADER = "bootloader"
FOREIGN = "foreign"
DEVICE = "device"

#: seconds to wait for the bootloader drive, and for the board after writing
WAIT_FOR_DRIVE_S = 15.0
WAIT_FOR_BOARD_S = 20.0
POLL_MS = 250


@dataclass
class HardwareEntry:
    """One board or device connected to the computer."""

    key: str
    kind: str
    title: str
    connection: str
    firmware: str = ""
    #: "current", "update", "unknown", "none" (no firmware of this application), "" (not applicable)
    state: str = ""
    board: Optional[firmware.BoardModel] = None
    chip: Optional[str] = None
    port: Optional[str] = None
    drive: Optional[firmware.BootDrive] = None
    #: the device list entry that opens it
    entry: object = None
    #: the instrument of the hub when it is open
    instrument: Optional[Instrument] = None
    #: the image this application would install
    image: Optional[firmware.Uf2Image] = None
    notes: list[str] = field(default_factory=list)
    #: how the firmware of an open device that is no UF2 board is updated (``firmware.UpdateMethod``)
    method: Optional[firmware.UpdateMethod] = None

    @property
    def can_install(self) -> bool:
        return self.kind in (ANALYZER, BOOTLOADER, FOREIGN)


def _safe(call: Callable[[], list]) -> list:
    try:
        return list(call())
    except Exception:  # noqa: BLE001 - enumeration must never break the overview
        return []


def scan(hub: Optional[Hub], versions: dict[str, Optional[str]],
         find_drives: Callable[[], list] = None, detect: Callable[[], list] = None,
         detect_foreign: Callable[[], list] = None, images: Optional[list] = None,
         other_entries: Callable[[], list] = None) -> list[HardwareEntry]:
    """Everything connected. ``versions``: firmware of analyzers that are not open, by port (read
    in the background; a missing port is still being read)."""
    from ...driver.pico import detector
    from .. import devices
    from . import pico as pico_devices

    find_drives = find_drives or firmware.find_boot_drives
    detect = detect or detector.detect
    detect_foreign = detect_foreign or detector.detect_foreign_picos
    images = images if images is not None else _safe(firmware.find_images)
    entries: list[HardwareEntry] = []
    open_ports: dict[str, Instrument] = {}

    for instrument in (hub.instruments() if hub is not None else []):
        if getattr(instrument, "simulated_driver", None) is not None or instrument.capture is None \
                or instrument.is_simulated:
            continue  # (simulated devices are no hardware)
        driver = instrument.capture.driver
        if not driver.is_hardware:
            continue
        port = instrument.uri.split(":", 1)[1] if instrument.uri.startswith("pico:") else None
        if port:
            open_ports[port] = instrument
        version = driver.device_version
        state, image = firmware.firmware_state(version, images) if driver.supports_bootloader else ("", None)
        identity = firmware.parse_device_version(version)
        method = firmware.update_method(driver)
        entries.append(HardwareEntry(
            method=method if method is not None and method.key != "uf2" else None,
            key=f"open:{instrument.name}", kind=ANALYZER if driver.supports_bootloader else DEVICE,
            title=instrument.name, connection=instrument.uri or "-", firmware=firmware.describe_firmware(version)
            if driver.supports_bootloader else (version or ""), state=state,
            board=identity.board if identity else None, port=port, instrument=instrument, image=image,
            chip=identity.board.chip if identity and identity.board else None))

    for device in _safe(detect):
        if device.port_name in open_ports:
            continue
        version = versions.get(device.port_name)
        identity = firmware.parse_device_version(version)
        state, image = firmware.firmware_state(version, images) if version else ("", None)
        reading = device.port_name not in versions
        entries.append(HardwareEntry(
            key=f"analyzer:{device.port_name}", kind=ANALYZER, title="openSciLab Pico",
            connection=device.port_name + (f" · {device.serial_number}" if device.serial_number else ""),
            firmware="reading..." if reading else (firmware.describe_firmware(version) if version
                                                   else "could not be read (port in use?)"),
            state=state, board=identity.board if identity else None, port=device.port_name,
            chip=identity.board.chip if identity and identity.board else None,
            entry=pico_devices.serial_entry(device.port_name), image=image))

    for drive in _safe(find_drives):
        fitting = firmware.images_for(images, chip=drive.chip)
        entries.append(HardwareEntry(
            key=f"boot:{drive.path}", kind=BOOTLOADER, title=f"Board in bootloader mode ({drive.chip})",
            connection=drive.path, firmware="no firmware running", state="none", chip=drive.chip, drive=drive,
            image=fitting[0] if fitting else None,
            notes=[f"UF2 bootloader {drive.bootloader}"] if drive.bootloader else []))

    for board in _safe(detect_foreign):
        entries.append(HardwareEntry(
            key=f"foreign:{board.port_name}", kind=FOREIGN, title="Raspberry Pi board",
            connection=board.port_name, firmware=board.description, state="none", port=board.port_name))

    for entry in _safe(other_entries or (lambda: [found for backend in devices.backends()
                                                  if backend.id not in ("pico", "sim")
                                                  for found in backend.detected()])):
        entries.append(HardwareEntry(key=f"device:{entry.backend}:{entry.value}", kind=DEVICE, title=entry.label,
                                     connection=f"{entry.backend}:{entry.value}" if entry.value else entry.backend,
                                     entry=entry))
    return entries


class _FlashWorker(QThread):
    done = Signal(object)

    def __init__(self, flash: Callable, image_path: str, drive, parent=None) -> None:
        super().__init__(parent)
        self.flash, self.image_path, self.drive = flash, image_path, drive

    def run(self) -> None:  # noqa: D401 - QThread entry point
        try:
            self.flash(self.image_path, self.drive)
        except Exception as error:  # noqa: BLE001 - reported to the user
            self.done.emit(error)
            return
        self.done.emit(None)


class FirmwareInstaller(QObject):
    """Installs an image on a board: bootloader, write, wait until it is back (see the module)."""

    #: a step of the installation, for people
    progress = Signal(str)
    #: (success, message)
    finished = Signal(bool, str)

    def __init__(self, parent: Optional[QObject] = None, find_drives: Callable[[], list] = None,
                 touch_reset: Callable[[str], bool] = None, flash: Callable = None,
                 detect: Callable[[], list] = None, clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__(parent)
        from ...driver.pico import detector

        self._find_drives = find_drives or firmware.find_boot_drives
        self._touch_reset = touch_reset or firmware.reboot_via_serial_touch
        self._flash = flash or firmware.flash_image
        self._detect = detect or detector.detect
        self._clock = clock
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._poll)
        self._step = ""
        self._deadline = 0.0
        self._image: Optional[firmware.Uf2Image] = None
        self._drives_before: set[str] = set()
        self._ports_before: set[str] = set()
        self._worker: Optional[_FlashWorker] = None
        self.new_port: Optional[str] = None

    @property
    def busy(self) -> bool:
        return bool(self._step)

    def install(self, entry: HardwareEntry, image: firmware.Uf2Image,
                restart_open: Optional[Callable[[], bool]] = None) -> bool:
        """Start installing ``image`` on ``entry``. ``restart_open`` restarts an open instrument
        into its bootloader (and closes it)."""
        if self.busy:
            return False
        if entry.chip and firmware.is_compatible(image, entry.chip) is False:
            self.finished.emit(False, f"{image.file_name} is built for the {image.chip}, the board has an {entry.chip}.")
            return False
        self._image = image
        self.new_port = None
        if entry.kind == BOOTLOADER and entry.drive is not None:
            self._write(entry.drive)
            return True
        self._drives_before = {drive.path for drive in _safe(self._find_drives)}
        if entry.instrument is not None and restart_open is not None:
            restarted = restart_open()
        elif entry.port:
            restarted = self._touch_reset(entry.port)
        else:
            restarted = False
        if not restarted:
            self.finished.emit(False, "The board did not restart into its bootloader. Unplug it and plug it in "
                                      "again while holding BOOTSEL, then install on the board in bootloader mode.")
            return False
        self._step = "drive"
        self._deadline = self._clock() + WAIT_FOR_DRIVE_S
        self.progress.emit("Restarting into the bootloader...")
        self._timer.start()
        return True

    def _write(self, drive) -> None:
        # the board is in its bootloader now: its serial port is gone and comes back after writing
        self._ports_before = {device.port_name for device in _safe(self._detect)}
        self._step = "flash"
        self.progress.emit(f"Writing {self._image.file_name}...")
        self._worker = _FlashWorker(self._flash, self._image.path, drive, self)
        self._worker.done.connect(self._written)
        self._worker.start()

    def _written(self, error) -> None:
        if error is not None:
            self._end(False, f"Writing the firmware failed: {error}")
            return
        self._step = "board"
        self._deadline = self._clock() + WAIT_FOR_BOARD_S
        self.progress.emit("Firmware written, waiting for the board to start...")
        self._timer.start()

    def _poll(self) -> None:
        if self._step == "drive":
            drives = [drive for drive in _safe(self._find_drives) if drive.path not in self._drives_before]
            if drives:
                self._timer.stop()
                self._write(drives[0])
            elif self._clock() > self._deadline:
                self._end(False, "No bootloader drive appeared. Hold BOOTSEL while plugging the board in and "
                                 "install on the board in bootloader mode.")
        elif self._step == "board":
            ports = [device.port_name for device in _safe(self._detect) if device.port_name not in self._ports_before]
            if ports:
                self.new_port = ports[0]
                self._end(True, f"Firmware {self._image.version or ''} installed; the board is on {ports[0]}.".replace(
                    "  ", " "))
            elif self._clock() > self._deadline:
                self._end(True, "The firmware was written, but the board did not appear yet. Reconnect it if it "
                                "does not show up.")

    def _end(self, success: bool, message: str) -> None:
        self._timer.stop()
        self._step = ""
        self.finished.emit(success, message)
