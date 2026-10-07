# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Panels: widgets placed freely on a surface, each bound to a port of a flow (``*.panel.yaml``).

Controls (switch, button, slider, input, choice) send values into the flow: to an input of a node
as if a wire brought them, or out of an output to the inputs wired to it. Displays (number, LED,
chart, scope, label) show the values of an output. The model knows nothing of Qt; the panel
document draws it (``ui/documents/panel.py``)::

    panel: Characteristic curve
    flow: ../flows/curve.flow.yaml
    width: 960
    height: 600
    widgets:
      start: {kind: button, bind: sweep.next, title: Start, x: 16, y: 16, width: 160, height: 64}
      curve: {kind: chart, bind: measure.value, x: 16, y: 96, width: 464, height: 240}
      slope: {kind: number, bind: fit.slope, unit: Ω, x: 496, y: 96, width: 160, height: 80, tab: Results}

Places and sizes are pixels of the panel at its size (``width`` × ``height``). The editor keeps
them on a raster of :data:`GRID` pixels and lines them up with each other (:func:`snap`,
:func:`align`, :func:`distribute`); a panel that is operated grows or shrinks with its window as a
whole.
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
_PLACE = ("x", "y", "width", "height")
#: the raster of places and sizes in the editor (pixels)
GRID = 8
#: the space around the widgets and between them where a panel places them itself (pixels)
MARGIN = 16
#: the size of a new panel (pixels)
PANEL_SIZE = (960, 600)
#: the size of a new widget of each kind (pixels)
WIDGET_SIZES = {
    "switch": (160, 72), "button": (160, 64), "slider": (240, 72), "input": (272, 72), "choice": (200, 72),
    "number": (160, 80), "led": (120, 80), "chart": (464, 240), "scope": (464, 240), "label": (240, 40),
}
#: the smallest a widget can be (pixels)
MIN_SIZE = (40, 24)
#: a place: (x, y, width, height)
Rect = tuple[int, int, int, int]


class PanelError(ValueError):
    pass


@dataclass
class PanelWidget:
    id: str
    kind: str
    #: ``node.port`` of the flow (empty: not bound yet)
    bind: str = ""
    title: str = ""
    #: the place on the panel (pixels from its top left corner); ``width``/``height`` 0: the size of
    #: its kind (:data:`WIDGET_SIZES`)
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0
    tab: str = ""
    group: str = ""
    options: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in WIDGET_KINDS:
            raise PanelError(f"{self.id}: unknown widget kind {self.kind!r} (kinds: {', '.join(WIDGET_KINDS)})")
        width, height = WIDGET_SIZES[self.kind]
        self.width = max(int(self.width or width), MIN_SIZE[0])
        self.height = max(int(self.height or height), MIN_SIZE[1])

    @property
    def rect(self) -> Rect:
        return self.x, self.y, self.width, self.height

    @rect.setter
    def rect(self, rect: Rect) -> None:
        self.x, self.y, self.width, self.height = (int(round(value)) for value in rect)

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
            data[name] = getattr(self, name)
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
    #: the size of the panel (pixels): its widgets lie within it
    width: int = PANEL_SIZE[0]
    height: int = PANEL_SIZE[1]
    #: in the order they are drawn: a later one lies on an earlier one
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
        """A new widget; without ``x`` and ``y`` on the first free place of its tab (the panel grows
        when there is none)."""
        widget = PanelWidget(self.unique_id(settings.pop("id", kind)), kind, bind, **settings)
        if "x" not in settings and "y" not in settings:
            widget.x, widget.y = self.free_place(widget.width, widget.height, widget.tab)
        self.widgets.append(widget)
        self.grow_to_fit()
        if widget.tab and widget.tab not in self.tabs:
            self.tabs.append(widget.tab)
        return widget

    def remove(self, widget_id: str) -> PanelWidget:
        widget = self.widget(widget_id)
        self.widgets.remove(widget)
        return widget

    def free_place(self, width: int, height: int, tab: str = "") -> tuple[int, int]:
        """The first place (top to bottom, left to right) where a widget of this size touches no
        other one of ``tab`` (:data:`MARGIN` apart) - below them all when the panel has none."""
        others = [widget.rect for widget in self.widgets if widget.tab == tab]
        xs = sorted({MARGIN, *(x + w + MARGIN for x, _y, w, _h in others)})
        ys = sorted({MARGIN, *(y + h + MARGIN for _x, y, _w, h in others)})
        for y in ys:
            for x in xs:
                if x + width + MARGIN > self.width:
                    break
                if not any(_overlap((x, y, width, height), other, MARGIN) for other in others):
                    return x, y
        return MARGIN, max(ys)

    def grow_to_fit(self) -> None:
        """Make the panel as large as its widgets need."""
        for widget in self.widgets:
            self.width = max(self.width, widget.x + widget.width + MARGIN)
            self.height = max(self.height, widget.y + widget.height + MARGIN)

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
        for widget in self.widgets:
            if widget.x < 0 or widget.y < 0 or widget.x + widget.width > self.width \
                    or widget.y + widget.height > self.height:
                found.append(f"{widget.id} lies outside the panel ({self.width} × {self.height})")
        return found

    # ------------------------------------------------------------- storage
    def to_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {"panel": self.name}
        if self.flow:
            data["flow"] = self.flow
        data["width"], data["height"] = self.width, self.height
        if self.tabs:
            data["tabs"] = list(self.tabs)
        data["widgets"] = {widget.id: widget.to_data() for widget in self.widgets}
        return data

    @staticmethod
    def from_data(data: dict[str, Any]) -> "Panel":
        if not isinstance(data, dict):
            raise PanelError("a panel file holds a mapping")
        unknown = [name for name in data if name not in ("panel", "flow", "width", "height", "tabs", "widgets")]
        if unknown:
            raise PanelError(f"unknown setting {', '.join(map(str, unknown))} of a panel")
        panel = Panel(name=str(data.get("panel", "Panel")), flow=str(data.get("flow", "") or ""),
                      width=int(data.get("width", PANEL_SIZE[0])), height=int(data.get("height", PANEL_SIZE[1])),
                      tabs=[str(tab) for tab in data.get("tabs", [])])
        for widget_id, widget in (data.get("widgets") or {}).items():
            panel.widgets.append(PanelWidget.from_data(str(widget_id), widget or {}))
        return panel


