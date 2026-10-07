# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The application's side of a device process: an instrument whose driver and facets live there.

:class:`Connection` starts the process (:mod:`.host`), sends calls and receives answers and events.
:class:`ProcessDriver` is the driver for the application - the same interface as every driver: what
does not change while the device is open is read once, captures start there and report here, their
sessions are the application's own objects. Facets, sub-drivers and functions the device returns are
:class:`RemoteObject` s that pass every call on. When the process ends the instrument is disconnected
and a running capture fails with a message; when the application ends, the process stops the device
and ends too. Qt free.
"""

from __future__ import annotations

import itertools
import logging
import multiprocessing
import sys
import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any, Callable, Optional

from ...core.instrument import CaptureFacet, Instrument, InstrumentError, InstrumentStatus
from ..base import AnalyzerDriverBase, CaptureCompletedArgs, CaptureError, DeviceConnectionError
from . import host as host_module
from . import wire

log = logging.getLogger(__name__)

#: seconds to wait for a device process to open its device
OPEN_TIMEOUT = 90.0
#: a call the device process does not answer within this time ends it: a driver stuck in a call
#: that never returns would otherwise freeze what waits for it (seconds; ``None``: wait)
CALL_TIMEOUT = 60.0
#: the device process sends a sign of life every second (``host.BEAT_INTERVAL``); none for this
#: long means it hangs (the interpreter stuck, the OS stopped it): it is ended (seconds)
BEAT_TIMEOUT = 10.0
#: "the timeout of the moment" (``CALL_TIMEOUT``, which tests lower)
DEFAULT_TIMEOUT = object()


class ProcessEnded(DeviceConnectionError, InstrumentError):
    """The device process ended (it crashed, or the device went away)."""


class Connection:
    """One device process."""

    def __init__(self, address: str, options: Optional[dict] = None, timeout: float = OPEN_TIMEOUT) -> None:
        context = multiprocessing.get_context("spawn")
        self.address = address
        self.requests, child_requests = context.Pipe()
        events, child_events = context.Pipe(duplex=False)
        self._events = events
        # the levels of the application's log and of its driver log (openscilab --debug-driver)
        levels = (logging.getLogger().getEffectiveLevel(), logging.getLogger("openscilab.driver").getEffectiveLevel())
        self.process = context.Process(target=host_module.main, name=f"openscilab-device {address}",
                                       args=(child_requests, child_events, address, dict(options or {}), levels),
                                       daemon=True)
        self.process.start()
        child_requests.close()
        child_events.close()
        self.pid = self.process.pid
        self._numbers = itertools.count(1)
        self._send_lock = threading.Lock()
        self._lock = threading.Lock()
        self._pending: dict[int, Future] = {}
        #: application objects the device process calls back (handlers), by id and by the object
        self._callbacks: dict[int, Callable] = {}
        self._callback_ids: list[tuple[Callable, int]] = []
        #: sessions of running captures: id -> the application's session
        self.sessions: dict[int, Any] = {}
        self._session_ids: dict[int, int] = {}
        #: sessions sent by value (a capture that starts: the device process gets its own copy)
        self._by_value: set[int] = set()
        self.ended = threading.Event()
        self.end_reason = ""
        self._killed = False
        #: when the last sign of life of the device process came (``time.monotonic``)
        self.last_beat = time.monotonic()
        #: the share of a processor core the device process took between its last two signs of life
        #: (``None`` until two came)
        self.cpu_load: Optional[float] = None
        self._last_times: Optional[tuple[float, float]] = None
        self.driver: Optional[ProcessDriver] = None
        self._listeners: list[Callable[[], None]] = []
        if not self.requests.poll(timeout):
            self.process.kill()
            raise DeviceConnectionError(f"{address}: the device process did not answer within {timeout:g} s")
        try:
            message = self._load(self.requests.recv_bytes())
        except (EOFError, OSError):
            self.process.join(2)
            raise DeviceConnectionError(f"{address}: the device process ended while opening "
                                        f"(exit code {self.process.exitcode})") from None
        _opened, ok, value = message
        if not ok:
            self.process.join(5)
            raise wire.unpack_error(value)
        self.description: dict = value
        self.last_beat = time.monotonic()
        threading.Thread(target=self._read_answers, name="openscilab-device-answers", daemon=True).start()
        threading.Thread(target=self._read_events, name="openscilab-device-events", daemon=True).start()
        threading.Thread(target=self._watch, name="openscilab-device-watchdog", daemon=True).start()

    # ------------------------------------------------------------ pickling
    def _reference(self, obj: Any) -> Optional[tuple]:
        if isinstance(obj, RemoteObject):
            return ("object", object.__getattribute__(obj, "_id"))
        if isinstance(obj, ProcessDriver):
            return ("object", wire.DRIVER)
        session_id = self._session_ids.get(id(obj))
        if session_id is not None and self.sessions.get(session_id) is obj and id(obj) not in self._by_value:
            return ("session", session_id)
        if wire.is_handler(obj):
            return ("callback", self._callback_id(obj))
        return None

    def _callback_id(self, function: Callable) -> int:
        with self._lock:
            for known, number in self._callback_ids:
                if known == function:
                    return number
            number = next(self._numbers)
            self._callback_ids.append((function, number))
            self._callbacks[number] = function
            return number

    def _resolve(self, pid: tuple) -> Any:
        if pid[0] == "object":
            number = pid[1]
            if number == wire.DRIVER and self.driver is not None:
                return self.driver
            return RemoteObject(self, number, pid[2] if len(pid) > 2 else {})
        if pid[0] == "session":
            return self.sessions.get(pid[1])
        raise ValueError(f"unknown reference {pid[0]!r}")

    def _load(self, data: bytes) -> Any:
        held = None
        if data[:1] != b"\x80":
            prefix, _, data = data.partition(b"|")
            held = int(prefix)
        message = wire.loads(data, self._resolve)
        if held is not None:
            self._send("ack", None, held)  # mapped: the device process may let them go
        return message

    def _send(self, kind: str, number: Optional[int], payload: Any) -> None:
        data, _keep = wire.dumps((kind, number, payload), self._reference)
        with self._send_lock:
            self.requests.send_bytes(data)

    # --------------------------------------------------------------- calls
    def request(self, kind: str, payload: Any, timeout: Any = DEFAULT_TIMEOUT) -> Any:
        """Ask the device process and wait for its answer; a call it does not answer within
        ``timeout`` seconds (default :data:`CALL_TIMEOUT`) ends the process (``None``: wait as
        long as it takes)."""
        if timeout is DEFAULT_TIMEOUT:
            timeout = CALL_TIMEOUT
        if self.ended.is_set():
            raise ProcessEnded(self._ended_text())
        number = next(self._numbers)
        future: Future = Future()
        with self._lock:
            self._pending[number] = future
        try:
            self._send(kind, number, payload)
        except (OSError, EOFError, BrokenPipeError):
            with self._lock:
                self._pending.pop(number, None)
            raise ProcessEnded(self._ended_text()) from None
        try:
            return future.result(timeout)
        except FutureTimeout:
            what = payload[1] if kind in ("call", "get", "set") and isinstance(payload, tuple) else kind
            log.warning("The device process of %s did not answer %s within %g s: ended", self.address, what, timeout)
            self._kill(f"the device process did not answer ({what}) within {timeout:g} s")
            raise ProcessEnded(self._ended_text()) from None

    def call(self, number: int, name: str, args: tuple = (), kwargs: Optional[dict] = None) -> Any:
        return self.request("call", (number, name, tuple(args), dict(kwargs or {})))

    def get(self, number: int, name: str) -> Any:
        return self.request("get", (number, name))

    def set(self, number: int, name: str, value: Any) -> None:
        self.request("set", (number, name, value))

    def _read_answers(self) -> None:
        while True:
            try:
                kind, number, value = self._load(self.requests.recv_bytes())
            except (EOFError, OSError):
                break
            except Exception:
                log.exception("An answer of the device process %s cannot be read", self.address)
                continue
            with self._lock:
                future = self._pending.pop(number, None)
            if future is None:
                continue
            if kind == "ok":
                future.set_result(value)
            else:
                future.set_exception(wire.unpack_error(value))
        self._end("the device process ended")

    def _read_events(self) -> None:
        while True:
            try:
                data = self._events.recv_bytes()
            except (EOFError, OSError):
                break
            try:
                kind, payload = self._load(data)
                self._event(kind, payload)
            except Exception:
                log.exception("An event of the device process %s failed", self.address)
        self._end("the device process ended")

    def _event(self, kind: str, payload: Any) -> None:
        if kind == "beat":
            self.last_beat = time.monotonic()
            wall, cpu = payload
            if self._last_times is not None and wall > self._last_times[0]:
                self.cpu_load = max(cpu - self._last_times[1], 0.0) / (wall - self._last_times[0])
            self._last_times = (wall, cpu)
        elif kind == "log":
            name, level, text = payload
            logging.getLogger(name).log(level, "%s", text)
        elif kind == "callback":
            number, args, kwargs = payload
            function = self._callbacks.get(number)
            if function is not None:
                function(*args, **kwargs)
        elif self.driver is not None:
            self.driver._event(kind, payload)

    def _ended_text(self) -> str:
        return f"{self.address}: {self.end_reason or 'the device process ended'}"

    def _watch(self) -> None:
        """Ends a device process that stopped giving signs of life (see :data:`BEAT_TIMEOUT`)."""
        while not self.ended.wait(1.0):
            silent = time.monotonic() - self.last_beat
            if silent > BEAT_TIMEOUT and self.process.is_alive():
                log.warning("The device process of %s gave no sign of life for %.0f s: ended", self.address, silent)
                self._kill(f"the device process stopped answering ({silent:.0f} s without a sign of life)")
                return

    def _kill(self, reason: str) -> None:
        """End a device process that hangs: killed, not asked (it would not answer)."""
        self._killed = True
        self.end_reason = reason  # (before the kill: the readers see the pipes close and end it too)
        try:
            self.process.kill()
        except (OSError, ValueError):
            log.debug("The device process of %s could not be killed", self.address, exc_info=True)
        self._end(reason)

    def _end(self, reason: str) -> None:
        if self.ended.is_set():
            return
        self.process.join(0.5)
        code = self.process.exitcode
        if self._killed:
            reason = self.end_reason or reason
        self.end_reason = reason if code in (0, None) or self._killed else f"{reason} (exit code {code})"
        self.ended.set()
        with self._lock:
            pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(ProcessEnded(self._ended_text()))
        if self.driver is not None:
            self.driver._process_ended(self._ended_text())
        for listener in list(self._listeners):
            try:
                listener()
            except Exception:
                log.exception("listener of the device process")

    def on_end(self, listener: Callable[[], None]) -> None:
        self._listeners.append(listener)

    @property
    def alive(self) -> bool:
        return not self.ended.is_set() and self.process.is_alive()

    def register_session(self, session: Any) -> int:
        number = next(self._numbers)
        with self._lock:
            self.sessions[number] = session
            self._session_ids[id(session)] = number
        return number

    def forget_session(self, number: int) -> None:
        with self._lock:
            session = self.sessions.pop(number, None)
            if session is not None and self._session_ids.get(id(session)) == number:
                self._session_ids.pop(id(session), None)

    def close(self, timeout: float = 5.0) -> None:
        if not self.ended.is_set():
            try:
                number = next(self._numbers)
                future: Future = Future()
                with self._lock:
                    self._pending[number] = future
                self._send("close", number, None)
                future.result(timeout)
            except Exception:  # noqa: BLE001 - a process that does not answer is ended below
                log.debug("The device process of %s did not confirm its close", self.address, exc_info=True)
        self.process.join(timeout)
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(2)
        self._end("closed")


class RemoteObject:
    """An object of the device process (a facet, a sub-driver, a function): every attribute and call
    goes there. ``instrument`` (set by the instrument holding a facet) stays here."""

    _LOCAL = ("instrument",)

    def __init__(self, connection: Connection, number: int, members: dict[str, str],
                 constants: Optional[dict] = None) -> None:
        object.__setattr__(self, "_connection", connection)
        object.__setattr__(self, "_id", number)
        object.__setattr__(self, "_members", dict(members))
        object.__setattr__(self, "_constants", dict(constants or {}))

    def __getattr__(self, name: str) -> Any:
        members = object.__getattribute__(self, "_members")
        kind = members.get(name)
        if kind is None or name.startswith("__"):
            raise AttributeError(name)
        constants = object.__getattribute__(self, "_constants")
        if name in constants:
            return constants[name]
        connection = object.__getattribute__(self, "_connection")
        number = object.__getattribute__(self, "_id")
        if kind == "method":
            def method(*args: Any, **kwargs: Any) -> Any:
                return connection.call(number, name, args, kwargs)

            method.__name__ = name
            return method
        return connection.get(number, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in self._LOCAL:
            object.__setattr__(self, name, value)
            return
        connection = object.__getattribute__(self, "_connection")
        connection.set(object.__getattribute__(self, "_id"), name, value)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        connection = object.__getattribute__(self, "_connection")
        return connection.call(object.__getattribute__(self, "_id"), "__call__", args, kwargs)

    def __repr__(self) -> str:
        return f"<RemoteObject {object.__getattribute__(self, '_id')} of {object.__getattribute__(self, '_connection').address}>"


#: members of the driver base the proxy implements itself; every other one is passed on
_OWN = frozenset({
    "add_capture_completed_handler", "remove_capture_completed_handler", "add_capture_progress_handler",
    "remove_capture_progress_handler", "add_capture_tile_handler", "remove_capture_tile_handler",
    "wait_for_events", "start_capture", "stop_capture", "dispose", "is_capturing",
})
#: methods that answer the same for the same arguments while the device is open: asked once each
_PURE = frozenset({"get_limits", "max_frequency_for", "sample_rates", "memory_depth", "sample_bits",
                   "get_capture_mode"})


class ProcessDriver(AnalyzerDriverBase):
    """The driver of a device process (see the module)."""

    #: the device process wraps its driver in the software trigger itself (next to the device)
    software_trigger_inside = True

    def __init__(self, connection: Connection) -> None:
        super().__init__()
        self._connection = connection
        description = connection.description
        self._members: dict[str, str] = dict(description.get("driver") or {})
        self._snapshot: dict = dict(description.get("snapshot") or {})
        self._pure: dict = {}
        self._capturing = False
        self._handlers: dict[int, Any] = {}
        #: ``time.monotonic`` right before the device process told the device to start (the earliest
        #: a sample of the capture can have been taken)
        self.command_time: Optional[float] = None
        connection.driver = self

    # ---------------------------------------------------------- passed on
    def _pass(self, name: str, args: tuple, kwargs: dict) -> Any:
        if not args and not kwargs and name + "()" in self._snapshot:
            return self._snapshot[name + "()"]
        if name in _PURE:
            key = _frozen((name, args, tuple(sorted(kwargs.items()))))
            try:
                hash(key)
            except TypeError:
                key = None
            if key is not None and key in self._pure:
                return self._pure[key]
            value = self._connection.call(wire.DRIVER, name, args, kwargs)
            if key is not None:
                self._pure[key] = value
            return value
        return self._connection.call(wire.DRIVER, name, args, kwargs)

    def _get(self, name: str) -> Any:
        if name in self._snapshot:
            return self._snapshot[name]
        return self._connection.get(wire.DRIVER, name)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        kind = self.__dict__.get("_members", {}).get(name)
        if kind is None:
            raise AttributeError(name)
        if kind == "method":
            def method(*args: Any, **kwargs: Any) -> Any:
                return self._pass(name, args, kwargs)

            method.__name__ = name
            return method
        return self._get(name)

    def refresh(self) -> None:
        """Read again what was read once (after the device reconnected)."""
        self._snapshot = dict(self._connection.request("snapshot", None) or {})
        self._pure.clear()

    # ------------------------------------------------------------ captures
    @property
    def is_capturing(self) -> bool:
        return self._capturing

    @property
    def process(self) -> Connection:
        return self._connection

    def start_capture(self, session: Any, completed_handler: Optional[Any] = None, **kwargs: Any) -> CaptureError:
        connection = self._connection
        # known before it starts (its first progress may come before the answer), sent by value
        number = connection.register_session(session)
        self._handlers[number] = completed_handler
        # capturing from before the command on: a short capture can end - and say so - before the
        # answer to its start arrives (a slow computer); set after the answer, the flag stayed on
        self._capturing = True
        connection._by_value.add(id(session))
        try:
            error, commanded = connection.request("start", (number, session, kwargs))
        except BaseException:
            self._handlers.pop(number, None)
            connection.forget_session(number)
            self._capturing = bool(self._handlers)
            raise
        finally:
            connection._by_value.discard(id(session))
        if error != CaptureError.NONE:
            self._handlers.pop(number, None)
            connection.forget_session(number)
            self._capturing = bool(self._handlers)
            return error
        self.command_time = commanded
        return error

    def stop_capture(self) -> bool:
        if self._connection.ended.is_set():
            return False
        return bool(self._connection.call(wire.DRIVER, "stop_capture"))

    def _event(self, kind: str, payload: Any) -> None:
        if kind == "progress":
            number, args = payload
            if args.session is not None:
                self._raise_capture_progress(args)
        elif kind == "tile":
            number, args = payload
            if args.session is not None:
                self._raise_capture_tile(args)
        elif kind == "completed":
            number, args, state = payload
            session = self._connection.sessions.get(number)
            if session is not None:
                wire.adopt(session, state)
                args.session = session
            self._connection.forget_session(number)
            handler = self._handlers.pop(number, None)
            if not self._handlers:
                self._capturing = False
            self._raise_capture_completed(args, handler)

    def _process_ended(self, reason: str) -> None:
        handlers, self._handlers = self._handlers, {}
        self._capturing = False
        for number, handler in handlers.items():
            session = self._connection.sessions.get(number)
            self._connection.forget_session(number)
            if session is not None:
                self._raise_capture_completed(CaptureCompletedArgs(success=False, session=session, error=reason),
                                              handler)

    def dispose(self) -> None:
        self._connection.close()
        super().dispose()


def _frozen(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return tuple(_frozen(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(value)
    return value


def _passed(name: str, member: Any) -> Any:
    if isinstance(member, property):
        return property(lambda self: self._get(name), doc=member.__doc__)

    def method(self: ProcessDriver, *args: Any, **kwargs: Any) -> Any:
        return self._pass(name, args, kwargs)

    method.__name__ = name
    method.__doc__ = getattr(member, "__doc__", None)
    return method


for _name, _member in list(vars(AnalyzerDriverBase).items()):
    if _name.startswith("_") or _name in _OWN or _name in vars(ProcessDriver):
        continue
    if isinstance(_member, property) or callable(_member):
        setattr(ProcessDriver, _name, _passed(_name, _member))
# plain values of the base (``is_simulator = False``) that a driver sets to its own: read once, there
for _name in host_module.STATIC_PROPERTIES:
    _member = vars(AnalyzerDriverBase).get(_name)
    if _member is not None and not isinstance(_member, property) and not callable(_member):
        setattr(ProcessDriver, _name, property(lambda self, name=_name: self._get(name)))
del _name, _member


class ProcessInstrument(Instrument):
    """An instrument whose device runs in a device process."""

    def __init__(self, connection: Connection) -> None:
        description = connection.description
        super().__init__(description["name"], kind=description["kind"], uri=description["uri"],
                         status=InstrumentStatus(description["status"]),
                         trigger_outputs=tuple(description["trigger_outputs"]),
                         trigger_inputs=tuple(description["trigger_inputs"]))
        self.process = connection
        if description.get("driver") is not None:
            self.add_facet(CaptureFacet(ProcessDriver(connection)))
        for number, paths, members, constants in description["facets"]:
            facet = RemoteObject(connection, number, members, constants)
            facet.instrument = self
            for path in paths:
                cls = wire.load_class(path)
                if cls is None or cls.__name__ in ("Facet", "object") or path.endswith(":Facet"):
                    continue
                self._facets.setdefault(cls, facet)  # type: ignore[arg-type]
        connection.on_end(self._ended)

    def _ended(self) -> None:
        self.status = InstrumentStatus.DISCONNECTED

    def facets(self) -> list:
        unique: list = []
        for facet in self._facets.values():
            if not any(facet is known for known in unique):
                unique.append(facet)
        return unique

    def pins(self) -> list:
        if self.gpio is None and self.capture is not None:
            return super().pins()
        return self.process.call(wire.INSTRUMENT, "pins") if self.process.alive else []

    def details(self) -> list[tuple[str, list[tuple[str, str]]]]:
        sections = super().details() if self.process.alive else [("Instrument", [("Name", self.name)])]
        sections.append(("Process", [
            ("Device process", f"pid {self.process.pid}" + ("" if self.process.alive else
                                                             f" - {self.process.end_reason or 'ended'}")),
            ("Priority", _priority_of(self.process.pid) if self.process.alive else "-"),
            ("Why", "the device is read in a process of its own: nothing the application does can delay it"),
        ]))
        return sections

    @property
    def is_simulated(self) -> bool:
        return self.status == InstrumentStatus.SIMULATED or self.uri.startswith("sim:")

    def close(self) -> None:
        if self.process.alive:
            # the facets close with the instrument in the device process
            try:
                capture = self.capture
                if capture is not None:
                    capture.driver.dispose()
                else:
                    self.process.close()
            except Exception:
                log.exception("closing the device process of %s", self.name)
        self.status = InstrumentStatus.DISCONNECTED


def open_instrument(address: str, timeout: float = OPEN_TIMEOUT, **options: Any) -> ProcessInstrument:
    """The instrument at ``address`` (as ``lab.engine.devices.open_instrument`` opens it), in a device
    process of its own. ``factory="module:function"`` opens it with that function instead."""
    started = time.monotonic()
    from .. import kinds

    kind = kinds.find(kinds.split(address)[0])
    if kind is not None and not kind.builtin and kind.module and kind.module != "__main__":
        options = {**options, "modules": [kind.module]}  # (the device process loads openSciLab's own)
    connection = Connection(address, options, timeout)
    log.debug("Device process %s for %s ready after %.2f s", connection.pid, address, time.monotonic() - started)
    return ProcessInstrument(connection)


def _priority_of(pid: Optional[int]) -> str:
    from ...core import priority

    text = priority.describe(pid or 0)
    if not priority.is_raised(pid or 0) and sys.platform != "win32":
        text += " - Settings → Devices: high priority"
    return text
