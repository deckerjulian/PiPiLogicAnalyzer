# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of PiPiLogicAnalyzer, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Install or update the PiPiLogicAnalyzer firmware on a Raspberry Pi Pico."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from PySide6.QtCore import QThread, QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core import firmware
from ...core.formatting import to_thousands
from ...driver import detector
from ...driver.analyzer import PiPiLogicAnalyzerDriver
from .. import messages
from ..icons import set_icon
from ..theme import set_role, set_variant
from .common import dialog_layout, hint

REFRESH_INTERVAL_MS = 1000
WAIT_FOR_ANALYZER_S = 20
QUERY_TIMEOUT_S = 3.0


def _scan(call: Callable[[], list]) -> list:
    try:
        return list(call())
    except Exception:  # noqa: BLE001 - device enumeration must never break the dialog
        return []


@dataclass(frozen=True)
class ConnectedDevice:
    """An analyzer the main window is connected to (one entry per board of a multi device set)."""

    location: str
    version: Optional[str]
    details: dict = field(default_factory=dict)

    @property
    def firmware_text(self) -> str:
        return firmware.describe_firmware(self.version, self.details)


def query_analyzer(port: str) -> tuple[Optional[str], dict]:
    """Identification and device details of an analyzer that is not connected."""
    driver = PiPiLogicAnalyzerDriver(port, connect_timeout=QUERY_TIMEOUT_S)
    try:
        return driver.device_version, driver.device_details()
    finally:
        driver.dispose()


class FlashWorker(QThread):
    done = Signal(object)  # None or the exception

    def __init__(self, image_path: str, drive: firmware.BootDrive, parent=None) -> None:
        super().__init__(parent)
        self._image_path = image_path
        self._drive = drive

    def run(self) -> None:  # noqa: D401 - QThread entry point
        try:
            firmware.flash_image(self._image_path, self._drive)
        except Exception as error:  # noqa: BLE001 - shown in the dialog
            self.done.emit(error)
            return
        self.done.emit(None)


#: Identity queries still running; they outlive a closed dialog and are dropped when finished.
_running_queries: set = set()


class IdentityWorker(QThread):
    found = Signal(str, object)  # port, firmware description or None

    def __init__(self, port: str, query: Callable[[str], tuple]) -> None:
        super().__init__()
        self._port = port
        self._query = query
        _running_queries.add(self)
        self.finished.connect(lambda: _running_queries.discard(self))

    def run(self) -> None:  # noqa: D401 - QThread entry point
        try:
            version, details = self._query(self._port)
            text: Optional[str] = firmware.describe_firmware(version, details) if version else None
        except Exception:  # noqa: BLE001 - port busy, other firmware, ...
            text = None
        self.found.emit(self._port, text)


