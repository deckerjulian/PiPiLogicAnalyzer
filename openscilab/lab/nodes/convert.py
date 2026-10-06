# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Conversion nodes: the editor offers them when a wire's types do not fit (``core.signals.CONVERSIONS``)."""

from __future__ import annotations

from typing import Any, Optional

import numpy as np

from ...core import signals
from ..engine.runtime import NodeError, NodeRuntime
from .registry import In, Out, Param, collect, node


class _Convert(NodeRuntime):
    source = signals.ANY
    target = signals.ANY

    def options(self) -> dict:
        return {}

    async def on_input(self, port: str, value: Any) -> None:
        if isinstance(value, (int, float)) and self.source == signals.SCALAR:
            value = signals.Scalar(value=float(value), at=self.ctx.now())
        if isinstance(value, bool) and self.source == signals.BOOL:
            value = signals.Bool(value=value, at=self.ctx.now())
        if not isinstance(value, signals.TYPES[self.source]):
            raise NodeError(f"{self.node.id}: expects {self.source}, got {type(value).__name__}")
        conversion = signals.conversion(self.source, self.target)
        self.ctx.emit("out", conversion.function(value, **self.options()))


@node("convert.to_analog", title="Digital to analog", description="Logic levels as voltages.",
      inputs=[In("in", signals.DIGITAL, optional=False)], outputs=[Out("out", signals.ANALOG)],
      params=[Param("low", "quantity", 0.0, "V"), Param("high", "quantity", 3.3, "V")], icon="wave")
class ToAnalog(_Convert):
    source, target = signals.DIGITAL, signals.ANALOG

    def options(self) -> dict:
        return {"low": self.q("low"), "high": self.q("high")}


@node("convert.edges", title="Edges", description="The edges of a digital signal as events.",
      inputs=[In("in", signals.DIGITAL, optional=False)], outputs=[Out("out", signals.EVENT)],
      params=[Param("kind", "choice", "both", choices=("both", "rising", "falling"))], icon="wave")
class Edges(_Convert):
    source, target = signals.DIGITAL, signals.EVENT

    async def setup(self) -> None:
        #: the last level of the previous block and when it ended (a stream: an edge between two
        #: blocks is an edge too)
        self.last: Optional[tuple[int, float]] = None

    def options(self) -> dict:
        return {"kind": self.p("kind", "both")}

    async def on_input(self, port: str, value: Any) -> None:
        if not isinstance(value, signals.Digital):
            raise NodeError(f"expects {self.source}, got {type(value).__name__}")
        kind = self.p("kind", "both")
        found = value.edges(kind)
        if len(value) and value.time.is_known:
            start = value.time.time_of(0)
            last = self.last
            rate = value.rate or 0.0
            follows = last is not None and rate > 0 and abs(start - last[1]) < 1.5 / rate
            first = int(value.values[0])
            if follows and first != last[0] and (kind == "both" or (kind == "rising") == (first == 1)):
                found = signals.Event(name=found.name, times=np.concatenate([[start], found.times]),
                                      data=[first] + list(found.data))
            self.last = (int(value.values[-1]), value.time.end(len(value)) if value.time.is_uniform else start)
        self.ctx.emit("out", found)


@node("convert.to_scalar", title="Bool to number", description="1 for true, 0 for false.",
      inputs=[In("in", signals.BOOL, optional=False)], outputs=[Out("out", signals.SCALAR)])
class ToScalar(_Convert):
    source, target = signals.BOOL, signals.SCALAR


@node("convert.state_bit", title="State bit",
      description="One channel of the states (values taken on the edges of a clock) as a digital signal, "
                  "one sample per state.",
      inputs=[In("in", signals.STATES, optional=False)], outputs=[Out("out", signals.DIGITAL)],
      params=[Param("bit", "any", 0, description="the channel's name (D3), or its number (0: the first)")])
class StateBit(_Convert):
    source, target = signals.STATES, signals.DIGITAL

    async def on_input(self, port: str, value: Any) -> None:
        if isinstance(value, signals.States):
            self.index = self.bit_of(value)
        await super().on_input(port, value)

    def bit_of(self, states: signals.States) -> int:
        bit = self.p("bit", 0)
        if isinstance(bit, str) and not bit.strip().isdigit():
            if bit not in states.channels:
                raise NodeError(f"the states have no channel {bit!r} ({', '.join(states.channels) or 'none named'})")
            return states.channels.index(bit)
        index = int(bit)
        if index < 0 or index >= (len(states.channels) or 32):
            raise NodeError(f"the states have {len(states.channels)} channels (bits 0 to {len(states.channels) - 1}), "
                            f"no bit {index}")
        return index

    def options(self) -> dict:
        return {"bit": self.index}


@node("convert.channel", title="Channel",
      description="One channel of a capture: 'channel' by its name, else the first one of 'kind' (any, digital, "
                  "analog).",
      inputs=[In("in", signals.CAPTURE, optional=False)], outputs=[Out("out", signals.ANY)],
      params=[Param("channel", "str", "", suggest="upstream_channels"),
              Param("kind", "choice", "any", choices=("any", "digital", "analog"))], icon="channels")
class Channel(NodeRuntime):
    async def on_input(self, port: str, value: Any) -> None:
        if not isinstance(value, signals.Capture):
            raise NodeError("expects a capture")
        kind = self.p("kind", "any")
        choices = {"digital": list(value.digital), "analog": list(value.analog)}.get(kind, value.channels)
        name = self.p("channel") or next(iter(choices), None)
        if name is None:
            raise NodeError(f"the capture has no {'' if kind == 'any' else kind + ' '}channels")
        if name not in value.channels:
            raise NodeError(f"the capture has no channel {name} (it has {', '.join(value.channels)})")
        self.ctx.emit("out", value.channel(name))


NODES = collect(globals())
