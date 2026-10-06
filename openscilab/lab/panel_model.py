# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Panels: widgets on a grid, each bound to a port of a flow (``*.panel.yaml``).

Controls (switch, button, slider, input, choice) send values into the flow: to an input of a node
as if a wire brought them, or out of an output to the inputs wired to it. Displays (number, LED,
chart, scope, label) show the values of an output. The model knows nothing of Qt; the panel
document draws it (``ui/documents/panel.py``)::

    panel: Characteristic curve
    flow: ../flows/curve.flow.yaml
    columns: 6
    widgets:
      start: {kind: button, bind: sweep.next, title: Start, row: 0, column: 0}
      curve: {kind: chart, bind: measure.value, row: 1, column: 0, columns: 4, rows: 3}
      slope: {kind: number, bind: fit.slope, unit: Ω, row: 1, column: 4, tab: Results}
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Optional

from ..core import signals
from ..core.files import atomic_write
from .model import Flow, FlowError
from .yaml_io import safe_load as yaml_safe_load

#: kind -> (role, title); controls send values, displays show them
WIDGET_KINDS: dict[str, tuple[str, str]] = {
    "switch": ("control", "Switch"),
    "button": ("control", "Push button"),
    "slider": ("control", "Slider"),
    "input": ("control", "Input"),
    "choice": ("control", "Choice"),
    "number": ("display", "Number"),
    "led": ("display", "LED"),
    "chart": ("display", "Chart"),
    "scope": ("display", "Scope"),
    "label": ("static", "Label"),
}
CONTROL_KINDS = tuple(kind for kind, (role, _title) in WIDGET_KINDS.items() if role == "control")
DISPLAY_KINDS = tuple(kind for kind, (role, _title) in WIDGET_KINDS.items() if role == "display")
PANEL_SUFFIX = ".panel.yaml"

#: options each kind understands (beyond kind, bind, title, the grid place, tab and group)
OPTIONS = {
    "slider": ("min", "max", "step", "unit", "value"),
    "input": ("unit", "value"),
    "choice": ("options", "value"),
    "switch": ("value",),
    "number": ("unit", "digits"),
    "led": ("color", "threshold"),
    "chart": ("points", "unit"),
    "scope": (),
    "label": ("text",),
    "button": (),
}
_PLACE = ("row", "column", "rows", "columns")


class PanelError(ValueError):
    pass


@dataclass
class PanelWidget:
    id: str
    kind: str
    #: ``node.port`` of the flow (empty: not bound yet)
    bind: str = ""
    title: str = ""
    row: int = 0
    column: int = 0
    rows: int = 1
    columns: int = 1
    tab: str = ""
    group: str = ""
    options: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in WIDGET_KINDS:
            raise PanelError(f"{self.id}: unknown widget kind {self.kind!r} (kinds: {', '.join(WIDGET_KINDS)})")

    @property
    def role(self) -> str:
        return WIDGET_KINDS[self.kind][0]

    @property
    def node(self) -> str:
        return self.bind.partition(".")[0]

    @property
    def port(self) -> str:
        return self.bind.partition(".")[2]

    @property
    def label(self) -> str:
        return self.title or self.id

    def option(self, name: str, default: Any = None) -> Any:
        value = self.options.get(name)
        return default if value is None else value

    def to_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {"kind": self.kind}
        if self.bind:
            data["bind"] = self.bind
        if self.title:
            data["title"] = self.title
        for name in _PLACE:
            value = getattr(self, name)
            if value != (1 if name in ("rows", "columns") else 0):
                data[name] = value
        if self.tab:
            data["tab"] = self.tab
        if self.group:
            data["group"] = self.group
        data.update(self.options)
        return data

    @staticmethod
    def from_data(widget_id: str, data: dict[str, Any]) -> "PanelWidget":
        data = dict(data)
        kind = str(data.pop("kind", ""))
        values = {name: data.pop(name) for name in ("bind", "title", "tab", "group") if name in data}
        place = {name: int(data.pop(name)) for name in _PLACE if name in data}
        unknown = [name for name in data if name not in OPTIONS.get(kind, ())]
        if kind in OPTIONS and unknown:
            raise PanelError(f"{widget_id}: unknown setting {', '.join(unknown)} for a {kind}")
        return PanelWidget(widget_id, kind, **{key: str(value) for key, value in values.items()}, **place,
                           options=data)


