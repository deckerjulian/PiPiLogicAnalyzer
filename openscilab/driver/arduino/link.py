# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The connection to an Arduino with the openSciLab firmware: bytes in and out (a serial port or
the pipe of the simulated Arduino) and a reader thread that hands answers to the requests waiting
for them and events to their handlers."""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Callable, Optional

from . import protocol

log = logging.getLogger("openscilab.driver")

#: seconds a request waits for its answer
REQUEST_TIMEOUT_S = 3.0


class LinkError(IOError):
    """The board does not answer or is gone."""


class ArduinoError(Exception):
    """The board answered a request with an error frame."""

    def __init__(self, code: Optional[int], text: str) -> None:
        super().__init__(text)
        self.code = code


class ByteLink:
    """Bytes to and from the board."""

    def write(self, data: bytes) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def read(self, timeout: float) -> bytes:  # pragma: no cover - interface
        """What arrived within ``timeout`` seconds (``b""``: nothing); raises ``LinkError`` when gone."""
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class SerialLink(ByteLink):
    """A serial port. Opening it resets most Arduino boards (DTR); the firmware needs a moment
    before it answers (:meth:`Connection.hello` asks until it does)."""

    def __init__(self, port: str, baud: int) -> None:
        import serial

        self.port = port
        try:
            self._serial = serial.Serial(port, baud, timeout=0.05, write_timeout=2.0, exclusive=True)
        except (serial.SerialException, ValueError) as error:
            raise LinkError(f"{port} cannot be opened: {error}") from error
        from ..ports import enlarge_receive_buffer

        enlarge_receive_buffer(self._serial)

    def write(self, data: bytes) -> None:
        try:
            self._serial.write(data)
        except Exception as error:  # noqa: BLE001 - the port went away
            raise LinkError(str(error)) from error

    def read(self, timeout: float) -> bytes:
        try:
            self._serial.timeout = timeout
            first = self._serial.read(1)
            if not first:
                return b""
            return first + self._serial.read(self._serial.in_waiting)
        except Exception as error:  # noqa: BLE001 - the port went away
            raise LinkError(str(error)) from error

    def close(self) -> None:
        try:
            self._serial.close()
        except Exception:  # noqa: BLE001 - best effort
            log.debug("self._serial.close() failed: best effort", exc_info=True)


class PipeLink(ByteLink):
    """One end of an in-memory connection (:func:`pipe`)."""

    def __init__(self, incoming: "queue.Queue[bytes]", outgoing: "queue.Queue[bytes]") -> None:
        self._incoming = incoming
        self._outgoing = outgoing
        self.closed = False

    def write(self, data: bytes) -> None:
        if self.closed:
            raise LinkError("the connection is closed")
        self._outgoing.put(bytes(data))

    def read(self, timeout: float) -> bytes:
        if self.closed:
            raise LinkError("the connection is closed")
        try:
            data = self._incoming.get(timeout=timeout)
        except queue.Empty:
            return b""
        if data is None:
            self.closed = True
            raise LinkError("the connection is closed")
        return data

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._outgoing.put(None)  # the other end reads "closed"


def pipe() -> tuple[PipeLink, PipeLink]:
    """Two connected ends: what one writes, the other reads."""
    first: "queue.Queue[bytes]" = queue.Queue()
    second: "queue.Queue[bytes]" = queue.Queue()
    return PipeLink(first, second), PipeLink(second, first)


EventHandler = Callable[[protocol.Frame], None]


class Connection:
    """Requests with answers and events over a :class:`ByteLink`."""

    def __init__(self, link: ByteLink, name: str = "arduino") -> None:
        self.link = link
        self.name = name
        self._sequence = 0
        self._sequence_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._pending: dict[int, list] = {}
        self._pending_lock = threading.Lock()
        #: event code -> handler (called in the reader thread)
        self.event_handlers: dict[int, EventHandler] = {}
        self.decoder = protocol.Decoder()
        self.error: Optional[str] = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._read, name=f"openscilab-{name}", daemon=True)
        self._thread.start()

    @property
    def alive(self) -> bool:
        return self.error is None and not self._stop.is_set()

    def _next_sequence(self) -> int:
        with self._sequence_lock:
            self._sequence = (self._sequence + 1) & 0xFF
            return self._sequence

    def send(self, frame_type: int, payload: bytes, sequence: Optional[int] = None) -> int:
        sequence = self._next_sequence() if sequence is None else sequence
        data = protocol.encode(frame_type, sequence, payload)
        with self._write_lock:
            self.link.write(data)
        return sequence

    def request(self, command: int, data: bytes = b"", timeout: float = REQUEST_TIMEOUT_S) -> bytes:
        """Sends a request and returns the data of its answer; raises :class:`ArduinoError` for an
        error answer and :class:`LinkError` when none comes."""
        if not self.alive:
            raise LinkError(self.error or "the connection is closed")
        waiter = [threading.Event(), None]
        sequence = self._next_sequence()
        with self._pending_lock:
            self._pending[sequence] = waiter
        try:
            payload = protocol.request(command, data)
            with self._write_lock:
                self.link.write(protocol.encode(protocol.REQUEST, sequence, payload))
            if not waiter[0].wait(timeout):
                raise LinkError(self.error or f"the board did not answer command 0x{command:02X}")
        finally:
            with self._pending_lock:
                self._pending.pop(sequence, None)
        frame: Optional[protocol.Frame] = waiter[1]
        if frame is None:
            raise LinkError(self.error or "the connection is closed")
        if frame.type == protocol.ERROR:
            raise ArduinoError(protocol.error_code(frame), protocol.error_text(frame))
        return frame.data

    def notify(self, command: int, data: bytes = b"") -> None:
        """A request whose answer nobody waits for (the heartbeat)."""
        self.send(protocol.REQUEST, protocol.request(command, data))

    def _read(self) -> None:
        while not self._stop.is_set():
            try:
                data = self.link.read(0.1)
            except LinkError as error:
                if not self._stop.is_set():
                    self.error = str(error) or "the connection was lost"
                    log.debug("%s: %s", self.name, self.error)
                break
            if not data:
                continue
            for frame in self.decoder.feed(data):
                self._dispatch(frame)
        with self._pending_lock:
            for waiter in self._pending.values():
                waiter[0].set()
        handler = self.event_handlers.get(-1)
        if handler is not None and self.error is not None:
            handler(protocol.Frame(protocol.EVENT, 0, b""))

    def _dispatch(self, frame: protocol.Frame) -> None:
        if frame.type in (protocol.ANSWER, protocol.ERROR):
            with self._pending_lock:
                waiter = self._pending.get(frame.sequence)
            if waiter is not None:
                waiter[1] = frame
                waiter[0].set()
            return
        if frame.type == protocol.EVENT:
            handler = self.event_handlers.get(frame.command)
            if handler is None:
                if frame.command == protocol.EVENT_WATCHDOG:
                    log.warning("%s: the watchdog released the outputs", self.name)
                return
            try:
                handler(frame)
            except Exception:  # noqa: BLE001 - a handler that fails does not stop the reader
                log.exception("Handling an event of %s failed", self.name)

    def hello(self, attempts_until: float = 4.0) -> dict[str, str]:
        """``HELLO`` until the board answers (it restarts when the port is opened)."""
        deadline = time.monotonic() + attempts_until
        last: Optional[Exception] = None
        while time.monotonic() < deadline and self.alive:
            try:
                return protocol.parse_text(self.request(protocol.HELLO, timeout=0.5))
            except LinkError as error:
                last = error
        raise LinkError(f"no openSciLab Arduino firmware answers ({last or self.error})")

    def close(self) -> None:
        self._stop.set()
        self.link.close()
        if self._thread is not threading.current_thread():
            self._thread.join(1)
