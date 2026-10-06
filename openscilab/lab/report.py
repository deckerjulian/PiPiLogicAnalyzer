# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Reports of flow runs as HTML: the run itself, and the sections report nodes collect."""

from __future__ import annotations

import datetime
import html
import math
from typing import Any, Optional

from ..core.files import atomic_write
from .engine.runtime import summarize

STYLE = """
body { font-family: -apple-system, 'Segoe UI', Roboto, sans-serif; margin: 2em auto; max-width: 960px;
       color: #1f1f22; padding: 0 16px; }
h1 { font-size: 1.6em; margin-bottom: 0.2em; } h2 { font-size: 1.2em; margin-top: 1.6em; }
table { border-collapse: collapse; margin: 0.6em 0; } th, td { border: 1px solid #ccc; padding: 3px 8px; }
th { background: #f2f2f4; text-align: left; } .muted { color: #77777f; }
.pass { color: #1f8a3a; font-weight: 600; } .fail { color: #c62828; font-weight: 600; }
pre { background: #f6f6f8; padding: 8px; overflow-x: auto; }
img { max-width: 100%; }
"""


def verdict(passed: Optional[bool]) -> str:
    if passed is None:
        return "<span class='muted'>no checks</span>"
    return "<span class='pass'>PASSED</span>" if passed else "<span class='fail'>FAILED</span>"


def table_html(columns: dict[str, list]) -> str:
    names = list(columns)
    rows = zip(*columns.values()) if names else []
    head = "".join(f"<th>{html.escape(str(name))}</th>" for name in names)
    body = "".join("<tr>" + "".join(f"<td>{html.escape(_cell(value))}</td>" for value in row) + "</tr>" for row in rows)
    return f"<table><tr>{head}</tr>{body}</table>"


def _cell(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def document(title: str, sections: list[str], passed: Optional[bool] = None) -> str:
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{html.escape(title)}</title><style>{STYLE}</style></head><body>"
        f"<h1>{html.escape(title)}</h1><p class='muted'>openSciLab report · {stamp} · {verdict(passed)}</p>"
        + "".join(sections) + "</body></html>"
    )


def run_section(flow, result) -> str:
    rows = {"Node": [], "State": []}
    for node, state in result.node_states.items():
        rows["Node"].append(node)
        rows["State"].append(state)
    values = {"Output": [f"{node}.{port}" for node, port in sorted(result.values)],
              "Last value": [summarize(result.values[key]) for key in sorted(result.values)]}
    error = f"<p class='fail'>{html.escape(result.error)}</p>" if result.error else ""
    log = "\n".join(line.rstrip() for line in result.log)
    return (
        f"<h2>Run</h2><p>{html.escape(flow.name)}: {html.escape(result.state)} after {result.time:.6g} s</p>{error}"
        + table_html(rows) + "<h2>Values</h2>" + table_html(values)
        + (f"<h2>Log</h2><pre>{html.escape(log)}</pre>" if log else "")
    )


def report_html(title: str, views=None, result=None, flow=None, sections: Optional[list[str]] = None,
                passed: Optional[bool] = None) -> str:
    """The report: the sections the report nodes collected (in the views), the checks with their
    verdict, and the run when it is given."""
    items = list(getattr(views, "items", []) if views is not None else [])
    body = list(sections or [])
    # a report node may show its part again with more data: the last one counts, in the first place
    parts: dict[str, str] = {}
    for kind, node, _value, options in items:
        if kind == "report" and options.get("html"):
            # a text, or a function that renders it now (once, not on every value of the run)
            text = options["html"]
            parts[node] = text() if callable(text) else text
    body += list(parts.values())
    checks = [item for item in items if item[0] == "check"]
    if checks:
        body.append(checks_section(checks))
    if passed is None and checks:
        passed = all(bool(item[2]) for item in checks)
    if passed is None and result is not None and not result.ok:
        passed = False
    if result is not None and flow is not None:
        body.append(run_section(flow, result))
    return document(title, body, passed)


def checks_section(checks: list) -> str:
    rows = {"Check": [], "Result": []}
    for _kind, node, passed, options in checks:
        rows["Check"].append(options.get("text") or node)
        rows["Result"].append("PASSED" if passed else "FAILED")
    table = table_html(rows)
    table = table.replace("<td>PASSED</td>", "<td class='pass'>PASSED</td>").replace(
        "<td>FAILED</td>", "<td class='fail'>FAILED</td>")
    return "<h2>Checks</h2>" + table


