# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""A node on the canvas: title, ports coloured by type, parameters, run state, breakpoint."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QGraphicsItem, QGraphicsObject, QStyleOptionGraphicsItem

from ...core import units
from ...lab.model import Node
from ...lab.nodes.registry import NodeSpec, PortSpec
from ...lab.nodes.structure import DECORATIONS
from ..theme import (
    ACCENT,
    BORDER,
    BORDER_STRONG,
    ERROR,
    PANEL,
    PANEL_LIGHT,
    SELECTION,
    TEXT,
    TEXT_MUTED,
    qcolor,
    token,
    type_color,
)
from . import preview

if TYPE_CHECKING:  # pragma: no cover
    from .canvas import FlowScene

NODE_WIDTH = 190
HEADER = 28
PORT_ROW = 20
PORT_RADIUS = 5
PARAM_ROW = 15
MAX_PARAM_ROWS = 4
GRID = 8
#: the corner that resizes a group or comment, the edge a group is grabbed by
GRIP = 14
GROUP_EDGE = 8


#: the colour of a node's kind (the first part of its type: ``device.stream`` -> device)
CATEGORY_COLORS = {
    "device": "#d4a23c", "gpio": "#e07b53", "gen": "#e0605f", "measure": "#3fb0a6", "dsp": "#47a3d6",
    "convert": "#8590e3", "decode": "#6bb352", "view": "#5b8def", "data": "#a07fe0", "report": "#d0679f",
    "control": "#b5b84a", "timing": "#e8a33d", "remote": "#4fb6e3", "structure": "#8a8a92",
}
#: the radius of a node's corners
RADIUS = 7.0

_PIXMAPS: dict = {}


def category_color(node_type: str) -> QColor:
    return QColor(CATEGORY_COLORS.get(node_type.split(".", 1)[0], "#8a8a92"))


def _icon_pixmap(name: str, color: str, size: int):
    """The icon ``name`` in ``color`` (drawn once)."""
    key = (name, color, size)
    if key not in _PIXMAPS:
        from ..icons import icon

        _PIXMAPS[key] = icon(name, color).pixmap(size, size)
    return _PIXMAPS[key]


def snap(value: float) -> float:
    return round(value / GRID) * GRID


def param_text(value) -> str:
    if isinstance(value, float):
        return units.format_quantity(value) if abs(value) >= 1e4 or (value and abs(value) < 1e-2) else f"{value:g}"
    if isinstance(value, (list, tuple)):
        text = ", ".join(str(item) for item in value)
        return f"[{text}]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{key}: {item}" for key, item in value.items()) + "}"
    text = str(value).replace("\n", " ⏎ ")
    return text


