# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of PiPiLogicAnalyzer 7, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Small device related dialogs.

Ports of ``NetworkDialog``, ``NetworkSettingsDialog``, ``MultiConnectDialog``,
``AnalyzerInfoDialog`` and ``AboutDialog``.
"""

from __future__ import annotations

import ipaddress
from typing import Optional, Sequence

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHeaderView,
    QLabel,
    QLineEdit,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ... import __version__
from ...core.device_info import as_text, describe_device
from ...core.formatting import to_thousands
from ...driver.base import AnalyzerDriverBase
from ...driver.detector import DetectedDevice, UsbPortInfo
from ..icons import set_icon
from ..theme import ACCENT_HOVER, set_role
from .common import InlineMessage, button_box, dialog_layout, heading, hint


def _valid_ipv4(address: str) -> bool:
    try:
        ipaddress.IPv4Address(address)
    except ValueError:
        return False
    return True


class NetworkConnectDialog(QDialog):
    """Asks for the address and port of a networked analyzer."""

    def __init__(self, address: str = "", port: int = 24000, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Connect to a network analyzer")
        self.setMinimumWidth(420)

        layout = dialog_layout(self)
        layout.addWidget(
            hint("Enter the address and port configured in the network settings of the analyzer.", self)
        )

        form = QFormLayout()
        self.address_edit = QLineEdit(address, self)
        self.address_edit.setPlaceholderText("192.168.1.50")
        form.addRow("Address:", self.address_edit)

        self.port_box = QSpinBox(self)
        self.port_box.setRange(1, 65535)
        self.port_box.setValue(port)
        form.addRow("Port:", self.port_box)
        layout.addLayout(form)

        self.message = InlineMessage(self)
        layout.addWidget(self.message)
        self.address_edit.textChanged.connect(lambda _text: self.message.clear())

        buttons = button_box(self, "Connect", icon="plug")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @property
    def address(self) -> str:
        return self.address_edit.text().strip()

    @property
    def port(self) -> int:
        return self.port_box.value()

    def _accept(self) -> None:
        if not _valid_ipv4(self.address):
            self.message.show_error("Enter a valid IPv4 address, for example 192.168.1.50.")
            self.address_edit.setFocus()
            return
        self.accept()


class NetworkSettingsDialog(QDialog):
    """Configures the WiFi settings stored in the device."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Network settings")
        self.setMinimumWidth(440)

        layout = dialog_layout(self)
        layout.addWidget(
            hint(
                "The settings are stored on the analyzer. It joins the access point and listens "
                "on this address the next time it starts.",
                self,
            )
        )

        form = QFormLayout()
        self.ap_edit = QLineEdit(self)
        self.ap_edit.setMaxLength(32)
        self.ap_edit.setPlaceholderText("Network name (SSID)")
        form.addRow("Access point:", self.ap_edit)

        self.password_edit = QLineEdit(self)
        self.password_edit.setMaxLength(63)
        self.password_edit.setEchoMode(QLineEdit.Password)
        form.addRow("Password:", self.password_edit)

        self.address_edit = QLineEdit(self)
        self.address_edit.setMaxLength(15)
        self.address_edit.setPlaceholderText("192.168.1.50")
        form.addRow("Device address:", self.address_edit)

        self.port_box = QSpinBox(self)
        self.port_box.setRange(1, 65535)
        self.port_box.setValue(24000)
        form.addRow("Port:", self.port_box)
        layout.addLayout(form)

        self.message = InlineMessage(self)
        layout.addWidget(self.message)

        buttons = button_box(self, "Save to device", icon="save")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @property
    def access_point(self) -> str:
        return self.ap_edit.text()

    @property
    def password(self) -> str:
        return self.password_edit.text()

    @property
    def address(self) -> str:
        return self.address_edit.text().strip()

    @property
    def port(self) -> int:
        return self.port_box.value()

    def _accept(self) -> None:
        if not self.access_point:
            self.message.show_error("The name of the access point is required.")
            self.ap_edit.setFocus()
            return
        if not _valid_ipv4(self.address):
            self.message.show_error("Enter a valid IPv4 address for the device, for example 192.168.1.50.")
            self.address_edit.setFocus()
            return
        self.accept()


