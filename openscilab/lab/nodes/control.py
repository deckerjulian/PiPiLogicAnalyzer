# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Control nodes: timers, sweeps, sequences, state machines, Python, comparisons, limits, counters."""

from __future__ import annotations

import asyncio
import operator
from typing import Any, Optional

import numpy as np

from ...core import signals, units
from ..engine.runtime import NodeError, NodeRuntime
from .registry import In, Out, Param, collect, node


def event(ctx, data: Any = None) -> signals.Event:
    return signals.Event(times=[ctx.now()], data=[data])


@node("control.timer", title="Timer",
      description="Ticks every 'interval' (after 'delay'); 'count' ticks, or endless with 0.",
      outputs=[Out("tick", signals.EVENT), Out("index", signals.SCALAR)],
      params=[Param("interval", "quantity", "1 s", "s"), Param("count", "int", 1), Param("delay", "quantity", 0, "s")],
      icon="clock")
class TimerNode(NodeRuntime):
    async def run(self) -> None:
        interval = self.q("interval")
        if interval is None:
            raise NodeError("needs an interval (1 s, 100 ms)")
        if interval < 0:
            raise NodeError("the interval must not be negative")
        count = int(self.p("count", 1))
        if interval == 0 and count <= 0:
            raise NodeError(f"{self.node.id}: an endless timer needs an interval (it would never let time pass)")
        delay = self.q("delay") or 0.0
        if delay:
            await self.ctx.sleep(delay)
        start = self.ctx.now()
        index = 0
        while count <= 0 or index < count:
            self.ctx.emit("index", float(index))
            await self.ctx.send("tick", event(self.ctx, index))
            index += 1
            if count <= 0 or index < count:
                # the n-th tick at start + n * interval: what the tick itself took does not add up
                await self.ctx.sleep_until(start + index * interval)


@node("control.sweep", title="Sweep",
      description="Steps a value from 'start' to 'stop' (or through 'values'): sends 'value', waits "
                  "'dwell', then sends 'step'. With a wire at 'next' it waits for it before the next value; "
                  "with one at 'trigger' it sweeps for every value there (a start button).",
      inputs=[In("next", signals.ANY, "continue with the next value"), In("trigger", signals.ANY, "start a sweep")],
      outputs=[Out("value", signals.SCALAR), Out("step", signals.EVENT), Out("index", signals.SCALAR),
               Out("done", signals.EVENT)],
      params=[Param("start", "any", 0.0, description="a number or a quantity (0 V)"), Param("stop", "any", 1.0),
              Param("step", "any", 0.1), Param("values", "list", description="explicit values instead of start/stop/step"),
              Param("dwell", "quantity", 0, "s"),
              Param("unit", "str", "", description="of the values (without: the unit of the quantities)")],
      icon="sliders")
class SweepNode(NodeRuntime):
    pulled = ("next", "trigger")

    def unit(self) -> str:
        """The unit of the values: the parameter, or the base unit of the quantities (start: 0 V)."""
        if self.p("unit"):
            return str(self.p("unit"))
        for item in [*(self.p("values") or []), self.p("start"), self.p("stop"), self.p("step")]:
            try:
                found = units.split(item)[1] if item is not None else ""
            except units.UnitError:
                continue
            if found:
                return found
        return ""

    def number(self, value: Any, name: str) -> float:
        try:
            return units.in_unit(value, self.unit())
        except units.UnitError as error:
            raise NodeError(f"{name}: {error}") from None

    def values(self) -> list[float]:
        explicit = self.p("values")
        if explicit:
            return [self.number(value, "values") for value in explicit]
        start, stop = self.number(self.p("start", 0), "start"), self.number(self.p("stop", 1), "stop")
        step = self.number(self.p("step", 0.1), "step")
        if step == 0:
            raise NodeError("step must not be 0")
        if (stop - start) * step < 0:
            raise NodeError("step goes away from stop")
        count = int(np.floor((stop - start) / step + 1e-9)) + 1
        return [round(start + index * step, 12) for index in range(count)]

    async def run(self) -> None:
        if not self.ctx.wired("trigger"):
            await self.sweep()
            return
        while True:  # a sweep for every value at 'trigger'
            await self.ctx.receive("trigger")
            await self.sweep()

    async def sweep(self) -> None:
        dwell = self.q("dwell") or 0.0
        unit = self.unit()
        for index, value in enumerate(self.values()):
            self.ctx.emit("index", float(index))
            await self.ctx.send("value", signals.Scalar(name=self.node.id, unit=unit, value=value, at=self.ctx.now()))
            if dwell:
                await self.ctx.sleep(dwell)
            await self.ctx.send("step", event(self.ctx, value))
            if self.ctx.wired("next"):
                await self.ctx.receive("next")
        self.ctx.emit("done", event(self.ctx))