@dataclass
class Panel:
    name: str = "Panel"
    #: the flow file, relative to the panel file
    flow: str = ""
    columns: int = 6
    widgets: list[PanelWidget] = field(default_factory=list)
    #: tab names in order ("" is the first, unnamed tab)
    tabs: list[str] = field(default_factory=list)

    def widget(self, widget_id: str) -> PanelWidget:
        for widget in self.widgets:
            if widget.id == widget_id:
                return widget
        raise PanelError(f"no widget {widget_id!r}")

    def unique_id(self, base: str) -> str:
        names = {widget.id for widget in self.widgets}
        if base not in names:
            return base
        number = 2
        while f"{base}{number}" in names:
            number += 1
        return f"{base}{number}"

    def add(self, kind: str, bind: str = "", **settings) -> PanelWidget:
        widget = PanelWidget(self.unique_id(settings.pop("id", kind)), kind, bind, **settings)
        if "row" not in settings:
            widget.row = self.free_row(widget.tab)
        self.widgets.append(widget)
        if widget.tab and widget.tab not in self.tabs:
            self.tabs.append(widget.tab)
        return widget

    def remove(self, widget_id: str) -> PanelWidget:
        widget = self.widget(widget_id)
        self.widgets.remove(widget)
        return widget

    def free_row(self, tab: str = "") -> int:
        return max((widget.row + widget.rows for widget in self.widgets if widget.tab == tab), default=0)

    def tab_names(self) -> list[str]:
        names = list(self.tabs)
        for widget in self.widgets:
            if widget.tab not in names:
                names.append(widget.tab)
        if "" in names:
            names.remove("")
            names.insert(0, "")
        return names or [""]

    def bindings(self) -> list[str]:
        """``node.port`` of the controls (they count as wired inputs while the panel runs the flow)."""
        return [widget.bind for widget in self.widgets if widget.role == "control" and widget.bind]

    def problems(self, flow: Optional[Flow], registry=None) -> list[str]:
        """Widgets bound to nodes or ports the flow does not have, overlapping places."""
        found = []
        if flow is not None:
            for widget in self.widgets:
                if widget.role == "static" or not widget.bind:
                    continue
                node = flow.nodes.get(widget.node)
                if node is None:
                    found.append(f"{widget.id}: the flow has no node {widget.node!r}")
                    continue
                try:
                    inputs, outputs = flow.spec(node, registry).resolve_ports(node.params)
                except (FlowError, KeyError, ValueError):
                    continue
                names_in = {port.name for port in inputs}
                names_out = {port.name for port in outputs}
                if widget.role == "display" and widget.port not in names_out:
                    found.append(f"{widget.id}: {widget.bind} is no output (displays show outputs)")
                elif widget.role == "control" and widget.port not in names_in | names_out:
                    found.append(f"{widget.id}: {widget.node} has no port {widget.port!r}")
        taken: dict[tuple[str, int, int], str] = {}
        for widget in self.widgets:
            for row in range(widget.row, widget.row + widget.rows):
                for column in range(widget.column, widget.column + widget.columns):
                    key = (widget.tab, row, column)
                    if key in taken:
                        found.append(f"{widget.id} overlaps {taken[key]}")
                        break
                    taken[key] = widget.id
                else:
                    continue
                break
        return found

    # ------------------------------------------------------------- storage
    def to_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {"panel": self.name}
        if self.flow:
            data["flow"] = self.flow
        if self.columns != 6:
            data["columns"] = self.columns
        if self.tabs:
            data["tabs"] = list(self.tabs)
        data["widgets"] = {widget.id: widget.to_data() for widget in self.widgets}
        return data

    @staticmethod
    def from_data(data: dict[str, Any]) -> "Panel":
        if not isinstance(data, dict):
            raise PanelError("a panel file holds a mapping")
        panel = Panel(name=str(data.get("panel", "Panel")), flow=str(data.get("flow", "") or ""),
                      columns=int(data.get("columns", 6)), tabs=[str(tab) for tab in data.get("tabs", [])])
        for widget_id, widget in (data.get("widgets") or {}).items():
            panel.widgets.append(PanelWidget.from_data(str(widget_id), widget or {}))
        return panel


