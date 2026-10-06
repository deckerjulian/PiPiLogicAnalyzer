# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The serial ports of the computer, shared by the device kinds that use them.

One look at the system (cached for :data:`PORTS_CACHE_S`) serves the Pico boards, the Arduino
boards, the boards with other firmware and the watch for unplugged devices of the same moment.
Each kind claims its ports by USB identifiers, so no port is opened by two kinds.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from serial.tools import list_ports

#: seconds the list of serial ports is reused
PORTS_CACHE_S = 0.5
#: bytes the operating system keeps for a serial port the application does not read for a moment
#: (Windows; macOS and Linux keep what the USB driver delivers)
RECEIVE_BUFFER = 4 << 20


def enlarge_receive_buffer(port) -> None:
    """A large receive buffer for an open serial port, where pyserial can set it (Windows): a
    stream then survives a pause of the reading thread instead of overflowing on the device."""
    setter = getattr(port, "set_buffer_size", None)
    if callable(setter):
        try:
            setter(rx_size=RECEIVE_BUFFER)
        except Exception:  # noqa: BLE001 - the default buffer works too
            pass
_ports_cache: tuple = (None, 0.0, [])


def comports() -> list:
    """The serial ports of the computer (asked at most every ``PORTS_CACHE_S`` seconds)."""
    global _ports_cache
    function, stamp, ports = _ports_cache
    now = time.monotonic()
    if function is list_ports.comports and now - stamp < PORTS_CACHE_S:
        return ports
    ports = list(list_ports.comports())
    _ports_cache = (list_ports.comports, now, ports)
    return ports


#: USB identifiers of Arduino boards and the USB serial chips on their clones:
#: (vendor, product or None for every product, what it usually is, baud rate)
ARDUINO_USB_IDS = (
    (0x2341, None, "Arduino", None),
    (0x2A03, None, "Arduino", None),
    (0x1A86, 0x7523, "CH340 (Nano, Uno clone)", 115200),
    (0x10C4, 0xEA60, "CP210x (ESP32)", 2_000_000),
    (0x0403, None, "FTDI (Nano, older boards)", 115200),
    (0x303A, None, "Espressif (ESP32-S3)", 2_000_000),
)
#: Arduino products with native USB (the baud rate does not matter) or an AVR behind a USB chip
ARDUINO_AVR_BAUD = 115200
ARDUINO_FAST_BAUD = 2_000_000
#: Arduino boards whose USB is the MCU itself (Uno R4, Leonardo): any baud rate works
_ARDUINO_NATIVE = {0x0069, 0x1002, 0x0036, 0x8036}


@dataclass
class ArduinoPort:
    port_name: str
    vid: int
    pid: int
    description: str
    baud: int
    serial_number: Optional[str] = None

    @property
    def label(self) -> str:
        extras = [self.description] + ([f"S/N {self.serial_number}"] if self.serial_number else [])
        return f"{self.port_name} ({', '.join(extras)})"


def arduino_baud(vid: Optional[int], pid: Optional[int]) -> Optional[int]:
    """Baud rate of the openSciLab firmware behind a port with these USB identifiers, ``None``
    when they are no Arduino's."""
    for known_vid, known_pid, _name, baud in ARDUINO_USB_IDS:
        if vid == known_vid and (known_pid is None or pid == known_pid):
            if baud is not None:
                return baud
            return ARDUINO_FAST_BAUD if pid in _ARDUINO_NATIVE else ARDUINO_AVR_BAUD
    return None


def detect_arduinos() -> list[ArduinoPort]:
    """Ports with the USB identifiers of an Arduino board (nothing is opened: that resets it)."""
    found = []
    for port in comports():
        baud = arduino_baud(port.vid, port.pid)
        if baud is None:
            continue
        name = next(text for vid, pid, text, _ in ARDUINO_USB_IDS
                    if port.vid == vid and (pid is None or port.pid == pid))
        description = port.product if port.product and port.vid in (0x2341, 0x2A03) else name
        found.append(ArduinoPort(port.device, port.vid, port.pid or 0, description, baud, port.serial_number))
    return sorted(found, key=lambda item: item.port_name)