# ------------------------------------------------------------------ helpers
def plain(value: Any) -> Any:
    """A value as Python: numbers of scalars, truth of bools, the data of single events."""
    if isinstance(value, (signals.Scalar, signals.Bool)):
        return value.value
    if isinstance(value, signals.Event):
        return value.data[-1] if len(value) == 1 else list(value.data)
    if isinstance(value, np.generic):
        return value.item()
    return value


def literal(value: Any) -> Any:
    """A parameter value: quantities like "3.3 V" become numbers, other text stays text."""
    if isinstance(value, str):
        try:
            return units.parse(value)
        except units.UnitError:
            return value
    return value


def unit_of(value: Any) -> str:
    return value.unit if isinstance(value, (signals.Scalar, signals.Analog)) else ""


def in_unit(value: Any, unit: str) -> Any:
    """A value compared with a signal in ``unit``, as a number in that unit: ``"20 mA"`` and a Scalar
    of 0.02 A both become 20 for a signal in mA; a plain number is in that unit already. Text that is
    no quantity stays text; a quantity of another unit is refused."""
    if isinstance(value, signals.Scalar) and value.unit:
        factor, base = units.scale_of(value.unit)
        own, own_base = units.scale_of(unit)
        if own_base and base != own_base:
            raise NodeError(f"{value.value:g} {value.unit} is not in {unit}")
        return float(f"{value.value * factor / own:.12g}")
    value = plain(value)
    if isinstance(value, str):
        try:
            units.split(value)
        except units.UnitError:
            return value  # (text: a state, a name)
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        try:
            return units.in_unit(value, unit)
        except units.UnitError as error:
            raise NodeError(str(error)) from None
    return value


# ----------------------------------------------------------------- sequence
def _sequence_ports(params: dict):
    inputs, outputs, seen_in, seen_out = [], [], set(), set()
    for step in params.get("steps") or []:
        if not isinstance(step, dict):
            continue
        name = step.get("set") or step.get("emit")
        if name and name not in seen_out:
            seen_out.add(name)
            outputs.append(Out(str(name), signals.ANY))
        name = step.get("wait_for")
        if name and name not in seen_in:
            seen_in.add(name)
            inputs.append(In(str(name), signals.ANY, "the sequence waits for a value here"))
    return inputs, outputs


@node("control.sequence", title="Sequence",
      description="Steps without code, one after the other: {set: out, value: 1} sends a value, "
                  "{emit: out} an event, {wait: 100 ms} waits, {wait_for: in, timeout: 1 s} waits for a "
                  "value on an input. 'repeat' runs it several times (0: for ever).",
      inputs=[In("start", signals.ANY, "start, for every value again (without a wire: at once)")],
      outputs=[Out("step", signals.SCALAR), Out("done", signals.EVENT), Out("timeout", signals.EVENT)],
      params=[Param("steps", "table", [], required=True), Param("repeat", "int", 1)],
      ports=_sequence_ports, icon="sequence")
