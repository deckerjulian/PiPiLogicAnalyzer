# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""What a node is while a flow runs: :class:`NodeRuntime` and the context it works with."""

from __future__ import annotations

import inspect
import os
from typing import TYPE_CHECKING, Any, Callable, Optional

import numpy as np

from ...core import signals, units
from ..model import Node
from ..nodes.registry import NodeSpec

if TYPE_CHECKING:  # pragma: no cover
    from .engine import Engine


class NodeError(RuntimeError):
    """A node cannot run (bad parameter, missing device); stops the flow with a message."""


class NodeContext:
    """The engine as one node sees it."""

    def __init__(self, engine: "Engine", node: Node, spec: NodeSpec) -> None:
        self.engine = engine
        self.node_id = node.id
        self.node = node
        self.spec = spec
        inputs, outputs = spec.resolve_ports(node.params)
        self.input_ports = {port.name: port for port in inputs}
        self.output_ports = {port.name: port for port in outputs}
        seed = (engine.seed * 1_000_003 + sum(ord(char) * (index + 1) for index, char in enumerate(node.id))) % (2**32)
        #: deterministic random numbers: the same flow with the same seed gives the same values
        self.rng = np.random.default_rng(seed)

    # ------------------------------------------------------------- values
    def emit(self, port: str, value: Any) -> None:
        """Send ``value`` on the output ``port`` to every wired input."""
        if port not in self.output_ports:
            raise NodeError(f"{self.node_id} has no output {port!r}")
        self.engine._emit(self.node_id, port, value)

    async def send(self, port: str, value: Any) -> None:
        """:meth:`emit` and wait until the receivers took it (back pressure for sources)."""
        self.emit(port, value)
        await self.engine._drain(self.node_id)

    async def receive(self, port: str) -> Any:
        """The next value on the input ``port`` (for ports a node pulls in its ``run()``)."""
        return await self.engine._receive(self.node_id, port)

    async def receive_any(self, ports, timeout: Optional[float] = None):
        """The next value on any of the pulled ``ports``: ``(port, value)``, or ``(None, None)``
        when ``timeout`` seconds passed first."""
        return await self.engine._receive_any(self.node_id, list(ports), timeout)

    def discard(self, ports) -> None:
        """Drop the values waiting on the pulled ``ports`` (they came before the node was ready for
        them); :meth:`latest` keeps the last of them."""
        self.engine._discard(self.node_id, list(ports))

    def latest(self, port: str, default: Any = None) -> Any:
        """The last value that arrived on the input ``port``."""
        return self.engine._latest.get((self.node_id, port), default)

    def wired(self, port: str) -> bool:
        """Whether a wire leads to the input ``port`` or from the output ``port``."""
        return self.engine._is_wired(self.node_id, port)

    # --------------------------------------------------------- parameters
    def param(self, name: str, default: Any = None) -> Any:
        """The parameter ``name`` as written (or its default)."""
        value = self.node.params.get(name)
        if value is None:
            spec = self.spec.param(name)
            value = spec.default if spec is not None and spec.default is not None else default
        return value

    def quantity(self, name: str, default: Any = None) -> Optional[float]:
        """The parameter ``name`` as a number in its base unit (``"200 ms"`` → 0.2)."""
        value = self.param(name, default)
        if value is None or value == "":
            return None
        spec = self.spec.param(name)
        try:
            return units.parse(value, spec.unit if spec else "")
        except units.UnitError as error:
            raise NodeError(f"{self.node_id}: parameter {name}: {error}") from None

    # --------------------------------------------------------------- time
    def now(self) -> float:
        """Seconds since the flow started (virtual or real)."""
        return self.engine.clock.now()

    async def sleep(self, seconds: float) -> None:
        await self.engine._sleep(max(float(seconds), 0.0))

    async def sleep_until(self, time: float) -> None:
        """Sleep until the flow time ``time`` (at once when it has passed). For periodic work:
        waiting for ``start + n * period`` keeps the period, where waiting ``period`` after each
        round adds the time the round took, round after round."""
        await self.engine._sleep_until(float(time))

    async def device_call(self, device: Any, function: Callable, *args) -> Any:
        """Call ``function(*args)`` of a device. A simulator answers at once and is called
        directly (in virtual time that keeps the run deterministic); a real device may take
        its time or not answer at all, so it is called in a thread – one call at a time per
        device – and the other nodes, *Pause* and *Stop* go on meanwhile."""
        return await self.engine._device_call(device, function, *args)

    async def external(self, function: Callable, *args) -> Any:
        """Run a blocking ``function`` (device I/O) in a thread; the flow waits for it."""
        return await self.engine._external_call(function, *args)

    def external_queue(self):
        """A queue device threads put values into (:class:`~.engine.ExternalQueue`)."""
        return self.engine._external_queue()

    def external_future(self):
        """``(awaitable, complete)``: ``complete(value)`` may be called from any thread once."""
        return self.engine._external_future()

    # ------------------------------------------------------------- others
    def device(self, name: str):
        """The instrument of the device node ``name``."""
        return self.engine.device(name)

    def instrument(self, port: str = "device"):
        """The instrument of the device node wired to the input ``port``."""
        name = self.engine.flow.device_of(self.node_id, port)
        if name is None:
            raise NodeError(f"{self.node_id}: no device is wired to '{port}'")
        return self.engine.device(name)

    def log(self, text: str) -> None:
        self.engine._log(self.node_id, text)

    def stop_flow(self, reason: str = "") -> None:
        self.engine._request_finish("finished", reason)

    @property
    def views(self):
        return self.engine.views

    @property
    def mode(self) -> str:
        return self.engine.mode

    @property
    def fast(self) -> bool:
        return self.engine.mode == "virtual"

    def path(self, path: str) -> str:
        """``path`` relative to the project (its ``data/`` for plain names) or the flow file."""
        return self.engine.resolve_path(path)


