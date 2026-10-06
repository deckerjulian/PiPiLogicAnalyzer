# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of openSciLab, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Byte transports used by :class:`~openscilab.driver.pico.analyzer.PicoDriver`.

The original C# driver mixed ``SerialPort``/``TcpClient`` handling with the
protocol logic and relied on ``StreamReader``/``BinaryReader`` buffering, which
made the serial and network paths behave differently (the network path could
block forever, the serial path read a pre-computed amount of bytes).  Both
transports are unified here behind a tiny interface with explicit timeouts.
"""

from __future__ import annotations

import socket
from typing import Optional

import serial


class TransportError(IOError):
    """Raised when the underlying transport fails or times out."""


class TransportTimeout(TransportError):
    """Nothing (or no whole line) arrived in time; the connection itself is fine."""


class Transport:
    """Minimal byte stream interface."""

    def write(self, data: bytes) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def read_exactly(self, count: int, timeout: Optional[float] = None) -> bytes:  # pragma: no cover
        raise NotImplementedError

    def read_line(self, timeout: Optional[float] = None) -> str:  # pragma: no cover
        raise NotImplementedError

    def reset_input(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def reopen(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    @property
    def is_open(self) -> bool:  # pragma: no cover - interface
        raise NotImplementedError


class SerialTransport(Transport):
    def __init__(self, port: str, baud_rate: int = 115200, timeout: float = 10.0) -> None:
        self.port = port
        self.baud_rate = baud_rate
        self._default_timeout = timeout
        #: the start of a line whose end had not arrived when a read timed out
        self._partial = b""
        self._serial = self._open()

    def _open(self) -> serial.Serial:
        port = serial.Serial()
        port.port = self.port
        port.baudrate = self.baud_rate
        port.timeout = self._default_timeout
        port.write_timeout = self._default_timeout
        port.rts = True
        port.dtr = True
        try:
            # one user at a time: a second opener would mix its bytes into the protocol
            port.exclusive = True
        except (AttributeError, ValueError):  # pragma: no cover - platforms without it
            pass
        port.open()
        from ..ports import enlarge_receive_buffer

        enlarge_receive_buffer(port)
        return port

    def write(self, data: bytes) -> None:
        self._serial.write(data)
        self._serial.flush()

    def read_exactly(self, count: int, timeout: Optional[float] = None) -> bytes:
        if count <= 0:
            return b""
        self._serial.timeout = timeout if timeout is not None else None
        data = self._serial.read(count)
        if len(data) != count:
            raise TransportError(f"Expected {count} bytes, received {len(data)}")
        return data

    def read_line(self, timeout: Optional[float] = None) -> str:
        self._serial.timeout = timeout if timeout is not None else self._default_timeout
        line = self._partial + self._serial.readline()
        if not line.endswith(b"\n"):
            # the rest comes with the next read: kept, so no line is torn in two
            self._partial = line
            raise TransportTimeout("Timeout waiting for a response from the device")
        self._partial = b""
        return line.decode("ascii", "replace").strip("\r\n")

    def reset_input(self) -> None:
        self._partial = b""
        try:
            self._serial.reset_input_buffer()
        except Exception:  # pragma: no cover - depends on the OS/driver
            pass

    def reopen(self) -> None:
        self._partial = b""
        try:
            self._serial.close()
        except Exception:  # pragma: no cover - best effort
            pass
        self._serial = self._open()

    def close(self) -> None:
        try:
            self._serial.close()
        except Exception:  # pragma: no cover - best effort
            pass

    @property
    def is_open(self) -> bool:
        try:
            return bool(self._serial.is_open)
        except Exception:  # pragma: no cover - best effort
            return False


#: a board that vanished from the network is noticed after about idle + interval × count seconds
KEEPALIVE_IDLE_S, KEEPALIVE_INTERVAL_S, KEEPALIVE_COUNT = 10, 3, 3


def _keep_alive(sock: socket.socket) -> None:
    """Probe the connection while nothing is sent: a capture waiting for its trigger over WiFi
    fails when the board is gone instead of waiting forever."""
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        if hasattr(socket, "SIO_KEEPALIVE_VALS"):  # Windows
            sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, KEEPALIVE_IDLE_S * 1000, KEEPALIVE_INTERVAL_S * 1000))
            return
        idle = getattr(socket, "TCP_KEEPIDLE", None) or getattr(socket, "TCP_KEEPALIVE", None) or (
            0x10 if hasattr(socket, "AF_SYSTEM") else None)  # macOS: TCP_KEEPALIVE
        if idle is not None:
            sock.setsockopt(socket.IPPROTO_TCP, idle, KEEPALIVE_IDLE_S)
        for name, value in (("TCP_KEEPINTVL", KEEPALIVE_INTERVAL_S), ("TCP_KEEPCNT", KEEPALIVE_COUNT)):
            if hasattr(socket, name):
                sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, name), value)
    except OSError:  # pragma: no cover - a platform that refuses an option: no probing
        pass


class NetworkTransport(Transport):
    def __init__(self, address: str, port: int, timeout: float = 10.0) -> None:
        self.address = address
        self.port = port
        self._default_timeout = timeout
        self._buffer = bytearray()
        self._socket = self._connect()

    def _connect(self) -> socket.socket:
        sock = socket.create_connection((self.address, self.port), timeout=self._default_timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        _keep_alive(sock)
        return sock

    def write(self, data: bytes) -> None:
        self._socket.sendall(data)

    def _receive(self, timeout: Optional[float]) -> bytes:
        self._socket.settimeout(timeout)
        try:
            chunk = self._socket.recv(65536)
        except socket.timeout as exc:
            raise TransportTimeout("Timeout waiting for data from the device") from exc
        if not chunk:
            raise TransportError("Connection closed by the device")
        return chunk

    def read_exactly(self, count: int, timeout: Optional[float] = None) -> bytes:
        while len(self._buffer) < count:
            self._buffer.extend(self._receive(timeout))
        data = bytes(self._buffer[:count])
        del self._buffer[:count]
        return data

    def read_line(self, timeout: Optional[float] = None) -> str:
        timeout = timeout if timeout is not None else self._default_timeout
        while True:
            index = self._buffer.find(b"\n")
            if index >= 0:
                line = bytes(self._buffer[:index])
                del self._buffer[: index + 1]
                return line.decode("ascii", "replace").strip("\r\n")
            self._buffer.extend(self._receive(timeout))

    def reset_input(self) -> None:
        self._buffer.clear()

    def reopen(self) -> None:
        self.close()
        self._buffer.clear()
        self._socket = self._connect()

    def close(self) -> None:
        # close() alone does not wake a recv() blocked in another thread (the capture reader);
        # shutdown() does, and it sends the FIN the single-client WiFi firmware waits for.
        try:
            self._socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._socket.close()
        except Exception:  # pragma: no cover - best effort
            pass

    @property
    def is_open(self) -> bool:
        return self._socket.fileno() != -1