class SequenceNode(NodeRuntime):
    async def setup(self) -> None:
        inputs, _outputs = _sequence_ports(self.params)
        self.pulled = ("start",) + tuple(port.name for port in inputs)
        for index, step in enumerate(self.p("steps") or []):
            if isinstance(step, dict) and step.get("wait_for") and step.get("timeout") is None \
                    and not self.ctx.wired(str(step["wait_for"])):
                raise NodeError(f"step {index + 1} waits for {step['wait_for']!r}, which has no wire (and no timeout)")

    async def run(self) -> None:
        if not self.ctx.wired("start"):
            await self.sequence()
            return
        while True:  # the steps for every value at 'start' (a start button)
            await self.ctx.receive("start")
            await self.sequence()

    async def sequence(self) -> None:
        steps = list(self.p("steps") or [])
        repeat = int(self.p("repeat", 1))
        round_ = 0
        while repeat <= 0 or round_ < repeat:
            for index, step in enumerate(steps):
                self.ctx.emit("step", float(index))
                await self.run_step(step, index)
            round_ += 1
            # steps that never wait (set, emit, log) would repeat without letting anything else
            # run – not even Stop
            await asyncio.sleep(0)
        self.ctx.emit("done", event(self.ctx, round_))

    async def run_step(self, step: dict, index: int) -> None:
        if not isinstance(step, dict):
            raise NodeError(f"{self.node.id}: step {index + 1} is not a mapping")
        if "set" in step:
            await self.ctx.send(str(step["set"]), literal(step.get("value")))
        elif "emit" in step:
            await self.ctx.send(str(step["emit"]), event(self.ctx, step.get("value")))
        elif "wait" in step:
            await self.ctx.sleep(units.parse(step["wait"], "s"))
        elif "wait_for" in step:
            timeout = units.parse(step["timeout"], "s") if step.get("timeout") is not None else None
            port, _value = await self.ctx.receive_any([str(step["wait_for"])], timeout)
            if port is None:
                self.ctx.emit("timeout", event(self.ctx, index))
                if step.get("on_timeout", "continue") == "stop":
                    raise NodeError(f"{self.node.id}: step {index + 1}: no value on {step['wait_for']} within "
                                    f"{step['timeout']}")
        elif "log" in step:
            self.ctx.log(str(step["log"]))
        else:
            raise NodeError(f"{self.node.id}: step {index + 1} has no action (set, emit, wait, wait_for, log)")


# ------------------------------------------------------------ state machine
def _machine_ports(params: dict):
    inputs, outputs, seen_in, seen_out = [], [], set(), set()
    for state in (params.get("states") or {}).values():
        if not isinstance(state, dict):
            continue
        for name in (state.get("enter") or {}):
            if name not in seen_out:
                seen_out.add(name)
                outputs.append(Out(str(name), signals.ANY))
        for transition in state.get("on") or []:
            name = transition.get("input") if isinstance(transition, dict) else None
            if name and name not in seen_in:
                seen_in.add(name)
                inputs.append(In(str(name), signals.ANY))
    return inputs, outputs


@node("control.state_machine", title="State machine",
      description="States with what they set and when they go on. It starts in 'initial'; entering a state "
                  "sends its 'enter' values on outputs of those names (enter: {red: 1, green: 0} makes the "
                  "outputs red and green) and 'state' says which state it is. 'on' lists the ways out: "
                  "{after: 1 s, to: green} after a time, {input: button, when: 1, to: green} when a value "
                  "arrives at an input of that name; the first that happens wins. A state without 'on' ends "
                  "the machine ('done').",
      outputs=[Out("state", signals.ANY), Out("done", signals.EVENT)],
      params=[Param("states", "dict", {}, required=True), Param("initial", "str", required=True)],
      ports=_machine_ports, icon="sequence")
