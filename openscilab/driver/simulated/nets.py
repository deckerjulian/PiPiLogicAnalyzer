# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Nets of a simulator read from another process: wires between simulators wherever they run.

A wired input reads the net of another simulator when it samples (:class:`.circuit.RemoteSource`):
every source is a function of time, so the input asks for exactly the samples of its window. In one
process it reads the other circuit directly; across processes a :class:`NetSource` asks the *net
server* of the process the other simulator runs in (:func:`serve`; a ``multiprocessing.connection``
listener - a Unix socket or a named pipe with a random key - started when a net is first served).
An input talks to that process directly, not through the application.

Clocks: a simulator's time is ``clock()``; its *origin* is ``time.monotonic() - clock()``, found in
its own process (the system's monotonic clock is the same in every process). The input reads the
other net at its own time plus ``origin(input) - origin(source)``, minus the cable delay. Qt free.
"""

from __future__ import annotations

import itertools
import logging
import os
import threading
import time
from dataclasses import dataclass
from multiprocessing.connection import Client, Listener
from typing import Any, Callable, Optional

import numpy as np

from .circuit import Source

log = logging.getLogger(__name__)

#: a net whose server is gone is asked again at most this often (seconds); it reads low meanwhile
RETRY_INTERVAL = 1.0


@dataclass(frozen=True)
class Endpoint:
    """Where a simulator's circuit is read from another process."""

    #: the address of the net server (a socket path or a pipe name)
    address: Any
    authkey: bytes
    #: the circuit at that server
    key: int
    #: ``time.monotonic() - clock()`` of the simulator: what its time 0 is on the system's clock
    origin: float
    #: volts of a logic 1 of the simulator
    high: float = 3.3
    #: the process of the server (a wire within one process reads the circuit directly)
    pid: int = 0


def origin_of(clock: Callable[[], float]) -> float:
    """``time.monotonic() - clock()``: the system's monotonic time at which ``clock`` reads 0."""
    return time.monotonic() - float(clock())


class _Server:
    """The net server of this process: serves the circuits registered with :func:`serve`."""

    def __init__(self) -> None:
        self.authkey = os.urandom(32)
        self.listener = Listener(authkey=self.authkey)
        self.circuits: dict[int, Any] = {}
        self._keys: dict[int, int] = {}
        self._numbers = itertools.count(1)
        self._lock = threading.Lock()
        threading.Thread(target=self._accept, name="openscilab-nets", daemon=True).start()

    @property
    def address(self) -> Any:
        return self.listener.address

    def register(self, circuit: Any) -> int:
        with self._lock:
            known = self._keys.get(id(circuit))
            if known is not None and self.circuits.get(known) is circuit:
                return known
            key = next(self._numbers)
            self.circuits[key] = circuit
            self._keys[id(circuit)] = key
            return key

    def _accept(self) -> None:
        while True:
            try:
                connection = self.listener.accept()
            except (OSError, EOFError):
                return  # (closed)
            except Exception:  # noqa: BLE001 - a client with the wrong key: refused, the server goes on
                log.debug("A connection to the net server was refused", exc_info=True)
                continue
            threading.Thread(target=self._serve, args=(connection,), name="openscilab-nets-client",
                             daemon=True).start()

    def _serve(self, connection) -> None:
        with connection:
            while True:
                try:
                    request = connection.recv()
                except (OSError, EOFError):
                    return
                try:
                    reply: Any = ("ok", self._answer(*request))
                except Exception as error:  # noqa: BLE001 - told to the input, which reads low
                    reply = ("error", f"{type(error).__name__}: {error}")
                try:
                    connection.send(reply)
                except (OSError, EOFError):
                    return

    def _answer(self, what: str, key: int, net: str, start: float, rate: float, count: int, *rest: Any) -> Any:
        circuit = self.circuits[key]
        if what == "digital":
            return circuit.digital(net, start, rate, count)
        if what == "analog":
            return circuit.analog(net, start, rate, count)
        if what == "envelope":
            block, analog = rest
            return circuit.envelope(net, start, rate, count, block, analog)
        if what == "describe":
            source = circuit.sources.get(net)
            return source.describe() if source is not None else "low (nothing connected)"
        raise ValueError(f"unknown request {what!r}")


_server: Optional[_Server] = None
_server_lock = threading.Lock()


def serve(circuit: Any, clock: Callable[[], float], high: float = 3.3) -> Endpoint:
    """Make ``circuit`` readable from other processes (its simulator's ``clock``); returns where."""
    global _server
    with _server_lock:
        if _server is None:
            _server = _Server()
        server = _server
    return Endpoint(server.address, server.authkey, server.register(circuit), origin_of(clock), float(high),
                    os.getpid())


def local_circuit(endpoint: Endpoint) -> Optional[Any]:
    """The circuit of ``endpoint`` when it is served by this process (read directly, then)."""
    server = _server
    if server is None or endpoint.pid != os.getpid() or endpoint.address != server.address:
        return None
    return server.circuits.get(endpoint.key)


class NetSource(Source):
    """A net of a simulator in another process (see the module): its time is this simulator's time
    plus ``offset`` minus the cable ``delay``. A server that is gone reads low."""

    kind = "remote"

    def __init__(self, endpoint: Endpoint, net: str, offset: float, delay: float = 0.0, label: str = "") -> None:
        self.endpoint = endpoint
        self.net = net
        self.offset = float(offset)
        self.delay = float(delay)
        self.label = label or net
        self.high = endpoint.high
        self.frequency = None
        self._connection = None
        self._lock = threading.Lock()
        self._failed_at: Optional[float] = None
        #: why the last request failed ("" while the net answers)
        self.error = ""

    def _ask(self, request: tuple) -> Any:
        with self._lock:
            if self._connection is None:
                if self._failed_at is not None and time.monotonic() - self._failed_at < RETRY_INTERVAL:
                    return None
                try:
                    self._connection = Client(self.endpoint.address, authkey=self.endpoint.authkey)
                except (OSError, EOFError, ValueError) as error:
                    self._failed(f"cannot reach the simulator ({error})")
                    return None
                except Exception as error:  # noqa: BLE001 - e.g. a refused key: the net reads low
                    self._failed(f"cannot reach the simulator ({type(error).__name__})")
                    return None
            try:
                self._connection.send(request)
                kind, value = self._connection.recv()
            except (OSError, EOFError) as error:
                self._close()
                self._failed(f"the simulator is gone ({error or type(error).__name__})")
                return None
            if kind != "ok":
                self.error = str(value)
                return None
            self.error, self._failed_at = "", None
            return value

    def _failed(self, reason: str) -> None:
        if not self.error:
            log.info("Wire from %s: %s", self.label, reason)
        self.error = reason
        self._failed_at = time.monotonic()

    def _close(self) -> None:
        if self._connection is not None:
            try:
                self._connection.close()
            except OSError:
                pass
            self._connection = None

    def _time(self, start: float) -> float:
        return start + self.offset - self.delay

    def digital(self, start, rate, count):
        value = self._ask(("digital", self.endpoint.key, self.net, self._time(start), rate, count))
        return np.asarray(value, dtype=np.uint8) if value is not None else np.zeros(count, dtype=np.uint8)

    def analog(self, start, rate, count):
        value = self._ask(("analog", self.endpoint.key, self.net, self._time(start), rate, count))
        return np.asarray(value, dtype=np.float64) if value is not None else np.zeros(count, dtype=np.float64)

    def envelope(self, start, rate, count, block, analog=False):
        value = self._ask(("envelope", self.endpoint.key, self.net, self._time(start), rate, count, block, analog))
        if value is None:
            blocks = (count + block - 1) // block
            return np.zeros(blocks), np.zeros(blocks)
        return value

    def describe(self) -> str:
        return f"wired from {self.label}" + (f" (gone: {self.error})" if self.error else "")

    def close(self) -> None:
        with self._lock:
            self._close()
