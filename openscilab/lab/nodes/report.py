# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Report nodes: sections, tables, images and checks collected into an HTML (or PDF) report with
passed or failed. The parts go into the engine's view sink, so ``openscilab run --report`` and
``report.write`` produce the same report."""

from __future__ import annotations

import html
import math
import os
from typing import Any, Optional

import numpy as np

from ...core import signals, units
from ..engine.runtime import NodeError, NodeRuntime, summarize
from ..report import report_html, svg_plot, table_html, write_html
from .registry import In, Out, Param, node


def _show(ctx, node_id: str, render) -> None:
    """The part of ``node_id`` in the report: ``render()`` gives its HTML when the report is
    written (a node that gets a thousand values renders once, not a thousand times)."""
    ctx.views.show("report", node_id, None, html=render)


@node("report.section", title="Report section",
      description="A heading and text in the report; values at 'in' are added below it.",
      inputs=[In("in", signals.ANY, "values to list")],
      params=[Param("title", "str", "Results"), Param("text", "str", "")],
      icon="book")
class SectionNode(NodeRuntime):
    async def run(self) -> None:
        if not self.ctx.wired("in"):
            _show(self.ctx, self.node.id, self.section)

    async def setup(self) -> None:
        self.values: list[str] = []

    async def on_input(self, port: str, value: Any) -> None:
        self.values.append(summarize(value))
        _show(self.ctx, self.node.id, self.section)

    def section(self) -> str:
        text = str(self.p("text", ""))
        body = f"<p>{html.escape(text)}</p>" if text else ""
        if self.values:
            body += "<ul>" + "".join(f"<li>{html.escape(value)}</li>" for value in self.values) + "</ul>"
        return f"<h2>{html.escape(str(self.p('title', 'Results')))}</h2>{body}"


@node("report.table", title="Report table",
      description="The last table (or the values) arriving at 'in' as a table in the report.",
      inputs=[In("in", signals.ANY)],
      params=[Param("title", "str", "Table")],
      icon="list")
class TableNode(NodeRuntime):
    async def setup(self) -> None:
        self.table = None
        self.rows: list[Any] = []

    async def on_input(self, port: str, value: Any) -> None:
        if isinstance(value, signals.Table):
            self.table = value
        else:
            self.rows.append(value)
        _show(self.ctx, self.node.id, self.render)

    def render(self) -> str:
        if self.table is not None:
            columns = self.table.columns
        else:
            columns = {"#": list(range(1, len(self.rows) + 1)), "Value": [summarize(value) for value in self.rows]}
        return f"<h2>{html.escape(str(self.p('title', 'Table')))}</h2>" + table_html(columns)


@node("report.image", title="Report image",
      description="A diagram in the report: an XY chart of two table columns ('x', 'y') or of (x, y) "
                  "values, or the traces of a capture or signal.",
      inputs=[In("in", signals.ANY)],
      params=[Param("title", "str", "Diagram"), Param("x", "str", description="table column for x", suggest="upstream_columns"),
              Param("y", "str", description="table column(s) for y, comma separated",
                    suggest="upstream_columns"),
              Param("x_label", "str", ""), Param("y_label", "str", ""), Param("points", "bool", True)],
      icon="chart")
class ImageNode(NodeRuntime):
    async def setup(self) -> None:
        self.last: Any = None
        self.pairs: list[tuple[float, float]] = []

    async def on_input(self, port: str, value: Any) -> None:
        if isinstance(value, (list, tuple)) and len(value) == 2 and not isinstance(value[0], (list, tuple)):
            self.pairs.append((float(_number(value[0])), float(_number(value[1]))))
        elif isinstance(value, dict) and {"x", "y"} <= set(value):
            self.pairs.append((float(_number(value["x"])), float(_number(value["y"]))))
        else:
            self.last = value
        if isinstance(self.last, signals.Table) and not self.pairs:
            self.series()  # a table without the columns asked for is an error now, not in the report
        _show(self.ctx, self.node.id, self.render)

    def series(self) -> list[tuple[str, list, list]]:
        if self.pairs:
            return [("", [x for x, _y in self.pairs], [y for _x, y in self.pairs])]
        value = self.last
        if isinstance(value, signals.Table):
            columns = value.columns
            x_name = self.p("x") or next(iter(columns), None)
            y_names = [name.strip() for name in str(self.p("y") or "").split(",") if name.strip()] or [
                name for name in columns if name != x_name]
            if x_name not in columns or any(name not in columns for name in y_names):
                raise NodeError(f"{self.node.id}: the table has the columns {', '.join(columns)}")
            series = []
            for name in y_names:
                # rows without a number in x or y (an empty cell, a text) are left out
                pairs = [(_maybe_number(x), _maybe_number(y)) for x, y in zip(columns[x_name], columns[name])]
                pairs = [(x, y) for x, y in pairs if x is not None and y is not None]
                series.append((name, [x for x, _y in pairs], [y for _x, y in pairs]))
            return series
        if isinstance(value, signals.Capture):
            times = np.arange(value.sample_count) / value.rate
            series = [(name, *envelope(times, np.asarray(levels, dtype=float) + 1.5 * index))
                      for index, (name, levels) in enumerate(value.digital.items())]
            series += [(name, *envelope(times, np.asarray(values, dtype=float)))
                       for name, values in value.analog.items()]
            return series
        if isinstance(value, (signals.Analog, signals.Digital)):
            return [(value.name, *envelope(np.asarray(value.times()), np.asarray(value.values, dtype=float)))]
        return []

    def render(self) -> str:
        series = self.series()
        if not series:
            return ""
        figure = svg_plot(series, x_label=str(self.p("x_label") or self.p("x") or ""),
                          y_label=str(self.p("y_label") or self.p("y") or ""), points=bool(self.p("points", True)))
        return f"<h2>{html.escape(str(self.p('title', 'Diagram')))}</h2>{figure}"


#: points of a trace in a diagram of the report (more are drawn as their envelope)
DIAGRAM_POINTS = 2000


def envelope(times: np.ndarray, values: np.ndarray, limit: int = DIAGRAM_POINTS) -> tuple[list, list]:
    """``times`` and ``values`` as lists for a diagram: at most about ``limit`` points, the
    lowest and the highest of every stretch (a pulse is never lost, as it would be by taking
    every n-th sample)."""
    count = min(len(times), len(values))
    if count <= limit:
        return list(times[:count]), list(values[:count])
    step = -(-count // (limit // 2))
    whole = count // step * step
    blocks = values[:whole].reshape(-1, step)
    low, high = blocks.min(axis=1), blocks.max(axis=1)
    starts = times[:whole:step]
    xs = np.repeat(starts, 2)
    ys = np.empty(len(xs), dtype=float)
    # rising within the stretch: low first, else high first
    rising = blocks.argmin(axis=1) <= blocks.argmax(axis=1)
    ys[0::2] = np.where(rising, low, high)
    ys[1::2] = np.where(rising, high, low)
    if whole < count:
        xs = np.concatenate((xs, times[whole:count]))
        ys = np.concatenate((ys, values[whole:count]))
    return xs.tolist(), ys.tolist()


def _number(value: Any) -> float:
    from .control import plain

    value = plain(value)  # (a scalar, a truth value, the data of a single event)
    if isinstance(value, list):
        raise TypeError("several values")
    return float(value)


def _maybe_number(value: Any) -> Optional[float]:
    """``value`` as a finite number, else ``None`` (an empty cell, a text, NaN)."""
    try:
        number = _number(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


@node("report.check", title="Check",
      description="Checks the values at 'in' against limits ('low', 'high') or an 'expected' value "
                  "with a 'tolerance'; 'pass' tells the result, the report lists it.",
      inputs=[In("in", signals.ANY)],
      outputs=[Out("pass", signals.BOOL)],
      params=[Param("name", "str", ""), Param("low", "float"), Param("high", "float"),
              Param("expected", "float"), Param("tolerance", "float", 0.0), Param("unit", "str", "")],
      icon="check")
class CheckNode(NodeRuntime):
    async def on_input(self, port: str, value: Any) -> None:
        try:
            number = _number(value)
        except (TypeError, ValueError):
            raise NodeError(f"{self.node.id}: not a number: {value!r}") from None
        unit = str(self.p("unit", "") or getattr(value, "unit", "") or "")
        low, high, expected = self.p("low"), self.p("high"), self.p("expected")
        passed = True
        limits = []
        if expected is not None:
            tolerance = float(self.p("tolerance", 0.0))
            passed = abs(number - float(expected)) <= tolerance
            limits.append(f"= {_quantity(float(expected), unit)} ± {_quantity(tolerance, unit)}")
        if low is not None:
            passed = passed and number >= float(low)
            limits.append(f"≥ {_quantity(float(low), unit)}")
        if high is not None:
            passed = passed and number <= float(high)
            limits.append(f"≤ {_quantity(float(high), unit)}")
        name = str(self.p("name") or self.node.id)
        text = f"{name}: {_quantity(number, unit)} " + (", ".join(limits) if limits else "(no limits)")
        self.ctx.views.show("check", self.node.id, passed, text=text, measured=number)
        self.ctx.emit("pass", signals.Bool(name=name, value=passed, at=self.ctx.now()))


def _quantity(value: float, unit: str) -> str:
    return units.format_quantity(value, unit, 5) if unit else f"{value:.6g}"


@node("report.write", title="Write report",
      description="Writes the report (sections, tables, images, checks with passed or failed) to 'path' "
                  "when the flow ends, or on every value at 'write'. HTML, or PDF for a .pdf path.",
      inputs=[In("write", signals.ANY)],
      outputs=[Out("written", signals.EVENT), Out("passed", signals.BOOL)],
      params=[Param("path", "path", "report.html"), Param("title", "str", "")],
      icon="export")
class WriteNode(NodeRuntime):
    finish_last = True

    async def on_input(self, port: str, value: Any) -> None:
        self.write()

    async def finish(self) -> None:
        if not self.ctx.wired("write"):
            self.write()

    def write(self) -> None:
        path = self.ctx.path(str(self.p("path", "report.html")))
        title = str(self.p("title") or self.ctx.engine.flow.name)
        error = self.ctx.engine.error
        failed = [f"<p class='fail'>The flow ended with an error: {html.escape(error)}</p>"] if error else None
        text = report_html(title, self.ctx.views, sections=failed, passed=False if error else None)
        checks = [bool(item[2]) for item in self.ctx.views.items if item[0] == "check"]
        if error:
            checks.append(False)
        try:
            if path.lower().endswith(".pdf"):
                from ...ui.report_pdf import write_pdf  # Qt renders the PDF

                write_pdf(text, path)
            else:
                write_html(path, text)
        except OSError as error:
            raise NodeError(f"{self.node.id}: {error}") from None
        self.ctx.log(f"report: {os.path.basename(path)}")
        self.ctx.emit("written", signals.Event(times=[self.ctx.now()], data=[path]))
        if checks:
            self.ctx.emit("passed", signals.Bool(name="report", value=all(checks), at=self.ctx.now()))


NODES = [cls.node_spec for cls in (SectionNode, TableNode, ImageNode, CheckNode, WriteNode)]