def _read_only_table(headers: Sequence[str], rows: Sequence[Sequence[str]], parent: QWidget) -> QTableWidget:
    table = QTableWidget(len(rows), len(headers), parent)
    table.setHorizontalHeaderLabels(list(headers))
    table.setEditTriggers(QTableWidget.NoEditTriggers)
    table.setSelectionMode(QTableWidget.NoSelection)
    table.setAlternatingRowColors(True)
    table.setShowGrid(False)
    table.verticalHeader().setVisible(False)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
    table.horizontalHeader().setStretchLastSection(True)
    table.horizontalHeader().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    for row, values in enumerate(rows):
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            item.setToolTip(value)
            table.setItem(row, column, item)
    return table


class MultiConnectDialog(QDialog):
    """Picks the serial ports of a cascaded (multi device) analyzer.

    Identical boards differ only by their USB serial number (the unique board ID) and the USB
    socket they are plugged into, so both are shown next to every port.
    """

    def __init__(self, ports: Sequence[UsbPortInfo | str], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Connect multiple devices")
        self.resize(660, 520)

        self.ports = [
            port if isinstance(port, UsbPortInfo) else UsbPortInfo(port, None, None, None, None, None, None, None)
            for port in ports
        ]

        layout = dialog_layout(self)
        layout.addWidget(
            hint(
                "Cascade 2 to 5 analyzers into one multi device set. The first device is the "
                "master, the boards are aligned to it. The board of the trigger channel evaluates "
                "the trigger and starts the other devices through the trigger line.",
                self,
            )
        )

        ports_group = QGroupBox("Available ports", self)
        ports_layout = QVBoxLayout(ports_group)
        self.port_table = _read_only_table(
            ["Port", "Device", "Serial number", "USB location"],
            [
                (port.port_name, port.device_name or "-", port.serial_number or "-", port.location or "-")
                for port in self.ports
            ],
            ports_group,
        )
        ports_layout.addWidget(self.port_table)
        layout.addWidget(ports_group, 1)

        order_group = QGroupBox("Device order", self)
        form = QFormLayout(order_group)
        self.combos: list[QComboBox] = []
        for index in range(5):
            combo = QComboBox(order_group)
            combo.addItem("Not used", None)
            for port in self.ports:
                combo.addItem(port.label, port.port_name)
                if port.location:
                    combo.setItemData(combo.count() - 1, f"USB location {port.location}", Qt.ToolTipRole)
            combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
            combo.currentIndexChanged.connect(lambda *_: self.message.clear())
            form.addRow(f"Device {index + 1}" + (" (master):" if index == 0 else ":"), combo)
            self.combos.append(combo)
        layout.addWidget(order_group)

        self.message = InlineMessage(self)
        layout.addWidget(self.message)

        buttons = button_box(self, "Connect", icon="plug")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.connection_strings: list[str] = []

    def _accept(self) -> None:
        selected = [combo.currentData() for combo in self.combos if combo.currentData()]
        if len(selected) < 2:
            self.message.show_error("Select at least two devices.")
            return
        if len(set(selected)) != len(selected):
            self.message.show_error("Each device must use a different port.")
            return
        self.connection_strings = selected
        self.accept()


class MultiComposeDialog(QDialog):
    """Assigns an order to the detected devices of a multi device analyzer."""

    def __init__(self, devices: Sequence[DetectedDevice], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Multi device set")
        self.resize(660, 340)
        self.devices = list(devices)
        self.ordered_devices: list[DetectedDevice] = []

        layout = dialog_layout(self)
        layout.addWidget(
            hint(
                "Assign a position to every detected analyzer; device 1 is the master. The serial "
                "number is the unique board ID, the USB location the socket the board is plugged "
                "into. The order is remembered for these boards.",
                self,
            )
        )

        self.table = _read_only_table(
            ["Port", "Serial number", "USB location", "Position"],
            [
                (device.port_name, device.serial_number or "-", device.parent_id or "-", "")
                for device in self.devices
            ],
            self,
        )
        self.combos: list[QComboBox] = []
        for row in range(len(self.devices)):
            combo = QComboBox(self)
            for position in range(len(self.devices)):
                combo.addItem(f"Device {position + 1}" + (" (master)" if position == 0 else ""), position)
            combo.setCurrentIndex(row)
            combo.currentIndexChanged.connect(lambda *_: self.message.clear())
            self.table.setCellWidget(row, 3, combo)
            self.combos.append(combo)
        layout.addWidget(self.table, 1)

        self.message = InlineMessage(self)
        layout.addWidget(self.message)

        buttons = button_box(self, "Connect", icon="plug")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _accept(self) -> None:
        positions = [combo.currentData() for combo in self.combos]
        if len(set(positions)) != len(positions):
            self.message.show_error("Every device needs a different position.")
            return
        self.ordered_devices = [
            device for _, device in sorted(zip(positions, self.devices), key=lambda item: item[0])
        ]
        self.accept()


class DeviceInfoDialog(QDialog):
    """Board, firmware, USB connection and capture limits of the connected device."""

    def __init__(self, driver: AnalyzerDriverBase, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Device information")
        self.resize(680, 660)

        self.sections = describe_device(driver)
        info = driver.get_device_info()
        layout = dialog_layout(self)

        self.tree = QTreeWidget(self)
        self.tree.setHeaderLabels(["Property", "Value"])
        self.tree.setColumnWidth(0, 220)
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        for title, rows in self.sections:
            section = QTreeWidgetItem(self.tree, [title])
            font = section.font(0)
            font.setBold(True)
            section.setFont(0, font)
            section.setFirstColumnSpanned(True)
            for key, value in rows:
                item = QTreeWidgetItem(section, [key, value])
                item.setToolTip(1, value)
            section.setExpanded(True)
        layout.addWidget(self.tree, 2)

        layout.addWidget(heading("Capture limits", self))
        table = QTableWidget(len(info.mode_limits), 5, self)
        table.setHorizontalHeaderLabels(
            ["Mode", "Min. pre-trigger", "Max. pre-trigger", "Max. post-trigger", "Max. total"]
        )
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setSelectionMode(QTableWidget.NoSelection)
        table.setShowGrid(False)
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        for row, limits in enumerate(info.mode_limits):
            values = (
                f"{8 * (row + 1)} channels",
                to_thousands(limits.min_pre_samples),
                to_thousands(limits.max_pre_samples),
                to_thousands(limits.max_post_samples),
                to_thousands(limits.max_total_samples),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                table.setItem(row, column, item)
        table.setMaximumHeight(table.horizontalHeader().height() + 3 * 30 + 8)
        layout.addWidget(table)

        buttons = button_box(self, None)
        self.copy_button = buttons.addButton("Copy to clipboard", QDialogButtonBox.ActionRole)
        self.copy_button.clicked.connect(self._copy)
        set_icon(self.copy_button, "copy")
        layout.addWidget(buttons)

    def _copy(self) -> None:
        QApplication.clipboard().setText(self.text())
        self.copy_button.setText("Copied")
        QTimer.singleShot(1500, lambda: self.copy_button.setText("Copy to clipboard"))

    def text(self) -> str:
        return as_text(self.sections)


class AboutDialog(QDialog):
    """Port of ``Dialogs/AboutDialog.axaml.cs``."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("About PiPiLogicAnalyzer")
        self.setMinimumWidth(480)

        layout = dialog_layout(self)
        title = QLabel(f"PiPiLogicAnalyzer {__version__}", self)
        set_role(title, "title")
        layout.addWidget(title)

        link = f"style='color: {ACCENT_HOVER};'"
        label = QLabel(
            "<p>Software and firmware for the RP2040/RP2350 logic analyzer.</p>"
            "<p>This project is based on and extends the "
            f"<a {link} href='https://github.com/gusmanb/logicanalyzer'>PiPiLogicAnalyzer</a> "
            "by <b>Agustín Giménez Bernad (gusmanb)</b>: his hardware design, firmware and "
            "software 6.5. Many thanks to him for creating it and sharing it under the GPL.</p>"
            f"<p><a {link} href='https://github.com/deckerjulian/PiPiLogicAnalyzer'>"
            "github.com/deckerjulian/PiPiLogicAnalyzer</a></p>"
            "<p>Licensed under the GNU General Public License v3. Protocol decoders from "
            f"<a {link} href='https://github.com/sigrokproject/libsigrokdecode'>libsigrokdecode</a> "
            "(GPL-2.0-or-later / GPL-3.0-or-later, a few MIT or BSD); Qt for Python (LGPL-3.0). "
            "See THIRD_PARTY_NOTICES.md.</p>",
            self,
        )
        label.setOpenExternalLinks(True)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextBrowserInteraction)
        layout.addWidget(label, 1)

        buttons = button_box(self, None)
        layout.addWidget(buttons)
