# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The device process: an instrument opened and driven in a process of its own.

It reads the device with nothing of the application beside it: no drawing, no flows, no garbage
collection of the application's objects can make its reading thread wait. The samples go into
shared memory (``core/sample_store.share_arrays``); progress, completion and the calls of the
application's handlers go back as events, in order - a newer progress of a capture replaces one
not sent yet, so a busy application lags behind but never slows the reading down.

:func:`main` is the process (started by :mod:`.proxy`): it opens the instrument, answers the
application's calls (several at a time, as the application calls a driver from several threads),
and ends when the application closes it or goes away. Qt free.
"""

from __future__ import annotations

import gc
import itertools
import logging
import os
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

from ...core import sample_store
from ...core.shared_arrays import Sent
from . import wire

log = logging.getLogger(__name__)

#: calls answered at a time (the application calls a driver from several threads)
WORKERS = 8
#: a sign of life to the application this often (seconds; ``proxy.BEAT_TIMEOUT`` ends a process
#: without one)
BEAT_INTERVAL = 1.0


class _Events:
    """Sends events in order from a thread of its own; a pending progress of a capture is replaced
    by a newer one (its samples are views of the same arrays: nothing is lost)."""

    def __init__(self, connection, host: "Host") -> None:
        self.connection = connection
        self.host = host
        self._order: deque = deque()
        self._payloads: dict = {}
        self._condition = threading.Condition()
        self._numbers = itertools.count()
        self._closed = False
        threading.Thread(target=self._run, name="openscilab-device-events", daemon=True).start()
        threading.Thread(target=self._beat, name="openscilab-device-beat", daemon=True).start()

    def _beat(self) -> None:
        """A sign of life every :data:`BEAT_INTERVAL` seconds, from a thread of its own: it goes on
        while a call takes long, it stops when the interpreter is stuck or the OS stopped the process.
        It carries the processor time the process used so far (the application shows its load)."""
        while not self._closed:
            # (a beat not sent yet is replaced, not queued)
            self.put("beat", (time.monotonic(), time.process_time()), key="beat")
            time.sleep(BEAT_INTERVAL)

    def put(self, kind: str, payload: Any, key: Any = None) -> None:
        with self._condition:
            if key is None:
                key = (kind, next(self._numbers))
            if key not in self._payloads:
                self._order.append(key)
            self._payloads[key] = (kind, payload)
            self._condition.notify()

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._order and not self._closed:
                    self._condition.wait()
                if not self._order:
                    return
                key = self._order.popleft()
                kind, payload = self._payloads.pop(key)
            try:
                if kind == "completed":
                    # the progress sent before still named the session; from here it goes by value
                    self.host.forget_session(payload[0])
                data, keep = wire.dumps((kind, payload), self.host.reference)
                held = self.host.sent.hold(keep)
                self.connection.send_bytes(data if held is None else b"%d|" % held + data)
            except (OSError, EOFError):
                return
            except Exception:
                log.exception("An event of the device process (%s) cannot be sent", kind)

    def flush(self, timeout: float = 2.0) -> None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self._condition:
                if not self._order:
                    return
            time.sleep(0.01)

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify()


class _LogForward(logging.Handler):
    def __init__(self, events: _Events) -> None:
        super().__init__()
        self.events = events

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = record.getMessage()
            if record.exc_info:
                text += "\n" + logging.Formatter().formatException(record.exc_info)
            self.events.put("log", (record.name, record.levelno, text))
        except Exception:
            pass  # (a logging handler cannot log what it failed to forward)


class Host:
    """The objects of the device process the application reaches, and its calls."""

    def __init__(self, requests, events) -> None:
        self.requests = requests
        self.sent = Sent()
        self.events = _Events(events, self)
        self.objects: dict[int, Any] = {}
        self._ids: dict[int, int] = {}
        self._numbers = itertools.count(100)
        self._lock = threading.Lock()
        #: capture id -> its session, and the reverse (by id of the session)
        self.sessions: dict[int, Any] = {}
        self._session_ids: dict[int, int] = {}
        self._callbacks: dict[int, Any] = {}
        self._reply_lock = threading.Lock()
        self.instrument = None
        self.driver = None

    # ------------------------------------------------------------- objects
    def export(self, obj: Any) -> int:
        with self._lock:
            known = self._ids.get(id(obj))
            if known is not None and self.objects.get(known) is obj:
                return known
            number = next(self._numbers)
            self.objects[number] = obj
            self._ids[id(obj)] = number
            return number

    def reference(self, obj: Any) -> Optional[tuple]:
        """How an object goes to the application: a session by its capture, a driver, a facet or a
        function by reference; ``None``: by value."""
        session_id = self._session_ids.get(id(obj))
        if session_id is not None and self.sessions.get(session_id) is obj:
            return ("session", session_id)
        from ...core.instrument import Facet, Instrument
        from ..base import AnalyzerDriverBase

        if isinstance(obj, (AnalyzerDriverBase, Facet, Instrument)) or wire.is_handler(obj):
            number = self.export(obj)
            return ("object", number, self.members(obj))
        return None

    def members(self, obj: Any) -> dict[str, str]:
        found = wire.members_of(obj)
        inner = getattr(type(obj), "inner", None)
        if isinstance(inner, property):  # a wrapper (software trigger): the members of the driver it wraps
            for name, kind in wire.members_of(obj.inner).items():
                found.setdefault(name, kind)
        if callable(obj) and not isinstance(obj, type):
            found["__call__"] = "method"
        return found

    def resolve(self, pid: tuple) -> Any:
        kind = pid[0]
        if kind == "object":
            return self.objects[pid[1]]
        if kind == "session":
            return self.sessions[pid[1]]
        if kind == "callback":
            number = pid[1]
            with self._lock:
                stub = self._callbacks.get(number)
                if stub is None:
                    def stub(*args: Any, **kwargs: Any) -> None:
                        self.events.put("callback", (number, args, kwargs))

                    self._callbacks[number] = stub
            return stub
        raise ValueError(f"unknown reference {kind!r}")

    # ---------------------------------------------------------------- open
    def open(self, address: str, options: dict) -> None:
        sample_store.share_arrays()
        from ... import plugins

        for module in options.pop("modules", ()):  # (what registered the kind of the device)
            plugins.import_module(module)
        factory = options.pop("factory", None)
        if factory:
            module, _, name = factory.partition(":")
            import importlib

            self.instrument = getattr(importlib.import_module(module), name)(address, **options)
        elif "download_bitstream" in options:
            from ...core.instrument import Instrument
            from ..discovery import open_device

            self.instrument = Instrument.from_driver(
                open_device(address, download_bitstream=bool(options["download_bitstream"])), uri=address)
        else:
            from ...lab.engine.devices import open_instrument

            self.instrument = open_instrument(address, **options)
        capture = self.instrument.capture
        self.driver = capture.driver if capture is not None else None
        self.objects[wire.INSTRUMENT] = self.instrument
        self._ids[id(self.instrument)] = wire.INSTRUMENT
        if self.driver is not None:
            self.objects[wire.DRIVER] = self.driver
            self._ids[id(self.driver)] = wire.DRIVER
            self.driver.add_capture_progress_handler(self._progress)
            self.driver.add_capture_tile_handler(self._tile)
        gc.collect()
        gc.freeze()  # what opening made stays: collections walk only what streaming makes

    def describe(self) -> dict:
        instrument = self.instrument
        facets = []
        for facet in instrument.facets():
            if self.driver is not None and getattr(facet, "driver", None) is self.driver and \
                    type(facet).__name__ == "CaptureFacet":
                continue  # the application makes its own capture facet around the driver
            members = self.members(facet)
            facets.append((self.export(facet), wire.class_paths(facet), members, wire.constants_of(facet, members)))
        return {
            "name": instrument.name, "kind": instrument.kind, "uri": instrument.uri,
            "status": instrument.status.value, "trigger_outputs": instrument.trigger_outputs,
            "trigger_inputs": instrument.trigger_inputs, "facets": facets, "pid": os.getpid(),
            "driver": self.members(self.driver) if self.driver is not None else None,
            "snapshot": snapshot(self.driver) if self.driver is not None else {},
            "instrument": self.members(instrument),
        }

    # -------------------------------------------------------------- events
    def forget_session(self, number: int) -> None:
        session = self.sessions.pop(number, None)
        if session is not None and self._session_ids.get(id(session)) == number:
            self._session_ids.pop(id(session), None)

    def _session_of(self, session: Any) -> Optional[int]:
        return self._session_ids.get(id(session)) if session is not None else None

    def _progress(self, args: Any) -> None:
        number = self._session_of(args.session)
        if number is not None:
            self.events.put("progress", (number, args), key=("progress", number))

    def _tile(self, args: Any) -> None:
        number = self._session_of(args.session)
        if number is not None:
            self.events.put("tile", (number, args))

    def start(self, number: int, session: Any, kwargs: dict) -> tuple:
        from ..base import CaptureError

        self.sessions[number] = session
        self._session_ids[id(session)] = number

        def completed(args: Any) -> None:
            # the state of the session travels by value (its samples as shared arrays)
            self.events.put("completed", (number, args, args.session))

        commanded = time.monotonic()
        error = self.driver.start_capture(session, completed, **kwargs)
        if error != CaptureError.NONE:
            self.sessions.pop(number, None)
            self._session_ids.pop(id(session), None)
        return error, commanded

    # ---------------------------------------------------------------- calls
    def handle(self, kind: str, payload: Any) -> Any:
        if kind == "call":
            number, name, args, kwargs = payload
            target = self.objects[number]
            function = target if name == "__call__" else getattr(target, name)
            return function(*args, **kwargs)
        if kind == "get":
            number, name = payload
            return getattr(self.objects[number], name)
        if kind == "set":
            number, name, value = payload
            setattr(self.objects[number], name, value)
            return None
        if kind == "start":
            return self.start(*payload)
        if kind == "forget":
            with self._lock:
                for number in payload:
                    obj = self.objects.pop(number, None)
                    if obj is not None:
                        self._ids.pop(id(obj), None)
            return None
        if kind == "snapshot":
            return snapshot(self.driver) if self.driver is not None else {}
        raise ValueError(f"unknown call {kind!r}")

    def reply(self, number: int, ok: bool, value: Any) -> None:
        try:
            data, keep = wire.dumps(("ok" if ok else "error", number, value), self.reference)
        except Exception as error:
            data, keep = wire.dumps(("error", number, wire.pack_error(error)), self.reference)
        held = self.sent.hold(keep)
        with self._reply_lock:
            self.requests.send_bytes(data if held is None else b"%d|" % held + data)

    def serve(self) -> None:
        with ThreadPoolExecutor(WORKERS, thread_name_prefix="openscilab-device-call") as pool:
            while True:
                try:
                    data = self.requests.recv_bytes()
                except (EOFError, OSError):
                    break  # the application went away
                kind, number, payload = wire.loads(data, self.resolve)
                if kind == "ack":
                    self.sent.release(payload)
                    continue
                if kind == "close":
                    self.close()
                    self.reply(number, True, None)
                    break
                pool.submit(self._answer, kind, number, payload)
        self.close()

    def _answer(self, kind: str, number: int, payload: Any) -> None:
        try:
            value = self.handle(kind, payload)
        except BaseException as error:
            try:
                self.reply(number, False, wire.pack_error(error))
            except (OSError, EOFError):
                pass
            return
        try:
            self.reply(number, True, value)
        except (OSError, EOFError):
            pass

    def close(self) -> None:
        instrument, self.instrument = self.instrument, None
        if instrument is None:
            return
        driver = self.driver
        try:
            if driver is not None and driver.is_capturing:
                driver.stop_capture()
        except Exception:
            log.exception("stopping the capture")
        try:
            instrument.close()
        except Exception:
            log.exception("closing the instrument")
        if driver is not None:
            driver.wait_for_events(2.0)
        self.events.flush()


#: properties and methods without arguments that do not change while a device is open: read once
STATIC_PROPERTIES = (
    "device_version", "blast_frequency", "max_frequency", "min_frequency", "channel_count", "max_loop_count",
    "buffer_size", "driver_type", "driver_id", "is_network", "channels_per_device", "analog_channel_count",
    "has_self_test", "self_test_description", "address", "is_hardware", "board_count", "supports_bootloader",
    "supports_network_config", "is_simulator",
)
STATIC_METHODS = (
    "capabilities", "acquisition_modes", "has_external_trigger", "pattern_trigger_groups", "edge_trigger_channels",
    "analog_channel_names", "supports_state_mode", "state_clock_channels", "supports_stream_state",
)


def snapshot(driver: Any) -> dict:
    found: dict = {}
    for name in STATIC_PROPERTIES:
        try:
            found[name] = getattr(driver, name)
        except Exception:  # (a probe, not an error)
            continue
    for name in STATIC_METHODS:
        method = getattr(driver, name, None)
        if callable(method):
            try:
                found[name + "()"] = method()
            except Exception:  # (a probe, not an error)
                continue
    import pickle

    return {name: value for name, value in found.items() if _picklable(pickle, value)}


def _picklable(pickle_module, value: Any) -> bool:
    try:
        pickle_module.dumps(value)
        return True
    except Exception:  # (a probe, not an error)
        return False


def _raise_priority() -> None:
    """Above the application where the system allows it without rights (Windows, or a limit set
    with ``core.priority.allow_permanently``); otherwise the priority it took over from the
    application (raised there with the user's rights, ``core.priority``)."""
    from ...core import priority

    priority.raise_own()


def main(requests, events, address: str, options: dict, levels: tuple) -> None:  # pragma: no cover - a process
    """The device process: open, then answer until closed. ``levels``: those of the application's
    log and of its driver log; the records go to the application."""
    from ... import driver as _driver  # noqa: F401
    from .. import process as package

    package.INSIDE = True
    from ...core import crashes

    crashes.enable_faulthandler()  # (a crash of the device process leaves its stacks in faults.log)
    host = Host(requests, events)
    root = logging.getLogger()
    root.setLevel(levels[0])
    logging.getLogger("openscilab.driver").setLevel(levels[1])
    root.addHandler(_LogForward(host.events))
    _raise_priority()
    try:
        host.open(address, dict(options))
        reply = (True, host.describe())
    except BaseException as error:
        reply = (False, wire.pack_error(error))
    try:
        data, _keep = wire.dumps(("opened",) + reply, host.reference)
        requests.send_bytes(data)
    except (OSError, EOFError):
        return
    if reply[0]:
        host.serve()
    host.events.flush()
    host.events.close()
