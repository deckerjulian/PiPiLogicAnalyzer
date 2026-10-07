# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Connected hardware: every board and device on this computer, its firmware, and installing the
firmware that fits it.

One row per board or device (analyzers with their firmware and whether it is current, boards in
bootloader mode, Raspberry Pi boards with other firmware, other devices). *Install firmware* /
*Update firmware* picks the image of this application that fits the board, restarts it into its
bootloader, writes the image and waits until the board is back; *Choose an image…* is the manual
way (any image, any board).
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core import firmware
from ...core.hub import EVENT_ADDED, EVENT_REMOVED, Hub
from .. import messages
from ..devices import hardware
from ..devices.hardware import HardwareEntry
from ..devices.hub_bridge import HubBridge
from ..icons import set_icon
from ..theme import TEXT_MUTED, set_role, set_variant, token
from .base import DocumentWidget

log = logging.getLogger(__name__)

#: how the firmware gets onto a board (the guide above the list)
GUIDE = (
    "<table cellspacing='0' cellpadding='2'>"
    "<tr><td><b>1.</b>&nbsp;</td><td><b>Plug the board in.</b> A Pico without openSciLab firmware: hold its "
    "<b>BOOTSEL</b> button while plugging it in - it then shows as <i>Board in bootloader mode</i>.</td></tr>"
    "<tr><td><b>2.</b>&nbsp;</td><td><b>Find it in the list.</b> <i>State</i> says whether its firmware is up to "
    "date, has an update or is missing.</td></tr>"
    "<tr><td><b>3.</b>&nbsp;</td><td><b>Click <i>Install firmware</i> or <i>Update firmware</i> in its row.</b> "
    "openSciLab picks the image that fits the board, restarts it into its bootloader, writes the image and "
    "connects the board again. Nothing else is needed.</td></tr></table>"
    f"<span style='color:{TEXT_MUTED}'>Arduino boards are flashed with PlatformIO or avrdude, the bridge app of an "
    "oscilloscope with adb (see the Firmware page of the wiki). "
    "<i>Choose an image...</i> writes any .uf2 file to any board by hand.</span>"
)

REFRESH_MS = 2000
COLUMNS = ["Device", "Connection", "Firmware", "State", ""]
STATES = {
    "current": ("up to date", "device.connected"),
    "update": ("update available", "device.busy"),
    "unknown": ("board not recognised", "device.disconnected"),
    "none": ("no openSciLab firmware", "device.busy"),
}


#: version queries still running; they outlive a closed overview and are dropped when finished
_running_workers: set = set()


class _VersionWorker(QThread):
    """Reads the firmware version of an analyzer that is not open (once per port)."""

    found = Signal(str, object)

    def __init__(self, port: str, query: Callable[[str], tuple], parent=None) -> None:
        # no parent: a query still running when the overview closes ends by itself (a port that
        # does not answer takes seconds); destroying a running thread would end the application
        super().__init__()
        self.port, self.query = port, query
        _running_workers.add(self)
        self.finished.connect(lambda: _running_workers.discard(self))

    def run(self) -> None:  # noqa: D401 - QThread entry point
        try:
            version, _details = self.query(self.port)
        except Exception:  # noqa: BLE001 - shown as "could not be read"
            log.debug("version, _details = self.query(self.port) failed: shown as 'could not be read'", exc_info=True)
            version = None
        self.found.emit(self.port, version)


