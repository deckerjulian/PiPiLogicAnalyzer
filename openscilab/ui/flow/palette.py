# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The node palette in the sidebar: search, groups, drag onto the canvas or double-click."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import QMimeData, Qt, QTimer, Signal
from PySide6.QtGui import QDrag
from PySide6.QtWidgets import QFrame, QLineEdit, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget

from ...core import fuzzy
from ...lab.model import DEVICE_NODE
from ...lab.nodes.registry import GROUPS, Registry, default_registry
from ..icons import icon
from .canvas import NODE_MIME, encode_node


#: a group whose nodes are loaded when it is opened: its name
PENDING_ROLE = Qt.UserRole + 1


class _Tree(QTreeWidget):
    def startDrag(self, actions) -> None:  # noqa: N802 - Qt naming
        item = self.currentItem()
        type_name = item.data(0, Qt.UserRole) if item is not None else None
        if not type_name:
            return
        mime = QMimeData()
        mime.setData(NODE_MIME, type_name.encode("utf-8"))
        mime.setText(item.text(0))
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.CopyAction)


@dataclass(frozen=True)
class DevicePreset:
    """A device ready to be placed: an open instrument, a simulator, a device of the project."""

    title: str
    node_id: str
    address: str
    #: "open", "project" or "simulator"
    kind: str = "open"


class NodePalette(QWidget):
    #: a node to add (:func:`~.canvas.encode_node`: a type, or a preset with id and parameters)
    node_requested = Signal(str)

    def __init__(self, registry: Optional[Registry] = None, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.registry = registry or default_registry
        self.presets: list[DevicePreset] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 8)
        self.search = QLineEdit(self)
        self.search.setPlaceholderText("Search nodes")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.refresh)
        layout.addWidget(self.search)
        self.tree = _Tree(self)
        self.tree.setHeaderHidden(True)
        self.tree.setFrameShape(QFrame.NoFrame)
        self.tree.setDragEnabled(True)
        # a double-click activates the item (Enter too); connecting both would add two nodes
        self.tree.itemActivated.connect(self._activated)
        self.tree.itemExpanded.connect(self._expanded)
        layout.addWidget(self.tree, 1)
        #: also the groups that take a moment to load (the decoders): once one was opened
        self._all_groups = False
        self.refresh()

    def set_registry(self, registry: Registry) -> None:
        self.registry = registry
        self.refresh()

    def set_device_presets(self, presets: list[DevicePreset]) -> None:
        """The devices the palette offers as ready device nodes (above the node types)."""
        if presets != self.presets:
            self.presets = list(presets)
            self.refresh()

    def refresh(self) -> None:
        self.tree.clear()
        query = self.search.text()
        # The decoders (133 modules) are only imported when they are looked at or searched for.
        specs = self.registry.specs(lazy=self._all_groups or bool(query.strip()))
        if query.strip():
            specs = fuzzy.rank(query, specs, key=lambda spec: f"{spec.type} {spec.title}")
        groups: dict[str, QTreeWidgetItem] = {}
        presets = self.presets
        if query.strip():
            presets = fuzzy.rank(query, presets, key=lambda preset: f"{preset.title} {preset.address}")
        if presets:
            group = QTreeWidgetItem(self.tree, [GROUPS["device"]])
            group.setFlags(Qt.ItemIsEnabled)
            group.setExpanded(True)
            groups["device"] = group
            hints = {"open": "open in the device list", "project": "device of the project",
                     "simulator": "simulator"}
            for preset in presets:
                item = QTreeWidgetItem(group, [preset.title])
                item.setData(0, Qt.UserRole, encode_node(DEVICE_NODE, preset.node_id, {"address": preset.address}))
                item.setToolTip(0, f"{hints.get(preset.kind, preset.kind)}: {preset.address or preset.node_id}\n"
                                   "A device node; wire its 'device' output to the nodes that use it.")
                item.setIcon(0, icon("chip" if preset.kind != "simulator" else "devices"))
                item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsDragEnabled)
        order = list(GROUPS)
        for spec in sorted(specs, key=lambda spec: order.index(spec.group) if spec.group in order else len(order)) \
                if not query.strip() else specs:
            group = groups.get(spec.group)
            if group is None:
                group = QTreeWidgetItem(self.tree, [GROUPS.get(spec.group, spec.group.capitalize())])
                group.setFlags(Qt.ItemIsEnabled)
                group.setExpanded(True)
                groups[spec.group] = group
            item = QTreeWidgetItem(group, [spec.title])
            item.setData(0, Qt.UserRole, spec.type)
            item.setToolTip(0, f"{spec.type}\n{spec.description}")
            item.setIcon(0, icon(spec.icon if spec.icon else "nodes"))
            item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsDragEnabled)
        for name in self.registry.pending_groups():
            group = QTreeWidgetItem(self.tree, [GROUPS.get(name, name.capitalize())])
            group.setFlags(Qt.ItemIsEnabled)
            group.setData(0, PENDING_ROLE, name)
            group.setToolTip(0, "Open to load these nodes")
            QTreeWidgetItem(group, ["Loading..."]).setFlags(Qt.NoItemFlags)

    def _expanded(self, item: QTreeWidgetItem) -> None:
        if item.data(0, PENDING_ROLE) and not self._all_groups:
            self._all_groups = True
            QTimer.singleShot(0, self.refresh)  # not while the tree handles the click

    def load_all(self) -> None:
        """Show every group now (also those loaded on demand)."""
        if not self._all_groups:
            self._all_groups = True
            self.refresh()

    def types(self) -> list[str]:
        result = []
        for index in range(self.tree.topLevelItemCount()):
            group = self.tree.topLevelItem(index)
            for child in range(group.childCount()):
                result.append(group.child(child).data(0, Qt.UserRole))
        return result

    def _activated(self, item: QTreeWidgetItem, _column: int = 0) -> None:
        type_name = item.data(0, Qt.UserRole)
        if type_name:
            self.node_requested.emit(type_name)