def save(panel: Panel, path: str) -> None:
    atomic_write(path, dumps(panel))


def dumps(panel: Panel) -> str:
    import yaml

    return yaml.safe_dump(panel.to_data(), sort_keys=False, allow_unicode=True, default_flow_style=None)


def loads(text: str) -> Panel:
    import yaml

    try:
        data = yaml_safe_load(text) or {}
    except yaml.YAMLError as error:
        raise PanelError(str(error)) from None
    return Panel.from_data(data)


def load(path: str) -> Panel:
    with open(path, encoding="utf-8") as handle:
        return loads(handle.read())


def flow_path(panel: Panel, panel_path: Optional[str]) -> Optional[str]:
    """The flow file of ``panel`` (relative to the panel file)."""
    if not panel.flow:
        return None
    if os.path.isabs(panel.flow) or not panel_path:
        return panel.flow
    return os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(panel_path)), panel.flow))


# -------------------------------------------------------------------- values
def control_value(widget: PanelWidget, value: Any, at: float = 0.0) -> Any:
    """What a control sends into the flow for ``value`` (the state of the control)."""
    if widget.kind == "switch":
        return signals.Bool(name=widget.label, value=bool(value), at=at)
    if widget.kind == "button":
        return signals.Event(name=widget.label, times=[at], data=[widget.label])
    if widget.kind in ("slider", "input"):
        try:
            return signals.Scalar(name=widget.label, unit=str(widget.option("unit", "")), value=float(value), at=at)
        except (TypeError, ValueError):
            return value
    return value


def display_number(value: Any) -> Optional[float]:
    """The number a display shows for a value of the flow (``None`` when it is none)."""
    if isinstance(value, (signals.Scalar, signals.Bool)):
        return float(value.value)
    if isinstance(value, signals.Event):
        data = [item for item in value.data if isinstance(item, (int, float))]
        return float(data[-1]) if data else None
    if isinstance(value, (signals.Analog, signals.Digital)):
        # a block of samples: its last one (what the signal is now)
        return float(value.values[-1]) if len(value.values) else None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        import numpy as np

        if isinstance(value, np.generic):
            return float(value)
    except ImportError:  # pragma: no cover
        pass
    return None


# ------------------------------------------------------------- suggestions
#: controls a new panel offers for inputs nobody wires: node type -> (input, kind, options from params)
_CONTROLS = {
    "control.sweep": ("trigger", "button", lambda params: {}),
    "gpio.pwm": ("duty", "slider", lambda params: {"min": 0, "max": 1, "step": 0.05,
                                                   "value": params.get("duty", 0.5)}),
    "gpio.write": ("value", "switch", lambda params: {"value": bool(params.get("level", 1))}),
    "gpio.dac": ("volts", "slider", lambda params: {"min": 0, "max": 5, "step": 0.1, "unit": "V"}),
}
#: view nodes show what is wired to them; the widget that shows the same
_VIEWS = {"view.scope": "scope", "view.strip_chart": "chart", "view.number": "number", "view.led": "led"}
#: at most this many widgets in a suggested panel (more are added by hand)
SUGGESTED_WIDGETS = 12