def write_html(path: str, text: str) -> None:
    atomic_write(path, text)


def write_run_report(path: str, flow, result, views=None, sections: Optional[list[str]] = None,
                     passed: Optional[bool] = None) -> str:
    """Write the report of a run; ``sections`` are HTML parts collected by report nodes."""
    text = report_html(f"{flow.name}", views, result, flow, sections, passed)
    write_html(path, text)
    return text


# --------------------------------------------------------------------- plots
PLOT_COLORS = ("#3b7ddd", "#d0782a", "#2e9d5b", "#b0479a", "#7a6cc4", "#c14848")


def _finite(xs: list, ys: list) -> tuple[list[float], list[float]]:
    """The points of a trace that are finite numbers (NaN of a failed measurement is left out)."""
    pairs = [(float(x), float(y)) for x, y in zip(xs, ys)]
    pairs = [(x, y) for x, y in pairs if math.isfinite(x) and math.isfinite(y)]
    return [x for x, _y in pairs], [y for _x, y in pairs]


def svg_plot(series: list[tuple[str, list, list]], width: int = 640, height: int = 260, x_label: str = "",
             y_label: str = "", points: bool = False) -> str:
    """Lines of ``(name, xs, ys)`` as an inline SVG (no plotting library needed)."""
    series = [(name, *_finite(xs, ys)) for name, xs, ys in series]
    xs_all = [x for _name, xs, _ys in series for x in xs]
    ys_all = [y for _name, _xs, ys in series for y in ys]
    if not xs_all:
        return "<p class='muted'>no data</p>"
    x0, x1 = min(xs_all), max(xs_all)
    y0, y1 = min(ys_all), max(ys_all)
    if x1 - x0 < 1e-300:
        x0, x1 = x0 - 0.5, x1 + 0.5
    if y1 - y0 < 1e-300:
        y0, y1 = y0 - 0.5, y1 + 0.5
    left, right, top, bottom = 56, 12, 12, 34
    plot_width, plot_height = width - left - right, height - top - bottom

    def place(x: float, y: float) -> tuple[float, float]:
        return (left + (x - x0) / (x1 - x0) * plot_width, top + plot_height - (y - y0) / (y1 - y0) * plot_height)

    parts = [(f"<svg xmlns='http://www.w3.org/2000/svg' width='{width}' height='{height}' "
              f"viewBox='0 0 {width} {height}' font-family='sans-serif' font-size='11'>"),
             f"<rect x='{left}' y='{top}' width='{plot_width}' height='{plot_height}' fill='none' stroke='#bbb'/>"]
    for value, anchor_y in ((y1, top + 4), (y0, top + plot_height)):
        parts.append(f"<text x='{left - 4}' y='{anchor_y}' text-anchor='end'>{value:.4g}</text>")
    for value, anchor in ((x0, "start"), (x1, "end")):
        x = left if anchor == "start" else left + plot_width
        parts.append(f"<text x='{x}' y='{height - 18}' text-anchor='{anchor}'>{value:.4g}</text>")
    if x_label:
        parts.append(f"<text x='{left + plot_width / 2}' y='{height - 4}' text-anchor='middle'>"
                     f"{html.escape(x_label)}</text>")
    if y_label:
        parts.append(f"<text x='12' y='{top + plot_height / 2}' text-anchor='middle' "
                     f"transform='rotate(-90 12 {top + plot_height / 2})'>{html.escape(y_label)}</text>")
    for index, (name, xs, ys) in enumerate(series):
        color = PLOT_COLORS[index % len(PLOT_COLORS)]
        coordinates = [place(float(x), float(y)) for x, y in zip(xs, ys)]
        if len(coordinates) > 4000:  # keep the file small: every n-th point
            step = len(coordinates) // 4000 + 1
            coordinates = coordinates[::step]
        path = " ".join(f"{x:.1f},{y:.1f}" for x, y in coordinates)
        parts.append(f"<polyline fill='none' stroke='{color}' stroke-width='1.5' points='{path}'/>")
        if points:
            parts += [f"<circle cx='{x:.1f}' cy='{y:.1f}' r='2.5' fill='{color}'/>" for x, y in coordinates]
        if name:
            parts.append(f"<text x='{left + 8}' y='{top + 14 + index * 14}' fill='{color}'>{html.escape(name)}</text>")
    parts.append("</svg>")
    return "".join(parts)
