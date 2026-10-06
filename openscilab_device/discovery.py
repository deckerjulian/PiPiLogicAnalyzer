# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Finding openSciLab in the local network: it sends a beacon (UDP broadcast) while it accepts
remote devices; a device without a server address listens for it."""

from __future__ import annotations

import json
import socket
from typing import Optional

from . import protocol


def beacon(name: str, port: int) -> bytes:
    return json.dumps({"magic": protocol.BEACON_MAGIC, "name": name, "port": int(port),
                       "protocol": protocol.PROTOCOL}).encode("utf-8")


def read_beacon(data: bytes) -> Optional[dict]:
    try:
        message = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(message, dict) or message.get("magic") != protocol.BEACON_MAGIC:
        return None
    return message


def find_server(timeout: float = 3.0, port: int = protocol.BEACON_PORT) -> Optional[tuple[str, int]]:
    """``(host, port)`` of the first openSciLab heard within ``timeout`` seconds, else ``None``."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        sock.bind(("", port))
        sock.settimeout(timeout)
        try:
            while True:
                data, (host, _port) = sock.recvfrom(4096)
                message = read_beacon(data)
                if message is not None:
                    return host, int(message.get("port") or protocol.DEFAULT_PORT)
        except OSError:
            return None
