# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Running a flow from the user interface: the engine in a thread, its events as Qt signals."""

from __future__ import annotations

import threading
from collections import deque
from typing import Any, Optional

from PySide6.QtCore import QObject, QTimer, Signal

from ...lab.engine import Engine, EngineEvent, RunResult, ViewSink
from ...lab.model import Flow


#: milliseconds between two deliveries of what the engine reported (25 times a second)
FLUSH_MS = 40
#: values of one output kept between two deliveries (a flow in virtual time sends thousands)
VALUE_BATCH = 1000
#: log lines kept between two deliveries (the console shows the last 2000)
LOG_BATCH = 2000


class GuiViewSink(QObject, ViewSink):
    """View nodes of a running flow: what they show reaches the user interface with
    :meth:`flush`, called there – the last value of every view, not each one."""

    shown = Signal(str, str, object, dict)

    def __init__(self) -> None:
        QObject.__init__(self)
        ViewSink.__init__(self)
        self._pending: dict[tuple[str, str], tuple] = {}
        self._pending_lock = threading.Lock()

    def show(self, kind: str, node: str, value: Any, **options) -> None:
        super().show(kind, node, value, **options)
        with self._pending_lock:
            self._pending[(kind, node)] = (kind, node, value, options)

    def flush(self) -> None:
        """Emit ``shown`` for what the views showed since the last call (user interface thread)."""
        with self._pending_lock:
            pending, self._pending = self._pending, {}
        for item in pending.values():
            self.shown.emit(*item)


class FlowRunner(QObject):
    """One run of a flow at a time; ``state`` is the engine's flow state or ``"idle"``.

    The engine reports every value, state and log line from its thread – in virtual time
    hundreds of thousands a second. They are collected there and delivered here
    :data:`FLUSH_MS` apart: the last state of every node, the values of every output as a
    batch, the log lines, the views. Everything is delivered before ``finished``.
    """

    state_changed = Signal(str, str)
    node_state = Signal(str, str, str)
    #: node, port, the values since the last delivery (oldest first)
    values = Signal(str, str, list)
    #: node, port, the flow times of those values, the values (emitted right before ``values``)
    timed_values = Signal(str, str, list, list)
    log = Signal(str)
    finished = Signal(object)
    #: events of the engine thread, delivered in the thread of the runner
    _event = Signal(object)
    _done = Signal(object)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.engine: Optional[Engine] = None
        self.thread: Optional[threading.Thread] = None
        self.state = "idle"
        self.result: Optional[RunResult] = None
        self.views = GuiViewSink()
        self._event.connect(self._deliver)
        self._done.connect(self._completed)
        # what the engine thread collected for the next delivery
        self._lock = threading.Lock()
        self._states: dict[str, tuple[str, str]] = {}
        self._values: dict[tuple[str, str], deque] = {}
        self._logs: deque = deque(maxlen=LOG_BATCH)
        self._unsubscribe = None
        self._closed = False
        self._timer = QTimer(self)
        self._timer.setInterval(FLUSH_MS)
        self._timer.timeout.connect(self._flush)

    @property
    def running(self) -> bool:
        # ended once the result arrived (the thread may still be finishing then)
        return self.thread is not None and self.result is None and self.thread.is_alive()

    def start(self, flow: Flow, **options) -> Engine:
        """Start ``flow`` (raises :class:`~openscilab.lab.FlowError` when it cannot run)."""
        if self.running:
            raise RuntimeError("the flow is running")
        self.views = options.pop("views", None) or GuiViewSink()
        engine = Engine(flow, views=self.views, **options)
        with self._lock:
            self._states.clear()
            self._values.clear()
            self._logs.clear()
        self._unsubscribe = engine.subscribe(self._collect)
        self.engine = engine
        self.result = None

        def work() -> None:
            try:
                result = engine.run()
            except Exception as error:  # noqa: BLE001 - reported as the result
                result = RunResult("error", engine.clock.now(), str(error))
            if not self._closed:
                try:
                    self._done.emit(result)
                except RuntimeError:  # the runner was deleted with its document
                    pass

        self.thread = threading.Thread(target=work, name="openscilab-flow", daemon=True)
        self.thread.start()
        self._timer.start()
        return engine

    def detach(self) -> None:
        """The document closes: nothing of a run that is still ending reaches this object."""
        self._closed = True
        self._timer.stop()
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    def stop(self) -> None:
        if self.engine is not None and self.running:
            self.engine.stop()

    def pause(self) -> None:
        if self.engine is not None and self.running:
            self.engine.pause()

    def resume(self) -> None:
        if self.engine is not None and self.running:
            self.engine.resume()

    def step(self) -> None:
        if self.engine is not None and self.running:
            self.engine.step()

    def set_breakpoint(self, node_id: str, enabled: bool) -> None:
        if self.engine is not None:
            self.engine.set_breakpoint(node_id, enabled)

    def wait(self, timeout: Optional[float] = None) -> bool:
        if self.thread is None:
            return True
        self.thread.join(timeout)
        return not self.thread.is_alive()

    def _collect(self, event: EngineEvent) -> None:
        """An event of the engine, in its thread: kept for the next delivery."""
        if event.kind == "flow":
            if not self._closed:
                try:
                    self._event.emit(event)  # rare (paused, running, ended): at once, in order
                except RuntimeError:
                    pass
            return
        with self._lock:
            if event.kind == "node":
                self._states[event.node] = (event.state, event.message)
            elif event.kind == "value":
                batch = self._values.get((event.node, event.port))
                if batch is None:
                    batch = self._values[(event.node, event.port)] = deque(maxlen=VALUE_BATCH)
                batch.append((event.time, event.value))
            elif event.kind == "log":
                self._logs.append(f"{event.time:10.6f} {event.node}: {event.message}")

    def _flush(self) -> None:
        """Deliver what was collected: states, then values, then the log, then the views."""
        with self._lock:
            states, self._states = self._states, {}
            batches, self._values = self._values, {}
            logs = list(self._logs)
            self._logs.clear()
        for node, (state, message) in states.items():
            self.node_state.emit(node, state, message)
        for (node, port), batch in batches.items():
            times = [time for time, _value in batch]
            values = [value for _time, value in batch]
            self.timed_values.emit(node, port, times, values)
            self.values.emit(node, port, values)
        for line in logs:
            self.log.emit(line)
        self.views.flush()

    def _deliver(self, event: EngineEvent) -> None:
        self._flush()  # what happened before the flow changed its state
        self.state = event.state
        self.state_changed.emit(event.state, event.message)

    def _completed(self, result: RunResult) -> None:
        self._timer.stop()
        self._flush()  # the last values and states: complete when the result is there
        self.result = result
        self.state = result.state
        self.finished.emit(result)
