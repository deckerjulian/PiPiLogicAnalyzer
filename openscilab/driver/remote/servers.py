# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The servers of a process: the one remote devices connect to (started by the application when
*Remote devices* is on in the settings, or by a flow run from the command line), and a local one
on 127.0.0.1 for the simulated remote devices (no token, not reachable from outside)."""

from __future__ import annotations

import secrets
import threading
from typing import Optional

from .server import RemoteError, RemoteServer

_lock = threading.Lock()
_main: Optional[RemoteServer] = None
_local: Optional[RemoteServer] = None
#: the application decides itself whether the server runs (a flow does not start it then)
managed = False


def new_token() -> str:
    return secrets.token_urlsafe(9)


def start_main(port: int, token: str, name: str = "openSciLab", beacon: bool = True) -> RemoteServer:
    """(Re)start the server remote devices connect to."""
    global _main
    with _lock:
        if _main is not None:
            _main.stop()
            _main = None
        _main = RemoteServer(port=port, token=token, name=name, beacon=beacon).start()
        return _main


def stop_main() -> None:
    global _main
    with _lock:
        if _main is not None:
            _main.stop()
        _main = None


def main_server(start: bool = True) -> RemoteServer:
    """The server for remote devices. Outside the application (``openscilab run``) it is started on
    the port and with the token of the settings when a flow needs it."""
    with _lock:
        server = _main
    if server is not None and server.running:
        return server
    if managed or not start:
        raise RemoteError("remote devices are off: switch them on in Settings → Remote devices")
    from ...core import preferences

    token = str(preferences.get("remote.token") or "")
    if not token:
        token = new_token()
        preferences.update({"remote.token": token})
    return start_main(int(preferences.get("remote.port")), token, beacon=bool(preferences.get("remote.beacon")))


def running_main() -> Optional[RemoteServer]:
    with _lock:
        return _main if _main is not None and _main.running else None


def local_server() -> RemoteServer:
    """The server of the simulated remote devices (started on first use)."""
    global _local
    with _lock:
        if _local is None or not _local.running:
            _local = RemoteServer(port=0, host="127.0.0.1", name="openSciLab (simulators)").start()
        return _local


def connections() -> list:
    """Every connected remote device (of the main and the simulators' server)."""
    found = []
    for server in (running_main(), _local):
        if server is not None and server.running:
            found += server.devices()
    return found


def find(name: str) -> Optional[tuple[RemoteServer, object]]:
    """The server and connection of a connected device ``name``."""
    for server in (running_main(), _local):
        if server is not None and server.running:
            connection = server.device(name)
            if connection is not None:
                return server, connection
    return None
