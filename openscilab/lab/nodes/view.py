# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""View nodes: show values in documents of the shell (scope, charts, tables, numbers, LEDs, logs).

Without a user interface the values stay in the engine's view sink (reports use them)."""

from __future__ import annotations

from typing import Any, Optional

import numpy as np

from ...core import signals
from ..engine.runtime import NodeRuntime
from .control import plain
from .registry import In, Param, collect, node


def as_capture(value: Any) -> signals.Capture:
    if isinstance(value, signals.Capture):
        return value
    if isinstance(value, signals.Digital):
        return signals.Capture(name=value.name, rate=value.rate or 1.0, start=value.time.start,
                               digital={value.name or "D0": value.values})
    if isinstance(value, signals.Analog):
        return signals.Capture(name=value.name, rate=value.rate or 1.0, start=value.time.start,
                               analog={value.name or "A0": value.values}, analog_units={value.name or "A0": value.unit})
    raise TypeError(f"the scope shows captures and signals, not {type(value).__name__}")


@node("view.scope", title="Scope",
      description="Shows captures in a data view (with decoders, cursors and "
                  "measurements). Blocks of a stream are joined.",
      inputs=[In("in", signals.ANY, "a capture, a digital or an analog signal", multiple=True)],
      params=[Param("title", "str", ""), Param("join", "bool", True, description="join stream blocks")],
      icon="channels")
class ScopeNode(NodeRuntime):
    async def setup(self) -> None:
        self.capture = None

    async def on_input(self, port: str, value: Any) -> None:
        capture = as_capture(value)
        current = self.capture
        # the next block of a stream: same channels, and it starts where the shown capture ends
        # (windows of a buffer overlap: each one replaces the last)
        follows = current is not None and capture.start >= current.start + (current.sample_count - 0.5) / current.rate
        if self.p("join", True) and current is not None and capture.name == "stream" and follows \
                and current.rate == capture.rate and set(current.digital) == set(capture.digital) \
                and set(current.analog) == set(capture.analog):
            self.capture.append(capture)
        else:
            self.capture = signals.Capture(name=capture.name, rate=capture.rate, start=capture.start,
                                           digital=dict(capture.digital), analog=dict(capture.analog),
                                           analog_units=dict(capture.analog_units), trigger=capture.trigger,
                                           session=capture.session)
        # what is shown is read in another thread (the window) while the next block is joined:
        # it gets the capture as it is now (the sample arrays are shared, they are never changed)
        shown = self.capture
        snapshot = signals.Capture(name=shown.name, rate=shown.rate, start=shown.start, digital=dict(shown.digital),
                                   analog=dict(shown.analog), analog_units=dict(shown.analog_units),
                                   trigger=shown.trigger, session=shown.session)
        self.ctx.views.show("scope", self.node.id, snapshot, title=self.p("title") or self.node.id)


def _number(value: Any):
    value = plain(value)
    if isinstance(value, (bool, np.bool_)):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float, np.number)):
        return float(value)
    return None


def _text_number(text: str) -> Any:
    try:
        return float(text)
    except ValueError:
        return None


def _ports(params: dict):
    return [In(str(name), signals.ANY, multiple=True) for name in params.get("inputs") or []], []


@node("view.strip_chart", title="Strip chart",
      description="Values over time, like a chart recorder: numbers, events, the samples of analog and "
                  "digital signals, the channels of a capture or stream (a trace each). More traces: name "
                  "them in 'inputs'.",
      inputs=[In("in", signals.ANY, multiple=True)],
      params=[Param("title", "str", ""), Param("window", "quantity", "10 s", "s"), Param("inputs", "list", [])],
      ports=_ports, icon="chart")