class PortItem(QGraphicsItem):
    """A port: a dot on the left (input) or right (output) edge of its node."""

    def __init__(self, node_item: "NodeItem", spec: PortSpec, is_input: bool, index: int) -> None:
        super().__init__(node_item)
        self.node_item = node_item
        self.spec = spec
        self.is_input = is_input
        self.index = index
        self.hovered = False
        #: while a wire is dragged: "ok" (it fits here), "no" (it does not), None (no wire)
        self.highlight: Optional[str] = None
        self.setAcceptHoverEvents(True)
        self.setCursor(Qt.CrossCursor)
        self.setToolTip(f"{spec.name}: {spec.type}" + (" (needs a wire)" if is_input and not spec.optional else "")
                        + (f"\n{spec.description}" if spec.description else "")
                        + "\nDrag to connect; right-click for more")
        x = 0 if is_input else NODE_WIDTH
        self.setPos(x, HEADER + 4 + PORT_ROW * index + PORT_ROW / 2)
        self.setZValue(2)

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def ref(self) -> str:
        return f"{self.node_item.node_id}.{self.spec.name}"

    def boundingRect(self) -> QRectF:  # noqa: N802 - Qt naming
        r = PORT_RADIUS + 6
        return QRectF(-r, -r, 2 * r, 2 * r)

    def set_highlight(self, highlight: Optional[str]) -> None:
        if highlight != self.highlight:
            self.highlight = highlight
            self.update()

    def shape(self) -> QPainterPath:
        path = QPainterPath()
        path.addEllipse(self.boundingRect())
        return path

    def scene_center(self) -> QPointF:
        return self.mapToScene(QPointF(0, 0))

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget=None) -> None:
        painter.setRenderHint(QPainter.Antialiasing)
        color = QColor(type_color(self.spec.type))
        if self.highlight == "ok":
            painter.setPen(QPen(color, 2))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(QPointF(0, 0), PORT_RADIUS + 4, PORT_RADIUS + 4)
        elif self.highlight == "no":
            color.setAlpha(70)
        connected = self.node_item.scene_ref is not None and \
            self.node_item.scene_ref.port_connected(self.ref, self.is_input)
        if self.is_input and not self.spec.optional and not connected and self.highlight is None:
            # a required input without a wire: the flow cannot run like this
            painter.setPen(QPen(QColor(ERROR), 1.5, Qt.DashLine))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(QPointF(0, 0), PORT_RADIUS + 3.5, PORT_RADIUS + 3.5)
        radius = PORT_RADIUS + (1.5 if self.hovered or self.highlight == "ok" else 0)
        # a ring in the colour of its type, filled when wired; a dark rim lifts it off the node's edge
        painter.setPen(QPen(QColor(PANEL), 3))
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(QPointF(0, 0), radius + 0.5, radius + 0.5)
        painter.setPen(QPen(color, 2))
        painter.setBrush(QBrush(color if connected or self.hovered else QColor(PANEL)))
        painter.drawEllipse(QPointF(0, 0), radius - 0.5, radius - 0.5)

    def hoverEnterEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self.hovered = True
        self.update()

    def hoverLeaveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self.hovered = False
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.LeftButton and self.node_item.scene_ref is not None:
            self.node_item.scene_ref.begin_wire(self)
            event.accept()
            return
        super().mousePressEvent(event)