class StateMachineNode(NodeRuntime):
    async def setup(self) -> None:
        inputs, _outputs = _machine_ports(self.params)
        self.pulled = tuple(port.name for port in inputs)
        states = self.p("states") or {}
        if self.p("initial") not in states:
            raise NodeError(f"{self.node.id}: the initial state {self.p('initial')!r} is not one of the states")
        for name, state in states.items():
            unknown = [str(key) for key in (state or {}) if key not in ("enter", "on")] if isinstance(state, dict) else []
            if unknown:  # (YAML 1.1 reads an unquoted on: as true)
                raise NodeError(f"{self.node.id}: state {name!r} has {', '.join(unknown)} - a state has 'enter' "
                                "and 'on'")
            for item in (state or {}).get("on") or [] if isinstance(state, dict) else []:
                if not isinstance(item, dict):
                    raise NodeError(f"state {name!r}: a way out is a mapping ({{after: 1 s, to: green}})")
                if item.get("to") not in states:
                    raise NodeError(f"state {name!r} goes to {item.get('to')!r}, which is no state")
                if "after" not in item and "input" not in item:
                    raise NodeError(f"state {name!r}: the way to {item.get('to')!r} needs 'after' (a time) or "
                                    "'input' (a value arriving)")
                if "after" in item:
                    try:
                        units.parse(item["after"], "s")
                    except units.UnitError as error:
                        raise NodeError(f"state {name!r}: after: {error}") from None

    async def run(self) -> None:
        states = self.p("states") or {}
        current = self.p("initial")
        while True:
            state = states.get(current)
            if not isinstance(state, dict):
                raise NodeError(f"{self.node.id}: unknown state {current!r}")
            self.ctx.emit("state", current)
            # what arrived before this state was entered does not lead out of it
            self.ctx.discard(self.pulled)
            for name, value in (state.get("enter") or {}).items():
                await self.ctx.send(str(name), literal(value))
            transitions = list(state.get("on") or [])
            if not transitions:
                self.ctx.emit("done", event(self.ctx, current))
                return
            current = await self.next_state(transitions)

    async def next_state(self, transitions: list) -> str:
        timers = [(units.parse(item["after"], "s"), item["to"]) for item in transitions if "after" in item]
        inputs = [item for item in transitions if "input" in item]
        started = self.ctx.now()
        while True:
            timeout: Optional[float] = None
            if timers:
                timeout = max(min(delay for delay, _to in timers) - (self.ctx.now() - started), 0.0)
            if not self.pulled:
                await self.ctx.sleep(timeout or 0.0)
                return min(timers)[1]
            # every input is read, also those this state does not listen to: their values are
            # dropped instead of piling up (and holding up whoever sends them)
            port, value = await self.ctx.receive_any(self.pulled, timeout)
            if port is None:
                return min(timers)[1]
            for item in inputs:
                if str(item["input"]) != port:
                    continue
                if "when" not in item or literal(item["when"]) == plain(value):
                    return str(item["to"])


# ------------------------------------------------------------------- python
def _python_ports(params: dict):
    return ([In(str(name), signals.ANY) for name in params.get("inputs") or []],
            [Out(str(name), signals.ANY) for name in params.get("outputs") or []])


@node("control.python", title="Python",
      description="Your own code: 'async def run(ctx)' runs with the flow (ctx.emit, await ctx.sleep, "
                  "await ctx.receive(port), ctx.device(name), ctx.log); 'def on_input(ctx, port, value)' "
                  "is called for values on the inputs. 'inputs' and 'outputs' name the ports.",
      params=[Param("code", "code", "async def run(ctx):\n    ctx.log('hello')\n", required=True),
              Param("inputs", "list", []), Param("outputs", "list", ["out"]),
              Param("pulled", "list", [], description="inputs read with await ctx.receive() in run()")],
      ports=_python_ports, icon="terminal")
class PythonNode(NodeRuntime):
    async def setup(self) -> None:
        namespace = {"np": np, "signals": signals, "units": units, "__name__": f"flow_node_{self.node.id}"}
        try:
            exec(compile(str(self.p("code", "")), f"<{self.node.id}>", "exec"), namespace)  # noqa: S102 - the flow's own code
        except SyntaxError as error:
            raise NodeError(f"{self.node.id}: line {error.lineno}: {error.msg}") from None
        self._run = namespace.get("run")
        self._on_input = namespace.get("on_input")
        if self._run is None and self._on_input is None:
            raise NodeError(f"{self.node.id}: define async def run(ctx) or def on_input(ctx, port, value)")
        self.pulled = tuple(str(name) for name in self.p("pulled") or [])

    async def run(self) -> None:
        if self._run is not None:
            result = self._run(self.ctx)
            if hasattr(result, "__await__"):
                await result

    async def on_input(self, port: str, value: Any) -> None:
        if self._on_input is not None:
            result = self._on_input(self.ctx, port, value)
            if hasattr(result, "__await__"):
                await result


# ------------------------------------------------------------ comparisons
OPERATORS = {
    "==": operator.eq, "!=": operator.ne, "<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge,
    "contains": lambda a, b: b in a, "startswith": lambda a, b: str(a).startswith(str(b)),
}


@node("control.compare", title="Compare",
      description="Compares 'a' with 'b' (or with 'value' when 'b' has no wire): ==, !=, <, <=, >, >=, "
                  "within (|a - b| <= tolerance), contains, startswith. Sends the result and 'passed' or "
                  "'failed'.",
      inputs=[In("a", signals.ANY, optional=False), In("b", signals.ANY)],
      outputs=[Out("result", signals.BOOL), Out("passed", signals.EVENT), Out("failed", signals.EVENT)],
      params=[Param("op", "choice", "==", choices=tuple(OPERATORS) + ("within",)),
              Param("value", "any", description="a number in the unit of 'a', or a quantity (20 mA)"),
              Param("tolerance", "any", 0.0, description="for 'within', like 'value'")], icon="compare")