def save(panel: Panel, path: str) -> None:
    atomic_write(path, dumps(panel))


def dumps(panel: Panel) -> str:
    from ..core import yaml_text

    return yaml_text.dump(panel.to_data(), default_flow_style=None, width=120)


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


# -------------------------------------------------------------------- layout
#: a guide the editor draws while a widget snaps: ("x", x, top, bottom) - a vertical line - or
#: ("y", y, left, right)
Guide = tuple[str, float, float, float]


def _overlap(first: Rect, second: Rect, gap: int = 0) -> bool:
    """Whether the rectangles come closer than ``gap``."""
    x1, y1, w1, h1 = first
    x2, y2, w2, h2 = second
    return x1 < x2 + w2 + gap and x2 < x1 + w1 + gap and y1 < y2 + h2 + gap and y2 < y1 + h1 + gap


def _anchors(start: float, size: float) -> tuple[float, float, float]:
    return start, start + size / 2, start + size


def snap(rect: Rect, others: list[Rect], bounds: tuple[int, int], sides: str = "move", threshold: float = 6,
         grid: int = GRID) -> tuple[Rect, list[Guide]]:
    """Where a widget moved or resized with the mouse lands, and the guides that show why.

    ``sides``: ``"move"`` (the whole widget), else the edges a resize moves (``"left top"``,
    ``"right"``, ...). An edge - or, when moving, also the middle - that comes within ``threshold``
    of an edge or the middle of another widget or of the panel (``bounds``: its width and height)
    lines up with it; otherwise it lands on the raster of ``grid``. The widget stays within the
    panel and at least :data:`MIN_SIZE`. ``grid`` 0 and ``threshold`` 0: where the mouse puts it.
    """
    x, y, width, height = (float(value) for value in rect)
    panel_width, panel_height = bounds
    targets_x = [value for ox, _oy, ow, _oh in others for value in _anchors(ox, ow)] + [0, panel_width / 2, panel_width]
    targets_y = [value for _ox, oy, _ow, oh in others for value in _anchors(oy, oh)] + [0, panel_height / 2,
                                                                                      panel_height]

    def nearest(anchors: list[float], targets: list[float]) -> Optional[float]:
        """The shift that puts one of ``anchors`` on one of ``targets`` (the smallest), ``None``."""
        best: Optional[float] = None
        for anchor in anchors:
            for target in targets:
                shift = target - anchor
                if abs(shift) <= threshold and (best is None or abs(shift) < abs(best)):
                    best = shift
        return best

    def on_grid(value: float) -> float:
        return round(value / grid) * grid if grid else value

    if sides == "move":
        shift = nearest(list(_anchors(x, width)), targets_x)
        x = x + shift if shift is not None else on_grid(x)
        shift = nearest(list(_anchors(y, height)), targets_y)
        y = y + shift if shift is not None else on_grid(y)
        x = min(max(x, 0), max(panel_width - width, 0))
        y = min(max(y, 0), max(panel_height - height, 0))
    else:
        left, top, right, bottom = x, y, x + width, y + height
        if "left" in sides:
            shift = nearest([left], targets_x)
            left = min(max(left + shift if shift is not None else on_grid(left), 0), right - MIN_SIZE[0])
        if "right" in sides:
            shift = nearest([right], targets_x)
            right = max(min(right + shift if shift is not None else on_grid(right), panel_width), left + MIN_SIZE[0])
        if "top" in sides:
            shift = nearest([top], targets_y)
            top = min(max(top + shift if shift is not None else on_grid(top), 0), bottom - MIN_SIZE[1])
        if "bottom" in sides:
            shift = nearest([bottom], targets_y)
            bottom = max(min(bottom + shift if shift is not None else on_grid(bottom), panel_height),
                         top + MIN_SIZE[1])
        x, y, width, height = left, top, right - left, bottom - top
    result = (int(round(x)), int(round(y)), int(round(width)), int(round(height)))
    return result, guides_for(result, others, bounds, sides)


