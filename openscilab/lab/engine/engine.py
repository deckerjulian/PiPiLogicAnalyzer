# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The engine: runs a flow.

Two planes, one event loop (asyncio):

* **data plane** – values (blocks of samples, measurements, events) flow along the wires into
  per-node mailboxes; each node handles them in arrival order. A mailbox that fills up holds
  the sender back (back pressure) until the receiver caught up.
* **control plane** – sequences, timers, sweeps and Python nodes are coroutines (``run()``);
  they sleep, wait for inputs, arm captures, set pins.

Time is either **real** (devices) or **virtual** (simulator, ``--fast``): in virtual time the
clock jumps to the next wake-up as soon as every node waits, so a flow of minutes runs in
milliseconds and gives the same result every time. The engine counts the coroutines that can
make progress; when none can, the clock advances (virtual) or the flow is finished.

Breakpoints stop the flow before a node handles a value; :meth:`Engine.step` lets one handler
run. The engine reports what happens to subscribers (:class:`EngineEvent`): flow state, node
states, values on outputs, log lines. It has no Qt dependency; the editor runs it in a thread.
"""

from __future__ import annotations

import asyncio
import heapq
import inspect
import itertools
import logging
import os
import signal
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from ..model import Flow, FlowError
from ..nodes.registry import Registry, default_registry
from .runtime import NodeContext, NodeError, NodeRuntime, runtime_class

# flow states
IDLE = "idle"
RUNNING = "running"
PAUSED = "paused"
FINISHED = "finished"
STOPPED = "stopped"
ERROR = "error"

# node states
NODE_IDLE = "idle"
NODE_WAITING = "waiting"
NODE_RUNNING = "running"
NODE_DONE = "done"
NODE_ERROR = "error"

#: a mailbox holding more values than this holds back its senders ...
HIGH_WATER = 64
#: ... until it is down to this
LOW_WATER = 16


@dataclass(frozen=True)
class EngineEvent:
    """``kind``: ``flow``, ``node``, ``value``, ``log``."""

    kind: str
    node: str = ""
    port: str = ""
    state: str = ""
    value: Any = None
    time: float = 0.0
    message: str = ""


@dataclass
class RunResult:
    state: str
    time: float
    error: str = ""
    #: last value of every output: (node, port) -> value
    values: dict[tuple[str, str], Any] = field(default_factory=dict)
    node_states: dict[str, str] = field(default_factory=dict)
    log: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.state == FINISHED

    def value(self, node: str, port: str, default: Any = None) -> Any:
        return self.values.get((node, port), default)

log = logging.getLogger(__name__)


class _StopRun(Exception):
    pass


# ------------------------------------------------------------------- clocks
class VirtualClock:
    """Time that jumps forward when everything waits."""

    virtual = True

    def __init__(self) -> None:
        self.time = 0.0

    def now(self) -> float:
        return self.time


class RealClock:
    virtual = False

    def __init__(self) -> None:
        self._start = time.monotonic()

    def reset(self) -> None:
        self._start = time.monotonic()

    def now(self) -> float:
        return time.monotonic() - self._start


# ----------------------------------------------------------------- mailboxes
class _Mailbox:
    def __init__(self) -> None:
        self.items: deque = deque()
        self.waiter: Optional[asyncio.Future] = None
        self.space_waiters: list[asyncio.Future] = []


#: seconds between two looks of the engine at a flow running in real time (see Engine._drive)
REAL_TIME_POLL = 0.01

#: log lines kept for the result of a run
LOG_LINES = 10_000


class ViewSink:
    """Where view nodes show their values; the shell opens documents, headless runs keep them."""

    def __init__(self) -> None:
        # A view shows its value again and again while the flow runs (a chart: on every point).
        # Only the last one counts, so only that one is kept – a flow that logs for days must
        # not grow. The verdicts of checks are all kept: reports list them, the command line
        # counts the failed ones.
        self._latest: dict[tuple[str, str], tuple[str, str, Any, dict]] = {}
        self._checks: list[tuple[str, str, Any, dict]] = []
        self._last: dict[str, Any] = {}

    @property
    def items(self) -> list[tuple[str, str, Any, dict]]:
        """(kind, node, value, options): what every view shows now, in the order the views first
        showed something, then every check in the order of the verdicts."""
        return [*self._latest.values(), *self._checks]

    def show(self, kind: str, node: str, value: Any, **options) -> None:
        item = (kind, node, value, options)
        if kind == "check":
            self._checks.append(item)
        else:
            self._latest[(kind, node)] = item  # stays at its first position
        self._last[node] = value

    def last(self, node: str) -> Any:
        """What ``node`` showed last (``None``: nothing yet)."""
        return self._last.get(node)


class Engine:
    """Runs one flow. ``mode``: ``"virtual"`` (fast, deterministic) or ``"real"``."""

    def __init__(
        self,
        flow: Flow,
        *,
        registry: Optional[Registry] = None,
        mode: str = "real",
        hub=None,
        devices: Optional[dict[str, Any]] = None,
        simulate: bool = False,
        seed: Optional[int] = None,
        views: Optional[ViewSink] = None,
        base_dir: Optional[str] = None,
        data_dir: Optional[str] = None,
        duration: Optional[float] = None,
        breakpoints: Iterable[str] = (),
        project=None,
        interactive: bool = False,
        bound: Iterable[str] = (),
    ) -> None:
        if mode not in ("real", "virtual"):
            raise ValueError("mode is 'real' or 'virtual'")
        self.registry = registry or default_registry
        bound = list(bound)
        problems = flow.errors(self.registry, external=bound)
        if problems:
            raise FlowError("the flow cannot run:\n" + "\n".join(f"  {problem}" for problem in problems))
        self.source_flow = flow
        self.flow = flow.expanded(self.registry)
        self.mode = mode
        self.hub = hub
        self.device_overrides = dict(devices or {})
        self.simulate = simulate
        self.seed = int(seed if seed is not None else flow.settings.get("seed", 1))
        self.views = views if views is not None else ViewSink()
        self.project = project
        self.base_dir = base_dir or (project.root if project is not None else os.getcwd())
        self.data_dir = data_dir or (project.data_dir if project is not None else self.base_dir)
        setting = flow.settings.get("duration")
        if duration is None and setting is not None:
            from ...core import units

            duration = units.parse(setting, "s")
        self.duration = duration
        #: operated from outside (a panel): the flow runs until it is stopped, values arrive by
        #: :meth:`inject`
        self.interactive = interactive
        #: values injected before the nodes were set up (delivered once they are)
        self._early: list[tuple[str, str, Any]] = []
        self._ready = False
        self.clock: Any = VirtualClock() if mode == "virtual" else RealClock()

        self.state = IDLE
        self.node_states: dict[str, str] = {node_id: NODE_IDLE for node_id in self.flow.nodes}
        self.breakpoints: set[str] = set(breakpoints)
        self._listeners: list[Callable[[EngineEvent], None]] = []
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._runtimes: dict[str, NodeRuntime] = {}
        self._contexts: dict[str, NodeContext] = {}
        self._mailboxes: dict[str, _Mailbox] = {}
        self._pulled: dict[tuple[str, str], _Mailbox] = {}
        self._targets: dict[tuple[str, str], list[tuple[str, str]]] = {}
        self._wired: set[tuple[str, str]] = set()
        self._latest: dict[tuple[str, str], Any] = {}
        #: last value of every output
        self.values: dict[tuple[str, str], Any] = {}
        #: the last lines of the log (a flow that runs for days does not keep all of them)
        self._log_lines: deque[str] = deque(maxlen=LOG_LINES)
        self.devices: dict[str, Any] = {}
        self._opened: list[Any] = []
        #: nets of simulators of the device list wired for this run: (circuit, net, source before)
        self._rewired: list[tuple[Any, str, Any]] = []

        self._busy = 0
        self._external = 0
        self._pending_sleeps = 0
        self._sleepers: list[tuple[float, int, asyncio.Future]] = []
        self._sequence = itertools.count()
        self._tasks: list[asyncio.Task] = []
        #: one call at a time per device from the threads of :meth:`_device_call`
        self._device_locks: dict[int, threading.Lock] = {}
        #: nodes whose setup() ran (only those finish)
        self._set_up: set[str] = set()
        self._done: Optional[asyncio.Event] = None
        self._finish_state: Optional[tuple[str, str]] = None
        self._paused = False
        self._step = False
        self._gate_waiters: list[asyncio.Future] = []
        self._error = ""

        for edge in self.flow.edges:
            self._targets.setdefault((edge.source.node, edge.source.port), []).append((edge.target.node, edge.target.port))
            self._wired.add((edge.source.node, edge.source.port))
            self._wired.add((edge.target.node, edge.target.port))
        # ports a panel sends values to count as wired ("node.port")
        for binding in bound:
            node_id, _, port = str(binding).partition(".")
            self._wired.add((node_id, port))

    # ============================================================ events
    def subscribe(self, listener: Callable[[EngineEvent], None]) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(listener)

        def unsubscribe() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return unsubscribe

    def _event(self, event: EngineEvent) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(event)
            except Exception:  # noqa: BLE001 - a listener must not stop the flow
                traceback.print_exc()

    def _set_state(self, state: str, message: str = "") -> None:
        self.state = state
        self._event(EngineEvent("flow", state=state, time=self.clock.now(), message=message))

    def _set_node_state(self, node_id: str, state: str, message: str = "") -> None:
        if self.node_states.get(node_id) != state or message:
            self.node_states[node_id] = state
            self._event(EngineEvent("node", node=node_id, state=state, time=self.clock.now(), message=message))

    def _log(self, node_id: str, text: str) -> None:
        line = f"{self.clock.now():10.6f} {node_id}: {text}"
        self._log_lines.append(line)
        self._event(EngineEvent("log", node=node_id, time=self.clock.now(), message=text))

    # =========================================================== control
    def run(self, timeout: Optional[float] = None) -> RunResult:
        """Run the flow to its end in this thread (with an event loop of its own)."""
        loop = asyncio.new_event_loop()
        interrupts = 0

        def interrupted() -> None:
            # Ctrl+C ends the flow like stop(): the nodes finish (the logger writes its rows, the
            # report is written) and let go of their devices. A second Ctrl+C gives up at once.
            nonlocal interrupts
            interrupts += 1
            if interrupts > 1:
                raise KeyboardInterrupt
            self._request_finish(STOPPED, "interrupted")

        handled = False
        previous = None
        try:
            previous = signal.getsignal(signal.SIGINT)
            loop.add_signal_handler(signal.SIGINT, interrupted)
            handled = True
        except (NotImplementedError, RuntimeError, ValueError, AttributeError):
            pass  # not the main thread, or a platform without signal handlers for event loops
        try:
            coroutine = self.run_async()
            if timeout is not None:
                coroutine = asyncio.wait_for(coroutine, timeout)
            task = loop.create_task(coroutine)
            try:
                return loop.run_until_complete(task)
            except KeyboardInterrupt:
                if handled or task.done():
                    raise
                interrupted()
                return loop.run_until_complete(task)
        finally:
            if handled:
                loop.remove_signal_handler(signal.SIGINT)
                if previous is not None:
                    # what the program around the flow had set (a handler of its own, "ignore")
                    signal.signal(signal.SIGINT, previous)
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    @property
    def error(self) -> str:
        """What the flow failed with (empty while it is fine)."""
        return self._error

    def stop(self) -> None:
        """End the flow (from any thread)."""
        self._threadsafe(self._request_finish, STOPPED, "stopped")

    def pause(self) -> None:
        self._threadsafe(self._set_paused, True)

    def resume(self) -> None:
        self._threadsafe(self._set_paused, False)

    def step(self) -> None:
        """While paused: let one node handle one value, then pause again."""
        self._threadsafe(self._do_step)

    def set_breakpoint(self, node_id: str, enabled: bool = True) -> None:
        if enabled:
            self.breakpoints.add(node_id)
        else:
            self.breakpoints.discard(node_id)

    def inject(self, node_id: str, port: str, value: Any) -> None:
        """A value from outside (a panel, any thread): at an input of ``node_id`` it arrives as if
        a wire brought it, at an output it is sent to the wired inputs."""
        if node_id not in self.flow.nodes:
            raise FlowError(f"no node {node_id!r}")
        with self._lock:
            if not self._ready:
                self._early.append((node_id, port, value))
                return
        self._threadsafe(self._inject, node_id, port, value)

    def _inject(self, node_id: str, port: str, value: Any) -> None:
        context = self._contexts.get(node_id)
        if context is None:
            return
        if port in context.output_ports:
            self._emit(node_id, port, value)
            return
        if port not in context.input_ports:
            self._log(node_id, f"no port {port!r} for a value from outside")
            return
        pulled = self._pulled.get((node_id, port))
        mailbox = pulled if pulled is not None else self._mailboxes.get(node_id)
        if mailbox is None:
            return
        mailbox.items.append(value if pulled is not None else (port, value))
        if mailbox.waiter is not None and not mailbox.waiter.done():
            self._resolve(mailbox.waiter, None)

    def _threadsafe(self, function: Callable, *args) -> None:
        loop = self._loop
        if loop is None or loop.is_closed() or threading.current_thread() is getattr(self, "_thread", None):
            function(*args)
            return
        try:
            loop.call_soon_threadsafe(function, *args)
        except RuntimeError:  # the loop is closing
            pass

    def _set_paused(self, paused: bool) -> None:
        self._paused = paused
        if paused:
            self._set_state(PAUSED)
        else:
            self._set_state(RUNNING)
            self._release_gates()

    def _do_step(self) -> None:
        if self._paused:
            self._step = True
            self._release_gates()

    def _release_gates(self) -> None:
        waiters, self._gate_waiters = self._gate_waiters, []
        for waiter in waiters:
            self._resolve(waiter, None)

    # ======================================================= main coroutine
    async def run_async(self) -> RunResult:
        self._loop = asyncio.get_running_loop()
        self._thread = threading.current_thread()
        self._done = asyncio.Event()
        if self._finish_state is not None:
            self._done.set()  # stopped before it began (Stop right after Run)
        if isinstance(self.clock, RealClock):
            self.clock.reset()
        self._set_state(RUNNING)
        try:
            self._check_stopped()
            await self._open_devices()
            self._check_stopped()
            self._create_runtimes()
            for node_id, runtime in self._runtimes.items():
                self._check_stopped()
                self._busy += 1
                try:
                    await runtime.setup()
                    self._set_up.add(node_id)
                except Exception as error:  # noqa: BLE001 - reported as the error of the flow
                    self._node_failed(node_id, error)
                    raise _StopRun from None
                finally:
                    self._busy -= 1
            self._check_stopped()
            for node_id, runtime in self._runtimes.items():
                # ``pulled`` may depend on the parameters (set in setup()).
                for port in runtime.pulled:
                    self._pulled.setdefault((node_id, port), _Mailbox())
            with self._lock:
                self._ready = True
                early, self._early = self._early, []
            for item in early:
                self._inject(*item)
            for node_id, runtime in self._runtimes.items():
                if runtime.has_run:
                    self._spawn(self._run_node(node_id, runtime))
                if any(port not in runtime.pulled for port in self._contexts[node_id].input_ports):
                    self._spawn(self._dispatch(node_id, runtime))
            driver = asyncio.ensure_future(self._drive())
            await self._done.wait()
            driver.cancel()
        except _StopRun:
            pass
        finally:
            await self._shutdown()
        state, message = self._finish_state or (FINISHED, "")
        if self._error:
            state, message = ERROR, self._error
        self._set_state(state, message)
        return RunResult(state, self.clock.now(), self._error, dict(self.values), dict(self.node_states),
                         list(self._log_lines))

    def _check_stopped(self) -> None:
        """Between the steps of starting: a flow that was stopped meanwhile does not go on."""
        if self._finish_state is not None:
            raise _StopRun

    def _spawn(self, coroutine) -> None:
        self._busy += 1
        task = asyncio.ensure_future(self._counted(coroutine))
        self._tasks.append(task)

    async def _counted(self, coroutine) -> None:
        try:
            await coroutine
        except asyncio.CancelledError:
            pass
        except _StopRun:
            pass
        finally:
            self._busy -= 1

    async def _shutdown(self) -> None:
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        # Also after an error: what was measured until then is written (the rows of a logger, the
        # report – which then says what failed). Nodes that collect what the others finish with
        # (report.write) come last.
        ordered = sorted(self._runtimes.items(), key=lambda item: bool(getattr(item[1], "finish_last", False)))
        for node_id, runtime in ordered:
            if node_id not in self._set_up:
                continue
            try:
                result = runtime.finish()
                if inspect.isawaitable(result):
                    await result
            except Exception as error:  # noqa: BLE001
                self._node_failed(node_id, error)
        for node_id, state in self.node_states.items():
            if state in (NODE_WAITING, NODE_RUNNING, NODE_IDLE) and not self._error:
                self._set_node_state(node_id, NODE_DONE)
        # whatever happened: nothing of the flow keeps running on a device (those of the device
        # list stay open), and their circuits are wired as before
        for node_id, runtime in self._runtimes.items():
            try:
                runtime.cleanup()
            except Exception:  # noqa: BLE001 - the other nodes still let go of their devices
                log.exception("Cleaning up after node %s failed", node_id)
        for circuit, net, previous in reversed(self._rewired):
            if previous is None:
                circuit.sources.pop(net, None)
            else:
                circuit.sources[net] = previous
        self._rewired.clear()
        for instrument in self._opened:
            try:
                instrument.close()
            except Exception:  # noqa: BLE001
                log.debug("instrument.close() failed (ignored)", exc_info=True)
        self._opened.clear()

    def _create_runtimes(self) -> None:
        for node_id, node in self.flow.nodes.items():
            spec = self.flow.spec(node, self.registry)
            context = NodeContext(self, node, spec)
            runtime = runtime_class(spec)(node, spec, context)
            self._contexts[node_id] = context
            self._runtimes[node_id] = runtime
            self._mailboxes[node_id] = _Mailbox()
            for port in runtime.pulled:
                self._pulled[(node_id, port)] = _Mailbox()

    async def _open_devices(self) -> None:
        from .devices import open_instrument

        for name, address in self.flow.devices.items():
            address = self.device_overrides.get(name, address)
            if not isinstance(address, str):  # an instrument given directly
                self.devices[name] = address
                continue
            if not address:
                self._error = f"device {name}: no address (and no device {name!r} in the project)"
                raise _StopRun
            if address.startswith(("remote:", "remote-sim:")) and self.clock.virtual:
                self._error = (f"device {name} ({address}): a remote device runs in real time - switch "
                               "'Fast (virtual time)' off")
                raise _StopRun
            open_one = self._hub_instrument(address)
            if open_one is not None:
                self.devices[name] = open_one
                continue
            try:
                if address.startswith("remote-sim:"):
                    instrument = await self._external_call(open_instrument, address, False, self.clock, False,
                                                           self.seed)
                elif address.startswith("sim:") or self.simulate:
                    instrument = open_instrument(address, simulate=self.simulate, clock=self.clock, fast=self.clock.virtual,
                                                 seed=self.seed, wiring=self._wiring(name),
                                                 signals=self._signals(name, address))
                else:
                    instrument = await self._external_call(open_instrument, address, False, self.clock, False, self.seed)
            except Exception as error:  # noqa: BLE001
                self._error = f"device {name} ({address}): {error}"
                raise _StopRun from None
            instrument.name = name
            self.devices[name] = instrument
            self._opened.append(instrument)
        try:
            self._wire_devices()
        except FlowError as error:
            self._error = str(error)
            raise _StopRun from None

    def _hub_instrument(self, address: str):
        """An instrument open in the hub at ``address`` (or named so), used as it is – in real time
        only: in virtual time simulators run on the flow's own clock."""
        if self.hub is None or self.simulate or (self.clock.virtual and address.startswith("sim:")):
            return None
        for instrument in self.hub.instruments():
            if instrument.uri == address or instrument.name == address:
                return instrument
        return None

    def _signals(self, device: str, address: str) -> Optional[dict]:
        """What the simulator of the device node ``device`` simulates: its ``signals`` parameter,
        else what the simulator open in the device list at ``address`` was told (so a flow in
        virtual time sees the signals set on the device card)."""
        node = self.flow.nodes.get(device)
        signals = node.params.get("signals") if node is not None else None
        if signals:
            return dict(signals)
        if self.hub is not None:
            for instrument in self.hub.instruments():
                driver = getattr(instrument, "simulated_driver", None)
                if driver is not None and instrument.uri == address:
                    return dict(driver.signals)
        return None

    def _project_wiring(self, device: str) -> list:
        if self.project is None:
            return []
        simulation = self.project.extra.get("simulation") or {}
        return list((simulation.get("wiring") or {}).get(device) or [])

    def _wiring(self, device: str) -> Optional[list]:
        """Wires the project adds to the circuit of the simulated ``device`` (``simulation.wiring``);
        a wire from ``<other device>:<net>`` joins two simulated devices (see :meth:`_wire_devices`)."""
        wiring = [wire for wire in self._project_wiring(device) if ":" not in str(wire.get("from", ""))]
        return wiring or None

    def _wire_devices(self) -> None:
        """Wires between simulated devices: ``{from: "uno:A0", to: CH1}`` at the scope makes its CH1
        follow A0 of the Uno (both run on the engine's clock)."""
        from ...driver.simulated.circuit import RemoteSource

        for name, instrument in self.devices.items():
            # (a simulated remote device takes wires to its inputs too)
            target = getattr(instrument, "simulated_driver", None) or getattr(instrument, "wiring_source", None)
            for wire in self._project_wiring(name):
                source_name, separator, net = str(wire.get("from", "")).partition(":")
                if not separator:
                    continue
                source_device = self.devices.get(source_name)
                # (a simulated remote device offers its sync output as a net to wire)
                source = getattr(source_device, "simulated_driver", None) or getattr(source_device, "wiring_source", None)
                if target is None or source is None:
                    raise FlowError(f"simulation.wiring of {name}: {source_name} is no simulated device of the flow")
                offset = source.clock() - target.clock()
                wired_net = str(wire["to"])
                if instrument not in self._opened:
                    # a simulator of the device list: its circuit is given back as it was
                    self._rewired.append((target.circuit, wired_net, target.circuit.sources.get(wired_net)))
                target.circuit.drive(wired_net, RemoteSource(source.circuit, net, offset,
                                                             high=float(source.profile.get("logic_level", 3.3))))

    def device(self, name: str):
        try:
            return self.devices[name]
        except KeyError:
            raise NodeError(f"unknown device {name!r}") from None

    def resolve_path(self, path: str) -> str:
        path = os.path.expanduser(str(path))
        if os.path.isabs(path):
            return path
        if os.sep not in path and "/" not in path:
            return os.path.join(self.data_dir, path)
        return os.path.join(self.base_dir, path)

    # ========================================================== node tasks
    async def _run_node(self, node_id: str, runtime: NodeRuntime) -> None:
        try:
            await self._gate(node_id)
            self._set_node_state(node_id, NODE_RUNNING)
            await runtime.run()
            if self.node_states.get(node_id) == NODE_RUNNING:
                self._set_node_state(node_id, NODE_DONE)
        except (asyncio.CancelledError, _StopRun):
            raise
        except Exception as error:  # noqa: BLE001 - reported, then the flow stops
            self._node_failed(node_id, error)

    async def _dispatch(self, node_id: str, runtime: NodeRuntime) -> None:
        mailbox = self._mailboxes[node_id]
        while True:
            while not mailbox.items:
                if not runtime.has_run:
                    self._set_node_state(node_id, NODE_WAITING)
                mailbox.waiter = self._loop.create_future()
                await self._wait(mailbox.waiter)
                mailbox.waiter = None
            port, value = mailbox.items.popleft()
            # ``latest`` follows the values in the order the node handles them.
            self._latest[(node_id, port)] = value
            self._notify_space(mailbox)
            await self._gate(node_id)
            if not runtime.has_run:
                self._set_node_state(node_id, NODE_RUNNING)
            try:
                await runtime.on_input(port, value)
            except (asyncio.CancelledError, _StopRun):
                raise
            except Exception as error:  # noqa: BLE001
                self._node_failed(node_id, error)
                return
            await self._drain(node_id)

    def _node_failed(self, node_id: str, error: BaseException) -> None:
        text = str(error) if isinstance(error, (NodeError, FlowError, ValueError, KeyError, OSError)) else (
            f"{type(error).__name__}: {error}")
        text = text.removeprefix(f"{node_id}: ")  # (said once: the message gets the node's name below)
        if not isinstance(error, (NodeError, FlowError)):
            self._log_lines.append(traceback.format_exc())
        self._set_node_state(node_id, NODE_ERROR, text)
        if not self._error:
            self._error = f"{node_id}: {text}"
        self._request_finish(ERROR, self._error)

    def _request_finish(self, state: str, message: str = "") -> None:
        if self._finish_state is None:
            self._finish_state = (state, message)
        if self._done is not None:
            self._done.set()

    # ================================================================ data
    def _emit(self, node_id: str, port: str, value: Any) -> None:
        self.values[(node_id, port)] = value
        self._event(EngineEvent("value", node=node_id, port=port, value=value, time=self.clock.now()))
        for target, target_port in self._targets.get((node_id, port), ()):
            pulled = self._pulled.get((target, target_port))
            mailbox = pulled if pulled is not None else self._mailboxes.get(target)
            if mailbox is None:
                continue
            mailbox.items.append((target_port, value) if pulled is None else value)
            if mailbox.waiter is not None and not mailbox.waiter.done():
                self._resolve(mailbox.waiter, None)

    async def _receive(self, node_id: str, port: str) -> Any:
        mailbox = self._pulled.get((node_id, port))
        if mailbox is None:
            raise NodeError(f"{node_id}: the input {port!r} is not pulled (add it to 'pulled')")
        while not mailbox.items:
            mailbox.waiter = self._loop.create_future()
            await self._wait(mailbox.waiter)
            mailbox.waiter = None
        value = mailbox.items.popleft()
        self._latest[(node_id, port)] = value
        self._notify_space(mailbox)
        return value

    async def _receive_any(self, node_id: str, ports: list[str], timeout: Optional[float]) -> tuple[Optional[str], Any]:
        """The next value on any of the pulled ``ports``: ``(port, value)``; ``(None, None)`` after
        ``timeout`` seconds (``None``: wait for ever)."""
        mailboxes = []
        for port in ports:
            mailbox = self._pulled.get((node_id, port))
            if mailbox is None:
                raise NodeError(f"{node_id}: the input {port!r} is not pulled (add it to 'pulled')")
            mailboxes.append((port, mailbox))
        while True:
            for port, mailbox in mailboxes:
                if mailbox.items:
                    value = mailbox.items.popleft()
                    self._latest[(node_id, port)] = value
                    self._notify_space(mailbox)
                    return port, value
            if timeout is not None and timeout <= 0:
                return None, None
            future = self._loop.create_future()
            for _port, mailbox in mailboxes:
                mailbox.waiter = future
            handle = None
            fired = [False]
            if timeout is not None:
                if self.clock.virtual:
                    heapq.heappush(self._sleepers, (self.clock.time + timeout, next(self._sequence), future))
                else:
                    self._pending_sleeps += 1

                    def wake(fired: list = fired, future: asyncio.Future = future) -> None:
                        fired[0] = True
                        self._pending_sleeps -= 1
                        self._resolve(future, None)

                    handle = self._loop.call_later(timeout, wake)
            started = self.clock.now()
            try:
                await self._wait(future)
            finally:
                for _port, mailbox in mailboxes:
                    if mailbox.waiter is future:
                        mailbox.waiter = None
                if handle is not None and not fired[0]:
                    # a value came first: the timer no longer keeps the flow alive
                    handle.cancel()
                    self._pending_sleeps -= 1
            if timeout is not None:
                timeout -= self.clock.now() - started
                if not any(mailbox.items for _port, mailbox in mailboxes):
                    return None, None

    def _discard(self, node_id: str, ports: list[str]) -> None:
        for port in ports:
            mailbox = self._pulled.get((node_id, port))
            if mailbox is not None and mailbox.items:
                self._latest[(node_id, port)] = mailbox.items[-1]
                mailbox.items.clear()
                self._notify_space(mailbox)

    def _notify_space(self, mailbox: _Mailbox) -> None:
        if len(mailbox.items) <= LOW_WATER and mailbox.space_waiters:
            waiters, mailbox.space_waiters = mailbox.space_waiters, []
            for waiter in waiters:
                self._resolve(waiter, None)

    async def _drain(self, node_id: str) -> None:
        """Back pressure: wait while a receiver of ``node_id`` has too many values queued."""
        for (source, _port), targets in self._targets.items():
            if source != node_id:
                continue
            for target, target_port in targets:
                mailbox = self._pulled.get((target, target_port)) or self._mailboxes.get(target)
                while mailbox is not None and len(mailbox.items) > HIGH_WATER:
                    waiter = self._loop.create_future()
                    mailbox.space_waiters.append(waiter)
                    await self._wait(waiter)

    def _is_wired(self, node_id: str, port: str) -> bool:
        return (node_id, port) in self._wired

    # ================================================== waiting and time
    async def _wait(self, future: asyncio.Future) -> Any:
        """Wait for ``future`` as an idle coroutine (whoever completes it uses :meth:`_resolve`)."""
        self._busy -= 1
        try:
            return await future
        except asyncio.CancelledError:
            # The coroutine runs again (to unwind) unless the future was resolved already, which
            # counted it. Cancelling a task cancels the future it waits for, so "done" alone
            # does not tell.
            if not future.done() or future.cancelled():
                self._busy += 1
            raise

    def _resolve(self, future: asyncio.Future, value: Any) -> None:
        if not future.done():
            self._busy += 1
            future.set_result(value)

    async def _sleep(self, seconds: float) -> None:
        future = self._loop.create_future()
        if self.clock.virtual:
            heapq.heappush(self._sleepers, (self.clock.time + seconds, next(self._sequence), future))
        else:
            self._pending_sleeps += 1

            def wake() -> None:
                self._pending_sleeps -= 1
                self._resolve(future, None)

            self._loop.call_later(seconds, wake)
        await self._wait(future)

    async def _sleep_until(self, time: float) -> None:
        if not self.clock.virtual:
            await self._sleep(max(time - self.clock.now(), 0.0))
            return
        # exactly ``time``: no rounding from "now + (time - now)"
        future = self._loop.create_future()
        heapq.heappush(self._sleepers, (max(time, self.clock.time), next(self._sequence), future))
        await self._wait(future)

    async def _device_call(self, device: Any, function: Callable, *args) -> Any:
        driver = getattr(getattr(device, "capture", None), "driver", None)
        simulated = bool(getattr(driver, "is_simulator", False)) or getattr(device, "simulated_driver", None) is not None
        if simulated or self.clock.virtual:
            return function(*args)
        lock = self._device_locks.setdefault(id(device), threading.Lock())

        def call() -> Any:
            with lock:  # the calls of the nodes reach a device one after the other, as before
                return function(*args)

        return await self._external_call(call)

    async def _external_call(self, function: Callable, *args) -> Any:
        future, complete = self._external_future()

        def work() -> None:
            try:
                complete(function(*args))
            except BaseException as error:  # noqa: BLE001 - raised in the node
                complete(_Failure(error))

        threading.Thread(target=work, name="openscilab-flow-io", daemon=True).start()
        return await future

    def _external_future(self):
        loop = self._loop
        future = loop.create_future()
        self._external += 1
        done = threading.Event()

        def complete(value: Any = None) -> None:
            if done.is_set():
                return
            done.set()

            def finish() -> None:
                self._external -= 1
                self._resolve(future, value)

            try:
                loop.call_soon_threadsafe(finish)
            except RuntimeError:  # the loop is closed: the flow ended
                pass

        async def waiter() -> Any:
            try:
                value = await self._wait(future)
            except asyncio.CancelledError:
                if not done.is_set():
                    done.set()
                    self._external -= 1
                raise
            if isinstance(value, _Failure):
                raise value.error
            return value

        return waiter(), complete

    def _external_queue(self) -> "ExternalQueue":
        return ExternalQueue(self)

    async def _gate(self, node_id: str) -> None:
        """Hold a node at a breakpoint or while the flow is paused."""
        if node_id in self.breakpoints and not self._paused and not self._step:
            self._paused = True
            self._set_state(PAUSED, f"breakpoint at {node_id}")
        if self._step and self._paused:
            self._step = False
            self._set_state(RUNNING, f"step: {node_id}")
            return
        while self._paused:
            if self.state != PAUSED:
                self._set_state(PAUSED, f"at {node_id}")
            waiter = self._loop.create_future()
            self._gate_waiters.append(waiter)
            await self._wait(waiter)
            if self._step:
                self._step = False
                self._set_state(RUNNING, f"step: {node_id}")
                return

    async def _drive(self) -> None:
        """Advances virtual time and notices the end of the flow (the engine's own task)."""
        spins = 0
        # The driver only notices the end of the flow (the nodes are woken by their own timers
        # and by the device threads), so in real time it may look rarely: a logger running for
        # days must not wake a thousand times a second.
        poll = 0.0005 if self.clock.virtual else REAL_TIME_POLL
        while not self._done.is_set():
            await asyncio.sleep(0)
            if not self.clock.virtual and self.duration is not None and self.clock.now() >= self.duration:
                # also while a stream is open or a node is busy: the time is up
                self._request_finish(FINISHED, "duration reached")
                return
            if self._busy > 0:
                spins += 1
                if spins > 200:
                    await asyncio.sleep(0.0005 if spins < 2000 else poll)
                continue
            spins = 0
            if self._gate_waiters:
                # Held at a breakpoint or by the pause; time goes on until a node reaches a gate.
                await asyncio.sleep(0.005)
                continue
            if self._external > 0:
                await asyncio.sleep(poll)
                continue
            while self._sleepers and self._sleepers[0][2].done():
                heapq.heappop(self._sleepers)  # a timeout whose wait ended otherwise
            if self.clock.virtual and self._sleepers:
                wake_time = self._sleepers[0][0]
                if self.duration is not None and wake_time > self.duration:
                    self.clock.time = self.duration
                    self._request_finish(FINISHED, "duration reached")
                    return
                self.clock.time = max(self.clock.time, wake_time)
                while self._sleepers and self._sleepers[0][0] <= self.clock.time:
                    _wake, _order, future = heapq.heappop(self._sleepers)
                    self._resolve(future, None)
                continue
            if not self.clock.virtual and self._pending_sleeps:
                await asyncio.sleep(self._until_duration(poll))
                continue
            if self.interactive:
                # Waits for values from outside (inject) until it is stopped.
                await asyncio.sleep(0.005)
                continue
            # Nothing can happen any more. Nodes that wait for a receiver to take its values
            # (which waits itself) are stuck, not done: that is an error, not a result.
            stuck = sorted(node_id for node_id, mailbox in self._owned_mailboxes() if mailbox.space_waiters)
            if stuck:
                self._error = self._error or (
                    f"deadlock: {', '.join(stuck)} do not take their values while the nodes that send "
                    "them wait for that")
                self._request_finish(ERROR, self._error)
                return
            self._request_finish(FINISHED, "")
            return

    def _until_duration(self, poll: float) -> float:
        """Seconds the driver may sleep in real time: ``poll``, less when the duration ends sooner."""
        if self.duration is None:
            return poll
        return min(poll, max(self.duration - self.clock.now(), 0.0))

    def _owned_mailboxes(self):
        """(node, mailbox) of every mailbox, also those of pulled inputs."""
        yield from self._mailboxes.items()
        for (node_id, _port), mailbox in self._pulled.items():
            yield node_id, mailbox