class StripChartNode(NodeRuntime):
    async def setup(self) -> None:
        self.traces: dict[str, tuple[list, list]] = {}

    async def on_input(self, port: str, value: Any) -> None:
        if isinstance(value, signals.Capture):
            # a trace per channel (named by the channel; by port and channel on more inputs)
            for name in value.channels:
                await self.on_input(name if port == "in" else f"{port}.{name}", value.channel(name))
            return
        times, values = self.traces.setdefault(port, ([], []))
        if isinstance(value, (signals.Analog, signals.Digital)) and value.time.is_known and len(value):
            step = max(len(value) // 2000, 1)  # a chart needs no more points than pixels
            times.extend(value.times()[::step].tolist())
            values.extend(value.values[::step].astype(float).tolist())
        elif isinstance(value, signals.Event) and len(value):
            for stamp, data in value:  # at their own times
                number = _number(data)
                if number is not None:
                    times.append(float(stamp))
                    values.append(number)
        else:
            number = _number(value)
            if number is None:
                return
            times.append(value.at if isinstance(value, (signals.Scalar, signals.Bool)) and value.at else self.ctx.now())
            values.append(number)
        window = self.q("window") or 0
        if window and times:
            cut = max(times) - window
            kept = [(stamp, number) for stamp, number in zip(times, values) if stamp >= cut]
            times[:] = [stamp for stamp, _number_ in kept]
            values[:] = [number for _stamp, number in kept]
        self.ctx.views.show("strip_chart", self.node.id, {name: (list(t), list(v)) for name, (t, v) in self.traces.items()},
                            title=self.p("title") or self.node.id)


@node("view.xy", title="XY chart",
      description="Points (x, y): a point for every value at 'y', with the x it belongs to – the x values "
                  "in the order they came, the latest one again for more values at 'y' (a characteristic "
                  "curve: several measurements per step are fine).",
      inputs=[In("x", signals.ANY, optional=False), In("y", signals.ANY, optional=False)],
      params=[Param("title", "str", ""), Param("x_label", "str", "x"), Param("y_label", "str", "y")], icon="chart")
class XyNode(NodeRuntime):
    async def setup(self) -> None:
        self.points = signals.Table(name=self.node.id, columns={"x": [], "y": []})
        self.pending: list[float] = []
        self.x: Optional[float] = None

    async def on_input(self, port: str, value: Any) -> None:
        if port == "x":
            number = _number(value)
            if number is not None:
                self.pending.append(number)
            return
        if self.pending:
            self.x = self.pending.pop(0)
        x, y = self.x, _number(value)
        if x is None or y is None:
            return
        self.points.add_row({"x": x, "y": y})
        # a copy: the table shown is read in another thread while points are added here
        shown = signals.Table(name=self.points.name,
                              columns={name: list(values) for name, values in self.points.columns.items()})
        self.ctx.views.show("xy", self.node.id, shown, title=self.p("title") or self.node.id,
                            x_label=self.p("x_label", "x"), y_label=self.p("y_label", "y"))


@node("view.spectrum", title="Spectrum",
      description="The spectrum of an analog signal (or a table of frequency and magnitude from dsp.fft).",
      inputs=[In("in", signals.ANY, optional=False)],
      params=[Param("title", "str", ""), Param("scale", "choice", "dB", choices=("dB", "linear"))], icon="chart")
class SpectrumNode(NodeRuntime):
    async def on_input(self, port: str, value: Any) -> None:
        from .dsp import spectrum

        if isinstance(value, signals.Analog) and value.time.is_uniform and len(value) > 1:
            value = spectrum(value.values, value.rate, scale=self.p("scale", "dB"))
        if not isinstance(value, signals.Table):
            return
        self.ctx.views.show("spectrum", self.node.id, value, title=self.p("title") or self.node.id)


@node("view.table", title="Table view", description="Shows a table (or the rows of other values).",
      inputs=[In("in", signals.ANY, optional=False)], params=[Param("title", "str", "")], icon="list")
class TableViewNode(NodeRuntime):
    async def on_input(self, port: str, value: Any) -> None:
        from .data import row_of

        table = value if isinstance(value, signals.Table) else signals.Table.from_rows(row_of(value, self.ctx.now()))
        # a copy: the table shown is read in another thread while its node goes on filling it
        shown = signals.Table(name=table.name, columns={name: list(values) for name, values in table.columns.items()})
        self.ctx.views.show("table", self.node.id, shown, title=self.p("title") or self.node.id)


@node("view.number", title="Number",
      description="Shows the latest value as a number with its unit (of a signal: its last sample).",
      inputs=[In("in", signals.ANY, optional=False)],
      params=[Param("title", "str", ""),
              Param("unit", "str", "", description="the unit of values that have none (a measurement brings its own)"),
              Param("digits", "int", 4)], icon="info")
class NumberNode(NodeRuntime):
    async def on_input(self, port: str, value: Any) -> None:
        own = ""
        if isinstance(value, (signals.Analog, signals.Digital)):
            if not len(value):
                return
            own = getattr(value, "unit", "") or ""
            value = float(value.values[-1])
        elif isinstance(value, signals.Scalar):
            own = value.unit or ""
        self.ctx.views.show("number", self.node.id, plain(value), title=self.p("title") or self.node.id,
                            unit=own or self.p("unit") or "", digits=int(self.p("digits", 4)))


@node("view.led", title="LED",
      description="On for true, a number of 0.5 or more, or a text like on/yes/true; off otherwise.",
      inputs=[In("in", signals.ANY, optional=False)],
      params=[Param("title", "str", ""), Param("color", "str", "#7fd18b")], icon="info")
class LedNode(NodeRuntime):
    async def on_input(self, port: str, value: Any) -> None:
        number = _number(value)
        if number is not None:
            on = number >= 0.5
        else:
            data = plain(value)
            if isinstance(data, str):
                text = data.strip().lower()
                on = text in ("1", "on", "yes", "true", "high") or (_number(_text_number(text)) or 0) >= 0.5
            else:
                on = bool(data)
        self.ctx.views.show("led", self.node.id, on, title=self.p("title") or self.node.id, color=self.p("color"))


@node("view.log", title="Log", description="Every value that arrives as a line of text.",
      inputs=[In("in", signals.ANY, optional=False, multiple=True)],
      params=[Param("title", "str", ""), Param("lines", "int", 1000)], icon="list")
class LogNode(NodeRuntime):
    async def setup(self) -> None:
        self.lines: list[str] = []

    async def on_input(self, port: str, value: Any) -> None:
        from ..engine.runtime import summarize

        text = value if isinstance(value, str) else summarize(plain(value) if not isinstance(value, signals.Signal)
                                                              else value)
        self.lines.append(f"{self.ctx.now():.6f}  {text}")
        del self.lines[:-int(self.p("lines", 1000))]
        self.ctx.views.show("log", self.node.id, list(self.lines), title=self.p("title") or self.node.id)


NODES = collect(globals())
