# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The graph view of a flow: nodes and wires on a canvas.

The scene mirrors the model (:meth:`FlowScene.sync`); every edit goes to the *controller* (the
flow document), which changes the model with undo and syncs the scene again. Nodes without a
position are placed by the depth of their inputs; positions only enter the file once a node is
moved, so opening a flow does not change it.
"""

from __future__ import annotations

from typing import Optional, Protocol

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsPathItem,
    QGraphicsScene,
    QGraphicsView,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core import fuzzy, signals
from ...lab import layout
from ...lab.model import Edge, Flow
from ...lab.nodes.registry import NodeSpec, Registry, RegistryError
from ..theme import ACCENT_HOVER, BACKGROUND, BORDER, PANEL, qcolor, type_color
from ..widgets import navigation
from .node_item import GRID, NODE_WIDTH, NodeItem, PortItem
from .wire_item import WireItem, curve

NODE_MIME = "application/x-openscilab-node"


class Controller(Protocol):  # pragma: no cover - the interface the scene calls
    def connect_ports(self, source: str, target: str) -> tuple[bool, str, str]: ...
    def positions_changed(self, positions: dict[str, tuple[float, float]]) -> None: ...
    def toggle_breakpoint(self, node_id: str) -> None: ...
    def open_node_data(self, node_id: str) -> None: ...

    def edit_node(self, node_id: str) -> object: ...
    def open_node(self, node_id: str) -> None: ...
    def add_node(self, type_name: str, position: tuple[float, float], node_id: Optional[str] = None,
                 **params) -> Optional[str]: ...
    def delete_items(self, node_ids: list[str], edges: list[Edge]) -> None: ...
    def set_param(self, node_id: str, name: str, value) -> None: ...
    def insert_conversion(self, source: str, target: str, node_type: str) -> None: ...
    def wire_dropped(self, port: str, is_input: bool, type_name: str, position: tuple[float, float]) -> None: ...


#: the canvas reaches at least this far around the origin, and grows with the flow
SCENE_MARGIN = 4000
MIN_ZOOM = 0.15
MAX_ZOOM = 3.0
#: one wheel notch zooms by this factor
WHEEL_ZOOM = 1.15
#: a wire dropped this close to a fitting port (scene units) ends there
SNAP_RADIUS = 24.0


class FlowScene(QGraphicsScene):
    selection_changed_ids = Signal(list)
    #: a wire that does not fit was dropped: (message, conversion node type or "")
    rejected = Signal(str, str, str, str)

    def __init__(self, controller: Controller, registry: Registry) -> None:
        super().__init__()
        self.controller = controller
        self.registry = registry
        self.flow: Optional[Flow] = None
        self.nodes: dict[str, NodeItem] = {}
        self.wires: dict[Edge, WireItem] = {}
        #: (port, is input) of every end of a wire, for the edges it was built from
        self._connected: set[tuple[str, bool]] = set()
        self._connected_for = None
        self._connected_count = -1
        self.breakpoints: set[str] = set()
        #: the data of the current or last run, shown in the nodes (set by the flow document)
        self.run_data = None
        self.show_data = True
        self.snap_to_grid = True
        self._syncing = False
        self._drag_port: Optional[PortItem] = None
        self._drag_path: Optional[QGraphicsPathItem] = None
        self.setSceneRect(QRectF(-SCENE_MARGIN, -SCENE_MARGIN, 2 * SCENE_MARGIN, 2 * SCENE_MARGIN))
        self.setBackgroundBrush(QColor(BACKGROUND))
        self.selectionChanged.connect(self._emit_selection)

    # ------------------------------------------------------------------ sync
    def spec_of(self, node) -> Optional[NodeSpec]:
        try:
            return self.flow.spec(node, self.registry)
        except RegistryError:
            return None

    def sync(self, flow: Flow) -> None:
        """Show ``flow``: add, update and remove items so they match the model."""
        self._syncing = True
        try:
            self.flow = flow
            selected = {item.node_id for item in self.selected_nodes()}
            for node_id in list(self.nodes):
                if node_id not in flow.nodes:
                    self.removeItem(self.nodes.pop(node_id))
            for node_id, node in flow.nodes.items():
                spec = self.spec_of(node)
                item = self.nodes.get(node_id)
                if item is None:
                    item = NodeItem(node, spec, self)
                    self.nodes[node_id] = item
                    self.addItem(item)
                else:
                    item.update_from(node, spec)
                item.set_breakpoint(node_id in self.breakpoints)
                item.setSelected(node_id in selected)
            positions = self._auto_positions(flow)
            for node_id, node in flow.nodes.items():
                item = self.nodes[node_id]
                position = node.position or positions.get(node_id, (0.0, 0.0))
                if item.pos() != QPointF(*position):
                    item.setPos(QPointF(*position))
            for edge in list(self.wires):
                if edge not in flow.edges:
                    self.removeItem(self.wires.pop(edge))
            for edge in flow.edges:
                wire = self.wires.get(edge)
                source_type, target_type = flow.port_types(edge, self.registry)
                if wire is None:
                    wire = WireItem(edge, source_type or signals.ANY)
                    self.wires[edge] = wire
                    self.addItem(wire)
                wire.type_name = source_type or signals.ANY
                wire.invalid = (source_type is None or target_type is None
                                or not signals.compatible(source_type, target_type))
                self._place_wire(wire)
        finally:
            self._syncing = False

    def node_height(self, node_id: str) -> float:
        """The height a node is arranged with (with the room for its data)."""
        item = self.nodes.get(node_id)
        return item.layout_height() if item is not None else 80.0

    def port_offset(self, node_id: str, name: Optional[str], is_input: bool) -> float:
        """Where the port ``name`` sits below the top of its node (for arranging along the wires)."""
        item = self.nodes.get(node_id)
        port = item.port(name, is_input) if item is not None and name else None
        return port.pos().y() if port is not None else layout.DEFAULT_PORT

    def _auto_positions(self, flow: Flow) -> dict[str, tuple[float, float]]:
        """Places for the nodes that were never placed: along the wires (:mod:`.layout`)."""
        if all(node.position is not None for node in flow.nodes.values()):
            return {}
        return layout.arrange(flow, self.node_height, NODE_WIDTH, self.port_offset)

    def _place_wire(self, wire: WireItem) -> None:
        source = self.nodes.get(wire.edge.source.node)
        target = self.nodes.get(wire.edge.target.node)
        if source is None or target is None:
            wire.setVisible(False)
            return
        start_port = source.port(wire.edge.source.port, False)
        end_port = target.port(wire.edge.target.port, True)
        start = start_port.scene_center() if start_port else source.mapToScene(QPointF(NODE_WIDTH, 13))
        end = end_port.scene_center() if end_port else target.mapToScene(QPointF(0, 13))
        wire.setVisible(True)
        # a wire back (feedback) runs below both nodes
        lane = max(source.sceneBoundingRect().bottom(), target.sceneBoundingRect().bottom()) + 18
        wire.set_ends(start, end, lane)

    def node_moved(self, node_id: str) -> None:
        if self._syncing:
            return
        for wire in self.wires.values():
            if node_id in (wire.edge.source.node, wire.edge.target.node):
                self._place_wire(wire)

    def items_moved(self, moved_along: Optional[list[str]] = None) -> None:
        """Nodes were dragged (``moved_along``: nodes a group took with it)."""
        if self.flow is None:
            return
        positions = {}
        along = set(moved_along or ())
        for item in self.nodes.values():
            current = (round(item.pos().x(), 2), round(item.pos().y(), 2))
            stored = self.flow.nodes[item.node_id].position if item.node_id in self.flow.nodes else None
            if stored is None or (round(stored[0], 2), round(stored[1], 2)) != current:
                if item.isSelected() or stored is not None or item.node_id in along:
                    positions[item.node_id] = current
        if positions:
            self.controller.positions_changed(positions)

    def port_connected(self, ref: str, is_input: bool) -> bool:
        """Whether a wire ends at the port ``ref`` (asked by every port each time it is painted:
        looked up in a set built once per change of the flow)."""
        if self.flow is None:
            return False
        if self._connected_for is not self.flow.edges or len(self.flow.edges) != self._connected_count:
            self._connected = {(str(edge.target), True) for edge in self.flow.edges} | {
                (str(edge.source), False) for edge in self.flow.edges}
            self._connected_for, self._connected_count = self.flow.edges, len(self.flow.edges)
        return (ref, is_input) in self._connected

    # ---------------------------------------------------------------- state
    def selected_nodes(self) -> list[NodeItem]:
        return [item for item in self.selectedItems() if isinstance(item, NodeItem)]

    def selected_wires(self) -> list[WireItem]:
        return [item for item in self.selectedItems() if isinstance(item, WireItem)]

    def _emit_selection(self) -> None:
        if not self._syncing:
            self.selection_changed_ids.emit([item.node_id for item in self.selected_nodes()])

    def select_nodes(self, node_ids: list[str]) -> None:
        for node_id, item in self.nodes.items():
            item.setSelected(node_id in node_ids)

    def toggle_breakpoint(self, node_id: str) -> None:
        self.controller.toggle_breakpoint(node_id)

    def set_breakpoints(self, node_ids: set[str]) -> None:
        self.breakpoints = set(node_ids)
        for node_id, item in self.nodes.items():
            item.set_breakpoint(node_id in self.breakpoints)

    def node_double_clicked(self, node_id: str, on_data: bool = False) -> None:
        """A subflow opens; the data shown in a node open in a view (``on_data``: the double-click
        was on them); any other place of a node opens its editor window."""
        item = self.nodes.get(node_id)
        if item is None:
            return
        if item.node.type.startswith("subflow."):
            self.controller.open_node(node_id)
        elif on_data and item.data_entries:
            self.controller.open_node_data(node_id)  # its data in a data view
        else:
            self.controller.edit_node(node_id)

    def update_data(self, node_ids=None) -> None:
        """Show the run data in the nodes ``node_ids`` (all when ``None``) and in the nodes they
        feed (a view node shows what arrives at it)."""
        from . import preview

        if node_ids is None:
            chosen = set(self.nodes)
        else:
            chosen = set(node_ids)
            if self.flow is not None:
                chosen |= {edge.target.node for edge in self.flow.edges if edge.source.node in chosen}
        for node_id in chosen:
            item = self.nodes.get(node_id)
            if item is not None and not item.is_decoration:
                shown = preview.entries(self.run_data, self.flow, node_id) if self.show_data else []
                item.set_data(shown)
        self.update_wires_of(chosen)

    def update_wires_of(self, node_ids) -> None:
        """Place the wires of nodes whose height changed again (their ports did not move, but
        a wire ends at the node's outline only when the item knows its size)."""
        for wire in self.wires.values():
            if wire.edge.source.node in node_ids or wire.edge.target.node in node_ids:
                self._place_wire(wire)

    def set_node_state(self, node_id: str, state: str, message: str = "") -> None:
        item = self.nodes.get(node_id)
        if item is not None:
            item.set_state(state, message)

    def set_problems(self, problems) -> None:
        """Validation problems on their nodes (badges); ``problems``: :class:`~...lab.model.Problem`."""
        by_node: dict[str, list[tuple[str, str]]] = {}
        for problem in problems:
            if problem.node:
                by_node.setdefault(problem.node, []).append((problem.severity, problem.text))
        for node_id, item in self.nodes.items():
            item.set_problems(by_node.get(node_id, []))

    def reset_states(self) -> None:
        for item in self.nodes.values():
            item.set_state("idle")
        for wire in self.wires.values():
            wire.clear_value()

    def show_value(self, node_id: str, port: str, value) -> None:
        self.show_values(node_id, port, [value])

    def show_values(self, node_id: str, port: str, values: list) -> None:
        """The values an output sent (oldest first) on the wires leaving it."""
        for wire in self.wires.values():
            if wire.edge.source.node == node_id and wire.edge.source.port == port:
                wire.show_values(values)

    # ----------------------------------------------------------------- wires
    def all_ports(self) -> list[PortItem]:
        return [port for item in self.nodes.values() for port in item.ports.values()]

    @staticmethod
    def fits(start: PortItem, other: PortItem) -> bool:
        """A wire from ``start`` can end at ``other`` (other direction, another node, types fit)."""
        if other.is_input == start.is_input or other.node_item is start.node_item:
            return False
        source, target = (other, start) if start.is_input else (start, other)
        return signals.compatible(source.spec.type, target.spec.type)

    def begin_wire(self, port: PortItem) -> None:
        """Start dragging a wire: the ports it fits light up, the others fade."""
        self._drag_port = port
        self._drag_target: Optional[PortItem] = None
        self._drag_path = QGraphicsPathItem()
        self._drag_path.setPen(QPen(QColor(type_color(port.spec.type)), 2, Qt.DashLine))
        self._drag_path.setZValue(10)
        self.addItem(self._drag_path)
        for other in self.all_ports():
            if other is not port and other.is_input != port.is_input:
                other.set_highlight("ok" if self.fits(port, other) else "no")

    def _end_drag(self) -> None:
        for other in self.all_ports():
            other.set_highlight(None)
        if self._drag_path is not None:
            self.removeItem(self._drag_path)
        self._drag_port = None
        self._drag_path = None
        self._drag_target = None

    def target_near(self, start: PortItem, position: QPointF, radius: float = SNAP_RADIUS) -> Optional[PortItem]:
        """Where a wire dropped at ``position`` ends: a fitting port within ``radius``, or the
        first fitting port of the node under the pointer (the whole node is a target)."""
        best, distance = None, radius
        for other in self.all_ports():
            if not self.fits(start, other):
                continue
            gap = (other.scene_center() - position)
            length = (gap.x() ** 2 + gap.y() ** 2) ** 0.5
            if length <= distance:
                best, distance = other, length
        if best is not None:
            return best
        node = next((item for item in self.items(position) if isinstance(item, NodeItem)
                     and not item.is_decoration and item is not start.node_item), None)
        if node is not None:
            return next((port for port in node.ports.values() if self.fits(start, port)), None)
        return None

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._drag_port is not None and self._drag_path is not None:
            start = self._drag_port.scene_center()
            target = self.target_near(self._drag_port, event.scenePos())
            self._drag_target = target
            end = target.scene_center() if target is not None else event.scenePos()
            self._drag_path.setPath(curve(end, start) if self._drag_port.is_input else curve(start, end))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._drag_port is not None:
            port = self._drag_port
            target = self.target_near(port, event.scenePos())
            exact = next((item for item in self.items(event.scenePos()) if isinstance(item, PortItem)
                          and item is not port and item.is_input != port.is_input), None)
            over_node = any(isinstance(item, NodeItem) for item in self.items(event.scenePos()))
            self._end_drag()
            if target is None and exact is not None:
                target = exact  # a port that does not fit: say why (or offer a conversion)
            if target is not None:
                source, sink = (target, port) if port.is_input else (port, target)
                self.finish_wire(source.ref, sink.ref)
            elif not over_node:
                # dropped on the empty canvas: choose a node to connect there
                position = event.scenePos()
                self.controller.wire_dropped(port.ref, port.is_input, port.spec.type, (position.x(), position.y()))
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def finish_wire(self, source: str, target: str) -> bool:
        ok, message, suggestion = self.controller.connect_ports(source, target)
        if not ok:
            self.rejected.emit(source, target, message, suggestion)
        return ok


class NodeSearchPopup(QFrame):
    """Typing on the canvas: find a node type and place it where the mouse is."""

    chosen = Signal(str)

    def __init__(self, registry: Registry, parent: QWidget, text: str = "",
                 accepts=None, title: str = "Add a node") -> None:
        """``accepts(spec)``: only node types it accepts (e.g. those a dragged wire fits)."""
        super().__init__(parent, Qt.Popup)
        self.setObjectName("command-palette")
        self.registry = registry
        self.accepts = accepts
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        self.input = QLineEdit(self)
        self.input.setPlaceholderText(title)
        self.input.textChanged.connect(self.refresh)
        self.input.returnPressed.connect(self.accept_current)
        layout.addWidget(self.input)
        self.results = QListWidget(self)
        self.results.itemActivated.connect(lambda _item: self.accept_current())
        layout.addWidget(self.results)
        self.resize(320, 300)
        self.input.setText(text)
        self.refresh()

    def candidates(self) -> list[NodeSpec]:
        if self.accepts is not None:
            return [spec for spec in self.registry.specs() if self.accepts(spec)]
        return [spec for spec in self.registry.specs() if not spec.type.startswith("structure.")] + [
            self.registry.get("structure.comment"), self.registry.get("structure.group")]

    def refresh(self) -> None:
        self.results.clear()
        query = self.input.text()
        specs = fuzzy.rank(query, self.candidates(), key=lambda spec: f"{spec.type} {spec.title}")
        for spec in specs[:50]:
            item = QListWidgetItem(f"{spec.title}    {spec.type}", self.results)
            item.setData(Qt.UserRole, spec.type)
            item.setToolTip(spec.description)
        if self.results.count():
            self.results.setCurrentRow(0)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.key() in (Qt.Key_Down, Qt.Key_Up):
            row = self.results.currentRow() + (1 if event.key() == Qt.Key_Down else -1)
            if 0 <= row < self.results.count():
                self.results.setCurrentRow(row)
            return
        super().keyPressEvent(event)

    def accept_current(self) -> None:
        item = self.results.currentItem()
        if item is not None:
            self.close()
            self.chosen.emit(item.data(Qt.UserRole))


class Minimap(QWidget):
    """The whole flow small - its nodes as blocks in the colour of their kind, the wires as lines -
    with the visible part outlined; click or drag to move there, the wheel and gestures navigate the
    flow itself."""

    MARGIN = 8.0

    def __init__(self, view: "FlowView") -> None:
        super().__init__(view)
        self.main = view
        self.setFixedSize(190, 124)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Overview: click or drag to move there")
        self._bounds = QRectF()

    def viewport(self) -> QWidget:  # (the API of the view it replaced)
        return self

    def refit(self) -> None:
        self.update()

    def _scene_bounds(self) -> QRectF:
        bounds = QRectF()
        for item in self.main.flow_scene.nodes.values():
            bounds = bounds.united(item.sceneBoundingRect())
        return bounds.adjusted(-40, -40, 40, 40) if bounds.isValid() else bounds

    def _transform(self) -> tuple[float, float, float]:
        """(scale, x offset, y offset) from the scene into this widget."""
        bounds = self._bounds
        room_w, room_h = self.width() - 2 * self.MARGIN, self.height() - 2 * self.MARGIN
        scale = min(room_w / max(bounds.width(), 1.0), room_h / max(bounds.height(), 1.0))
        x = self.MARGIN + (room_w - bounds.width() * scale) / 2 - bounds.left() * scale
        y = self.MARGIN + (room_h - bounds.height() * scale) / 2 - bounds.top() * scale
        return scale, x, y

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        from .node_item import category_color

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        frame = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        background = QColor(PANEL)
        background.setAlpha(235)
        painter.setPen(QPen(QColor(BORDER), 1))
        painter.setBrush(background)
        painter.drawRoundedRect(frame, 6, 6)
        self._bounds = self._scene_bounds()
        if not self._bounds.isValid():
            return
        scale, dx, dy = self._transform()

        def mapped(point: QPointF) -> QPointF:
            return QPointF(point.x() * scale + dx, point.y() * scale + dy)

        scene = self.main.flow_scene
        for wire in scene.wires.values():
            if not wire.isVisible():
                continue
            path = wire.path()
            color = QColor(type_color(wire.type_name))
            color.setAlpha(150)
            painter.setPen(QPen(color, 1))
            painter.drawLine(mapped(path.pointAtPercent(0)), mapped(path.pointAtPercent(1)))
        painter.setPen(Qt.NoPen)
        for item in scene.nodes.values():
            rect = item.sceneBoundingRect()
            box = QRectF(mapped(rect.topLeft()), mapped(rect.bottomRight()))
            color = category_color(item.node.type)
            fill = QColor(color)
            fill.setAlpha(200 if item.isSelected() else 140)
            painter.setBrush(fill)
            painter.drawRoundedRect(box, 1.5, 1.5)
        visible = self.main.mapToScene(self.main.viewport().rect()).boundingRect()
        shown = QRectF(mapped(visible.topLeft()), mapped(visible.bottomRight())).intersected(frame.adjusted(2, 2, -2, -2))
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(QColor(ACCENT_HOVER), 1.5))
        painter.drawRoundedRect(shown, 3, 3)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._move_to(event.position())

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.buttons() & Qt.LeftButton:
            self._move_to(event.position())

    def _move_to(self, position: QPointF) -> None:
        if not self._bounds.isValid():
            return
        scale, dx, dy = self._transform()
        self.main.centerOn(QPointF((position.x() - dx) / scale, (position.y() - dy) / scale))
        self.main.minimap_moved()

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self.main.navigate(event, self.main.viewport().rect().center())

    def event(self, event) -> bool:  # noqa: D102
        if event.type() == QEvent.NativeGesture and self.main.gesture(event, self.main.viewport().rect().center()):
            return True
        return super().event(event)