@dataclass
class _Failure:
    error: BaseException


class ExternalQueue:
    """Values from device threads (stream blocks) for a node; the flow waits while it is open.

    ``put()`` may be called from any thread; ``close()`` ends it (``get()`` then returns ``None``
    once everything was taken).
    """

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._items: deque = deque()
        self._closed = False
        self._waiter: Optional[asyncio.Future] = None
        self._lock = threading.Lock()
        engine._external += 1

    def put(self, item: Any) -> None:
        with self._lock:
            if self._closed:
                return
        self._call(self._deliver, item)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._call(self._deliver_close)

    def _call(self, function, *args) -> None:
        loop = self.engine._loop
        try:
            if threading.current_thread() is getattr(self.engine, "_thread", None):
                function(*args)
            else:
                loop.call_soon_threadsafe(function, *args)
        except RuntimeError:  # the flow ended
            pass

    def _deliver(self, item: Any) -> None:
        self._items.append(item)
        if self._waiter is not None:
            self.engine._resolve(self._waiter, None)

    def _deliver_close(self) -> None:
        self.engine._external -= 1
        self._items.append(_CLOSED)
        if self._waiter is not None:
            self.engine._resolve(self._waiter, None)

    async def get(self) -> Any:
        """The next item; ``None`` after :meth:`close`."""
        while not self._items:
            self._waiter = self.engine._loop.create_future()
            try:
                await self.engine._wait(self._waiter)
            except asyncio.CancelledError:
                with self._lock:
                    open_ = not self._closed
                    self._closed = True
                if open_:
                    self.engine._external -= 1
                raise
            finally:
                self._waiter = None
        item = self._items.popleft()
        return None if item is _CLOSED else item


_CLOSED = object()