def suggest(flow: Flow, registry=None, columns: int = 4) -> list[PanelWidget]:
    """Widgets for a new panel of ``flow``: a control for each input a person sets (a start button
    of a sweep, the duty cycle of a PWM, an input that must have a value and has no wire), a display for each result (measurements, checks, what
    view nodes show, captures nobody views). Placed on a grid of ``columns``."""
    controls: list[tuple[str, str, str, dict]] = []
    small: list[tuple[str, str, str, dict]] = []
    large: list[tuple[str, str, str, dict]] = []
    seen: set[str] = set()

    def add(target: list, kind: str, bind: str, title: str, options: Optional[dict] = None) -> None:
        if bind not in seen:
            seen.add(bind)
            target.append((kind, bind, title, options or {}))

    viewed = set()
    for node_id, node in flow.nodes.items():
        if node.type in _VIEWS:
            for edge in flow.edges_into(node_id):
                viewed.add(str(edge.source))
    for node_id, node in flow.nodes.items():
        try:
            inputs, outputs = flow.spec(node, registry).resolve_ports(node.params)
        except (FlowError, KeyError, ValueError):
            continue
        control = _CONTROLS.get(node.type)
        if control is not None and not flow.edges_into(node_id, control[0]):
            title = "Start" if control[1] == "button" else f"{node_id} {control[0]}"
            add(controls, control[1], f"{node_id}.{control[0]}", title, control[2](node.params))
        for port in inputs:
            # an input that must have a value and has no wire: the panel gives it (else the flow cannot run)
            if not port.optional and port.type in (signals.SCALAR, signals.ANY) and not flow.edges_into(node_id, port.name):
                add(controls, "input", f"{node_id}.{port.name}", f"{node_id} {port.name}", {"value": 0})
        if node.type in _VIEWS:
            for edge in flow.edges_into(node_id):
                kind = _VIEWS[node.type]
                add(large if kind in ("scope", "chart") else small, kind, str(edge.source),
                    str(node.params.get("title") or node_id))
            continue
        group = node.type.split(".", 1)[0]
        for port in outputs:
            bind = f"{node_id}.{port.name}"
            if port.type == signals.BOOL:
                add(small, "led", bind, f"{node_id} {port.name}" if port.name not in ("pass", "result") else node_id)
            elif port.type in (signals.SCALAR, signals.ANY) and group in ("measure", "control", "dsp") \
                    and node.type != "control.sweep":
                add(small, "number", bind, node_id if port.name in ("out", "value") else f"{node_id} {port.name}",
                    {"unit": str(node.params.get("unit"))} if node.params.get("unit") else None)
            elif port.type == signals.CAPTURE and group == "device" and bind not in viewed \
                    and not any(f"{node_id}.{other.name}" in viewed for other in outputs):
                add(large, "scope", bind, node_id)

    widgets: list[PanelWidget] = []
    names: set[str] = set()

    def place(kind: str, bind: str, title: str, options: dict, row: int, column: int, rows: int = 1,
              span: int = 1) -> None:
        base = "".join(char if char.isalnum() else "_" for char in bind.replace(".", "_"))
        name, number = base, 2
        while name in names:
            name, number = f"{base}{number}", number + 1
        names.add(name)
        widgets.append(PanelWidget(name, kind, bind, title, row=row, column=column, rows=rows, columns=span,
                                   options=options))

    row = 0
    for start in range(0, len(controls), columns):
        for column, item in enumerate(controls[start:start + columns]):
            place(*item, row=row, column=column)
        row += 1
    room = max(SUGGESTED_WIDGETS - len(widgets), 0)
    large = large[:min(len(large), max(room // 3, 1) if room else 0)]
    small = small[:room - len(large)]
    for start in range(0, len(small), columns):
        for column, item in enumerate(small[start:start + columns]):
            place(*item, row=row, column=column)
        row += 1
    span = max(columns // 2, 1)
    for index, item in enumerate(large):
        place(*item, row=row + (index // 2) * 3, column=(index % 2) * span, rows=3, span=span)
    return widgets