class FirmwareDialog(QDialog):
    def __init__(
        self,
        parent: Optional[QWidget] = None,
        connected_devices: Sequence[ConnectedDevice] = (),
        restart_connected: Optional[Callable[[], bool]] = None,
        scan_boot_drives: Callable[[], list] = firmware.find_boot_drives,
        scan_foreign: Callable[[], list] = detector.detect_foreign_picos,
        scan_analyzers: Callable[[], list] = detector.detect,
        scan_images: Callable[[], list] = firmware.find_images,
        touch_reset: Callable[[str], bool] = firmware.reboot_via_serial_touch,
        query_device: Callable[[str], tuple] = query_analyzer,
    ) -> None:
        super().__init__(parent)
        self.connected_devices = list(connected_devices)
        self.restart_connected = restart_connected
        self._scan_boot_drives = scan_boot_drives
        self._scan_foreign = scan_foreign
        self._scan_analyzers = scan_analyzers
        self._scan_images = scan_images
        self._touch_reset = touch_reset
        self._query_device = query_device

        self.new_port: Optional[str] = None
        self._target_entries: list[tuple[str, str]] = []
        self._extra_images: list[firmware.Uf2Image] = []
        self._worker: Optional[FlashWorker] = None
        self._known_analyzers: set[str] = set()
        self._wait_deadline: Optional[float] = None
        # Firmware of analyzers that are not connected, by port (None: could not be read).
        self._identities: dict[str, Optional[str]] = {}
        self._queries: dict[str, IdentityWorker] = {}
        # A board in bootloader mode reports no firmware; remember what it ran before the restart.
        self._previous_firmware: Optional[str] = None

        self.setWindowTitle("Install or update firmware")
        self.resize(680, 600)
        layout = dialog_layout(self)

        intro = hint(
            "The firmware is copied onto the board while it is in bootloader mode (a drive named "
            "<b>RPI-RP2</b> or <b>RP2350</b>). Select a board below and press <i>Restart into "
            "bootloader</i>, or hold the <b>BOOTSEL</b> button while plugging the board in.",
            self,
        )
        layout.addWidget(intro)

        board_group = QGroupBox("1. Board", self)
        board_layout = QVBoxLayout(board_group)
        self.targets = QListWidget(board_group)
        self.targets.currentItemChanged.connect(lambda *_: self._update_state())
        board_layout.addWidget(self.targets)
        board_buttons = QHBoxLayout()
        self.restart_button = QPushButton("Restart into bootloader", board_group)
        set_icon(self.restart_button, "power")
        self.restart_button.clicked.connect(self.restart_selected)
        board_buttons.addWidget(self.restart_button)
        board_buttons.addStretch(1)
        board_layout.addLayout(board_buttons)
        layout.addWidget(board_group, 1)

        image_group = QGroupBox("2. Firmware image", self)
        image_layout = QVBoxLayout(image_group)
        image_row = QHBoxLayout()
        self.images_box = QComboBox(image_group)
        self.images_box.currentIndexChanged.connect(lambda *_: self._update_state())
        image_row.addWidget(self.images_box, 1)
        browse_button = QPushButton("Browse...", image_group)
        set_icon(browse_button, "folder")
        browse_button.clicked.connect(self.browse_image)
        image_row.addWidget(browse_button)
        image_layout.addLayout(image_row)
        self.image_label = QLabel(image_group)
        self.image_label.setWordWrap(True)
        image_layout.addWidget(self.image_label)
        layout.addWidget(image_group)

        self.warning_label = QLabel(self)
        self.warning_label.setWordWrap(True)
        set_role(self.warning_label, "warning")
        layout.addWidget(self.warning_label)

        self.status_label = QLabel(self)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        buttons = QDialogButtonBox(QDialogButtonBox.Close, self)
        self.flash_button = buttons.addButton("Flash firmware", QDialogButtonBox.ActionRole)
        set_variant(self.flash_button, "primary")
        set_icon(self.flash_button, "chip")
        self.flash_button.clicked.connect(self.flash_selected)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._close_button = buttons.button(QDialogButtonBox.Close)

        self.refresh_images()
        self.refresh_targets()
        self.timer = QTimer(self)
        self.timer.setInterval(REFRESH_INTERVAL_MS)
        self.timer.timeout.connect(self.refresh_targets)
        self.timer.start()

    # -------------------------------------------------------------- targets
    def _selected_target(self) -> Optional[tuple]:
        item = self.targets.currentItem()
        return None if item is None else item.data(Qt.UserRole)

    def _analyzer_firmware(self, port: str) -> str:
        """Firmware of a detected analyzer; the query runs in the background once per port."""
        if port in self._identities:
            return self._identities[port] or "firmware version could not be read (port in use?)"
        if port not in self._queries:
            worker = IdentityWorker(port, self._query_device)
            worker.found.connect(self._on_identity)
            self._queries[port] = worker
            worker.start()
        return "reading firmware version..."

    def _on_identity(self, port: str, text: Optional[str]) -> None:
        self._queries.pop(port, None)
        self._identities[port] = text
        if text and port == self.new_port:
            self.status_label.setText(f"Firmware installed. The analyzer is available on {port}: {text}.")
        self.refresh_targets()

    def refresh_targets(self) -> None:
        entries: list[tuple[str, str, tuple]] = []
        drives = _scan(self._scan_boot_drives)
        for drive in drives:
            info = ["no firmware running"]
            if drive.bootloader:
                info.append(f"UF2 bootloader {drive.bootloader}")
            if self._previous_firmware and len(drives) == 1:
                info.append(f"before the restart: {self._previous_firmware}")
            entries.append(
                (f"boot:{drive.path}",
                 f"Bootloader drive {drive.name} ({drive.chip}) - ready to flash\n    {' · '.join(info)}",
                 ("boot", drive))
            )

        connected_ports = set()
        for number, device in enumerate(self.connected_devices):
            connected_ports.add(device.location)
            if self.restart_connected is not None:
                action, target = "restart into bootloader first", ("connected", None)
            else:
                action, target = "restart it into the bootloader over USB", ("info", None)
            entries.append(
                (f"connected:{number}",
                 f"Connected analyzer on {device.location} - {action}\n    {device.firmware_text}",
                 target)
            )

        analyzers = [device for device in _scan(self._scan_analyzers) if device.port_name not in connected_ports]
        present = {device.port_name for device in analyzers}
        # Forget unplugged boards, so a board with new firmware is read again.
        for port in [port for port in self._identities if port not in present]:
            del self._identities[port]
        for device in analyzers:
            entries.append(
                (f"analyzer:{device.port_name}",
                 f"PiPiLogicAnalyzer on {device.port_name} - restart into bootloader first\n"
                 f"    {self._analyzer_firmware(device.port_name)}",
                 ("serial", device.port_name))
            )
        for board in _scan(self._scan_foreign):
            entries.append(
                (f"foreign:{board.port_name}",
                 f"Raspberry Pi board with {board.description} on {board.port_name} - restart into bootloader first",
                 ("serial", board.port_name))
            )

        keys = [key for key, _, _ in entries]
        shown = [(key, text) for key, text, _ in entries]
        if shown != self._target_entries:
            selected = self.targets.currentItem().data(Qt.UserRole + 1) if self.targets.currentItem() else None
            self.targets.blockSignals(True)
            self.targets.clear()
            for key, text, target in entries:
                item = QListWidgetItem(text)
                item.setData(Qt.UserRole, target)
                item.setData(Qt.UserRole + 1, key)
                self.targets.addItem(item)
            if not entries:
                placeholder = QListWidgetItem(
                    "No Raspberry Pi board found. Hold BOOTSEL while plugging the board in."
                )
                placeholder.setFlags(Qt.NoItemFlags)
                self.targets.addItem(placeholder)
            else:
                # Keep the selection, otherwise prefer a board that is ready to flash.
                preferred = selected if selected in keys else keys[0]
                self.targets.setCurrentRow(keys.index(preferred))
            self.targets.blockSignals(False)
            self._target_entries = shown

        self._check_new_analyzer()
        self._update_state()

    def restart_selected(self) -> None:
        target = self._selected_target()
        if target is None:
            return
        kind, value = target
        if kind == "connected" and self.restart_connected is not None:
            previous = " / ".join(device.firmware_text for device in self.connected_devices)
            restarted = self.restart_connected()
            if restarted:
                self.connected_devices = []
        elif kind == "serial":
            previous = self._identities.get(value)
            restarted = self._touch_reset(value)
        else:
            return
        if restarted:
            self._previous_firmware = previous or None
            self.status_label.setText("Restarting into the bootloader, the drive appears in a few seconds...")
        else:
            self.status_label.setText(
                "The board did not restart. Unplug it and plug it in again while holding BOOTSEL."
            )
        self.refresh_targets()

    # --------------------------------------------------------------- images
    def refresh_images(self, select_path: Optional[str] = None) -> None:
        images = _scan(self._scan_images)
        paths = {image.path for image in images}
        images += [image for image in self._extra_images if image.path not in paths]

        self.images_box.blockSignals(True)
        self.images_box.clear()
        for image in images:
            self.images_box.addItem(image.label, image)
        if not images:
            self.images_box.addItem("No firmware image found - build them or choose a file", None)
        if select_path is not None:
            for index in range(self.images_box.count()):
                image = self.images_box.itemData(index)
                if image is not None and image.path == select_path:
                    self.images_box.setCurrentIndex(index)
        self.images_box.blockSignals(False)
        self._update_state()

    def browse_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose a firmware image", "", "UF2 firmware (*.uf2)")
        if not path:
            return
        try:
            image = firmware.read_uf2(path)
        except (OSError, ValueError) as error:
            messages.warning(self, "Firmware image", "The file cannot be used.", str(error))
            return
        self._extra_images.append(image)
        self.refresh_images(select_path=image.path)

    def selected_image(self) -> Optional[firmware.Uf2Image]:
        return self.images_box.currentData()

    # ----------------------------------------------------------------- state
    def _update_state(self) -> None:
        target = self._selected_target()
        image = self.selected_image()
        flashing = self._worker is not None and self._worker.isRunning()

        if image is None:
            self.image_label.setText(
                "Build the images with firmware/build_all.sh (they are stored in firmware/uf2) "
                "or choose a .uf2 file."
            )
        else:
            parts = [
                image.board.label if image.board else "board not recognisable from the file name",
                image.version_label,
                image.chip or "unknown chip",
                f"{to_thousands(image.data_size)} bytes",
            ]
            if image.turbo:
                parts.append("Turbo: overclocked and overvolted")
            self.image_label.setText(" · ".join(parts))

        warning = ""
        compatible: Optional[bool] = None
        if target is not None and target[0] == "boot" and image is not None:
            drive = target[1]
            compatible = firmware.is_compatible(image, drive.chip)
            if compatible is False:
                warning = f"This image is built for the {image.chip}, the board in bootloader mode has an {drive.chip}."
            else:
                warning = (
                    "The bootloader cannot tell a Pico from a Pico W (or a Pico 2 from a Pico 2 W): "
                    "make sure the image matches your board."
                )
        self.warning_label.setText(warning)

        self.restart_button.setEnabled(
            not flashing and target is not None and target[0] in ("connected", "serial")
        )
        self.flash_button.setEnabled(
            not flashing and target is not None and target[0] == "boot" and image is not None and compatible is not False
        )
        self._close_button.setEnabled(not flashing)

    # -------------------------------------------------------------- flashing
    def flash_selected(self) -> None:
        target = self._selected_target()
        image = self.selected_image()
        if target is None or target[0] != "boot" or image is None:
            return
        drive = target[1]
        if not messages.confirm(
            self,
            "Flash firmware",
            f"Write {image.file_name} to the board at {drive.path}?",
            "Flash firmware",
            "The firmware currently on the board is replaced.",
        ):
            return

        self._known_analyzers = {device.port_name for device in _scan(self._scan_analyzers)}
        self.status_label.setText("Writing the firmware...")
        self._worker = FlashWorker(image.path, drive, self)
        self._worker.done.connect(self._on_flashed)
        self._worker.start()
        self._update_state()

    def _on_flashed(self, error: object) -> None:
        if error is not None:
            self.status_label.setText(f"Flashing failed: {error}")
            self._update_state()
            return
        self._previous_firmware = None
        self.status_label.setText("Firmware written, waiting for the analyzer to start...")
        self._wait_deadline = time.monotonic() + WAIT_FOR_ANALYZER_S
        self._update_state()

    def _check_new_analyzer(self) -> None:
        if self._wait_deadline is None:
            return
        new = [
            device.port_name
            for device in _scan(self._scan_analyzers)
            if device.port_name not in self._known_analyzers
        ]
        if new:
            self.new_port = new[0]
            self._wait_deadline = None
            text = self._identities.get(self.new_port)
            self.status_label.setText(
                f"Firmware installed. The analyzer is available on {self.new_port}"
                + (f": {text}." if text else ".")
            )
        elif time.monotonic() > self._wait_deadline:
            self._wait_deadline = None
            self.status_label.setText(
                "The firmware was written, but no analyzer appeared within "
                f"{WAIT_FOR_ANALYZER_S} s. Reconnect the board and check that the image matches it."
            )

    def done(self, result: int) -> None:  # noqa: D401 - QDialog override
        if self._worker is not None and self._worker.isRunning():
            return  # never close while writing
        self.timer.stop()
        for worker in self._queries.values():
            try:
                worker.found.disconnect(self._on_identity)
            except (RuntimeError, TypeError):
                pass
        self._queries.clear()
        super().done(result)