class FlowView(QGraphicsView):
    """The flow canvas.

    Navigation as in the data view: two fingers on a trackpad move the canvas, pinch zooms at the
    fingers (smart zoom fits), the mouse wheel zooms at the pointer (``Ctrl``/Cmd + wheel moves
    sideways, ``Shift`` + wheel up and down), ``Ctrl``/Cmd + swipe zooms. The middle button or
    Space + drag moves the canvas; ``+``/``-`` zoom; typing anything else adds a node.
    """

    def __init__(self, scene: FlowScene, parent: Optional[QWidget] = None) -> None:
        super().__init__(scene, parent)
        self.flow_scene = scene
        self.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
        self.setDragMode(QGraphicsView.RubberBandDrag)
        self.setViewportUpdateMode(QGraphicsView.BoundingRectViewportUpdate)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setAcceptDrops(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.viewport().setAttribute(Qt.WA_AcceptTouchEvents, False)
        self._panning: Optional[QPointF] = None
        self._space = False
        self.minimap = Minimap(self)
        self.popup: Optional[NodeSearchPopup] = None
        scene.changed.connect(self._content_changed)  # a method: dropped when the view is gone

    # ------------------------------------------------------------ geometry
    def _content_changed(self, *_rects) -> None:
        from .. import accessibility

        scene = self.flow_scene
        accessibility.describe(self, "Flow graph", f"{len(scene.nodes)} nodes, {len(scene.wires)} wires; "
                                                   "type to add a node")
        self.grow_scene()
        self.minimap.setVisible(bool(self.flow_scene.nodes))  # nothing to show in an empty flow
        self.minimap.viewport().update()

    def grow_scene(self) -> None:
        """The canvas reaches a screen beyond the flow in every direction, so every node can be
        scrolled to the middle (also far away ones, also zoomed out)."""
        scene = self.scene()
        visible = self.mapToScene(self.viewport().rect()).boundingRect()
        base = QRectF(-SCENE_MARGIN, -SCENE_MARGIN, 2 * SCENE_MARGIN, 2 * SCENE_MARGIN)
        wanted = scene.itemsBoundingRect().adjusted(-visible.width(), -visible.height(), visible.width(),
                                                    visible.height()).united(base).united(visible)
        if not scene.sceneRect().contains(wanted):
            scene.setSceneRect(scene.sceneRect().united(wanted))

    def zoom_level(self) -> float:
        return self.transform().m11()

    def zoom(self, factor: float) -> None:
        """Zoom at the pointer (or the middle when the pointer is elsewhere)."""
        cursor = self.viewport().mapFromGlobal(self.cursor().pos())
        anchor = cursor if self.viewport().rect().contains(cursor) else self.viewport().rect().center()
        self.zoom_at(factor, QPointF(anchor))

    def zoom_at(self, factor: float, position: QPointF) -> None:
        """Zoom by ``factor`` keeping the point under ``position`` (viewport pixels) in place;
        the zoom stops at its limits instead of skipping the step."""
        current = self.zoom_level()
        target = min(max(current * factor, MIN_ZOOM), MAX_ZOOM)
        if abs(target - current) < 1e-6:
            return
        point = QPoint(int(round(position.x())), int(round(position.y())))
        before = self.mapToScene(point)
        anchor = self.transformationAnchor()
        self.setTransformationAnchor(QGraphicsView.NoAnchor)
        self.scale(target / current, target / current)
        self.setTransformationAnchor(anchor)
        self.grow_scene()
        moved = self.mapFromScene(before) - point
        self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() + moved.x())
        self.verticalScrollBar().setValue(self.verticalScrollBar().value() + moved.y())
        self.minimap.viewport().update()

    def actual_size(self) -> None:
        center = self.mapToScene(self.viewport().rect().center())
        self.resetTransform()
        self.grow_scene()
        self.centerOn(center)
        self.minimap.viewport().update()

    def pan_by(self, dx: float, dy: float) -> None:
        """Move the content by ``dx``, ``dy`` pixels (as the fingers move)."""
        self.horizontalScrollBar().setValue(int(round(self.horizontalScrollBar().value() - dx)))
        self.verticalScrollBar().setValue(int(round(self.verticalScrollBar().value() - dy)))
        self.minimap.viewport().update()

    def minimap_moved(self) -> None:
        self.minimap.viewport().update()

    # -------------------------------------------------------------- input
    def navigate(self, event, anchor: Optional[QPointF] = None) -> None:
        """A wheel event: trackpad swipes move, wheel notches zoom (see the class)."""
        anchor = anchor if anchor is not None else event.position()
        modifiers = event.modifiers()
        if navigation.is_trackpad(event):
            dx, dy = navigation.trackpad_delta(event)
            if modifiers & Qt.ControlModifier:
                if dy:
                    self.zoom_at(2 ** (dy / navigation.PIXELS_PER_DOUBLING), QPointF(anchor))
            else:
                self.pan_by(dx, dy)
            event.accept()
            return
        angle = event.angleDelta()
        step = self.viewport().height() / 8
        control = bool(modifiers & Qt.ControlModifier)
        wheel_zooms = navigation.wheel_mode() == "zoom"
        direction = -1 if navigation.zoom_inverted() else 1
        if modifiers & Qt.ShiftModifier:
            # macOS turns Shift + wheel into a horizontal wheel
            self.pan_by(0, (angle.y() or angle.x()) / navigation.WHEEL_NOTCH * step)
        elif angle.x() and abs(angle.x()) >= abs(angle.y()):
            self.pan_by(angle.x() / navigation.WHEEL_NOTCH * step, 0)
        elif angle.y() and control != wheel_zooms:
            self.zoom_at(WHEEL_ZOOM ** (direction * angle.y() / navigation.WHEEL_NOTCH), QPointF(anchor))
        elif angle.y():
            # moves: sideways with Ctrl/Cmd (zoom mode), up and down otherwise (scroll mode)
            if wheel_zooms:
                self.pan_by(angle.y() / navigation.WHEEL_NOTCH * step, 0)
            else:
                self.pan_by(0, angle.y() / navigation.WHEEL_NOTCH * step)
        event.accept()

    def gesture(self, event, anchor: Optional[QPointF] = None) -> bool:
        """Pinch zooms at the fingers, smart zoom fits; returns whether the event was used."""
        if navigation.is_smart_zoom(event):
            if self.zoom_level() < 0.95 or self.zoom_level() > 1.05:
                self.actual_size()
            else:
                self.fit()
            return True
        factor = navigation.pinch_factor(event)
        if factor is not None:
            self.zoom_at(factor, QPointF(anchor) if anchor is not None else event.position())
            return True
        return event.gestureType() in (Qt.BeginNativeGesture, Qt.EndNativeGesture)

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self.navigate(event)

    def viewportEvent(self, event) -> bool:  # noqa: N802 - Qt naming
        if event.type() == QEvent.NativeGesture and self.gesture(event):
            return True
        return super().viewportEvent(event)

    def event(self, event) -> bool:  # noqa: A003 - Qt naming
        # a gesture that reaches the view itself (not its viewport): positions are the view's
        if event.type() == QEvent.NativeGesture and self.gesture(
                event, QPointF(self.viewport().mapFrom(self, event.position().toPoint()))):
            return True
        return super().event(event)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self.minimap.move(self.viewport().width() - self.minimap.width() - 10,
                          self.viewport().height() - self.minimap.height() - 10)
        self.minimap.refit()

    def focusNextPrevChild(self, forward: bool) -> bool:  # noqa: N802 - Qt naming
        return False  # Tab adds a node here instead of moving the focus

    def drawForeground(self, painter: QPainter, rect: QRectF) -> None:  # noqa: N802 - Qt naming
        super().drawForeground(painter, rect)
        if self.flow_scene.nodes:
            return
        # an empty flow: how to begin
        painter.save()
        painter.resetTransform()
        painter.setPen(qcolor("canvas.hint"))
        painter.drawText(self.viewport().rect(), Qt.AlignCenter,
                         "Type a name or press Tab to add a node,\nor drag one from the Nodes palette.")
        painter.restore()

    def drawBackground(self, painter: QPainter, rect: QRectF) -> None:  # noqa: N802 - Qt naming
        super().drawBackground(painter, rect)
        step = GRID * 4
        if self.transform().m11() < 0.4:
            return
        painter.setPen(QPen(qcolor("canvas.dots"), 0))
        left = int(rect.left()) - int(rect.left()) % step
        top = int(rect.top()) - int(rect.top()) % step
        points = []
        x = left
        while x < rect.right():
            y = top
            while y < rect.bottom():
                points.append(QPointF(x, y))
                y += step
            x += step
        painter.drawPoints(points)

    def fit(self) -> None:
        rect = self.scene().itemsBoundingRect().adjusted(-60, -60, 60, 60)
        if rect.isValid() and not rect.isEmpty():
            self.fitInView(rect, Qt.KeepAspectRatio)
            if self.transform().m11() > 1.2:
                self.resetTransform()
                self.centerOn(rect.center())
        self.grow_scene()
        self.minimap.refit()

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.MiddleButton or (self._space and event.button() == Qt.LeftButton):
            self._panning = event.position()
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._panning is not None:
            delta = event.position() - self._panning
            self._panning = event.position()
            self.horizontalScrollBar().setValue(int(self.horizontalScrollBar().value() - delta.x()))
            self.verticalScrollBar().setValue(int(self.verticalScrollBar().value() - delta.y()))
            self.minimap.viewport().update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._panning is not None:
            self._panning = None
            self.unsetCursor()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        key = event.key()
        if key == Qt.Key_Space and not event.isAutoRepeat():
            self._space = True
            return
        plain = not (event.modifiers() & (Qt.ControlModifier | Qt.MetaModifier | Qt.AltModifier))
        if plain and key in (Qt.Key_Plus, Qt.Key_Equal, Qt.Key_Minus):
            self.zoom(WHEEL_ZOOM if key != Qt.Key_Minus else 1 / WHEEL_ZOOM)
            return
        if key == Qt.Key_Tab and plain:
            self.open_search()
            return
        if key in (Qt.Key_Delete, Qt.Key_Backspace):
            scene = self.flow_scene
            scene.controller.delete_items([item.node_id for item in scene.selected_nodes()],
                                          [wire.edge for wire in scene.selected_wires()])
            return
        text = event.text()
        if text and text.isprintable() and not text.isspace() and not (event.modifiers() & (Qt.ControlModifier | Qt.MetaModifier)):
            self.open_search(text)
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.key() == Qt.Key_Space and not event.isAutoRepeat():
            self._space = False
        super().keyReleaseEvent(event)

    def focusOutEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._space = False  # Space released elsewhere
        super().focusOutEvent(event)

    def open_search(self, text: str = "", position: Optional[QPointF] = None, accepts=None,
                    chosen=None, title: str = "Add a node") -> NodeSearchPopup:
        """The node search at the pointer; ``chosen(type, position)`` instead of just adding it."""
        cursor = self.mapFromGlobal(self.cursor().pos())
        if position is None:
            position = self.mapToScene(cursor) if self.viewport().rect().contains(cursor) else \
                self.mapToScene(self.viewport().rect().center())
        popup = NodeSearchPopup(self.flow_scene.registry, self, text, accepts=accepts, title=title)
        popup.setAttribute(Qt.WA_DeleteOnClose)  # one per search: not kept as a hidden child
        add = chosen or (lambda type_name, at: self.flow_scene.controller.add_node(type_name, (at.x(), at.y())))
        popup.chosen.connect(lambda type_name, at=position: add(type_name, at))
        popup.move(self.mapToGlobal(self.mapFromScene(position)))
        popup.show()
        popup.input.setFocus()
        popup.input.end(False)
        self.popup = popup
        popup.destroyed.connect(lambda *_args, gone=popup: self._popup_gone(gone))
        return popup

    def _popup_gone(self, popup) -> None:
        if self.popup is popup:
            self.popup = None

    # --------------------------------------------------------- drag & drop
    def dragEnterEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.mimeData().hasFormat(NODE_MIME):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.mimeData().hasFormat(NODE_MIME):
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.mimeData().hasFormat(NODE_MIME):
            type_name, node_id, params = decode_node(bytes(event.mimeData().data(NODE_MIME)).decode("utf-8"))
            position = self.mapToScene(event.position().toPoint())
            self.flow_scene.controller.add_node(type_name, (position.x(), position.y()), node_id, **params)
            event.acceptProposedAction()
            return
        super().dropEvent(event)


def encode_node(type_name: str, node_id: Optional[str] = None, params: Optional[dict] = None) -> str:
    """A node to add (drag and drop, palette): its type, or a preset with id and parameters."""
    if node_id is None and not params:
        return type_name
    import json

    return json.dumps({"type": type_name, "id": node_id, "params": params or {}})


def decode_node(text: str) -> tuple[str, Optional[str], dict]:
    """``(type, id, params)`` of :func:`encode_node`."""
    if not text.startswith("{"):
        return text, None, {}
    import json

    data = json.loads(text)
    return str(data["type"]), data.get("id"), dict(data.get("params") or {})