class CompareNode(NodeRuntime):
    async def on_input(self, port: str, value: Any) -> None:
        a_value = self.ctx.latest("a")
        if self.ctx.wired("b"):
            if self.ctx.latest("b") is None:
                return
            b_value = self.ctx.latest("b")
        else:
            b_value = self.p("value")
            if b_value is None:
                raise NodeError("needs a 'value' to compare with, or a wire at 'b'")
        if a_value is None:
            return
        if isinstance(a_value, (signals.Analog, signals.Digital, signals.Capture, signals.Table)):
            raise NodeError(f"compares values, not a whole {a_value.type_name} signal: measure it first "
                            "(measure.mean, measure.max, ...)")
        a, unit, op = plain(a_value), unit_of(a_value), self.p("op", "==")
        b = b_value
        try:
            if op in ("contains", "startswith"):
                b = plain(b_value)  # (text stays text: "10" is no number here)
                result = bool(OPERATORS[op](a, b))
            elif op == "within":
                b = in_unit(b_value, unit)
                result = abs(float(a) - float(b)) <= float(in_unit(self.p("tolerance", 0.0) or 0.0, unit))
            else:
                b = b_value if isinstance(a, str) else in_unit(b_value, unit)
                result = bool(OPERATORS[op](a, b))
        except (TypeError, ValueError, KeyError) as error:
            raise NodeError(f"cannot compare {a!r} {op} {b!r}: {error}") from None
        shown = f"{a!r} {op} {b!r}" + (f" ({unit})" if unit else "")
        self.ctx.log(f"{shown}: {'passed' if result else 'failed'}")
        self.ctx.views.show("check", self.node.id, result, text=f"{self.node.id}: {shown}")
        self.ctx.emit("result", signals.Bool(name=self.node.id, value=result, at=self.ctx.now()))
        self.ctx.emit("passed" if result else "failed", event(self.ctx, a))


@node("control.limit", title="Limit",
      description="Checks a value against 'low' and 'high': 'ok' and the value clipped to the limits. The "
                  "limits are numbers in the unit of the value, or quantities (3.3 V, 20 mA).",
      inputs=[In("in", signals.SCALAR, optional=False)],
      outputs=[Out("ok", signals.BOOL), Out("out", signals.SCALAR), Out("violation", signals.EVENT)],
      params=[Param("low", "any"), Param("high", "any")], icon="compare")
class LimitNode(NodeRuntime):
    async def on_input(self, port: str, value: Any) -> None:
        number, unit = float(plain(value)), unit_of(value)
        low, high = (None if self.p(name) in (None, "") else float(in_unit(self.p(name), unit))
                     for name in ("low", "high"))
        ok = (low is None or number >= low) and (high is None or number <= high)
        clipped = number
        if low is not None:
            clipped = max(clipped, low)
        if high is not None:
            clipped = min(clipped, high)
        at = value.at if isinstance(value, signals.Scalar) and value.at else self.ctx.now()
        self.ctx.emit("ok", signals.Bool(name=self.node.id, value=ok, at=at))
        self.ctx.emit("out", signals.Scalar(name=self.node.id, unit=unit, value=clipped, at=at))
        if not ok:
            self.ctx.emit("violation", event(self.ctx, number))


@node("control.counter", title="Counter",
      description="Counts the values arriving at 'in' (events count each event); 'reset' starts again.",
      inputs=[In("in", signals.ANY, optional=False, multiple=True), In("reset", signals.ANY)],
      outputs=[Out("count", signals.SCALAR)], params=[Param("start", "int", 0)], icon="sliders")
class CounterNode(NodeRuntime):
    async def setup(self) -> None:
        self.count = int(self.p("start", 0))

    async def on_input(self, port: str, value: Any) -> None:
        if port == "reset":
            self.count = int(self.p("start", 0))
        else:
            self.count += len(value) if isinstance(value, signals.Event) else 1
        self.ctx.emit("count", float(self.count))


NODES = collect(globals())
