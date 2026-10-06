# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The UDP beacons of the bridge app (port 5561): ``OPENSCILAB_BRIDGE <version> <tcp port> <model> <serial>``.

A thread listens once started (:func:`seen`), so the device list can show the oscilloscopes on
the network without waiting.
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass
from typing import Optional

from .scpi import BEACON_PORT

#: seconds after its last beacon an oscilloscope is no longer listed
FORGET_S = 10.0


@dataclass
class Beacon:
    host: str
    version: str
    port: int
    model: str
    serial: str
    seen: float = 0.0

    @property
    def label(self) -> str:
        return f"Rigol {self.model} at {self.host}" + (f" (S/N {self.serial})" if self.serial else "")


def parse(text: str, host: str) -> Optional[Beacon]:
    parts = text.strip().split()
    if len(parts) < 3 or parts[0] != "OPENSCILAB_BRIDGE":
        return None
    try:
        port = int(parts[2])
    except ValueError:
        return None
    return Beacon(host, parts[1], port, parts[3] if len(parts) > 3 else "DHO", parts[4] if len(parts) > 4 else "",
                  time.monotonic())


class _Listener:
    def __init__(self) -> None:
        self.beacons: dict[str, Beacon] = {}
        self.lock = threading.Lock()
        self.thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self.thread is not None:
            return
        self.thread = threading.Thread(target=self._run, name="openscilab-dho-beacons", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("", BEACON_PORT))
        except OSError:
            return  # the port is taken (another openSciLab): no discovery, addresses by hand
        while True:
            try:
                data, (host, _port) = sock.recvfrom(1024)
            except OSError:
                return
            beacon = parse(data.decode("ascii", "replace"), host)
            if beacon is not None:
                with self.lock:
                    self.beacons[host] = beacon


_listener = _Listener()


def seen() -> list[Beacon]:
    """The oscilloscopes whose bridge app was heard lately (starts listening on the first call)."""
    _listener.start()
    now = time.monotonic()
    with _listener.lock:
        return sorted((beacon for beacon in _listener.beacons.values() if now - beacon.seen < FORGET_S),
                      key=lambda beacon: beacon.host)