def guides_for(rect: Rect, others: list[Rect], bounds: tuple[int, int], sides: str = "move") -> list[Guide]:
    """The lines on which ``rect`` meets an edge or the middle of the others or of the panel: a line
    over everything that lies on it."""
    x, y, width, height = rect
    panel = (0, 0, bounds[0], bounds[1])
    moving_x = _anchors(x, width) if sides == "move" else [value for side, value in (("left", x), ("right", x + width))
                                                            if side in sides]
    moving_y = _anchors(y, height) if sides == "move" else [value for side, value in (("top", y), ("bottom", y + height))
                                                             if side in sides]
    guides: list[Guide] = []
    for axis, anchors in (("x", moving_x), ("y", moving_y)):
        for anchor in anchors:
            touching = [other for other in [*others, panel]
                        if any(abs(anchor - value) < 0.5 for value in
                               (_anchors(other[0], other[2]) if axis == "x" else _anchors(other[1], other[3])))]
            if not touching:
                continue
            if axis == "x":
                low = min([y, *(other[1] for other in touching)])
                high = max([y + height, *(other[1] + other[3] for other in touching)])
            else:
                low = min([x, *(other[0] for other in touching)])
                high = max([x + width, *(other[0] + other[2] for other in touching)])
            guide = (axis, float(anchor), float(low), float(high))
            if guide not in guides:
                guides.append(guide)
    return guides


#: how :func:`align` lines widgets up
ALIGNMENTS = ("left", "center", "right", "top", "middle", "bottom")


def align(rects: dict[str, Rect], how: str) -> dict[str, Rect]:
    """The places of ``rects`` lined up on the left edge, the middle or the right edge of them all
    (``top``, ``middle``, ``bottom`` likewise)."""
    if how not in ALIGNMENTS:
        raise ValueError(f"no alignment {how!r} ({', '.join(ALIGNMENTS)})")
    left = min(x for x, _y, _w, _h in rects.values())
    right = max(x + w for x, _y, w, _h in rects.values())
    top = min(y for _x, y, _w, _h in rects.values())
    bottom = max(y + h for _x, y, _w, h in rects.values())
    result = {}
    for key, (x, y, width, height) in rects.items():
        if how == "left":
            x = left
        elif how == "center":
            x = round((left + right - width) / 2)
        elif how == "right":
            x = right - width
        elif how == "top":
            y = top
        elif how == "middle":
            y = round((top + bottom - height) / 2)
        else:
            y = bottom - height
        result[key] = (x, y, width, height)
    return result


def distribute(rects: dict[str, Rect], axis: str) -> dict[str, Rect]:
    """The places of ``rects`` with the same space between neighbours, side by side (``axis`` ``"x"``)
    or one above the other (``"y"``); the first and the last stay where they are."""
    index = 0 if axis == "x" else 1
    order = sorted(rects, key=lambda key: rects[key][index])
    if len(order) < 3:
        return dict(rects)
    first, last = rects[order[0]], rects[order[-1]]
    sizes = sum(rects[key][index + 2] for key in order)
    gap = (last[index] + last[index + 2] - first[index] - sizes) / (len(order) - 1)
    result, position = {}, float(first[index])
    for key in order:
        rect = list(rects[key])
        rect[index] = int(round(position))
        result[key] = tuple(rect)
        position += rects[key][index + 2] + gap
    return result  # type: ignore[return-value]


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


def suggest(flow: Flow, registry=None, width: int = PANEL_SIZE[0]) -> list[PanelWidget]:
    """Widgets for a new panel of ``flow``: a control for each input a person sets (a start button
    of a sweep, the duty cycle of a PWM, an input that must have a value and has no wire), a display
    for each result (measurements, checks, what view nodes show, captures nobody views). Placed in
    rows on a panel ``width`` pixels wide: the controls, the small displays, the charts and scopes."""
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
    cursor = {"x": MARGIN, "y": MARGIN, "row": 0}

    def place(kind: str, bind: str, title: str, options: dict) -> None:
        base = "".join(char if char.isalnum() else "_" for char in bind.replace(".", "_"))
        name, number = base, 2
        while name in names:
            name, number = f"{base}{number}", number + 1
        names.add(name)
        widget = PanelWidget(name, kind, bind, title, options=options)
        if cursor["x"] > MARGIN and cursor["x"] + widget.width + MARGIN > width:  # (the row is full)
            new_row()
        widget.x, widget.y = cursor["x"], cursor["y"]
        cursor["x"] += widget.width + MARGIN
        cursor["row"] = max(cursor["row"], widget.height)
        widgets.append(widget)

    def new_row() -> None:
        if cursor["row"]:
            cursor["y"] += cursor["row"] + MARGIN
        cursor["x"], cursor["row"] = MARGIN, 0

    room = max(SUGGESTED_WIDGETS - len(controls), 0)
    large = large[:min(len(large), max(room // 3, 1) if room else 0)]
    small = small[:room - len(large)]
    for group in (controls, small, large):
        for item in group:
            place(*item)
        new_row()
    return widgets