class HardwareDocument(DocumentWidget):
    """The connected hardware (see the module documentation)."""

    document_kind = "hardware"
    #: open a device of the list (a device list entry)
    open_requested = Signal(object)
    #: the device card of an open instrument
    card_requested = Signal(object)
    #: *Update firmware* of an open instrument that is no UF2 board (its update tool)
    firmware_requested = Signal(object)

    def __init__(self, hub: Hub, parent: Optional[QWidget] = None, scan: Optional[Callable] = None,
                 query: Optional[Callable[[str], tuple]] = None, installer: Optional[hardware.FirmwareInstaller] = None,
                 images: Optional[Callable[[], list]] = None) -> None:
        super().__init__(parent)
        from ..dialogs.firmware_dialog import query_analyzer

        self.hub = hub
        self._scan = scan or hardware.scan
        self._query = query or query_analyzer
        self._images = images or (lambda: hardware._safe(firmware.find_images))
        self.versions: dict[str, Optional[str]] = {}
        self._workers: dict[str, _VersionWorker] = {}
        self.entries: list[HardwareEntry] = []
        #: the table shows ``entries`` (False until the first scan, and while rows must be rebuilt)
        self._filled = False
        self.installer = installer or hardware.FirmwareInstaller(self)
        self.installer.setParent(self)
        self.installer.progress.connect(self._progress)
        self.installer.finished.connect(self._installed)
        self.bridge = HubBridge(hub, self)
        # devices coming and going change the overview; a status change of one does not
        self.bridge.changed.connect(lambda event: self.refresh() if getattr(event, "kind", "") in (
            EVENT_ADDED, EVENT_REMOVED) else None)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 14, 20, 10)
        layout.setSpacing(10)
        header = QHBoxLayout()
        title = QLabel("Connected hardware", self)
        set_role(title, "title")
        header.addWidget(title)
        header.addStretch(1)
        self.manual_button = QPushButton("Choose an image...", self)
        set_icon(self.manual_button, "folder")
        self.manual_button.setToolTip("Write any firmware image to any board (the firmware dialog)")
        self.manual_button.clicked.connect(self.manual_install)
        header.addWidget(self.manual_button)
        self.refresh_button = QPushButton("Refresh", self)
        set_icon(self.refresh_button, "refresh")
        self.refresh_button.clicked.connect(self.refresh)
        header.addWidget(self.refresh_button)
        layout.addLayout(header)
        hint = QLabel("Every board and device on this computer with the firmware on it.", self)
        hint.setWordWrap(True)
        set_role(hint, "hint")
        layout.addWidget(hint)
        # how the firmware gets onto a board: the steps, always in sight
        self.guide = QFrame(self)
        set_role(self.guide, "card")
        guide_layout = QVBoxLayout(self.guide)
        guide_layout.setContentsMargins(14, 10, 14, 10)
        guide_layout.setSpacing(4)
        guide_title = QLabel("Firmware on a board", self.guide)
        set_role(guide_title, "heading")
        guide_layout.addWidget(guide_title)
        self.guide_label = QLabel(GUIDE, self.guide)
        self.guide_label.setTextFormat(Qt.RichText)
        self.guide_label.setWordWrap(True)
        guide_layout.addWidget(self.guide_label)
        layout.addWidget(self.guide)

        self.table = QTableWidget(0, len(COLUMNS), self)
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.cellDoubleClicked.connect(self._double_clicked)
        layout.addWidget(self.table, 1)
        self.status_label = QLabel(self)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.timer = QTimer(self)
        self.timer.setInterval(REFRESH_MS)
        self.timer.timeout.connect(self.refresh)
        self.refresh()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt naming
        # looks for hardware only while it is shown (a tab in the background costs nothing)
        super().showEvent(event)
        self.refresh()
        self.timer.start()

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().hideEvent(event)
        if not self.installer.busy:
            self.timer.stop()

    @property
    def title(self) -> str:
        return "Connected hardware"

    # ------------------------------------------------------------- scanning
    def refresh(self) -> None:
        images = self._images()
        entries = self._scan(self.hub, self.versions, images=images)
        present = {entry.port for entry in entries if entry.port}
        for port in [port for port in self.versions if port not in present]:
            del self.versions[port]  # unplugged: read again when it is back
        for entry in entries:
            if entry.kind == hardware.ANALYZER and entry.instrument is None and entry.port \
                    and entry.port not in self.versions and entry.port not in self._workers:
                worker = _VersionWorker(entry.port, self._query, self)
                worker.found.connect(self._version_found)
                self._workers[entry.port] = worker
                worker.start()
        if self._filled and [entry.key for entry in entries] == [entry.key for entry in self.entries] and \
                [(entry.firmware, entry.state) for entry in entries] == \
                [(entry.firmware, entry.state) for entry in self.entries]:
            self.entries = entries
            return
        self.entries = entries
        self._filled = True
        self._fill()

    def _version_found(self, port: str, version) -> None:
        self._workers.pop(port, None)
        self.versions[port] = version
        self.refresh()

    def _fill(self) -> None:
        self.table.setRowCount(len(self.entries))
        for row, entry in enumerate(self.entries):
            text, color = STATES.get(entry.state, ("", ""))
            if entry.state == "update" and entry.image is not None:
                text = f"update to {entry.image.version}"
            values = [entry.title, entry.connection, entry.firmware + ("  ·  " + "  ·  ".join(entry.notes)
                                                                       if entry.notes else ""), text]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 3 and color:
                    item.setForeground(_color(color))
                if column == 0 and (entry.instrument is not None or entry.entry is not None):
                    item.setToolTip("Double-click to open the device card" if entry.instrument is not None
                                    else "Double-click to connect")
                self.table.setItem(row, column, item)
            self.table.setCellWidget(row, 4, self._actions(entry))
        if not self.entries:
            self.table.setRowCount(1)
            item = QTableWidgetItem("No board or device found. Plug one in - a Pico without firmware with its "
                                    "BOOTSEL button held. Simulated devices are not listed here.")
            item.setForeground(_color_muted())
            self.table.setItem(0, 0, item)
            self.table.setSpan(0, 0, 1, len(COLUMNS))
        else:
            self.table.clearSpans()
        header = self.table.horizontalHeader()
        for column in range(len(COLUMNS)):  # sized by contents and header, also under the placeholder
            header.setSectionResizeMode(column, QHeaderView.Stretch if column == 2 else QHeaderView.ResizeToContents)
        self.table.resizeRowsToContents()

    def _double_clicked(self, row: int, _column: int) -> None:
        if not 0 <= row < len(self.entries):
            return
        entry = self.entries[row]
        if entry.instrument is not None:
            self.card_requested.emit(entry.instrument)
        elif entry.entry is not None:
            self.open_requested.emit(entry.entry)

    def _actions(self, entry: HardwareEntry) -> QWidget:
        widget = QWidget(self.table)
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(2, 0, 2, 0)
        layout.setSpacing(4)
        busy = self.installer.busy
        if entry.instrument is not None:
            button = QPushButton("Device card", widget)
            set_icon(button, "devices")
            button.clicked.connect(lambda _checked=False, e=entry: self.card_requested.emit(e.instrument))
            layout.addWidget(button)
        elif entry.entry is not None:
            button = QPushButton("Open", widget)
            set_icon(button, "plug")
            button.setEnabled(entry.state not in ("none",) and not busy)
            button.clicked.connect(lambda _checked=False, e=entry: self.open_requested.emit(e.entry))
            layout.addWidget(button)
        if entry.can_install:
            label = "Update firmware" if entry.state == "update" else (
                "Install firmware" if entry.state in ("none", "unknown") else "Reinstall firmware")
            button = QPushButton(label, widget)
            button.setObjectName(f"install-{entry.key}")
            set_icon(button, "chip")
            if entry.state in ("update", "none"):
                set_variant(button, "primary")
            button.setToolTip("Writes the firmware image of this openSciLab that fits the board: restarts the "
                              "board into its bootloader, writes the image, connects it again")
            button.setEnabled(not busy)
            button.clicked.connect(lambda _checked=False, e=entry: self.install(e))
            layout.addWidget(button)
        elif entry.method is not None and entry.instrument is not None:
            button = QPushButton("Update firmware...", widget)
            button.setObjectName(f"update-{entry.key}")
            set_icon(button, "chip")
            button.setToolTip(f"Choose a firmware image and write it: {entry.method.title}")
            button.setEnabled(not busy)
            button.clicked.connect(lambda _checked=False, e=entry: self.firmware_requested.emit(e.instrument))
            layout.addWidget(button)
        elif entry.instrument is not None:
            note = QLabel("no firmware to install", widget)
            note.setToolTip("openSciLab has no firmware for this device; it is used as it is")
            set_role(note, "hint")
            layout.addWidget(note)
        layout.addStretch(1)
        return widget

    # ------------------------------------------------------------ installing
    def image_for(self, entry: HardwareEntry, ask: bool = True) -> Optional[firmware.Uf2Image]:
        """The image to install on ``entry``: the one for its board, else one for its chip (asked when
        there are several)."""
        images = self._images()
        if entry.board is not None:
            image = firmware.matching_image(images, entry.board)
            if image is not None:
                return image
        candidates = firmware.images_for(images, chip=entry.chip)
        if not candidates:
            return None
        if len(candidates) == 1 or not ask:
            return candidates[0]
        choice = messages.choose(self, "Install firmware",
                                 f"Which board is it? ({entry.chip or 'the chip is known in bootloader mode'})",
                                 [image.board.label + (" (Turbo)" if image.turbo else "") if image.board
                                  else image.file_name for image in candidates[:6]],
                                 "The bootloader cannot tell a Pico from a Pico W (or a Pico 2 from a Pico 2 W).")
        return None if choice is None else candidates[choice]

    def install(self, entry: HardwareEntry, restart_open: Optional[Callable[[], bool]] = None) -> bool:
        if self.installer.busy:
            return False
        image = self.image_for(entry)
        if image is None:
            messages.warning(self, "Install firmware", "There is no firmware image for this board.",
                             "Build them with firmware/build_all.sh (the packaged application includes them), or "
                             "use Choose an image… for a .uf2 file.")
            return False
        version = f" {image.version}" if image.version else ""
        if not messages.confirm(self, "Install firmware",
                                f"Install firmware{version} for the {image.board.label if image.board else 'board'} "
                                f"on {entry.connection}?", "Install",
                                f"{image.file_name}. The firmware on the board is replaced; the board restarts."):
            return False
        if entry.instrument is not None:
            driver = entry.instrument.capture.driver if entry.instrument.capture is not None else None
            if driver is not None and driver.is_capturing:
                messages.info(self, "Install firmware", f"{entry.instrument.name} is capturing; stop the capture first.")
                return False
            if restart_open is None:
                restart_open = self._restart_open(entry)
        #: an open device is opened again once its board is back with the new firmware
        self._reopen = entry.instrument is not None
        started = self.installer.install(entry, image, restart_open=restart_open)
        self._fill()
        return started

    def _restart_open(self, entry: HardwareEntry) -> Callable[[], bool]:
        def restart() -> bool:
            instrument = entry.instrument
            driver = instrument.capture.driver
            if driver.is_capturing or not driver.enter_bootloader():
                return False
            if instrument in self.hub:
                self.hub.remove(self.hub.name_of(instrument))
            return True

        return restart

    def install_for(self, instrument) -> bool:
        """*Update firmware* of an open instrument (from its device card)."""
        self.refresh()
        entry = next((entry for entry in self.entries if entry.instrument is instrument), None)
        if entry is None or not entry.can_install:
            messages.info(self, "Update firmware", f"{instrument.name} has no firmware this overview installs.")
            return False
        return self.install(entry)

    def _progress(self, text: str) -> None:
        self.status_label.setStyleSheet("")
        self.status_label.setText(text)

    def _installed(self, success: bool, message: str) -> None:
        self.status_label.setStyleSheet(f"color: {token('device.connected' if success else 'device.error')}")
        self.status_label.setText(message)
        self.versions.clear()  # read the firmware again
        self._filled = False  # the buttons were disabled while installing
        self.refresh()
        if success and getattr(self, "_reopen", False) and self.installer.new_port:
            from ..devices import pico as pico_devices

            self.open_requested.emit(pico_devices.serial_entry(self.installer.new_port))
        self._reopen = False

    def manual_install(self) -> None:
        """*Choose an image…*: the manual dialog. An open analyzer is released before its board
        restarts; the dialog never restarts the port of an open device under the application."""
        from ...driver.pico import detector
        from ..dialogs.firmware_dialog import FirmwareDialog, update_firmware

        open_entries = [entry for entry in self.entries if entry.instrument is not None and entry.kind == hardware.ANALYZER]
        if len(open_entries) == 1:
            instrument = open_entries[0].instrument

            def release() -> None:
                if instrument in self.hub:
                    self.hub.remove(self.hub.name_of(instrument))

            update_firmware(self, instrument, release)
        else:
            busy = {entry.port for entry in self.entries if entry.instrument is not None and entry.port}
            FirmwareDialog(self, scan_analyzers=lambda: [device for device in detector.detect()
                                                         if device.port_name not in busy]).exec()
        self.versions.clear()
        self._filled = False
        self.refresh()

    def shutdown(self) -> None:
        self.timer.stop()
        for worker in list(self._workers.values()):
            # still reading a version: it ends by itself and tells nobody (see _VersionWorker)
            try:
                worker.found.disconnect(self._version_found)
            except (RuntimeError, TypeError):
                pass
        self._workers.clear()
        self.bridge.close()


def _color(name: str):
    from PySide6.QtGui import QColor

    return QColor(token(name))


def _color_muted():
    from PySide6.QtGui import QColor

    return QColor(TEXT_MUTED)