class NodeItem(QGraphicsObject):
    """A node of the flow. Decorations (comment, group) draw as note and frame."""

    def __init__(self, node: Node, spec: Optional[NodeSpec], scene: "FlowScene") -> None:
        super().__init__()
        self.scene_ref = scene
        self.node_id = node.id
        self.node = node
        self.spec = spec
        self.state = "idle"
        self.state_message = ""
        #: problems of the flow at this node: ``(severity, text)``
        self.problems: list[tuple[str, str]] = []
        self.hovered = False
        self.error = ""
        self.breakpoint = False
        self.ports: dict[tuple[str, bool], PortItem] = {}
        #: the data of the run this node shows (:mod:`.preview`)
        self.data_entries: list = []
        self.reserved = 0.0
        self.setFlags(QGraphicsItem.ItemIsMovable | QGraphicsItem.ItemIsSelectable
                      | QGraphicsItem.ItemSendsGeometryChanges)
        self.setAcceptHoverEvents(True)
        self._drag_start: Optional[QPointF] = None
        self.update_from(node, spec)

    # ---------------------------------------------------------------- model
    @property
    def is_decoration(self) -> bool:
        return self.node.type in DECORATIONS

    def update_from(self, node: Node, spec: Optional[NodeSpec]) -> None:
        self.prepareGeometryChange()
        self.node = node
        self.node_id = node.id
        self.spec = spec
        inputs, outputs = spec.resolve_ports(node.params) if spec is not None and not self.is_decoration else ([], [])
        wanted = [(port, True, index) for index, port in enumerate(inputs)] + [
            (port, False, index) for index, port in enumerate(outputs)]
        # Every edit of the flow comes through here for every node: the ports are only built
        # again when they are other ports (a parameter that changes them, another node type).
        same = len(wanted) == len(self.ports) and all(
            (existing := self.ports.get((port.name, is_input))) is not None and existing.index == index
            and existing.spec == port for port, is_input, index in wanted)
        if not same:
            for port in list(self.ports.values()):
                port.setParentItem(None)
                if port.scene() is not None:
                    port.scene().removeItem(port)
            self.ports.clear()
            for port, is_input, index in wanted:
                self.ports[(port.name, is_input)] = PortItem(self, port, is_input, index)
        self.setZValue(-10 if node.type == "structure.group" else (5 if node.type == "structure.comment" else 0))
        self.reserved = preview.reserved_height(spec, self.is_decoration)
        tip = node.type if spec is None else f"{spec.title} ({node.type})\n{spec.description}"
        self.base_tooltip = tip + (f"\n\n{node.comment}" if node.comment else "")
        self.setToolTip(self.base_tooltip if not getattr(self, "error", "") else f"Error: {self.error}")
        self.update()

    def port(self, name: str, is_input: bool) -> Optional[PortItem]:
        return self.ports.get((name, is_input))

    def param_rows(self) -> list[str]:
        if self.spec is None:
            return [f"unknown type {self.node.type}"]
        rows = [f"{key}: {param_text(value)}" for key, value in self.node.params.items()]
        if len(rows) > MAX_PARAM_ROWS:
            rows = rows[:MAX_PARAM_ROWS - 1] + [f"… {len(rows) - MAX_PARAM_ROWS + 1} more"]
        return rows

    def _size(self) -> tuple[float, float]:
        if getattr(self, "_live_size", None) is not None:
            return self._live_size
        if self.is_decoration:
            size = self.node.params.get("size") or ([360, 240] if self.node.type == "structure.group" else [200, 80])
            try:
                return float(size[0]), float(size[1])
            except (TypeError, ValueError, IndexError):
                return 200.0, 80.0
        width, height = self._base_size()
        return width, height + self.data_height()

    def _base_size(self) -> tuple[float, float]:
        rows = max((sum(1 for key in self.ports if key[1]), sum(1 for key in self.ports if not key[1])), default=0)
        params = len(self.param_rows())
        return NODE_WIDTH, HEADER + 4 + rows * PORT_ROW + (params * PARAM_ROW + 8 if params else 0) + 6

    def layout_height(self) -> float:
        """The height to arrange the node with: with the room for its data, shown or not."""
        if self.is_decoration:
            return self._size()[1]
        return self._base_size()[1] + self.reserved

    def data_height(self) -> float:
        if self.is_decoration or not self.data_entries:
            return 0.0
        return preview.height_of(self.data_entries, self.reserved)

    def data_rect(self) -> QRectF:
        width, height = self._base_size()
        return QRectF(6, height - 4, width - 12, self.data_height() - 2)

    def set_data(self, entries: list) -> None:
        """The run data the node shows (``[]``: none)."""
        if not entries and not self.data_entries:
            return
        if bool(entries) != bool(self.data_entries):
            self.prepareGeometryChange()
        self.data_entries = list(entries)
        self.update()

    def boundingRect(self) -> QRectF:  # noqa: N802 - Qt naming
        width, height = self._size()
        return QRectF(-2, -2, width + 4, height + 4)

    @property
    def is_group(self) -> bool:
        return self.node.type == "structure.group"

    def grip_rect(self) -> QRectF:
        """The corner that resizes a group or comment."""
        width, height = self._size()
        return QRectF(width - GRIP, height - GRIP, GRIP, GRIP)

    def shape(self) -> QPainterPath:
        path = QPainterPath()
        width, height = self._size()
        if not self.is_group:
            path.addRect(QRectF(0, 0, width, height))
            return path
        # a group is grabbed by its title and its edge; clicks inside go to what is in it (or
        # start a selection rectangle on the canvas)
        path.addRect(QRectF(0, 0, width, HEADER))
        path.addRect(QRectF(0, 0, GROUP_EDGE, height))
        path.addRect(QRectF(width - GROUP_EDGE, 0, GROUP_EDGE, height))
        path.addRect(QRectF(0, height - GROUP_EDGE, width, GROUP_EDGE))
        path.addRect(self.grip_rect())
        return path

    # ---------------------------------------------------------------- state
    def set_state(self, state: str, message: str = "") -> None:
        self.state = state
        self.state_message = message
        self.error = message if state == "error" else ""
        base = getattr(self, "base_tooltip", "")
        self.setToolTip(f"Error: {message}" + (f"\n\n{base}" if base else "") if self.error else base)
        self.update()

    def set_problems(self, problems: list[tuple[str, str]]) -> None:
        if problems != self.problems:
            self.problems = list(problems)
            base = getattr(self, "base_tooltip", "")
            notes = "\n".join(f"{'Error' if severity == 'error' else 'Warning'}: {text}" for severity, text in problems)
            if not self.error:
                self.setToolTip(f"{notes}\n\n{base}" if notes else base)
            self.update()

    def hoverEnterEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self.hovered = True
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self.hovered = False
        self.update()
        super().hoverLeaveEvent(event)

    def set_breakpoint(self, enabled: bool) -> None:
        self.breakpoint = enabled
        self.update()

    def breakpoint_rect(self) -> QRectF:
        return QRectF(6, (HEADER - 12) / 2 + 1, 12, 12)

    # ---------------------------------------------------------------- paint
    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget=None) -> None:
        painter.setRenderHint(QPainter.Antialiasing)
        width, height = self._size()
        rect = QRectF(0, 0, width, height)
        selected = self.isSelected()
        if self.node.type == "structure.group":
            color = QColor(self.node.params.get("color") or ACCENT)
            color.setAlpha(28)
            painter.setBrush(color)
            border = QColor(color)
            border.setAlpha(140 if selected else 80)
            painter.setPen(QPen(border, 1.5, Qt.DashLine))
            painter.drawRoundedRect(rect, 8, 8)
            painter.setPen(QColor(TEXT_MUTED))
            font = QFont(painter.font())
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(QRectF(10, 4, width - 20, 20), Qt.AlignLeft | Qt.AlignVCenter,
                             str(self.node.params.get("title", "Group")))
            self._paint_grip(painter, border)
            return
        if self.node.type == "structure.comment":
            painter.setBrush(qcolor("comment.fill"))
            painter.setPen(QPen(qcolor("comment.border.selected" if selected else "comment.border"), 1.2))
            painter.drawRoundedRect(rect, 4, 4)
            painter.setPen(qcolor("comment.text"))
            painter.drawText(rect.adjusted(8, 6, -8, -6), Qt.TextWordWrap | Qt.AlignLeft | Qt.AlignTop,
                             str(self.node.params.get("text", "")))
            self._paint_grip(painter, qcolor("comment.border.selected"))
            return

        accent = category_color(self.node.type)
        frame = QColor(BORDER_STRONG)
        if self.error:
            frame = QColor(token("run.error"))
        elif self.state == "running":
            frame = QColor(token("run.running"))
        elif selected:
            frame = QColor(SELECTION)
        body = QPainterPath()
        body.addRoundedRect(rect, RADIUS, RADIUS)
        # a soft shadow under the node
        painter.setPen(Qt.NoPen)
        for spread, alpha in ((6, 14), (4, 22), (2, 34)):
            shade = QColor(0, 0, 0, alpha)
            painter.setBrush(shade)
            painter.drawRoundedRect(rect.adjusted(-spread / 2, spread / 2, spread / 2, spread), RADIUS + spread / 2,
                                    RADIUS + spread / 2)
        if selected or self.error or self.state == "running":
            glow = QColor(frame)
            glow.setAlpha(70)
            painter.setPen(QPen(glow, 5))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(rect, RADIUS, RADIUS)
        painter.fillPath(body, QColor(PANEL_LIGHT))
        # the title bar in the colour of the node's kind, a stripe of it on top
        painter.save()
        painter.setClipPath(body)
        tint = QColor(accent)
        tint.setAlpha(46)
        painter.fillRect(QRectF(0, 0, width, HEADER), QColor(PANEL))
        painter.fillRect(QRectF(0, 0, width, HEADER), tint)
        painter.fillRect(QRectF(0, 0, width, 3), accent)
        painter.restore()
        painter.setPen(QPen(QColor(BORDER), 1))
        painter.drawLine(QPointF(0.5, HEADER), QPointF(width - 0.5, HEADER))
        painter.setPen(QPen(frame, 1.5 if selected or self.error or self.state == "running" else 1))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), RADIUS, RADIUS)

        # breakpoint (where it goes is shown while the pointer is on the node), then the icon
        bp = self.breakpoint_rect()
        if self.breakpoint:
            painter.setBrush(QColor(token("run.breakpoint")))
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(bp)
        elif self.hovered or selected:
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor(BORDER_STRONG), 1))
            painter.drawEllipse(bp.adjusted(2, 2, -2, -2))
        left = 22.0
        if self.spec is not None and self.spec.icon:
            painter.drawPixmap(QPointF(left, (HEADER - 14) / 2 + 1), _icon_pixmap(self.spec.icon, accent.lighter(115).name(), 14))
            left += 19

        font = QFont(painter.font())
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(TEXT))
        title = self.node_id
        subtitle = self.spec.title if self.spec else self.node.type
        metrics = QFontMetrics(font)
        room = int(width - left - 22 - (18 if self.problems else 0))
        shown = metrics.elidedText(title, Qt.ElideRight, room)
        painter.drawText(QRectF(left, 1, room, HEADER), Qt.AlignLeft | Qt.AlignVCenter, shown)
        if self.spec is not None and subtitle and subtitle.lower() != title.lower():
            # what kind of node it is, after its name
            used = metrics.horizontalAdvance(shown) + 6
            if room - used > 30:
                light = QFont(font)
                light.setBold(False)
                light.setPointSizeF(max(light.pointSizeF() - 1, 7))
                painter.setFont(light)
                painter.setPen(QColor(TEXT_MUTED))
                painter.drawText(QRectF(left + used, 1, room - used, HEADER), Qt.AlignLeft | Qt.AlignVCenter,
                                 QFontMetrics(light).elidedText(subtitle, Qt.ElideRight, int(room - used)))
                painter.setFont(font)
                painter.setPen(QColor(TEXT))
        if self.problems:
            # what keeps the flow from running (or may go wrong), on the node itself
            severe = any(severity == "error" for severity, _text in self.problems)
            painter.setBrush(QColor(ERROR if severe else token("run.waiting")))
            painter.setPen(Qt.NoPen)
            badge = QRectF(width - 36, (HEADER - 12) / 2 + 1, 12, 12)
            painter.drawEllipse(badge)
            painter.setPen(QColor(PANEL))
            painter.drawText(badge, Qt.AlignCenter, "!")
        # state badge
        if self.state not in ("idle",):
            state_token = {"waiting": "run.waiting", "running": "run.running", "done": "run.done",
                           "error": "run.error"}.get(self.state, "run.idle")
            painter.setBrush(QColor(token(state_token)))
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(QRectF(width - 18, (HEADER - 10) / 2 + 1, 10, 10))

        font.setBold(False)
        font.setPointSizeF(max(font.pointSizeF() - 1, 7))
        painter.setFont(font)
        small = QFontMetrics(font)
        # ports: the name beside its dot, brighter when it is wired
        scene = self.scene_ref
        for (name, is_input), port in self.ports.items():
            y = port.pos().y()
            wired = scene is not None and scene.port_connected(port.ref, is_input)
            painter.setPen(QColor(TEXT if wired else TEXT_MUTED))
            if is_input:
                painter.drawText(QRectF(12, y - PORT_ROW / 2, width / 2 - 14, PORT_ROW), Qt.AlignLeft | Qt.AlignVCenter,
                                 small.elidedText(name, Qt.ElideRight, int(width / 2 - 14)))
            else:
                painter.drawText(QRectF(width / 2, y - PORT_ROW / 2, width / 2 - 12, PORT_ROW),
                                 Qt.AlignRight | Qt.AlignVCenter, small.elidedText(name, Qt.ElideLeft, int(width / 2 - 12)))
        # parameters: a small table below a line, the name muted, the value bright
        rows = max((sum(1 for key in self.ports if key[1]), sum(1 for key in self.ports if not key[1])), default=0)
        y = HEADER + 4 + rows * PORT_ROW
        params = self.param_rows()
        if params:
            painter.setPen(QPen(QColor(BORDER), 1))
            painter.drawLine(QPointF(10, y + 3), QPointF(width - 10, y + 3))
            y += 7
        if not self.ports and not params:
            painter.setPen(QColor(TEXT_MUTED))
            painter.drawText(QRectF(10, y, width - 20, PARAM_ROW), Qt.AlignLeft | Qt.AlignVCenter, subtitle)
        for row in params:
            key, separator, value = row.partition(": ")
            if not separator:
                painter.setPen(QColor(TEXT_MUTED))
                painter.drawText(QRectF(10, y, width - 20, PARAM_ROW), Qt.AlignLeft | Qt.AlignVCenter,
                                 small.elidedText(row, Qt.ElideRight, int(width - 20)))
            else:
                key_width = min(small.horizontalAdvance(key) + 8, int((width - 20) * 0.45))
                painter.setPen(QColor(TEXT_MUTED))
                painter.drawText(QRectF(10, y, key_width, PARAM_ROW), Qt.AlignLeft | Qt.AlignVCenter,
                                 small.elidedText(key, Qt.ElideRight, key_width - 6))
                painter.setPen(QColor(TEXT))
                painter.drawText(QRectF(10 + key_width, y, width - 20 - key_width, PARAM_ROW),
                                 Qt.AlignRight | Qt.AlignVCenter,
                                 small.elidedText(value, Qt.ElideRight, int(width - 20 - key_width)))
            y += PARAM_ROW
        if self.data_height():
            preview.paint(painter, self.data_rect(), self.data_entries, font)

    def _paint_grip(self, painter: QPainter, color: QColor) -> None:
        """Three short lines in the corner that resizes (shown on hover or selection)."""
        if not (self.hovered or self.isSelected()):
            return
        grip = self.grip_rect()
        painter.setPen(QPen(color, 1.2))
        for step in (4, 8, 12):
            painter.drawLine(QPointF(grip.right() - step, grip.bottom() - 2), QPointF(grip.right() - 2, grip.bottom() - step))

    # ---------------------------------------------------------------- mouse
    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.LeftButton and self.data_height() and \
                preview.open_rect(self.data_rect()).adjusted(-3, -3, 3, 3).contains(event.pos()):
            if self.scene_ref is not None:
                self.scene_ref.controller.open_node_data(self.node_id)
            event.accept()
            return
        if event.button() == Qt.LeftButton and not self.is_decoration and self.breakpoint_rect().contains(event.pos()):
            if self.scene_ref is not None:
                self.scene_ref.toggle_breakpoint(self.node_id)
            event.accept()
            return
        if event.button() == Qt.LeftButton and self.is_decoration and self.grip_rect().contains(event.pos()):
            self._resizing = (event.scenePos(), self._size())
            event.accept()
            return
        self._drag_start = self.pos()
        self._members = {}
        if self.is_group and self.scene_ref is not None:
            # the nodes inside move with the group
            frame = self.sceneBoundingRect()
            self._members = {item: item.pos() for item in self.scene_ref.nodes.values()
                             if item is not self and frame.contains(item.sceneBoundingRect())}
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        resizing = getattr(self, "_resizing", None)
        if resizing is not None:
            start, (width, height) = resizing
            delta = event.scenePos() - start
            self.prepareGeometryChange()
            self._live_size = (max(snap(width + delta.x()), 120.0), max(snap(height + delta.y()), 60.0))
            self.update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        resizing = getattr(self, "_resizing", None)
        if resizing is not None:
            self._resizing = None
            size = self._live_size
            if self.scene_ref is not None and size is not None and size != resizing[1]:
                self.scene_ref.controller.set_param(self.node_id, "size", [int(size[0]), int(size[1])])
            self.prepareGeometryChange()
            self._live_size = None
            event.accept()
            return
        super().mouseReleaseEvent(event)
        # a click without moving changes nothing (also for nodes without a stored position)
        if self._drag_start is not None and self.pos() != self._drag_start and self.scene_ref is not None:
            self.scene_ref.items_moved([item.node_id for item in getattr(self, "_members", {})])
        self._drag_start = None
        self._members = {}

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self.scene_ref is not None:
            on_data = bool(self.data_height()) and self.data_rect().contains(event.pos())
            self.scene_ref.node_double_clicked(self.node_id, on_data)
        event.accept()

    def itemChange(self, change, value):  # noqa: N802 - Qt naming
        if change == QGraphicsItem.ItemPositionChange and self.scene_ref is not None and self.scene_ref.snap_to_grid:
            return QPointF(snap(value.x()), snap(value.y()))
        if change == QGraphicsItem.ItemPositionHasChanged and self.scene_ref is not None:
            self.scene_ref.node_moved(self.node_id)
            members = getattr(self, "_members", None)
            if members and self._drag_start is not None:
                delta = self.pos() - self._drag_start
                for item, start in members.items():
                    if not item.isSelected():  # selected ones move by themselves
                        item.setPos(start + delta)
        return super().itemChange(change, value)
