# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The flow document: one ``*.flow.yaml`` in three views – Graph, YAML, Python.

The model is the file: every edit (canvas, inspector, YAML text) changes the :class:`Flow` with
undo, and all views show it. The YAML view is exactly what *Save* writes. Running the flow shows
the state of every node and the values on the wires; breakpoints and single steps work in the
graph.
"""

from __future__ import annotations

import html
import logging
import os
from typing import Any, Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAction, QFontDatabase, QKeySequence, QUndoStack
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QLabel,
    QMenu,
    QPlainTextEdit,
    QStackedWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from ...core import preferences, signals
from ...core.files import atomic_write
from ...core.preferences import default_folder
from ...lab import yaml_io
from ...lab.dsl import from_python, to_python
from ...lab.model import DEVICE_NODE, SUBFLOW_PREFIX, Edge, Flow, FlowError, Node, PortRef
from ...lab.nodes.registry import Registry, RegistryError, default_registry
from ...lab.run_data import RunData
from .. import messages
from ..flow.canvas import FlowScene, FlowView
from ..flow.node_item import NODE_WIDTH
from ..flow.commands import MERGE_PARAM, MERGE_YAML, FlowEdit
from ..flow.highlighter import PythonHighlighter, YamlHighlighter
from ..flow.inspector import FlowInspector, NodeInspector
from ..flow.runner import FlowRunner
from ..icons import icon
from ..theme import set_role
from ..widgets.completer import attach as attach_completer
from .base import DocumentWidget

log = logging.getLogger(__name__)


def device_addresses(hub=None) -> list[tuple[str, str]]:
    """``(label, address)`` of the instruments open in ``hub`` and of every simulator profile."""
    from ...driver.simulated.profiles import available_profiles, load_profile

    result = []
    if hub is not None:
        result += [(f"{instrument.name} (open)", instrument.uri) for instrument in hub.instruments() if instrument.uri]
    for name in available_profiles():
        try:
            result.append((load_profile(name)["title"], f"sim:{name}"))
        except (OSError, ValueError, KeyError):
            continue
    return result

VIEWS = ("Graph", "YAML", "Python")
FLOW_FILE_FILTER = "Flows (*.flow.yaml);;Python flows (*.py);;All files (*)"


class FlowDocument(DocumentWidget):
    document_kind = "flow"
    #: validation problems of the flow (for the console)
    problems_changed = Signal(list)
    #: show a tab of the console ("Problems", "Execution")
    console_requested = Signal(str)
    #: a short note for the status bar (what was done for the user)
    message = Signal(str)
    #: a view node shows a value: (node, kind, value, options)
    view_requested = Signal(str, str, object, dict)
    #: a document to open in the shell (a subflow)
    open_requested = Signal(object)
    #: run state changed (the shell updates its Start/Pause/Stop)
    run_state_changed = Signal(str)
    #: a line for the console's execution tab
    execution_line = Signal(str)
    #: a new panel for this flow (the shell makes it)
    panel_requested = Signal()
    #: a panel of this flow to show: its open document, or its file
    panel_open_requested = Signal(object)
    #: show run data in a data view: (key, title, capture session, bring it to the front); the
    #: same key updates it
    data_requested = Signal(str, str, object, bool)

    runnable = True

    def __init__(self, flow: Optional[Flow] = None, path: Optional[str] = None, registry: Optional[Registry] = None,
                 hub=None, project=None, parent_document: Optional["FlowDocument"] = None,
                 subflow_name: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.flow = flow if flow is not None else Flow("Flow")
        self._path = path
        self.registry = registry or (project.registry() if project is not None else default_registry)
        self.hub = hub
        self.project = project
        #: the open panel documents (set by the window), to find those of this flow
        self.open_panels = None
        self.parent_document = parent_document
        self.subflow_name = subflow_name
        #: open documents of subflows of this flow
        self.children: list["FlowDocument"] = []
        # A subflow document edits its parent's model, with its parent's undo.
        self.undo = parent_document.undo if parent_document is not None else QUndoStack(self)
        # methods, not lambdas: Qt drops the connection when the document is gone (a subflow shares
        # its parent's stack, which outlives it)
        self.undo.cleanChanged.connect(self._undo_state_changed)
        self.undo.indexChanged.connect(self._undo_state_changed)
        if parent_document is not None:
            parent_document.children.append(self)
        self.breakpoints: set[str] = set()
        self.selection: list[str] = []
        self.runner = FlowRunner(self)
        self.runner.state_changed.connect(self._run_state)
        self.runner.node_state.connect(self._node_state)
        self.runner.values.connect(self._show_values)
        self.runner.timed_values.connect(self._collect_values)
        #: what the outputs sent in the current or last run (the nodes and data views show it)
        self.run_data = RunData()
        #: data views opened for this flow: key -> ports (``None``: the whole run)
        self.data_views: dict[str, Optional[list[str]]] = {}
        self._data_seen = -1
        self._data_nodes: set[str] = set()
        self._data_timer = QTimer(self)
        self._data_timer.setInterval(1000)  # the open data views follow a running flow
        self._data_timer.timeout.connect(self._refresh_data_views)
        self.runner.log.connect(self.execution_line.emit)
        self.runner.finished.connect(self._run_finished)
        self._inspector: Optional[QWidget] = None
        self._yaml_error = ""
        self._updating_text = False
        #: one visit of the YAML view: its edits are one undo step
        self._yaml_session = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._toolbar = self._build_toolbar()
        layout.addWidget(self._toolbar)
        from ..dialogs.common import Banner

        # why the flow cannot run, or how its last run ended with an error (instead of a dialog)
        self.run_banner = Banner("error", self)
        self.run_banner.setVisible(False)
        self.run_banner.button.clicked.connect(self._banner_action)
        self._banner_kind = ""
        layout.addWidget(self.run_banner)
        self.stack = QStackedWidget(self)
        layout.addWidget(self.stack, 1)

        self.scene = FlowScene(self, self.registry)
        self.scene.run_data = self.run_data
        self.scene.selection_changed_ids.connect(self._selection_changed)
        self.scene.rejected.connect(self._wire_rejected)
        self.view = FlowView(self.scene, self.stack)
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._context_menu)
        self.stack.addWidget(self.view)

        yaml_page = QWidget(self.stack)
        yaml_layout = QVBoxLayout(yaml_page)
        yaml_layout.setContentsMargins(0, 0, 0, 0)
        self.yaml_edit = QPlainTextEdit(yaml_page)
        self.yaml_edit.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self.yaml_edit.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.yaml_highlighter = YamlHighlighter(self.yaml_edit.document())
        self._yaml_timer = QTimer(self)
        self._yaml_timer.setSingleShot(True)
        self._yaml_timer.setInterval(500)
        self._yaml_timer.timeout.connect(self.apply_yaml)
        self.yaml_edit.textChanged.connect(self._yaml_typed)
        self.yaml_completer = attach_completer(self.yaml_edit, self._yaml_completions)
        yaml_layout.addWidget(self.yaml_edit, 1)
        self.yaml_status = QLabel("", yaml_page)
        set_role(self.yaml_status, "error")
        self.yaml_status.setContentsMargins(8, 2, 8, 4)
        yaml_layout.addWidget(self.yaml_status)
        self.stack.addWidget(yaml_page)

        self.python_edit = QPlainTextEdit(self.stack)
        self.python_edit.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self.python_edit.setReadOnly(True)
        self.python_edit.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.python_highlighter = PythonHighlighter(self.python_edit.document())
        self.stack.addWidget(self.python_edit)

        self.refresh()
        #: the graph was fitted once; afterwards zoom and position stay as the user left them
        self._fitted = False
        QTimer.singleShot(0, self._fit_once)

    # ================================================================ document
    def _undo_state_changed(self, *_args) -> None:
        self.document_changed.emit()

    @property
    def title(self) -> str:
        if self.parent_document is not None:
            return f"{self.parent_document.title} › {self.subflow_name}"
        if self._path:
            return os.path.basename(self._path)
        if self.flow.name and self.flow.name != "Flow":
            return self.flow.name
        if getattr(self, "_untitled", None) is None:
            from .base import untitled_title

            self._untitled = untitled_title("flow")
        return self._untitled

    @property
    def path(self) -> Optional[str]:
        return self._path

    @property
    def dirty(self) -> bool:
        return not self.undo.isClean()

    def views(self) -> list[str]:
        return list(VIEWS)

    def current_view(self) -> str:
        return VIEWS[self.stack.currentIndex()]

    def set_view(self, name: str) -> None:
        index = VIEWS.index(name)
        if self.stack.currentIndex() == 1 and index != 1 and not self._leave_yaml():
            for position, action in enumerate(self.view_actions):
                action.setChecked(position == 1)
            return
        if index == 1:
            if self.stack.currentIndex() != 1:
                self._yaml_session += 1  # a new visit: a new undo step
            self._show_yaml()
        elif index == 2:
            self.python_edit.setPlainText(to_python(self.root_flow(), self.registry) if self.parent_document is None
                                          else to_python(self.flow, self.registry))
        self.stack.setCurrentIndex(index)
        for position, action in enumerate(self.view_actions):
            action.setChecked(position == index)
        self.document_changed.emit()  # (the node palette shows with the graph)
        if index == 0:
            QTimer.singleShot(0, self._fit_once)

    def toolbar(self) -> QToolBar:
        return self._toolbar

    def undo_stack(self):
        return self.undo


    def can_save(self) -> bool:
        return True

    def root_flow(self) -> Flow:
        return self.parent_document.root_flow() if self.parent_document is not None else self.flow

    def save(self) -> bool:
        if self.parent_document is not None:
            saved = self.parent_document.save()
            if saved:
                self.undo.setClean()
            return saved
        if not self._path:
            return self.save_as()
        if self._path.endswith(".py"):
            # the script may hold more than the flow (loops, own nodes, comments): never overwrite it
            return self.save_as()
        return self._write(self._path)

    def save_as(self) -> bool:
        if self.parent_document is not None:
            return self.parent_document.save_as()
        if self._path:
            start = self._path[:-3] + ".flow.yaml" if self._path.endswith(".py") else self._path
        else:
            start = os.path.join(self.project.flows_dir if self.project else default_folder(),
                                 f"{self.flow.name}.flow.yaml")
        path, _ = QFileDialog.getSaveFileName(self, "Save flow", start, FLOW_FILE_FILTER)
        if not path:
            return False
        if not path.endswith((".flow.yaml", ".py")):
            path += ".flow.yaml"
        return self._write(path)

    def _write(self, path: str) -> bool:
        try:
            if path.endswith(".py"):
                atomic_write(path, to_python(self.flow, self.registry))
            else:
                yaml_io.save(self.flow, path)
        except OSError as error:
            messages.error(self, "Save flow", f"{os.path.basename(path)} could not be saved.", str(error))
            return False
        self._path = path
        self.undo.setClean()
        self.document_changed.emit()
        return True

    # ================================================================ panels
    def panels(self) -> list[tuple[str, Any]]:
        """The panels that use this flow, as ``(title, target)``: the open panel documents made for it
        or bound to its file (``target`` is the document), then the panel files of the project - or of
        the flow's folder - that name its file (``target`` is the path)."""
        from ...lab import panel_model

        own = os.path.realpath(self._path) if self._path else None
        found: list[tuple[str, Any]] = []
        open_paths = set()
        for document in self.open_panels() if self.open_panels is not None else []:
            flow_file = document.flow_file()
            if document.flow_document is self or (own and flow_file and os.path.realpath(flow_file) == own):
                found.append((document.panel.name or document.title, document))
                if document.path:
                    open_paths.add(os.path.realpath(document.path))
        if own is None:
            return found
        if self.project is not None:
            files = self.project.panels()
        else:
            folder = os.path.dirname(own)
            files = [os.path.join(folder, name) for name in sorted(os.listdir(folder))
                     if name.endswith(panel_model.PANEL_SUFFIX)] if os.path.isdir(folder) else []
        for path in files:
            if os.path.realpath(path) in open_paths:
                continue
            try:
                panel = panel_model.load(path)
            except (OSError, panel_model.PanelError):
                panel = None  # a broken panel file is not listed
            flow_file = panel_model.flow_path(panel, path) if panel is not None else None
            if flow_file and os.path.realpath(flow_file) == own:
                found.append((panel.name or os.path.basename(path), path))
        return found

    def _fill_panels_menu(self) -> None:
        menu = self.panels_menu
        menu.clear()
        panels = self.panels()
        for title, target in panels:
            action = menu.addAction(icon("panel"), title)
            action.setToolTip(target if isinstance(target, str) else "open")
            action.triggered.connect(lambda _checked=False, target=target: self.panel_open_requested.emit(target))
        if not panels:
            menu.addAction("No panel uses this flow yet").setEnabled(False)
        menu.addSeparator()
        menu.addAction(icon("plus"), "New panel for this flow", self.panel_requested.emit)

    def inspector_widget(self, selection=None) -> Optional[QWidget]:
        if self._inspector is None:
            self._inspector = self._build_inspector()
        return self._inspector

    def _show_values(self, node: str, port: str, values: list) -> None:
        self.scene.show_values(node, port, values)
        if node in self._data_nodes:
            self._data_nodes.discard(node)
            self.scene.update_data([node])

    def _collect_values(self, node: str, port: str, times: list, values: list) -> None:
        self.run_data.add(node, port, values, times)
        self._data_nodes.add(node)

    # ============================================================== run data
    def _toggle_node_data(self) -> None:
        self.scene.show_data = self.action_show_data.isChecked()
        self.scene.update_data()

    def clear_run_data(self) -> None:
        self.run_data.clear()
        self.scene.update_data()
        self._refresh_data_views()

    def node_data_ports(self, node_id: str) -> list[str]:
        """``node.port`` of the data a node shows (its outputs, or what arrives at a view node)."""
        from ..flow import preview

        return [item.key for item in preview.entries(self.run_data, self.flow, node_id)]

    def open_node_data(self, node_id: str, port: Optional[str] = None) -> bool:
        """Show the data of ``node_id`` (or of its output ``port``): captures, signals and numbers in
        a data view, a table or text in a view of its own."""
        keys = [f"{node_id}.{port}"] if port else self.node_data_ports(node_id)
        if not keys:
            self.message.emit(f"{node_id} has no data yet: run the flow")
            return False
        viewable = [key for key in keys if (item := self.run_data.get(*key.split(".", 1))) is not None and item.viewable]
        if viewable:
            key = f"node:{node_id}" + (f".{port}" if port else "")
            self.data_views[key] = viewable
            self._emit_data_view(key, True)
            return True
        item = self.run_data.get(*keys[0].split(".", 1))
        value = item.latest
        if isinstance(value, signals.Event):
            value = value.to_table()
        kind = "table" if isinstance(value, signals.Table) else "log"
        self.view_requested.emit(keys[0], kind, value, {"title": keys[0]})
        return True

    def open_run_data(self) -> bool:
        """All data of the run in one data view (it follows the run while it is open)."""
        if not any(item.viewable for item in self.run_data.ports()):
            self.message.emit("There is no data yet: run the flow")
            return False
        self.data_views["run"] = None
        self._emit_data_view("run", True)
        return True

    def _emit_data_view(self, key: str, front: bool = False) -> None:
        ports = self.data_views.get(key)
        session = self.run_data.to_session(ports)
        if session is None:
            return
        if key == "run":
            title = f"{self.title} – run data"
        else:
            title = f"{self.title} – {key[5:]}"
        self.data_requested.emit(key, title, session, front)

    def _refresh_data_views(self) -> None:
        if self.run_data.version == self._data_seen:
            return
        self._data_seen = self.run_data.version
        for key in list(self.data_views):
            self._emit_data_view(key)

    def data_view_closed(self, key: str) -> None:
        self.data_views.pop(key, None)

    def shutdown(self) -> None:
        running = self.runner.running
        self.runner.detach()  # what the ending run still reports does not reach a closed document
        if running:
            self.runner.stop()
            self.runner.wait(5)
        if self.parent_document is not None and self in self.parent_document.children:
            self.parent_document.children.remove(self)

    # ============================================================== toolbar
    def _action(self, text: str, icon_name: str, slot, shortcut: Optional[str] = None, checkable: bool = False) -> QAction:
        action = QAction(icon(icon_name), text, self)
        action.setCheckable(checkable)
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
            action.setShortcutContext(Qt.WidgetWithChildrenShortcut)
            self.addAction(action)
        action.triggered.connect(lambda _checked=False: slot())
        return action

    def _build_toolbar(self) -> QToolBar:
        from ..widgets.toolbar import HIGH, LOW, NORMAL, AdaptiveToolBar

        bar = AdaptiveToolBar("flow", "Flow", self)
        self.view_actions = []
        for index, (name, icon_name) in enumerate(zip(VIEWS, ("nodes", "file-plus", "terminal"))):
            action = self._action(name, icon_name, lambda name=name: self.set_view(name), checkable=True)
            action.setChecked(index == 0)
            bar.add_action(f"view-{name.lower()}", action, "view", HIGH)
            self.view_actions.append(action)
        self.action_run = self._action("Run", "play", self.start_run, "F5")
        self.action_pause = self._action("Pause", "pause", self.pause_run)
        self.action_step = self._action("Step", "arrow-right", self.step_run, "F10")
        self.action_stop = self._action("Stop", "stop", self.stop_run, "Shift+F5")
        bar.add_action("run", self.action_run, "run", HIGH, pinned=True)
        bar.add_action("pause", self.action_pause, "run", NORMAL, text=False)
        bar.add_action("step", self.action_step, "run", NORMAL, text=False)
        bar.add_action("stop", self.action_stop, "run", HIGH, text=False)
        self.action_run_data = self._action("Run data", "channels", self.open_run_data)
        self.action_run_data.setToolTip("All data of the run in one data view (the nodes show theirs; "
                                        "double-click a node to open its data)")
        bar.add_action("run-data", self.action_run_data, "data", NORMAL)
        self.fast_box = QCheckBox("Fast (virtual time)", bar)
        self.fast_box.setToolTip("Run as fast as possible in virtual time (simulators only)")
        bar.add_widget("fast", self.fast_box, "Fast (virtual time)", "data", LOW)
        self.sim_box = QCheckBox("Simulate devices", bar)
        self.sim_box.setToolTip("Use simulators for every device of the flow")
        bar.add_widget("simulate", self.sim_box, "Simulate devices", "data", LOW)
        self.action_add = self._action("Add node...", "plus", lambda: self.view.open_search(""))
        self.action_add.setToolTip("Add a node (Tab, or type its name on the canvas)")
        bar.add_action("add", self.action_add, "edit", HIGH, title="Add node")
        self.action_panel = self._action("Panel", "panel", self.panel_requested.emit)
        self.action_panel.setToolTip("A front panel for this flow: buttons, sliders and displays bound to its ports")
        bar.add_action("panel", self.action_panel, "edit", NORMAL)
        #: the panels that use this flow, on the arrow of the Panel button
        self.panels_menu = QMenu("Panels of this flow", self)
        self.panels_menu.aboutToShow.connect(self._fill_panels_menu)
        self.action_panel.setMenu(self.panels_menu)
        self.action_comment = self._action("Comment", "pencil", lambda: self.add_node("structure.comment", self._center()))
        self.action_group = self._action("Group", "layers", self.group_selection)
        self.action_validate = self._action("Check", "checklist", self.report_problems)
        bar.add_action("comment", self.action_comment, "edit", LOW, text=False)
        bar.add_action("group", self.action_group, "edit", LOW, text=False)
        bar.add_action("check", self.action_validate, "edit", NORMAL, text=False)
        self.action_fit = self._action("Fit", "fit", lambda: self.view.fit(), "Ctrl+0")
        self.action_fit.setToolTip("Zoom to fit the whole flow (Ctrl+0)")
        self.action_zoom_in = self._action("Zoom in", "zoom-in", lambda: self.view.zoom(1.25), "Ctrl++")
        self.action_zoom_in.setShortcuts([QKeySequence(QKeySequence.ZoomIn), QKeySequence("Ctrl+=")])
        self.action_zoom_out = self._action("Zoom out", "zoom-out", lambda: self.view.zoom(0.8), "Ctrl+-")
        self.action_zoom_out.setShortcuts([QKeySequence(QKeySequence.ZoomOut)])
        self.action_actual_size = self._action("Actual size", "fit", lambda: self.view.actual_size())
        self.action_align_left = self._action("Align left", "align", lambda: self.align("left"))
        self.action_align_top = self._action("Align top", "align", lambda: self.align("top"))
        self.action_distribute = self._action("Distribute", "rows", lambda: self.align("distribute"))
        self.action_arrange = self._action("Arrange", "rows", self.arrange, "Ctrl+Shift+L")
        self.action_arrange.setToolTip("Arrange the nodes along their wires (the selected ones, or all) – Ctrl+Shift+L")
        self.action_auto_arrange = self._action(
            "Auto-arrange", "rows", lambda: self.set_auto_arrange(self.action_auto_arrange.isChecked()), checkable=True)
        self.action_auto_arrange.setChecked(bool(preferences.get("flow.auto_arrange")))
        self.action_auto_arrange.setIconText("Auto")
        self.action_auto_arrange.setToolTip("Arrange the nodes again whenever nodes or wires are added or removed "
                                            "(nodes moved by hand stay until then)")
        bar.add_action("fit", self.action_fit, "canvas", NORMAL, text=False)
        bar.add_action("zoom-in", self.action_zoom_in, "canvas", LOW, text=False)
        bar.add_action("zoom-out", self.action_zoom_out, "canvas", LOW, text=False)
        bar.add_action("arrange", self.action_arrange, "layout", NORMAL)
        bar.add_action("auto-arrange", self.action_auto_arrange, "layout", NORMAL, title="Auto-arrange")
        bar.add_action("align-left", self.action_align_left, "layout", LOW, text=False, default=False)
        bar.add_action("align-top", self.action_align_top, "layout", LOW, text=False, default=False)
        bar.add_action("distribute", self.action_distribute, "layout", LOW, text=False, default=False)
        for action in (self.action_pause, self.action_step, self.action_stop, self.action_fit, self.action_zoom_in,
                       self.action_zoom_out, self.action_arrange, self.action_align_left, self.action_align_top,
                       self.action_distribute, self.action_comment, self.action_group, self.action_validate):
            if not action.toolTip() or action.toolTip() == action.text():
                action.setToolTip(action.text())
        bar.finish()
        self.action_subflow = self._action("Make subflow", "nodes", lambda: self.make_subflow(self.flow.free_id("sub")))
        self.action_cut = self._action("Cut", "cut", self.cut_selection, QKeySequence(QKeySequence.Cut).toString())
        self.action_copy = self._action("Copy", "copy", self.copy_selection, QKeySequence(QKeySequence.Copy).toString())
        self.action_paste = self._action("Paste", "paste", self.paste, QKeySequence(QKeySequence.Paste).toString())
        self.action_duplicate = self._action("Duplicate", "copy", self.duplicate_selection, "Ctrl+D")
        self.action_select_all = self._action("Select all", "checklist", self.select_all,
                                              QKeySequence(QKeySequence.SelectAll).toString())

        self.flow_menu = QMenu("F&low", self)
        for action in (self.action_run, self.action_pause, self.action_step, self.action_stop):
            self.flow_menu.addAction(action)
        self.flow_menu.addSeparator()
        for action in (self.action_add, self.action_comment, self.action_group, self.action_subflow, self.action_panel):
            self.flow_menu.addAction(action)
        self.flow_menu.addSeparator()
        self.action_show_data = self._action("Show data in the nodes", "chart", self._toggle_node_data, checkable=True)
        self.action_show_data.setChecked(True)
        self.action_clear_data = self._action("Clear run data", "clear", self.clear_run_data)
        for action in (self.action_run_data, self.action_show_data, self.action_clear_data):
            self.flow_menu.addAction(action)
        self.flow_menu.addSeparator()
        for action in (self.action_cut, self.action_copy, self.action_paste, self.action_duplicate,
                       self.action_select_all):
            self.flow_menu.addAction(action)
        self.flow_menu.addSeparator()
        for action in (self.action_arrange, self.action_align_left, self.action_align_top, self.action_distribute):
            self.flow_menu.addAction(action)
        self.flow_menu.addSeparator()
        for action in (self.action_zoom_in, self.action_zoom_out, self.action_fit, self.action_actual_size):
            self.flow_menu.addAction(action)
        self.flow_menu.addSeparator()
        for action in self.view_actions:
            self.flow_menu.addAction(action)
        self.flow_menu.addAction(self.action_validate)
        self._update_run_actions()
        return bar

    def document_menus(self) -> list:
        return [self.flow_menu]

    def _fit_once(self) -> None:
        if not self._fitted:
            self._fitted = True
            self.view.fit()

    @property
    def data_dir(self) -> str:
        """Where the flow writes its files: the project's ``data/``, the folder of the flow file, or
        a scratch folder while the flow is not saved."""
        if self.project is not None:
            return self.project.data_dir
        if self._path:
            return os.path.dirname(os.path.abspath(self._path))
        if getattr(self, "_scratch", None) is None:
            import tempfile

            self._scratch = tempfile.mkdtemp(prefix="openscilab-flow-")
        return self._scratch

    def addresses(self) -> list[tuple[str, str]]:
        """``(label, address)`` a device node can take: open instruments, simulators."""
        return device_addresses(self.hub)

    def _center(self) -> tuple[float, float]:
        """Where a node added without a place goes: the middle of the view, beside nodes already there."""
        center = self.view.mapToScene(self.view.viewport().rect().center())
        return self.free_position((center.x() - 90, center.y() - 30))

    def free_position(self, position: tuple[float, float]) -> tuple[float, float]:
        """``position``, moved down and right until no node starts there (nodes do not pile up)."""
        x, y = position
        taken = [node.position for node in self.flow.nodes.values() if node.position]
        while any(abs(x - other[0]) < 24 and abs(y - other[1]) < 24 for other in taken):
            x, y = x + 32, y + 32
        return x, y

    # ============================================================= editing
    def _edit(self, text: str, change, merge_key: Optional[tuple] = None, merge_id: int = -1) -> bool:
        """Apply ``change(flow)`` to a copy of the model and push it as one undo step."""
        if self.parent_document is not None:
            name = self.subflow_name
            key = ("subflow", name) + merge_key if merge_key is not None else None
            if name not in self.parent_document.flow.subflows:
                messages.warning(self, "Flow", f"The subflow '{name}' is no longer part of its flow.")
                return False
            return self.parent_document._edit(text, lambda root: change(root.subflows[name]), key, merge_id)
        before = self.flow.copy()
        after = self.flow.copy()
        try:
            change(after)
        except (FlowError, RegistryError, ValueError) as error:
            messages.warning(self, "Flow", str(error))
            return False
        self._set_flow(after)
        self.undo.push(FlowEdit(text, before, after.copy(), self._set_flow, merge_key, merge_id))
        self._after_edit(text)
        return True

    # --------------------------------------------------------- auto-arrange
    #: edits that only place nodes: they never make the flow arrange itself
    PLACING = ("Arrange", "Move", "Align")

    def _structure(self) -> tuple:
        """What arranging depends on: the nodes, the wires and how tall the nodes are."""
        return (tuple(sorted(self.flow.nodes)), tuple(sorted(str(edge) for edge in self.flow.edges)),
                tuple(round(self.scene.node_height(node_id)) for node_id in sorted(self.flow.nodes)))

    @property
    def auto_arrange(self) -> bool:
        return self.action_auto_arrange.isChecked()

    def set_auto_arrange(self, enabled: bool) -> None:
        """Arrange the nodes whenever nodes or wires are added or removed (kept as a preference)."""
        self.action_auto_arrange.setChecked(bool(enabled))
        preferences.update({"flow.auto_arrange": bool(enabled)})
        if enabled:
            self._arrange_automatically()

    def _after_edit(self, text: str) -> None:
        if not self.auto_arrange or text in self.PLACING or getattr(self, "_arranging", False):
            return
        if self._structure() != getattr(self, "_arranged", None):
            QTimer.singleShot(0, self._arrange_automatically)

    def _arrange_automatically(self) -> None:
        try:
            if self._structure() == getattr(self, "_arranged", None):
                return
        except RuntimeError:  # the document was closed meanwhile
            return
        self._arranging = True
        try:
            self.arrange(everything=True, fit=False)
        finally:
            self._arranging = False
            self._arranged = self._structure()

    def _set_flow(self, flow: Flow) -> None:
        self.flow = flow
        self.refresh()
        for child in list(self.children):
            try:
                if child.subflow_name in flow.subflows:
                    child._set_flow(flow.subflows[child.subflow_name])
            except RuntimeError:  # the child document was deleted
                self.children.remove(child)

    def refresh(self) -> None:
        """Bring every view up to the model."""
        self.scene.sync(self.flow)
        self._refresh_node_editors()
        self.scene.set_breakpoints(self.breakpoints)
        if self.stack.currentIndex() == 1 and not self.yaml_edit.hasFocus():
            self._show_yaml()
        elif self.stack.currentIndex() == 2:
            self.python_edit.setPlainText(to_python(self.flow, self.registry))
        self._inspector_refresh()
        problems = self.problems()
        self.scene.set_problems(problems)
        self.problems_changed.emit(problems)
        self.document_changed.emit()

    def add_node(self, type_name: str, position: tuple[float, float] = (0.0, 0.0), node_id: Optional[str] = None,
                 **params) -> Optional[str]:
        """Place a node of ``type_name`` (with its default parameters) at ``position``."""
        try:
            spec = self.flow.spec(Node("x", type_name), self.registry) if not type_name.startswith(SUBFLOW_PREFIX) \
                else None
        except RegistryError as error:
            messages.warning(self, "Add node", str(error))
            return None
        created: list[str] = []
        wired: list[str] = []

        def change(flow: Flow) -> None:
            devices = list(flow.devices)
            if node_id is not None and node_id in flow.nodes:
                identifier = flow.free_id(node_id)
            else:
                identifier = node_id
            values = dict(params)
            if type_name == DEVICE_NODE and not values.get("address"):
                # without an address the node is the project's device of its name; elsewhere it
                # starts as the default device (a simulator), as the inspector shows it
                name = identifier or flow.free_id(type_name.rsplit(".", 1)[-1])
                project_devices = self.project.devices if self.project is not None else {}
                default = spec.param("address").default if spec is not None and spec.param("address") else None
                if name not in project_devices and default:
                    values["address"] = default
            node = flow.add_node(type_name, identifier, **values)
            node.position = (round(position[0], 2), round(position[1], 2))
            created.append(node.id)
            if spec is not None and len(devices) == 1:
                # a node that uses a device is wired to the only device of the flow
                inputs, _outputs = spec.resolve_ports(node.params)
                for port in inputs:
                    if port.type == signals.DEVICE:
                        flow.connect(f"{devices[0]}.device", f"{node.id}.{port.name}")
                        wired.append(f"{node.id}.{port.name} → {devices[0]}")
            if type_name == DEVICE_NODE and not devices:
                # the first device: the nodes waiting for one are wired to it
                for other_id, other in flow.nodes.items():
                    if other_id == node.id:
                        continue
                    other_spec = self.scene.spec_of(other)
                    if other_spec is None:
                        continue
                    other_inputs, _ = other_spec.resolve_ports(other.params)
                    for port in other_inputs:
                        if port.type == signals.DEVICE and not flow.edges_into(other_id, port.name):
                            flow.connect(f"{node.id}.device", f"{other_id}.{port.name}")
                            wired.append(f"{other_id}.{port.name} → {node.id}")

        if not self._edit(f"Add {type_name}", change):
            return None
        self.scene.select_nodes(created)
        if wired:
            self.message.emit("Wired to the device: " + ", ".join(wired))
        return created[0]

    def connect_ports(self, source: str, target: str) -> tuple[bool, str, str]:
        ok, suggestion = self.flow.can_connect(source, target, self.registry)
        if not ok:
            source_type, target_type = self.flow.port_types(Edge(PortRef.parse(source), PortRef.parse(target)), self.registry)
            if source_type is None or target_type is None:
                return False, "unknown port", ""
            return False, f"{source_type} does not fit {target_type}", suggestion
        target_ref = PortRef.parse(target)
        target_port = self.flow.spec(self.flow.nodes[target_ref.node], self.registry).input(
            target_ref.port, self.flow.nodes[target_ref.node].params)

        def change(flow: Flow) -> None:
            if target_port is not None and not target_port.multiple:
                for edge in flow.edges_into(target_ref.node, target_ref.port):
                    flow.disconnect(edge)  # a new wire replaces the old one
            flow.connect(source, target)

        self._edit(f"Connect {source} → {target}", change)
        return True, "", ""

    def insert_conversion(self, source: str, target: str, node_type: str) -> Optional[str]:
        """Wire ``source`` through a new ``node_type`` node to ``target``."""
        spec = self.registry.find(node_type)
        if spec is None:
            messages.warning(self, "Convert", f"There is no node {node_type}.")
            return None
        source_ref, target_ref = PortRef.parse(source), PortRef.parse(target)
        inputs, outputs = spec.resolve_ports({})
        created: list[str] = []
        # set as the conversion does it (a compare at or above 0.5, the first analog channel)
        params: dict = {}
        try:
            source_node, target_node = self.flow.nodes[source_ref.node], self.flow.nodes[target_ref.node]
            source_port = self.flow.spec(source_node, self.registry).output(source_ref.port, source_node.params)
            target_port = self.flow.spec(target_node, self.registry).input(target_ref.port, target_node.params)
            found = signals.conversion(source_port.type, target_port.type) if source_port and target_port else None
            if found is not None and found.node == node_type:
                params = dict(found.params)
        except (KeyError, RegistryError):
            pass

        def change(flow: Flow) -> None:
            first = flow.nodes[source_ref.node].position or (0.0, 0.0)
            second = flow.nodes[target_ref.node].position or (first[0] + 400, first[1])
            node = flow.add_node(node_type, **params)
            node.position = ((first[0] + second[0]) / 2, (first[1] + second[1]) / 2 + 40)
            created.append(node.id)
            flow.connect(source, f"{node.id}.{inputs[0].name}")
            flow.connect(f"{node.id}.{outputs[0].name}", target)

        self._edit(f"Insert {node_type}", change)
        return created[0] if created else None

    def _wire_rejected(self, source: str, target: str, message: str, suggestion: str) -> None:
        """A wire that does not fit: said above the graph; a conversion is one click away."""
        if suggestion:
            self._conversion = (source, target, suggestion)
            self.show_run_banner(f"{message}: a {suggestion} node turns one into the other.", "warning", "convert")
            return
        self.show_run_banner(f"{message}: the ports do not fit.", "warning")

    def wire_dropped(self, port: str, is_input: bool, type_name: str, position: tuple[float, float]) -> None:
        """A wire dropped on the empty canvas: choose a node it fits; it is placed there and wired."""
        def fitting(spec) -> Optional[str]:
            try:
                inputs, outputs = spec.resolve_ports({})
            except Exception:  # noqa: BLE001 - ports that need parameters
                log.debug("inputs, outputs = spec.resolve_ports({}) failed: ports that need parameters", exc_info=True)
                return None
            candidates = outputs if is_input else inputs
            for candidate in candidates:
                source, target = (candidate.type, type_name) if is_input else (type_name, candidate.type)
                if signals.compatible(source, target):
                    return candidate.name
            return None

        def place(type_name_chosen: str, at) -> None:
            spec = self.registry.get(type_name_chosen)
            node_id = self.add_node(type_name_chosen, (at.x(), at.y() - 20))
            other = fitting(spec) if spec is not None else None
            if node_id and other:
                source, target = (f"{node_id}.{other}", port) if is_input else (port, f"{node_id}.{other}")
                self.connect_ports(source, target)

        from PySide6.QtCore import QPointF

        self.view.open_search("", QPointF(*position), accepts=lambda spec: fitting(spec) is not None,
                              chosen=place, title=f"Connect a node to {port}")

    def disconnect_edge(self, edge: Edge) -> None:
        self._edit(f"Remove {edge}", lambda flow: flow.disconnect(edge))

    def delete_items(self, node_ids: list[str], edges: list[Edge]) -> None:
        if not node_ids and not edges:
            return

        def change(flow: Flow) -> None:
            for edge in edges:
                flow.disconnect(edge)
            for node_id in node_ids:
                flow.remove_node(node_id)

        self._edit("Delete", change)
        self.breakpoints -= set(node_ids)

    def positions_changed(self, positions: dict[str, tuple[float, float]]) -> None:
        def change(flow: Flow) -> None:
            for node_id, position in positions.items():
                if node_id in flow.nodes:
                    flow.nodes[node_id].position = position

        self._edit("Move", change)  # one drag is one step; two drags are two

    def set_param(self, node_id: str, name: str, value: Any) -> None:
        def change(flow: Flow) -> None:
            node = flow.nodes[node_id]
            if value is None:
                node.params.pop(name, None)
            else:
                node.params[name] = value

        self._edit(f"Set {node_id}.{name}", change, merge_key=("param", node_id, name), merge_id=MERGE_PARAM)

    def rename_node(self, old: str, new: str) -> None:
        editor = (self.__dict__.get("_node_editors") or {}).get(old)
        if editor is not None:
            editor.renamed_to = new  # (its window follows the node)
        if self._edit(f"Rename {old}", lambda flow: flow.rename_node(old, new)):
            if old in self.breakpoints:
                self.breakpoints.discard(old)
                self.breakpoints.add(new)
            self.scene.select_nodes([new])

    def set_comment(self, node_id: str, text: str) -> None:
        def change(flow: Flow) -> None:
            flow.nodes[node_id].comment = text

        if self.flow.nodes.get(node_id) is not None and self.flow.nodes[node_id].comment != text:
            self._edit(f"Comment {node_id}", change)

    def align(self, how: str) -> None:
        items = self.scene.selected_nodes()
        if len(items) < 2:
            return
        positions = {item.node_id: (item.pos().x(), item.pos().y()) for item in items}
        if how == "left":
            x = min(x for x, _y in positions.values())
            positions = {key: (x, y) for key, (_x, y) in positions.items()}
        elif how == "top":
            y = min(y for _x, y in positions.values())
            positions = {key: (x, y) for key, (x, _y) in positions.items()}
        else:
            ordered = sorted(positions.items(), key=lambda item: item[1][0])
            left, right = ordered[0][1][0], ordered[-1][1][0]
            step = (right - left) / (len(ordered) - 1)
            positions = {key: (left + index * step, y) for index, (key, (_x, y)) in enumerate(ordered)}

        def change(flow: Flow) -> None:
            for node_id, position in positions.items():
                flow.nodes[node_id].position = (round(position[0], 2), round(position[1], 2))

        self._edit("Align", change)

    def arrange(self, everything: bool = False, fit: bool = True) -> None:
        """Place the selected nodes (two or more), or all, along their wires (one undo step)."""
        from ...lab import layout

        selected = [] if everything else [item.node_id for item in self.scene.selected_nodes()]
        chosen = selected if len(selected) > 1 else list(self.flow.nodes)
        if not chosen:
            return
        positions = layout.layered(chosen, layout.flow_edges(self.flow), self.scene.node_height, NODE_WIDTH,
                                   port=self.scene.port_offset)
        if len(chosen) < len(self.flow.nodes):
            # the selection stays where it was: its top left corner does not move
            left = min(self.flow.nodes[node_id].position[0] if self.flow.nodes[node_id].position else
                       self.scene.nodes[node_id].pos().x() for node_id in chosen)
            top = min(self.flow.nodes[node_id].position[1] if self.flow.nodes[node_id].position else
                      self.scene.nodes[node_id].pos().y() for node_id in chosen)
            positions = {node_id: (x + left, y + top) for node_id, (x, y) in positions.items()}
        else:
            positions = layout.arrange(self.flow, self.scene.node_height, NODE_WIDTH, self.scene.port_offset)

        def change(flow: Flow) -> None:
            for node_id, (x, y) in positions.items():
                if node_id in flow.nodes:
                    flow.nodes[node_id].position = (round(x / 8) * 8.0, round(y / 8) * 8.0)

        if self._edit("Arrange", change) and fit:
            self.view.fit()

    def group_selection(self) -> Optional[str]:
        """A group frame around the selected nodes (or an empty one in the middle)."""
        items = self.scene.selected_nodes()
        if not items:
            return self.add_node("structure.group", self._center())
        rect = items[0].sceneBoundingRect()
        for item in items[1:]:
            rect = rect.united(item.sceneBoundingRect())
        rect = rect.adjusted(-20, -36, 20, 20)
        return self.add_node("structure.group", (rect.left(), rect.top()),
                             size=[round(rect.width()), round(rect.height())])

    def toggle_breakpoint(self, node_id: str) -> None:
        self.set_breakpoint(node_id, node_id not in self.breakpoints)

    def set_breakpoint(self, node_id: str, enabled: bool) -> None:
        if enabled:
            self.breakpoints.add(node_id)
        else:
            self.breakpoints.discard(node_id)
        self.scene.set_breakpoints(self.breakpoints)
        self.runner.set_breakpoint(node_id, enabled)
        if isinstance(self._inspector, NodeInspector) and self._inspector.node.id == node_id:
            self._inspector.breakpoint_box.blockSignals(True)
            self._inspector.breakpoint_box.setChecked(enabled)
            self._inspector.breakpoint_box.blockSignals(False)

    def open_node(self, node_id: str) -> None:
        node = self.flow.nodes.get(node_id)
        if node is None:
            return
        if node.type.startswith(SUBFLOW_PREFIX):
            name = node.type[len(SUBFLOW_PREFIX):]
            if name in self.flow.subflows:
                for child in self.children:
                    if child.subflow_name == name:
                        self.open_requested.emit(child)
                        return
                document = FlowDocument(self.flow.subflows[name], registry=self.registry, hub=self.hub,
                                        project=self.project, parent_document=self, subflow_name=name)
                self.open_requested.emit(document)

    # ============================================================ clipboard
    def selection_as_flow(self) -> Optional[Flow]:
        """The selected nodes with the wires between them (``None`` when nothing is selected)."""
        selected = [item.node_id for item in self.scene.selected_nodes()]
        if not selected:
            return None
        part = Flow("clipboard")
        for node_id in selected:
            node = self.flow.nodes[node_id]
            part.nodes[node_id] = Node(node.id, node.type, dict(node.params), node.position, node.comment)
        part.edges = [edge for edge in self.flow.edges if edge.source.node in selected and edge.target.node in selected]
        return part

    def copy_selection(self) -> bool:
        part = self.selection_as_flow()
        if part is None:
            return False
        # the nodes as the YAML of a flow, plain text: pasted here, or into a *.flow.yaml
        QApplication.clipboard().setText(yaml_io.dumps(part))
        return True

    def cut_selection(self) -> bool:
        if not self.copy_selection():
            return False
        self.delete_items([item.node_id for item in self.scene.selected_nodes()], [])
        return True

    def paste(self, text: Optional[str] = None, offset: tuple[float, float] = (40.0, 40.0)) -> list[str]:
        """Nodes from the clipboard (or ``text``): new names where taken, moved by ``offset``, selected."""
        if text is None:
            text = QApplication.clipboard().text()
        try:
            part = yaml_io.loads(text)
        except FlowError:
            return []
        if not part.nodes:
            return []
        created: list[str] = []

        def change(flow: Flow) -> None:
            names: dict[str, str] = {}
            for node_id, node in part.nodes.items():
                new_id = flow.free_id(node_id)
                names[node_id] = new_id
                x, y = node.position or (0.0, 0.0)
                flow.nodes[new_id] = Node(new_id, node.type, dict(node.params), (x + offset[0], y + offset[1]),
                                          node.comment)
                created.append(new_id)
            for edge in part.edges:
                flow.connect(f"{names[edge.source.node]}.{edge.source.port}",
                             f"{names[edge.target.node]}.{edge.target.port}")

        if not self._edit("Paste", change):
            return []
        self.scene.select_nodes(created)
        return created

    def paste_at(self, position) -> list[str]:
        """Paste so the first pasted node is at ``position`` (scene)."""
        text = QApplication.clipboard().text()
        try:
            part = yaml_io.loads(text)
        except FlowError:
            return []
        first = next((node.position for node in part.nodes.values() if node.position), (0.0, 0.0))
        return self.paste(text, (position.x() - first[0], position.y() - first[1]))

    def insert_on_wire(self, edge: Edge, position) -> None:
        """Choose a node to put into the wire ``edge`` (it gets the wire's value and passes one on)."""
        source_type, target_type = self.flow.port_types(edge, self.registry)

        def ports(spec) -> Optional[tuple[str, str]]:
            try:
                inputs, outputs = spec.resolve_ports({})
            except Exception:  # noqa: BLE001
                log.debug("inputs, outputs = spec.resolve_ports({}) failed (ignored)", exc_info=True)
                return None
            first_in = next((port.name for port in inputs if source_type and signals.compatible(source_type, port.type)),
                            None)
            first_out = next((port.name for port in outputs if target_type and signals.compatible(port.type, target_type)),
                             None)
            return (first_in, first_out) if first_in and first_out else None

        def place(type_name: str, at) -> None:
            names = ports(self.registry.get(type_name))
            if names is None:
                return
            created: list[str] = []

            def change(flow: Flow) -> None:
                flow.disconnect(edge)
                node = flow.add_node(type_name)
                node.position = (round(at.x(), 2), round(at.y(), 2))
                created.append(node.id)
                flow.connect(str(edge.source), f"{node.id}.{names[0]}")
                flow.connect(f"{node.id}.{names[1]}", str(edge.target))

            self._edit(f"Insert {type_name}", change)
            self.scene.select_nodes(created)

        self.view.open_search("", position, accepts=lambda spec: ports(spec) is not None, chosen=place,
                              title=f"Insert a node into {edge.source} → {edge.target}")

    def duplicate_selection(self) -> list[str]:
        part = self.selection_as_flow()
        return self.paste(yaml_io.dumps(part)) if part is not None else []

    def select_all(self) -> None:
        self.scene.select_nodes(list(self.flow.nodes))

    def make_subflow(self, name: str) -> Optional[str]:
        """Move the selected nodes into a new subflow and put a subflow node in their place."""
        selected = [item.node_id for item in self.scene.selected_nodes()]
        if not selected or name in self.flow.subflows:
            return None
        created: list[str] = []

        def change(flow: Flow) -> None:
            inner = Flow(name)
            for node_id in selected:
                inner.nodes[node_id] = flow.nodes[node_id]
            inner.edges = [edge for edge in flow.edges if edge.source.node in selected and edge.target.node in selected]
            outside_in = [edge for edge in flow.edges if edge.target.node in selected and edge.source.node not in selected]
            outside_out = [edge for edge in flow.edges if edge.source.node in selected and edge.target.node not in selected]
            for edge in outside_in:
                inner.inputs.setdefault(f"{edge.target.node}_{edge.target.port}", edge.target)
            for edge in outside_out:
                inner.outputs.setdefault(f"{edge.source.node}_{edge.source.port}", edge.source)
            positions = [node.position for node in inner.nodes.values() if node.position]
            for node_id in selected:
                flow.remove_node(node_id)
            flow.subflows[name] = inner
            node = flow.add_node(f"{SUBFLOW_PREFIX}{name}", flow.free_id(name))
            node.position = positions[0] if positions else (0.0, 0.0)
            created.append(node.id)
            for edge in outside_in:
                flow.connect(str(edge.source), f"{node.id}.{edge.target.node}_{edge.target.port}")
            for edge in outside_out:
                flow.connect(f"{node.id}.{edge.source.node}_{edge.source.port}", str(edge.target))

        self._edit(f"Subflow {name}", change)
        return created[0] if created else None

    # ============================================================ inspector
    def _selection_changed(self, node_ids: list[str]) -> None:
        self.selection = node_ids
        self._replace_inspector()
        self.document_changed.emit()

    def _replace_inspector(self) -> None:
        """A new inspector for what is selected now; the one before is deleted (it was only
        hidden: one more widget, with its connections to the flow, for every click)."""
        old, self._inspector = self._inspector, self._build_inspector()
        if old is not None:
            old.deleteLater()

    def _inspector_refresh(self) -> None:
        current = self._inspector
        if current is None:
            return
        focused = QApplication.focusWidget()
        if isinstance(current, NodeInspector) and current.node.id in self.flow.nodes and current.editors and \
                focused is not None and any(editor is focused or editor.isAncestorOf(focused)
                                            for editor in current.editors.values()):
            return  # do not rebuild under the cursor
        if isinstance(current, FlowInspector) and current.has_focus():
            current.flow = self.flow
            return  # typing the name or the description: the text field stays
        self._replace_inspector()

    def _node_inspector(self, node_id: str, wide: bool = False) -> NodeInspector:
        """The editors of the node ``node_id`` (the inspector; ``wide``: the node editor window)."""
        node = self.flow.nodes[node_id]
        try:
            spec = self.flow.spec(node, self.registry)
        except RegistryError:
            spec = None
        problems = [problem.text for problem in self.problems() if problem.node == node.id]
        inspector = NodeInspector(self.flow, node, spec, node.id in self.breakpoints, addresses=self.addresses(),
                                  problems=problems, running=self.runner.running,
                                  suggestions=self.suggestions_for(node.id), devices=list(self.flow.devices),
                                  folder=self.data_dir if self._path or self.project else "",
                                  base=self.project.root if self.project else "", wide=wide)
        inspector.param_changed.connect(self.set_param)
        inspector.renamed.connect(self.rename_node)
        inspector.comment_changed.connect(self.set_comment)
        inspector.breakpoint_toggled.connect(self.set_breakpoint)
        return inspector

    def edit_node(self, node_id: str):
        """The parameters of ``node_id`` in a window of their own (a double-click on the node)."""
        from ..flow.inspector import NodeEditor

        if node_id not in self.flow.nodes:
            return None
        editors = self.__dict__.setdefault("_node_editors", {})
        editor = editors.get(node_id)
        if editor is None:
            node = self.flow.nodes[node_id]
            try:
                title = self.flow.spec(node, self.registry).title
            except RegistryError:
                title = node.type
            editor = NodeEditor(node_id, lambda name: self._node_inspector(name, wide=True), f"{node_id} – {title}",
                                self)
            editor.finished.connect(lambda _result, editor=editor: editors.pop(editor.node_id, None))
            editors[node_id] = editor
        editor.show()
        editor.raise_()
        editor.activateWindow()
        return editor

    def _refresh_node_editors(self) -> None:
        """Node editor windows follow the flow: a renamed node keeps its window, a deleted one closes
        it, the others show the node as it is now (not while one is typed in)."""
        editors = self.__dict__.get("_node_editors") or {}
        for node_id, editor in list(editors.items()):
            if node_id not in self.flow.nodes:
                renamed = getattr(editor, "renamed_to", None)
                if renamed in self.flow.nodes:
                    editors.pop(node_id)
                    editor.node_id = renamed
                    editors[renamed] = editor
                    editor.setWindowTitle(editor.windowTitle().replace(node_id, renamed, 1))
                else:
                    editor.close()
                    editors.pop(node_id, None)
                    continue
            if not editor.has_focus():
                editor.rebuild()

    def _build_inspector(self) -> QWidget:
        if len(self.selection) == 1 and self.selection[0] in self.flow.nodes:
            return self._node_inspector(self.selection[0])
        inspector = FlowInspector(self.flow, panels=self.panels())
        inspector.panel_open_requested.connect(self.panel_open_requested.emit)
        inspector.new_panel_requested.connect(self.panel_requested.emit)
        inspector.name_changed.connect(lambda name: self._edit("Rename flow", lambda flow: setattr(flow, "name", name))
                                       if name != self.flow.name else None)
        inspector.description_changed.connect(lambda text: self._edit(
            "Description", lambda flow: setattr(flow, "description", text)) if text != self.flow.description else None)
        inspector.setting_changed.connect(self._set_setting)
        return inspector

    def device_hints(self) -> dict:
        """What the devices of the flow have (open instruments, simulator profiles), by device node."""
        from ...lab import hints

        return hints.flow_devices(self.flow, self.project.devices if self.project is not None else {},
                                  self.hub.instruments() if self.hub is not None else ())

    def suggestions_for(self, node_id: str) -> dict[str, list[str]]:
        """Values the inspector offers for the parameters of ``node_id`` (pins, channels, columns)."""
        from ...lab import hints

        node = self.flow.nodes.get(node_id)
        if node is None:
            return {}
        try:
            spec = self.flow.spec(node, self.registry)
        except RegistryError:
            return {}
        return hints.node_suggestions(self.flow, node_id, spec, self.device_hints())

    def problems(self) -> list:
        """What :meth:`Flow.validate` finds, and values that do not fit their kind or their device."""
        from ...lab import hints

        return self.flow.validate(self.registry) + hints.check(self.flow, self.registry, self.device_hints())

    def _set_setting(self, name: str, value: Any) -> None:
        if self.flow.settings.get(name) == value:
            return

        def change(flow: Flow) -> None:
            if value is None:
                flow.settings.pop(name, None)
            else:
                flow.settings[name] = value

        self._edit(f"Setting {name}", change)

    # =========================================================== yaml view
    def _leave_yaml(self) -> bool:
        """Take the YAML text in before another view shows; with an error the user decides."""
        if self.yaml_edit.toPlainText() == yaml_io.dumps(self.flow) or self.apply_yaml():
            return True
        choice = messages.choose(self, "YAML has an error", "The YAML text cannot be read, so the other views "
                                 "cannot show it.", ["Keep editing", "Discard the changes"], self.yaml_status.text())
        if choice == 1:
            self._show_yaml()
            return True
        return False

    def _show_yaml(self) -> None:
        self._updating_text = True
        try:
            text = yaml_io.dumps(self.flow)
            if self.yaml_edit.toPlainText() != text:
                self.yaml_edit.setPlainText(text)
            self.yaml_status.setText("")
        finally:
            self._updating_text = False

    def _yaml_completions(self, text: str):
        """Completions in the YAML view (from the flow as last read, its devices and node types)."""
        from ...lab.completion import yaml_completions

        return yaml_completions(text, self.flow, self.registry, self.device_hints(), self.addresses())

    def yaml_text(self) -> str:
        return self.yaml_edit.toPlainText() if self.stack.currentIndex() == 1 else yaml_io.dumps(self.flow)

    def _yaml_typed(self) -> None:
        if not self._updating_text:
            self._yaml_timer.start()

    def apply_yaml(self) -> bool:
        """Take the YAML text into the model (one undo step); errors stay in the text."""
        self._yaml_timer.stop()
        text = self.yaml_edit.toPlainText()
        try:
            flow = yaml_io.loads(text)
        except FlowError as error:
            self.yaml_status.setText(str(error))
            return False
        self.yaml_status.setText("")
        if yaml_io.dumps(flow) == yaml_io.dumps(self.flow):
            return True

        def replace(target: Flow) -> None:
            target.__dict__.update(flow.copy().__dict__)

        return self._edit("Edit YAML", replace, merge_key=("yaml", self._yaml_session), merge_id=MERGE_YAML)

    # ================================================================ menus
    def _context_menu(self, position) -> None:
        from ..flow.node_item import PortItem
        from ..flow.wire_item import WireItem

        scene_position = self.view.mapToScene(position)
        menu = QMenu(self)
        under = self.view.items(position)
        port = next((item for item in under if isinstance(item, PortItem)), None)
        wire = next((item for item in under if isinstance(item, WireItem)), None)
        items = [item for item in under if hasattr(item, "node_id")]
        if port is not None:
            edges = [edge for edge in self.flow.edges
                     if (edge.target if port.is_input else edge.source) == PortRef.parse(port.ref)]
            menu.addAction(icon("plus"), "Add a connected node...", lambda: self.wire_dropped(
                port.ref, port.is_input, port.spec.type,
                (scene_position.x() + (-260 if port.is_input else 80), scene_position.y())))
            action = menu.addAction(icon("clear"), "Disconnect all", lambda: self.delete_items([], edges))
            action.setEnabled(bool(edges))
            menu.exec(self.view.mapToGlobal(position))
            return
        if wire is not None and not items:
            edge = wire.edge
            menu.addAction(icon("plus"), "Insert a node...", lambda: self.insert_on_wire(edge, scene_position))
            menu.addAction(icon("trash"), "Remove wire", lambda: self.delete_items([], [edge]))
            menu.exec(self.view.mapToGlobal(position))
            return
        if items:
            node_id = items[0].node_id
            if node_id not in self.selection:
                self.scene.select_nodes([node_id])
            keys = self.node_data_ports(node_id)
            if keys:
                menu.addAction(icon("channels"), "Show data", lambda: self.open_node_data(node_id))
                if len(keys) > 1:
                    ports = menu.addMenu("Show data of")
                    for key in keys:
                        port = key.split(".", 1)[1]
                        owner = key.split(".", 1)[0]
                        ports.addAction(key, lambda owner=owner, port=port: self.open_node_data(owner, port))
                menu.addSeparator()
            menu.addAction(icon("target"), "Breakpoint", lambda: self.toggle_breakpoint(node_id))
            node = self.flow.nodes.get(node_id)
            if node is not None and node.type.startswith(SUBFLOW_PREFIX):
                menu.addAction("Open subflow", lambda: self.open_node(node_id))
            menu.addAction(icon("cut"), "Cut", self.cut_selection)
            menu.addAction(icon("copy"), "Copy", self.copy_selection)
            menu.addAction(icon("copy"), "Duplicate", self.duplicate_selection)
            menu.addAction(icon("trash"), "Delete", lambda: self.delete_items(
                [item.node_id for item in self.scene.selected_nodes()], []))
            if len(self.scene.selected_nodes()) > 1:
                menu.addAction(icon("layers"), "Group", self.group_selection)
                menu.addAction("Make subflow", lambda: self.make_subflow(self.flow.free_id("sub")))
        else:
            menu.addAction(icon("plus"), "Add node...", lambda: self.view.open_search("", scene_position))
            menu.addAction(icon("pencil"), "Comment", lambda: self.add_node(
                "structure.comment", (scene_position.x(), scene_position.y())))
            paste = menu.addAction(icon("paste"), "Paste", lambda: self.paste_at(scene_position))
            paste.setEnabled(bool(QApplication.clipboard().text()))
            menu.addAction(icon("checklist"), "Select all", self.select_all)
            if self.run_data:
                menu.addAction(icon("channels"), "Run data", self.open_run_data)
        wires = self.scene.selected_wires()
        if wires:
            menu.addAction("Remove wire", lambda: self.delete_items([], [wire.edge for wire in wires]))
        menu.exec(self.view.mapToGlobal(position))

    def report_problems(self) -> list:
        """*Check*: the problems go to the console (shown), the result into the banner."""
        problems = self.problems()
        self.problems_changed.emit(problems)
        errors = [problem for problem in problems if problem.severity == "error"]
        if problems:
            self.show_run_banner(f"{len(problems)} problem(s): " + "; ".join(str(problem) for problem in problems[:2])
                                 + (" …" if len(problems) > 2 else ""), "error" if errors else "warning", "problems")
        else:
            self.show_run_banner("No problems: the flow can run.", "info", "dismiss")
        self.console_requested.emit("Problems")
        return problems

    def show_run_banner(self, text: str, kind: str = "error", action: str = "dismiss") -> None:
        """A message above the flow (``""`` hides it); ``action``: what its button does
        (``problems``: show them, ``simulate``: run with simulators, ``dismiss``)."""
        from ..theme import set_role

        self._banner_kind = action
        set_role(self.run_banner, f"banner-{kind}")
        labels = {"problems": "Show problems", "simulate": "Run with simulators", "dismiss": "Dismiss"}
        if action == "convert" and getattr(self, "_conversion", None):
            labels["convert"] = f"Insert {self._conversion[2]}"
        self.run_banner.set_message(html.escape(text), labels.get(action) if text else None)

    def _banner_action(self) -> None:
        if self._banner_kind == "convert" and getattr(self, "_conversion", None):
            source, target, node_type = self._conversion
            self._conversion = None
            self.show_run_banner("")
            self.insert_conversion(source, target, node_type)
            return
        if self._banner_kind == "problems":
            self.console_requested.emit("Problems")
        elif self._banner_kind == "simulate":
            self.sim_box.setChecked(True)
            self.show_run_banner("")
            self.start_run()
            return
        self.show_run_banner("")

    # ================================================================== run
    def _update_run_actions(self) -> None:
        running = getattr(self, "runner", None) is not None and self.runner.running
        state = self.runner.state if running else "idle"
        self.action_run.setEnabled(not running or state == "paused")
        self.action_run.setText("Continue" if state == "paused" else "Run")
        self.action_pause.setEnabled(running and state != "paused")
        self.action_step.setEnabled(running and state == "paused")
        self.action_stop.setEnabled(running)

    @property
    def run_state(self) -> str:
        return self.runner.state if self.runner.running else "idle"

    def start_run(self) -> bool:
        if self.runner.running:
            if self.runner.state == "paused":
                self.runner.resume()
            return True
        if self.parent_document is not None:
            return self.parent_document.start_run()
        problems = [problem for problem in self.flow.validate(self.registry) if problem.severity == "error"]
        if problems:
            self.problems_changed.emit(problems)
            self.show_run_banner("The flow cannot run: " + "; ".join(str(problem) for problem in problems[:3])
                                 + (" …" if len(problems) > 3 else ""), "error", "problems")
            self.console_requested.emit("Problems")
            return False
        self.show_run_banner("")
        self.scene.reset_states()
        self.run_data.clear()
        self.scene.update_data()
        data_dir = self.data_dir
        flow = self.project.complete(self.flow.copy()) if self.project is not None else self.flow.copy()
        try:
            self.runner.start(flow, registry=self.registry, mode="virtual" if self.fast_box.isChecked() else "real",
                              simulate=self.sim_box.isChecked(), hub=self.hub, project=self.project, data_dir=data_dir,
                              base_dir=self.project.root if self.project else data_dir,
                              breakpoints=set(self.breakpoints))
        except FlowError as error:
            self.show_run_banner(f"The flow cannot run: {error}", "error")
            return False
        self.runner.views.shown.connect(lambda kind, node, value, options: self.view_requested.emit(
            node, kind, value, options))
        self.execution_line.emit(f"▶ {self.flow.name} ({'virtual' if self.fast_box.isChecked() else 'real'} time)")
        self._update_run_actions()
        self.run_state_changed.emit("running")
        self.console_requested.emit("Execution")
        self._data_timer.start()
        return True

    def pause_run(self) -> None:
        self.runner.pause()

    def step_run(self) -> None:
        self.runner.step()

    def stop_run(self) -> None:
        self.runner.stop()

    def _run_state(self, state: str, message: str) -> None:
        self._update_run_actions()
        self.run_state_changed.emit(state)
        if message:
            self.execution_line.emit(f"{state}: {message}")

    def _node_state(self, node_id: str, state: str, message: str) -> None:
        self.scene.set_node_state(node_id, state, message)
        if state == "error":
            self.execution_line.emit(f"{node_id}: {message}")

    def _run_finished(self, result) -> None:
        self._data_timer.stop()
        self.scene.update_data()
        self._refresh_data_views()
        self._update_run_actions()
        self.run_state_changed.emit(result.state)
        if result.state == "error" and result.error:
            # a device that is not there: running with simulators is one click away
            missing_device = result.error.startswith("device ") and not self.sim_box.isChecked()
            self.show_run_banner(f"The run stopped with an error: {result.error}", "error",
                                 "simulate" if missing_device else "dismiss")
        self.execution_line.emit(f"■ {self.flow.name}: {result.state} after {result.time:.6g} s"
                                 + (f" – {result.error}" if result.error else ""))


def open_flow_file(path: str, registry: Optional[Registry] = None, hub=None, project=None) -> FlowDocument:
    """A flow document for ``*.flow.yaml`` or a DSL script."""
    if path.lower().endswith(".py"):
        with open(path, encoding="utf-8") as handle:
            flow = from_python(handle.read(), registry, filename=path)
    else:
        flow = yaml_io.load(path)
    return FlowDocument(flow, path, registry=registry, hub=hub, project=project)