class NodeRuntime:
    """Base class of the built-in node implementations.

    * ``run()`` – the active part (overridden by sources and sequences), started with the flow;
    * ``on_input(port, value)`` – called for every value arriving on an input not in ``pulled``;
    * ``finish()`` – called once when the flow ends (write files, close what was opened).
    """

    #: inputs read with ``ctx.receive()`` in ``run()`` instead of ``on_input()``
    pulled: tuple[str, ...] = ()

    def __init__(self, node: Node, spec: NodeSpec, ctx: NodeContext) -> None:
        self.node = node
        self.spec = spec
        self.ctx = ctx
        self.params: dict[str, Any] = {**spec.defaults(), **node.params}

    # ------------------------------------------------------------ helpers
    def q(self, name: str, default: Any = None) -> Optional[float]:
        """The parameter ``name`` as a number in its base unit (``"200 ms"`` → 0.2)."""
        value = self.params.get(name, default)
        if value is None or value == "":
            return None
        spec = self.spec.param(name)
        try:
            return units.parse(value, spec.unit if spec else "")
        except units.UnitError as error:
            raise NodeError(f"{self.node.id}: parameter {name}: {error}") from None

    def p(self, name: str, default: Any = None) -> Any:
        value = self.params.get(name, default)
        return default if value is None else value

    @property
    def has_run(self) -> bool:
        return type(self).run is not NodeRuntime.run

    # ---------------------------------------------------------- lifecycle
    async def setup(self) -> None:
        pass

    async def run(self) -> None:
        pass

    async def on_input(self, port: str, value: Any) -> None:
        pass

    async def finish(self) -> None:
        pass

    def cleanup(self) -> None:
        """Called once when the run is over, also after an error or a stop: let go of what the
        node started on a device (a capture, a stream, a generator). Devices of the device list
        stay open after the flow, so nothing of it may keep running on them."""


class FunctionNode(NodeRuntime):
    """A plain function: computed from the latest values whenever an input changes."""

    async def on_input(self, port: str, value: Any) -> None:
        function = self.spec.implementation
        names = list(self.ctx.input_ports)
        required = [port.name for port in self.ctx.input_ports.values() if not port.optional]
        if any(self.ctx.latest(name) is None for name in required):
            return
        arguments = {name: self.ctx.latest(name) for name in names if self.ctx.latest(name) is not None}
        signature = inspect.signature(function)
        if any(parameter.name == "params" for parameter in signature.parameters.values()):
            arguments["params"] = self.params
        result = function(**arguments)
        if inspect.isawaitable(result):
            result = await result
        self._emit_result(result)

    def _emit_result(self, result: Any) -> None:
        if result is None:
            return
        outputs = list(self.ctx.output_ports)
        if isinstance(result, dict) and set(result) <= set(outputs) and len(outputs) > 1:
            for port, value in result.items():
                self.ctx.emit(port, value)
        elif outputs:
            self.ctx.emit(outputs[0], result)


class CoroutineNode(NodeRuntime):
    """An ``async def node(ctx)``: the function is the node's ``run()``."""

    async def run(self) -> None:
        await self.spec.implementation(self.ctx)


def runtime_class(spec: NodeSpec):
    implementation = spec.implementation
    if inspect.isclass(implementation) and issubclass(implementation, NodeRuntime):
        return implementation
    if inspect.iscoroutinefunction(implementation):
        return CoroutineNode
    if callable(implementation):
        return FunctionNode
    raise NodeError(f"the node type {spec.type} has no implementation")


def summarize(value: Any) -> str:
    """A short text for a value on a wire (inspector, wire label, log)."""
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return f"{value:.6g}"
    if isinstance(value, signals.Scalar):
        # without a unit an SI prefix misleads (a duty cycle of 0.5 is no "500 m")
        return units.format_quantity(value.value, value.unit) if value.unit else f"{value.value:.6g}"
    if isinstance(value, signals.Bool):
        return "true" if value.value else "false"
    if isinstance(value, (signals.Digital, signals.Analog)):
        rate = f" @ {units.format_quantity(value.rate, 'Hz')}" if value.rate else ""
        return f"{value.type_name} {len(value)} samples{rate}"
    if isinstance(value, signals.Capture):
        return f"Capture {len(value.channels)} ch × {value.sample_count} @ {units.format_quantity(value.rate, 'Hz')}"
    if isinstance(value, signals.Event):
        if len(value) == 1 and value.data[0] is not None and not isinstance(value.data[0], signals.Event):
            return summarize(value.data[0])  # one event with a value (a path, a reading): the value
        return f"{len(value)} events"
    if isinstance(value, signals.Table):
        return f"Table {len(value)} rows"
    if isinstance(value, str):
        return value if len(value) <= 40 else value[:37] + "..."
    if isinstance(value, (bytes, bytearray)):
        text = " ".join(f"{byte:02x}" for byte in value[:16])
        return text + (" ..." if len(value) > 16 else "")
    if isinstance(value, dict):  # a bundle: its fields
        return ", ".join(f"{key}: {summarize(item)}" for key, item in list(value.items())[:4]) + (
            ", ..." if len(value) > 4 else "")
    if isinstance(value, (list, tuple)):
        return f"[{len(value)} items]"
    if isinstance(value, os.PathLike):
        return os.fspath(value)
    return type(value).__name__
