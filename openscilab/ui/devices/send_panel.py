# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The *Send* tab of a device card: UART, SPI and I²C sent by the device itself.

Shown for devices whose generator sends protocols with their own hardware (capabilities
``TX_UART``, ``TX_SPI``, ``TX_I2C``). What an SPI or I²C target answers is shown below.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ...core import units
from ...core.instrument import GeneratorFacet, InstrumentError
from .. import background
from ..dialogs.common import InlineMessage, hint
from ..icons import set_icon

PROTOCOLS = (("uart", "UART"), ("spi", "SPI"), ("i2c", "I²C"))
#: the pins of a protocol by role, with their labels
ROLES = {
    "uart": (("tx", "TX"),),
    "spi": (("sck", "SCK"), ("mosi", "MOSI"), ("miso", "MISO (optional)"), ("cs", "CS (optional)")),
    "i2c": (("scl", "SCL"), ("sda", "SDA")),
}


def parse_data(text: str) -> bytes:
    """``Hello\\n`` as text, ``0x19 0x00`` or ``19 00`` (all of it hexadecimal bytes) as bytes."""
    stripped = text.strip()
    parts = stripped.replace(",", " ").split()
    if parts and all(_is_hex_byte(part) for part in parts):
        return bytes(int(part, 16) for part in parts)
    return text.encode("utf-8").decode("unicode_escape").encode("latin-1")


def _is_hex_byte(part: str) -> bool:
    digits = part[2:] if part.lower().startswith("0x") else part
    if part.lower().startswith("0x") or (len(digits) == 2):
        try:
            return 0 <= int(digits, 16) <= 0xFF
        except ValueError:
            return False
    return False


def describe_bytes(data: bytes) -> str:
    if not data:
        return "–"
    text = "".join(chr(byte) if 32 <= byte < 127 else "." for byte in data)
    return f"{' '.join(f'{byte:02X}' for byte in data)}   “{text}”"


class SendPanel(QWidget):
    """Protocol, pins, settings and the bytes to send; the answer of the target below."""

    def __init__(self, instrument, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.instrument = instrument
        self.generator: GeneratorFacet = instrument.facet(GeneratorFacet)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.addWidget(hint("Sent with the device's own UART, SPI or I²C; a target's answer is shown below.", self))
        form = QFormLayout()
        self.protocol_box = QComboBox(self)
        self.protocol_box.setObjectName("send-protocol")
        for key, title in PROTOCOLS:
            if self.generator.transmits(key):
                self.protocol_box.addItem(title, key)
        self.protocol_box.currentIndexChanged.connect(self._protocol_changed)
        form.addRow("Protocol:", self.protocol_box)
        self.pin_edits: dict[str, QLineEdit] = {}
        self.pin_labels: dict[str, QLabel] = {}
        defaults = self._default_pins()
        for role, label in {role: label for roles in ROLES.values() for role, label in roles}.items():
            edit = QLineEdit(defaults.get(role, ""), self)
            edit.setObjectName(f"send-pin-{role}")
            self.pin_edits[role] = edit
            self.pin_labels[role] = QLabel(f"{label}:", self)
            form.addRow(self.pin_labels[role], edit)
        self.rate_edit = QLineEdit(self)
        self.rate_edit.setObjectName("send-rate")
        self.rate_label = QLabel(self)
        form.addRow(self.rate_label, self.rate_edit)
        self.address_box = QSpinBox(self)
        self.address_box.setRange(0, 0x7F)
        self.address_box.setDisplayIntegerBase(16)
        self.address_box.setPrefix("0x")
        self.address_box.setValue(0x48)
        self.address_label = QLabel("Address:", self)
        form.addRow(self.address_label, self.address_box)
        self.read_box = QSpinBox(self)
        self.read_box.setRange(0, 4096)
        self.read_label = QLabel("Read bytes:", self)
        form.addRow(self.read_label, self.read_box)
        self.data_edit = QLineEdit("Hello\\n", self)
        self.data_edit.setObjectName("send-data")
        self.data_edit.setToolTip("Text (\\n, \\r and \\x41 as escapes) or bytes in hexadecimal: 19 00 or 0x19 0x00")
        form.addRow("Data:", self.data_edit)
        layout.addLayout(form)
        row = QHBoxLayout()
        self.send_button = QPushButton("Send", self)
        set_icon(self.send_button, "arrow-right")
        self.send_button.clicked.connect(self.send)
        row.addWidget(self.send_button)
        row.addStretch(1)
        layout.addLayout(row)
        self.message = InlineMessage(self)
        layout.addWidget(self.message)
        self.answer_label = QLabel("Answer: –", self)
        self.answer_label.setObjectName("send-answer")
        self.answer_label.setWordWrap(True)
        layout.addWidget(self.answer_label)
        layout.addStretch(1)
        self._protocol_changed()

    def _default_pins(self) -> dict[str, str]:
        pins = [pin.name for pin in (self.instrument.pins() or []) if pin.usable and "DOUT" in pin.capabilities]
        return dict(zip(("tx", "sck", "mosi", "miso", "scl", "sda"), pins[:1] + pins[:3] + pins[:2]))

    @property
    def protocol(self) -> str:
        return self.protocol_box.currentData() or ""

    def _protocol_changed(self) -> None:
        protocol = self.protocol
        wanted = {role for role, _label in ROLES.get(protocol, ())}
        for role, edit in self.pin_edits.items():
            edit.setVisible(role in wanted)
            self.pin_labels[role].setVisible(role in wanted)
        i2c = protocol == "i2c"
        for widget in (self.address_box, self.address_label, self.read_box, self.read_label):
            widget.setVisible(i2c)
        self.rate_label.setText("Baud rate:" if protocol == "uart" else "Clock:")
        self.rate_edit.setText({"uart": "115200 Hz", "spi": "1 MHz", "i2c": "100 kHz"}.get(protocol, ""))

    def settings(self) -> dict:
        rate = units.parse(self.rate_edit.text(), "Hz")
        if self.protocol == "uart":
            return {"baud": rate}
        if self.protocol == "i2c":
            return {"frequency": rate, "address": self.address_box.value(), "read": self.read_box.value()}
        return {"frequency": rate}

    def pins(self) -> dict[str, str]:
        return {role: self.pin_edits[role].text().strip() for role, _label in ROLES.get(self.protocol, ())
                if self.pin_edits[role].text().strip()}

    def send(self) -> Optional[bytes]:
        try:
            data = parse_data(self.data_edit.text())
            settings = self.settings()
            protocol, pins = self.protocol, self.pins()
            received = background.run(self, f"Sending {protocol.upper()}...",
                                      lambda: self.generator.transmit(protocol, data, pins, **settings))
        except background.Cancelled:
            return None
        except (InstrumentError, ValueError, units.UnitError) as error:
            self.message.show_error(str(error))
            return None
        self.message.clear()
        self.answer_label.setText(f"Sent {len(data)} byte(s). Answer: {describe_bytes(received)}")
        return received
