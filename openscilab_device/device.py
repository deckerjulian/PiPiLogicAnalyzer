# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""A remote device: what a script on another computer offers openSciLab.

::

    from openscilab_device import Device

    dev = Device("climate-pi", server="192.168.1.20", token="...")
    temperature = dev.input("temperature", kind="scalar", unit="°C")
    relay = dev.output("relay", kind="bool")

    @relay.on_set
    def switch(value, at):
        gpio.output(17, value)

    dev.start()
    while True:
        temperature.send(read_sensor())     # stamped with this computer's clock when called
        time.sleep(0.5)

Everything runs in background threads: connecting (and connecting again), answering the clock
measurements of openSciLab, executing outputs at the time openSciLab asked for, sending. Values
sent while there is no connection are kept (up to ``buffer`` messages) and follow with their
original time stamps.

Blocks of samples (a DAQ behind USB) are stamped from when they arrive (:mod:`.timing`): the
envelope of the arrivals, bounded by :meth:`Input.start` and less a latency measured with
:meth:`Input.calibrate` (or given). A device whose clock follows a time scale (PTP, GPS) says so
with ``timescale``; an input that records a sync signal of openSciLab is ``sync=True``; one whose
sample clock comes from an instrument of openSciLab is ``clock="shared"`` (``docs/timing.md``).
"""

from __future__ import annotations

import heapq
import itertools
import json
import math
import os
import random
import socket
import struct
import threading
import time
import uuid
from collections import deque
from typing import Any, Callable, Iterable, Optional, Union

from . import protocol
from .timing import TIMESCALES, ArrivalClock, Latency, Loopback

__all__ = ["Command", "Device", "Input", "Output", "SyncInput", "SyncOutput"]


class _Port:
    def __init__(self, device: Device, name: str, kind: str, unit: str = "", description: str = "",
                 **extra: Any) -> None:
        self.device, self.name, self.kind, self.unit, self.description = device, name, kind, unit, description
        self.extra = {key: value for key, value in extra.items() if value is not None}

    def describe(self) -> dict:
        return {"name": self.name, "kind": self.kind, "unit": self.unit, "description": self.description,
                **self.extra}


class Input(_Port):
    """Something the device measures. ``send`` for single values (scalar, bool, event, text),
    ``send_block`` for samples at the input's rate (analog, digital)."""

    def __init__(self, *args, latency: Optional[Latency] = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        #: subtracted from the arrival envelope of the blocks (seconds; see :meth:`calibrate`)
        self.latency = latency
        self._lock = threading.Condition()
        self._count = 0
        self._origin: Optional[float] = None
        self.arrival: Optional[ArrivalClock] = ArrivalClock(self.rate) if self.rate else None
        #: a loopback being measured: loop, waiting (for an edge), level (the last), low/high (seen), threshold
        self._loop: Optional[dict] = None

    @property
    def rate(self) -> Optional[float]:
        return self.extra.get("rate")

    def send(self, value: Any, at: Optional[float] = None) -> None:
        """``value`` measured at ``at`` (this device's clock; default: now)."""
        if self.kind in protocol.BLOCK_KINDS:
            self.send_block([value], at)
            return
        if self.kind == "bool":
            value = bool(value)
        elif self.kind == "scalar":
            value = float(value)
        elif self.kind == "text":
            value = str(value)
        self.device._send_input(self, {"t": "value", "ch": self.name, "at": self.device.now() if at is None else at,
                                       "v": value})

    def start(self, at: Optional[float] = None) -> None:
        """The acquisition was started now (or at ``at``): no sample is older, the samples count
        from here. Call it right before the command that starts the hardware."""
        with self._lock:
            self._count = 0
            self._origin = None
            if self.rate:
                self.arrival = ArrivalClock(self.rate)
                self.arrival.start(self.device.now() if at is None else at)

    def send_block(self, samples: Iterable, t0: Optional[float] = None, index: Optional[int] = None) -> None:
        """Samples at the rate of the input. ``t0``: the time of the first one when the hardware
        stamped it (else it is found from when the blocks arrive: call this as soon as they come);
        ``index``: the number of the first sample since :meth:`start` (when samples were lost)."""
        arrived = self.device.now()
        if self.kind not in protocol.BLOCK_KINDS:
            raise ValueError(f"{self.name} is a {self.kind} input: use send()")
        dtype = "u1" if self.kind == "digital" else "f4"
        payload = _pack(samples, dtype)
        count = len(payload) // protocol.DTYPES[dtype]
        if not count:
            return
        rate = float(self.rate or 1.0)
        header = {"t": "block", "ch": self.name, "rate": rate, "dtype": dtype, "n": count}
        with self._lock:
            first = self._count if index is None else int(index)
            self._count = first + count
            if t0 is None:
                self.arrival.add(first + count - 1, arrived)
                estimate = self.arrival.estimate(first, self.latency)
                t0 = estimate.time
                if self.extra.get("clock") == "shared":
                    # the sample clock is that of an instrument of openSciLab: no drift of its own
                    if self._origin is None:
                        self._origin = t0 - first / rate
                    t0 = self._origin + first / rate
                header["m"] = estimate.method
                if math.isfinite(estimate.uncertainty):
                    header["u"] = estimate.uncertainty
                # what the estimate was made of: openSciLab places earlier blocks again with all of it
                header["ta"] = arrived
                if self.arrival.started is not None:
                    header["ts"] = self.arrival.started
                if self.latency is not None:
                    header["l"] = [self.latency.value, self.latency.uncertainty]
            header["i"] = first
            header["t0"] = t0
            if self._loop is not None:
                self._find_loop_edge(payload, dtype, first)
        self.device._send_input(self, header, payload)

    # ------------------------------------------------------------ latency
    def calibrate(self, write: Callable[[int], Any], repeats: int = 10, interval: float = 0.05,
                  timeout: float = 5.0, threshold: Optional[float] = None, store: Optional[str] = None) -> Latency:
        """Measure the latency of this input with a loopback: an output of the same hardware, wired
        to this input, is switched with ``write(level)`` ``repeats`` times; the blocks must keep
        coming meanwhile (another thread). The latency is used from then on (and kept in ``store``,
        a JSON file, for later runs: ``dev.input(..., latency=store)``)."""
        if self.kind not in protocol.BLOCK_KINDS:
            raise ValueError(f"{self.name}: only an input of blocks has a latency")
        loop = Loopback()
        level = 0
        with self._lock:
            self._loop = {"loop": loop, "waiting": False, "level": None, "low": math.inf, "high": -math.inf,
                          "threshold": threshold}
        try:
            write(level)
            time.sleep(interval)
            for _ in range(int(repeats)):
                level ^= 1
                with self._lock:
                    self._loop["waiting"] = True
                    loop.command(self.device.now())
                write(level)
                deadline = time.monotonic() + timeout
                with self._lock:
                    while self._loop["waiting"] and time.monotonic() < deadline:
                        self._lock.wait(0.01)
                    if self._loop["waiting"]:
                        raise TimeoutError(f"{self.name}: no edge within {timeout:g} s - is the output wired to it "
                                           "and are blocks coming?")
                time.sleep(interval)
            with self._lock:
                latency = loop.latency(self.arrival)
        finally:
            with self._lock:
                self._loop = None
        self.latency = latency
        if store:
            save_latency(store, self.name, float(self.rate or 0.0), latency)
        return latency

    def _find_loop_edge(self, payload: bytes, dtype: str, first: int) -> None:
        state = self._loop
        values = _unpack(payload, dtype)
        if dtype == "u1":
            levels = [1 if value else 0 for value in values]
        else:
            state["low"], state["high"] = min(state["low"], min(values)), max(state["high"], max(values))
            middle = state["threshold"]
            if middle is None:
                middle = (state["low"] + state["high"]) / 2
            levels = [1 if value >= middle else 0 for value in values]
        last = state["level"] if state["level"] is not None else levels[0]
        for offset, level in enumerate(levels):
            if level != last and state["waiting"]:
                state["loop"].edge(first + offset)
                state["waiting"] = False
                self._lock.notify_all()
            last = level
        state["level"] = last


def save_latency(path: str, name: str, rate: float, latency: Latency) -> None:
    """Keep the latency of the input ``name`` at ``rate`` in the JSON file ``path``."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        data = {}
    data[name] = {"rate": rate, **latency.to_dict(), "measured": time.time()}
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)


def load_latency(path: str, name: str, rate: float) -> Optional[Latency]:
    """The latency kept for ``name`` at ``rate`` (``None``: none, or measured at another rate)."""
    try:
        with open(path, encoding="utf-8") as handle:
            entry = json.load(handle).get(name)
        if entry and abs(float(entry.get("rate", 0.0)) - rate) < 1e-9 * max(rate, 1.0):
            return Latency.from_dict(entry)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return None


def _unpack(payload: bytes, dtype: str) -> list:
    if dtype == "u1":
        return list(payload)
    return list(struct.unpack(f"<{len(payload) // 4}f", payload))


def _pack(samples: Iterable, dtype: str) -> bytes:
    """Little endian samples (a numpy array is converted without copying in Python)."""
    try:
        import numpy as np

        array = np.asarray(samples, dtype={"f4": "<f4", "u1": "u1"}[dtype])
        if dtype == "u1":
            array = (array != 0).astype("u1")
        return array.tobytes()
    except ImportError:  # pragma: no cover - devices without numpy
        values = list(samples)
        if dtype == "u1":
            return bytes(1 if value else 0 for value in values)
        return struct.pack(f"<{len(values)}f", *values)


class Output(_Port):
    """Something openSciLab sets. The function given to :meth:`on_set` gets ``(value, at)``;
    it is called at the time openSciLab asked for (``at``, this device's clock), or at once."""

    def __init__(self, *args, default: Any = None, **kwargs) -> None:
        super().__init__(*args, default=default, **kwargs)
        self.value = default
        self._handler: Optional[Callable[[Any, Optional[float]], Any]] = None

    def on_set(self, function: Callable[[Any, Optional[float]], Any]) -> Callable:
        self._handler = function
        return function

    def _apply(self, value: Any, at: Optional[float]) -> None:
        if self.kind == "bool":
            value = bool(value)
        elif self.kind == "scalar":
            value = float(value)
            limits = self.extra.get("range")
            if limits and not limits[0] <= value <= limits[1]:
                raise ValueError(f"{self.name}: {value} is outside {limits[0]}..{limits[1]}")
        if self._handler is not None:
            self._handler(value, at)
        self.value = value


class Command(_Port):
    """A function openSciLab can call by name; its keyword arguments come from the flow, its
    result (JSON: numbers, text, lists, mappings) goes back."""

    def __init__(self, device: Device, name: str, function: Callable[..., Any], description: str = "") -> None:
        super().__init__(device, name, "command", description=description)
        self.function = function

    def describe(self) -> dict:
        return {"name": self.name, "description": self.description}


class SyncOutput(_Port):
    """A synchronisation signal this device drives: ``write(level)`` switches the pin; the device
    toggles it at irregular intervals while openSciLab asks for it and reports the time of every
    edge. Wire the pin also to a channel openSciLab records."""

    def __init__(self, device: Device, name: str, write: Callable[[int], Any], min_interval: float = 0.02,
                 max_interval: float = 0.06, description: str = "") -> None:
        super().__init__(device, name, "output", description=description)
        self.write, self.min_interval, self.max_interval = write, float(min_interval), float(max_interval)
        self.level = 0

    def describe(self) -> dict:
        return {"name": self.name, "kind": "output", "description": self.description}


class SyncInput(_Port):
    """A synchronisation signal this device observes (a pulse openSciLab or a GPS receiver
    makes): call :meth:`edge` for every edge, with its time if the hardware stamps it."""

    def __init__(self, device: Device, name: str, description: str = "") -> None:
        super().__init__(device, name, "input", description=description)

    def describe(self) -> dict:
        return {"name": self.name, "kind": "input", "description": self.description}

    def edge(self, level: int = 1, at: Optional[float] = None) -> None:
        self.device._sync_edge(self.name, self.device.now() if at is None else at, level)


class Device:
    """A device for openSciLab. ``server``: the address of the computer with openSciLab (``None``:
    found by its beacon in the local network); ``token``: the token openSciLab shows in its
    settings; ``clock``: the device's time in seconds (a monotonic clock)."""

    def __init__(self, name: str, server: Optional[str] = None, port: int = protocol.DEFAULT_PORT, token: str = "",
                 description: str = "", clock: Callable[[], float] = time.monotonic, buffer: int = 100_000,
                 reconnect: bool = True, connect: Optional[Callable[[str, int], socket.socket]] = None,
                 timescale: Optional[str] = None, timescale_accuracy: float = 0.0) -> None:
        """``timescale``: ``clock`` follows a shared time scale (``utc``, ``tai``: PTP, GPS, NTP; e.g.
        ``clock=timing.timescale_clock("tai")``), ``timescale_accuracy`` how closely (seconds)."""
        if timescale is not None and timescale not in TIMESCALES:
            raise ValueError(f"timescale is one of {', '.join(TIMESCALES)}")
        self.name, self.server, self.port, self.token = name, server, int(port), token
        self.description_text = description
        self.now = clock
        self.timescale, self.timescale_accuracy = timescale, float(timescale_accuracy)
        self.reconnect = reconnect
        self._connect = connect or (lambda host, port: socket.create_connection((host, port), timeout=5.0))
        self.inputs: dict[str, Input] = {}
        self.outputs: dict[str, Output] = {}
        self.commands: dict[str, Command] = {}
        self.syncs: dict[str, _Port] = {}
        #: a new one for every start of the script: openSciLab knows whether the clock went on
        self.clock_id = uuid.uuid4().hex
        self._socket: Optional[socket.socket] = None
        self._send_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._backlog: deque = deque(maxlen=buffer)
        self._subscribed: set[str] = set()
        self._latest: dict[str, tuple[dict, bytes]] = {}
        self._schedule: list = []
        self._schedule_counter = itertools.count()
        self._schedule_event = threading.Event()
        self._sync_edges: dict[str, list] = {}
        self._stop = threading.Event()
        self._connected = threading.Event()
        self._threads: list[threading.Thread] = []
        #: why the last connection ended or was refused ("" while connected)
        self.last_error = ""

    # ---------------------------------------------------------- description
    def input(self, name: str, kind: str = "scalar", unit: str = "", rate: Optional[float] = None,
              range: Optional[tuple] = None, description: str = "", sync: bool = False,
              clock: Optional[str] = None, latency: Union[None, float, Latency, str] = None) -> Input:
        """``sync``: the input records a sync signal (openSciLab finds its edges in the blocks);
        ``clock="shared"``: its sample clock comes from an instrument of openSciLab (no drift of its
        own); ``latency``: seconds, a :class:`Latency` or the JSON file :meth:`Input.calibrate` keeps."""
        if kind not in protocol.INPUT_KINDS:
            raise ValueError(f"kind is one of {', '.join(protocol.INPUT_KINDS)}")
        if kind in protocol.BLOCK_KINDS and not rate:
            raise ValueError(f"a {kind} input needs its rate (samples per second)")
        if (sync or clock or latency is not None) and kind not in protocol.BLOCK_KINDS:
            raise ValueError("sync, clock and latency are for inputs of blocks (analog, digital)")
        if clock not in (None, "shared"):
            raise ValueError("clock is None or 'shared'")
        if isinstance(latency, (int, float)):
            latency = Latency(float(latency), 0.0, "given")
        elif isinstance(latency, str):
            latency = load_latency(latency, name, float(rate)) if os.path.exists(latency) else None
        port = Input(self, name, kind, unit, description, rate=float(rate) if rate else None,
                     range=list(range) if range else None, sync=True if sync else None, clock=clock,
                     latency=latency)
        self.inputs[name] = port
        return port

    def output(self, name: str, kind: str = "scalar", unit: str = "", range: Optional[tuple] = None,
               default: Any = None, description: str = "") -> Output:
        if kind not in protocol.OUTPUT_KINDS:
            raise ValueError(f"kind is one of {', '.join(protocol.OUTPUT_KINDS)}")
        port = Output(self, name, kind, unit, description, range=list(range) if range else None, default=default)
        self.outputs[name] = port
        return port

    def command(self, name: Optional[str] = None, description: str = "") -> Callable:
        """Decorator: ``@dev.command()`` makes the function a command (its name by default)."""
        def register(function: Callable) -> Callable:
            command_name = name or function.__name__
            self.commands[command_name] = Command(self, command_name, function, description or (function.__doc__ or ""))
            return function
        return register

    def sync_output(self, name: str, write: Callable[[int], Any], min_interval: float = 0.02,
                    max_interval: float = 0.06, description: str = "") -> SyncOutput:
        port = SyncOutput(self, name, write, min_interval, max_interval, description)
        self.syncs[name] = port
        return port

    def sync_input(self, name: str, description: str = "") -> SyncInput:
        port = SyncInput(self, name, description)
        self.syncs[name] = port
        return port

    def describe(self) -> dict:
        return {"name": self.name, "description": self.description_text,
                "inputs": [port.describe() for port in self.inputs.values()],
                "outputs": [port.describe() for port in self.outputs.values()],
                "commands": [port.describe() for port in self.commands.values()],
                "sync": [port.describe() for port in self.syncs.values()]}

    # ----------------------------------------------------------- lifecycle
    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def start(self) -> Device:
        """Connect in the background (and again after the connection was lost)."""
        protocol.check_description(self.describe())
        self._stop.clear()
        for target, name in ((self._run, "connection"), (self._scheduler, "scheduler")):
            thread = threading.Thread(target=target, name=f"openscilab-device-{name}", daemon=True)
            thread.start()
            self._threads.append(thread)
        return self

    def wait_connected(self, timeout: Optional[float] = None) -> bool:
        return self._connected.wait(timeout)

    def stop(self) -> None:
        self._stop.set()
        self._schedule_event.set()
        sock = self._socket
        if sock is not None:
            try:
                self._send({"t": "bye"})
            except OSError:
                pass
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(2.0)
        self._threads.clear()

    def __enter__(self) -> Device:
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    def run_forever(self) -> None:
        """Start and wait (for scripts that only react to openSciLab); Ctrl+C ends it."""
        self.start()
        try:
            while not self._stop.wait(0.5):
                pass
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    # --------------------------------------------------------- connection
    def _address(self) -> Optional[tuple[str, int]]:
        if self.server:
            host, _, port = self.server.partition(":")
            return host, int(port) if port else self.port
        from .discovery import find_server

        return find_server(timeout=3.0)

    def _run(self) -> None:
        delay = 0.2
        while not self._stop.is_set():
            address = self._address()
            if address is None:
                self.last_error = "no openSciLab found in the network"
            else:
                try:
                    sock = self._connect(*address)
                    sock.settimeout(None)
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                    self._session(sock)
                    delay = 0.2
                except (OSError, protocol.ProtocolError) as error:
                    self.last_error = str(error)
                finally:
                    self._connected.clear()
                    self._socket = None
            if not self.reconnect or self._stop.is_set():
                return
            self._stop.wait(delay)
            delay = min(delay * 2, 5.0)

    def _session(self, sock: socket.socket) -> None:
        header, _payload = protocol.receive(sock)
        if header.get("t") != "challenge":
            raise protocol.ProtocolError("openSciLab did not greet")
        hello = {"t": "hello", "protocol": protocol.PROTOCOL, "description": self.describe(),
                 "clock_id": self.clock_id, "package": "openscilab_device"}
        if self.timescale:
            hello["timescale"] = self.timescale
            hello["timescale_accuracy"] = self.timescale_accuracy
        if header.get("token"):
            hello["proof"] = protocol.proof(self.token, str(header.get("nonce", "")))
        sock.sendall(protocol.encode(hello))
        header, _payload = protocol.receive(sock)
        if header.get("t") == "refused":
            self.last_error = str(header.get("reason") or "refused")
            self._stop.wait(2.0)
            raise protocol.ProtocolError(self.last_error)
        if header.get("t") != "welcome":
            raise protocol.ProtocolError("openSciLab did not accept the device")
        with self._state_lock:
            self._socket = sock
            self._subscribed = set()
        self.last_error = ""
        self._connected.set()  # (what was kept follows once openSciLab subscribed)
        try:
            while not self._stop.is_set():
                header, payload = protocol.receive(sock)
                if not self._handle(header, payload):
                    break
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def _send(self, header: dict, payload: bytes = b"") -> None:
        sock = self._socket
        if sock is None:
            raise OSError("not connected")
        data = protocol.encode(header, payload)
        with self._send_lock:
            sock.sendall(data)

    def _send_input(self, port: _Port, header: dict, payload: bytes = b"") -> None:
        with self._state_lock:
            self._latest[port.name] = (header, payload)
            if not self.connected:
                self._backlog.append((port.name, header, payload))
                return
            if port.name not in self._subscribed:
                return
        try:
            self._send(header, payload)
        except OSError:
            with self._state_lock:
                self._backlog.append((port.name, header, payload))

    def _resend(self) -> None:
        """After a subscription: what was kept while openSciLab did not listen."""
        with self._state_lock:
            backlog = [item for item in self._backlog if item[0] in self._subscribed]
            self._backlog = deque((item for item in self._backlog if item[0] not in self._subscribed),
                                  maxlen=self._backlog.maxlen)
            latest = [self._latest[name] for name in self._subscribed if name in self._latest
                      and self.inputs.get(name) is not None and self.inputs[name].kind not in protocol.BLOCK_KINDS]
        sent = {id(item[1]) for item in backlog}
        for _name, header, payload in backlog:
            self._send(header, payload)
        for header, payload in latest:
            if id(header) not in sent:
                self._send(header, payload)  # the current value of a slow input

    # ------------------------------------------------------------ messages
    def _handle(self, header: dict, payload: bytes) -> bool:
        kind = header.get("t")
        if kind == "ping":
            received = self.now()
            self._send({"t": "pong", "id": header.get("id"), "t1": received, "t2": self.now()})
        elif kind == "subscribe":
            with self._state_lock:
                self._subscribed = {str(name) for name in header.get("channels") or []}
            self._resend()
        elif kind == "set":
            self._plan(header.get("at"), lambda header=header: self._set(header))
        elif kind == "call":
            threading.Thread(target=self._call, args=(header,), daemon=True).start()
        elif kind == "bye":
            return False
        return True

    def _set(self, header: dict) -> None:
        output = self.outputs.get(str(header.get("ch")))
        error = "" if output is not None else f"no output {header.get('ch')!r}"
        if output is not None:
            try:
                output._apply(header.get("v"), header.get("at"))
            except Exception as failure:  # noqa: BLE001 - reported to openSciLab
                error = str(failure)
        try:
            self._send({"t": "done", "id": header.get("id"), "at": self.now(), "error": error})
        except OSError:
            pass

    def _call(self, header: dict) -> None:
        command = self.commands.get(str(header.get("name")))
        answer: dict = {"t": "result", "id": header.get("id")}
        if command is None:
            answer["error"] = f"no command {header.get('name')!r}"
        else:
            try:
                answer["value"] = command.function(**(header.get("args") or {}))
            except Exception as failure:  # noqa: BLE001 - reported to openSciLab
                answer["error"] = str(failure)
        try:
            self._send(answer)
        except (OSError, TypeError, ValueError) as failure:
            try:
                self._send({"t": "result", "id": header.get("id"), "error": f"the result cannot be sent: {failure}"})
            except OSError:
                pass

    # ------------------------------------------------------------ schedule
    def _plan(self, at: Optional[float], action: Callable[[], None]) -> None:
        with self._state_lock:
            heapq.heappush(self._schedule, (float(at) if at is not None else self.now(), next(self._schedule_counter),
                                            action))
        self._schedule_event.set()

    def _scheduler(self) -> None:
        """Runs planned outputs at their time and drives the sync outputs."""
        next_toggle: dict[str, float] = {}
        while not self._stop.is_set():
            now = self.now()
            due = []
            with self._state_lock:
                while self._schedule and self._schedule[0][0] <= now:
                    due.append(heapq.heappop(self._schedule)[2])
                wake = self._schedule[0][0] if self._schedule else now + 0.05
                active = [name for name in self.syncs if name in self._subscribed]
            for action in due:
                action()
            for name in active:
                port = self.syncs[name]
                if isinstance(port, SyncOutput):
                    when = next_toggle.setdefault(name, now)
                    if now >= when:
                        port.level ^= 1
                        port.write(port.level)
                        self._sync_edge(name, self.now(), port.level)
                        next_toggle[name] = self.now() + random.uniform(port.min_interval, port.max_interval)
                    wake = min(wake, next_toggle[name])
            self._send_sync_edges()
            remaining = wake - self.now()
            if remaining > 0.003:
                self._schedule_event.wait(min(remaining - 0.002, 0.05))
                self._schedule_event.clear()
            else:
                time.sleep(0.0002)  # the last milliseconds: short naps, so the time is kept closely

    def _sync_edge(self, name: str, at: float, level: int) -> None:
        with self._state_lock:
            self._sync_edges.setdefault(name, []).append([at, int(level)])

    def _send_sync_edges(self) -> None:
        with self._state_lock:
            if not self.connected or not self._sync_edges:
                return
            ready = {name: edges for name, edges in self._sync_edges.items() if edges}
            self._sync_edges = {}
        for name, edges in ready.items():
            try:
                self._send({"t": "sync", "name": name, "edges": edges})
            except OSError:
                return
