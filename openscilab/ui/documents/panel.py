# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The panel document: a measuring station's front panel bound to a flow.

*Edit*: widgets from the palette onto a grid (tabs, groups), the inspector binds them to ports of
the flow and places them; undo/redo for every change. *Operate*: the widgets work – controls send
their values into the running flow, displays show the values of its outputs; ``F11`` shows the
panel full screen as an application of its own (kiosk). Files: ``*.panel.yaml``.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from PySide6.QtCore import QEvent, QMimeData, QObject, QRect, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QDrag,
    QKeySequence,
    QPainter,
    QPen,
    QShortcut,
    QUndoCommand,
    QUndoStack,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ...core import signals
from ...core.hub import Hub
from ...lab import panel_model
from ...lab.flow_files import load_flow
from ...lab.model import Flow, FlowError
from ...lab.panel_model import CONTROL_KINDS, WIDGET_KINDS, Panel, PanelError, PanelWidget
from .. import messages
from ..flow.runner import FlowRunner
from ..icons import icon, set_icon
from ..panel.widgets import PanelItem, make_item
from ..theme import ACCENT, BORDER, ERROR, TEXT_MUTED, set_role
from .base import DocumentWidget

log = logging.getLogger(__name__)

VIEWS = ("Edit", "Operate")
PANEL_FILE_FILTER = "Panels (*.panel.yaml);;All files (*)"


MERGE_SETTING = 1


class PanelEdit(QUndoCommand):
    """A change of the panel: the whole panel before and after (panels are small). Steps of one
    setting in a row (a spin box stepped up) are one undo step."""

    def __init__(self, document: "PanelDocument", text: str, before: dict, after: dict,
                 merge_key: Optional[tuple] = None) -> None:
        super().__init__(text)
        self.document, self.before, self.after = document, before, after
        self.merge_key = merge_key
        self._pushed = False

    def id(self) -> int:  # noqa: A003 - Qt naming
        return MERGE_SETTING if self.merge_key is not None else -1

    def mergeWith(self, other) -> bool:  # noqa: N802 - Qt naming
        if not isinstance(other, PanelEdit) or other.merge_key != self.merge_key:
            return False
        self.after = other.after
        return True

    def undo(self) -> None:
        self.document._inspector = None  # shows the restored settings
        self.document._restore(self.before)

    def redo(self) -> None:
        if self._pushed:
            self.document._inspector = None
        self._pushed = True
        self.document._restore(self.after)


class _Selector(QObject):
    """In the editor a click on a widget selects it instead of operating it."""

    def __init__(self, document: "PanelDocument", widget_id: str) -> None:
        super().__init__(document)
        self.document, self.widget_id = document, widget_id

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt naming
        if event.type() in (QEvent.MouseButtonPress, QEvent.MouseButtonDblClick):
            self.document.select(self.widget_id)
            if event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                self._press = event.globalPosition().toPoint()
            return True
        if event.type() == QEvent.MouseMove:
            press = getattr(self, "_press", None)
            if press is not None and event.buttons() & Qt.LeftButton:
                position = event.globalPosition().toPoint()
                if self.document.dragging is None and (position - press).manhattanLength() >= 6:
                    self.document.begin_drag(self.widget_id, "move")
                if self.document.dragging is not None:
                    self.document.drag_to(position)
            return True
        if event.type() == QEvent.MouseButtonRelease:
            self._press = None
            if self.document.dragging is not None:
                self.document.end_drag()
            return True
        if event.type() == QEvent.Wheel:
            # not into the widget (a spin box would change), but to the editor so it scrolls
            area = self.document.edit_area
            for bar, delta in ((area.verticalScrollBar(), event.pixelDelta().y() or event.angleDelta().y() / 4),
                               (area.horizontalScrollBar(), event.pixelDelta().x() or event.angleDelta().x() / 4)):
                bar.setValue(int(bar.value() - delta))
            return True
        return False


#: height of a grid row in the editor, and the free rows below the widgets
ROW_HEIGHT = 80
EMPTY_ROWS = 3
PANEL_WIDGET_MIME = "application/x-openscilab-panel-widget"
#: the signal types each kind of widget can show or set (missing: any)
KIND_TYPES = {
    "number": (signals.SCALAR, signals.ANALOG, signals.BOOL),
    "led": (signals.BOOL, signals.SCALAR, signals.DIGITAL),
    "chart": (signals.SCALAR, signals.ANALOG, signals.TABLE),
    "scope": (signals.CAPTURE, signals.DIGITAL, signals.ANALOG),
    "switch": (signals.BOOL, signals.SCALAR),
    "button": (signals.EVENT, signals.BOOL, signals.SCALAR),
    "slider": (signals.SCALAR,),
    "input": (signals.SCALAR,),
    "choice": (signals.SCALAR,),
}


class _Palette(QListWidget):
    """The widget kinds; dragged onto the grid they land where they are dropped."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setDragEnabled(True)

    def startDrag(self, actions) -> None:  # noqa: N802 - Qt naming
        item = self.currentItem()
        if item is None:
            return
        mime = QMimeData()
        mime.setData(PANEL_WIDGET_MIME, str(item.data(Qt.UserRole)).encode("ascii"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.CopyAction)


class _EditGrid(QWidget):
    """A tab of the panel in the editor: the cells drawn, widgets dropped and moved onto them."""

    def __init__(self, document: "PanelDocument", tab: str) -> None:
        super().__init__()
        self.document, self.tab = document, tab
        self.grid: Optional[QGridLayout] = None
        self.rows = 0
        self.target: Optional[tuple[QRect, bool]] = None
        self.setAcceptDrops(True)

    def cell_rect(self, row: int, column: int, rows: int = 1, columns: int = 1) -> QRect:
        first = self.grid.cellRect(row, column)
        last = self.grid.cellRect(min(row + rows - 1, self.rows - 1), min(column + columns - 1,
                                                                           self.document.panel.columns - 1))
        return first.united(last)

    def cell_at(self, point) -> Optional[tuple[int, int]]:
        if self.grid is None:
            return None
        for row in range(self.rows):
            for column in range(self.document.panel.columns):
                rect = self.grid.cellRect(row, column).adjusted(-4, -4, 4, 4)
                if rect.contains(point):
                    return row, column
        return None

    def show_target(self, place, ok: bool) -> None:
        self.target = (self.cell_rect(*place), ok) if place is not None and self.grid is not None else None
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().paintEvent(event)
        if self.grid is None:
            return
        painter = QPainter(self)
        pen = QPen(QColor(BORDER), 1, Qt.DashLine)
        painter.setPen(pen)
        for row in range(self.rows):
            for column in range(self.document.panel.columns):
                painter.drawRect(self.grid.cellRect(row, column).adjusted(0, 0, -1, -1))
        if self.target is not None:
            rect, ok = self.target
            color = QColor(ACCENT if ok else ERROR)
            painter.setPen(QPen(color, 2))
            fill = QColor(color)
            fill.setAlpha(40)
            painter.setBrush(fill)
            painter.drawRect(rect.adjusted(1, 1, -2, -2))

    def dragEnterEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.mimeData().hasFormat(PANEL_WIDGET_MIME):
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        cell = self.cell_at(event.position().toPoint())
        if cell is None or not event.mimeData().hasFormat(PANEL_WIDGET_MIME):
            return
        place = (cell[0], min(cell[1], self.document.panel.columns - 1), 1, 1)
        self.show_target(place, self.document.fits(self.tab, place))
        event.acceptProposedAction()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self.show_target(None, True)

    def dropEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self.show_target(None, True)
        cell = self.cell_at(event.position().toPoint())
        kind = bytes(event.mimeData().data(PANEL_WIDGET_MIME)).decode("ascii")
        if cell is not None and kind:
            event.acceptProposedAction()
            QTimer.singleShot(0, lambda: self.document.drop_new(kind, self.tab, cell))


class _Grip(QWidget):
    """The corner of a widget in the editor: drag it to give the widget more rows or columns."""

    def __init__(self, document: "PanelDocument", widget_id: str, item: QWidget) -> None:
        super().__init__(item)
        self.document, self.widget_id, self.item = document, widget_id, item
        self.setFixedSize(14, 14)
        self.setCursor(Qt.SizeFDiagCursor)
        self.setToolTip("Drag to resize")
        item.installEventFilter(self)
        self._place()
        self.show()

    def _place(self) -> None:
        self.move(self.item.width() - self.width(), self.item.height() - self.height())
        self.raise_()

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt naming
        if watched is self.item and event.type() == QEvent.Resize:
            self._place()
        return False

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setPen(QPen(QColor(TEXT_MUTED), 1.2))
        for step in (4, 8, 12):
            painter.drawLine(self.width() - step, self.height() - 2, self.width() - 2, self.height() - step)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self.document.select(self.widget_id)
        self.document.begin_drag(self.widget_id, "resize")

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self.document.drag_to(event.globalPosition().toPoint())

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self.document.end_drag()


class PanelDocument(DocumentWidget):
    """A panel (see the module documentation)."""

    document_kind = "panel"
    runnable = True
    run_state_changed = Signal(str)
    execution_line = Signal(str)
    selection_changed = Signal()
    #: show the flow of the panel: its open document, or its file
    flow_requested = Signal(object)

    def __init__(self, panel: Optional[Panel] = None, hub: Optional[Hub] = None, path: Optional[str] = None,
                 flow: Optional[Flow] = None, project=None, parent: Optional[QWidget] = None,
                 flow_document=None) -> None:
        super().__init__(parent)
        self.panel = panel or Panel()
        self.hub = hub
        #: the open flow document the panel was made for (its flow may not be saved yet)
        self._flow_document = flow_document
        if project is None and flow_document is not None:
            project = flow_document.project
        self.project = project
        self._path = path
        self._flow = flow
        self.selected: Optional[str] = None
        self.items: dict[str, PanelItem] = {}
        self.kiosk: Optional[QWidget] = None
        self.undo = QUndoStack(self)
        # methods, not lambdas: Qt drops the connection when the document is gone (a subflow shares
        # its parent's stack, which outlives it)
        self.undo.cleanChanged.connect(self._undo_state_changed)
        self.undo.indexChanged.connect(self._undo_state_changed)  # undo/redo states
        self._keep_inspector = False
        #: a widget being moved or resized with the mouse: (widget id, "move" or "resize")
        self.dragging: Optional[tuple[str, str]] = None
        self._drag_target: Optional[tuple[int, int, int, int]] = None
        self._flow_stamp: Optional[float] = None
        #: finds the open flow document of a file (set by the shell)
        self.flow_provider = None
        self.runner = FlowRunner(self)
        #: controls whose binding the running flow does not have (reported once per run)
        self._unbound: set[str] = set()
        self.runner.values.connect(self._on_values)
        self.runner.state_changed.connect(self._run_state)
        self.runner.log.connect(self.execution_line.emit)
        self.runner.finished.connect(lambda result: self.execution_line.emit(
            f"■ {result.state}" + (f": {result.error}" if result.error else "")))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        from ..widgets.toolbar import HIGH, LOW, NORMAL, AdaptiveToolBar

        self._toolbar = bar = AdaptiveToolBar("panel", "Panel", self)
        # Edit / Operate as two toggles side by side (as Graph / YAML in a flow)
        self.view_actions = []
        for view, icon_name, tip in (("Edit", "pencil", "Arrange and bind the widgets"),
                                     ("Operate", "play", "Use the panel as it runs")):
            action = QAction(icon(icon_name), view, self)
            action.setCheckable(True)
            action.setToolTip(tip)
            action.triggered.connect(lambda _checked=False, view=view: self.set_view(view))
            bar.add_action(f"view-{view.lower()}", action, "view", HIGH)
            self.view_actions.append(action)
        self.view_actions[0].setChecked(True)
        self.action_run = QAction(icon("play"), "Run", self)
        self.action_run.triggered.connect(self.start_run)
        self.action_stop = QAction(icon("stop"), "Stop", self)
        self.action_stop.triggered.connect(self.stop_run)
        self.action_kiosk = QAction(icon("detach"), "Full screen (F11)", self)
        self.action_kiosk.setIconText("Full screen")
        self.action_kiosk.triggered.connect(self.toggle_kiosk)
        self.action_delete = QAction(icon("trash"), "Remove widget", self)
        self.action_delete.setShortcut(QKeySequence.Delete)
        self.action_delete.setShortcutContext(Qt.WidgetWithChildrenShortcut)
        self.action_delete.triggered.connect(self.remove_selected)
        bar.add_action("run", self.action_run, "run", HIGH, pinned=True)
        bar.add_action("stop", self.action_stop, "run", HIGH, text=False)
        self.fast_box = QCheckBox("Virtual time", self)
        self.fast_box.setToolTip("Simulated instruments in the flow's own time: as fast as possible, the same "
                                 "result every run")
        bar.add_widget("virtual-time", self.fast_box, "Virtual time", "run", LOW)
        bar.add_action("full-screen", self.action_kiosk, "show", NORMAL, text=False)
        bar.add_action("remove", self.action_delete, "edit", LOW, text=False)
        self.addAction(self.action_delete)
        self.action_open_flow = QAction(icon("nodes"), "Open flow", self)
        self.action_open_flow.setToolTip("Open the flow this panel works with")
        self.action_open_flow.triggered.connect(self.open_flow)
        bar.add_action("open-flow", self.action_open_flow, "flow", NORMAL)
        self.flow_label = QLabel(self)
        set_role(self.flow_label, "hint")
        bar.add_widget("flow", self.flow_label, "The flow", "flow", LOW)
        for action in (self.action_stop, self.action_kiosk, self.action_delete):
            action.setToolTip(action.toolTip() or action.text())
        bar.finish()
        layout.addWidget(self._toolbar)

        self.stack = QStackedWidget(self)
        layout.addWidget(self.stack, 1)
        # editor: palette and grid
        editor = QSplitter(Qt.Horizontal, self.stack)
        self.palette = _Palette(editor)
        for kind, (role, title) in WIDGET_KINDS.items():
            item = QListWidgetItem(f"{title}  ·  {role}", self.palette)
            item.setData(Qt.UserRole, kind)
        self.palette.setToolTip("Drag a widget onto the grid, or double-click to add it below the others")
        self.palette.itemActivated.connect(lambda item: self.add_widget(item.data(Qt.UserRole)))
        self.palette.setMaximumWidth(220)
        self.edit_area = QScrollArea(editor)
        self.edit_area.setWidgetResizable(True)
        editor.addWidget(self.palette)
        editor.addWidget(self.edit_area)
        editor.setSizes([180, 800])
        self.stack.addWidget(editor)
        # operation
        self.operate_area = QScrollArea(self.stack)
        self.operate_area.setWidgetResizable(True)
        self.stack.addWidget(self.operate_area)

        QShortcut(QKeySequence(Qt.Key_F11), self, activated=self.toggle_kiosk, context=Qt.WidgetWithChildrenShortcut)
        self._inspector: Optional[QWidget] = None
        self.rebuild()
        self._update_run_actions()

    # ------------------------------------------------------------ identity
    def document_actions(self) -> list:
        """Run, stop, full screen, edit and operate, for the command palette."""
        return [self.action_run, self.action_stop, self.action_kiosk, self.action_delete, *self.view_actions]

    def _undo_state_changed(self, *_args) -> None:
        self.document_changed.emit()

    @property
    def title(self) -> str:
        if self._path:
            return os.path.basename(self._path)
        if self.panel.name and self.panel.name != "Panel":
            return self.panel.name
        if getattr(self, "_untitled", None) is None:
            from .base import untitled_title

            self._untitled = untitled_title("panel")
        return self._untitled

    @property
    def path(self) -> Optional[str]:
        return self._path

    @property
    def dirty(self) -> bool:
        return not self.undo.isClean()

    def undo_stack(self):
        return self.undo

    def toolbar(self):
        return None

    def views(self) -> list[str]:
        return list(VIEWS)

    def current_view(self) -> str:
        return VIEWS[self.stack.currentIndex()]

    def set_view(self, name: str) -> None:
        if name not in VIEWS:
            return
        self.stack.setCurrentIndex(VIEWS.index(name))
        for view, action in zip(VIEWS, self.view_actions):
            action.setChecked(view == name)
        self.rebuild()
        self.document_changed.emit()

    @property
    def operating(self) -> bool:
        return self.current_view() == "Operate"

    # ---------------------------------------------------------------- flow
    @property
    def flow_document(self):
        """The flow document the panel was made for, while it is open."""
        document = self._flow_document
        if document is not None:
            import shiboken6

            if not shiboken6.isValid(document):
                self._flow_document = document = None
        return document

    def flow_file(self) -> Optional[str]:
        path = panel_model.flow_path(self.panel, self._path)
        if not path and self.flow_document is not None and self.flow_document.path:
            return os.path.abspath(self.flow_document.path)  # the flow was saved after the panel was made
        return path

    def flow(self) -> Optional[Flow]:
        """The flow the panel works with: the open flow document of its file (with its unsaved
        changes), else the file, read again when it changed."""
        path = self.flow_file()
        if not path:
            return self.flow_document.flow if self.flow_document is not None else self._flow
        open_document = self.flow_provider(path) if self.flow_provider is not None else None
        if open_document is not None:
            return open_document.flow
        try:
            stamp = os.path.getmtime(path)
        except OSError:
            return self._flow
        if self._flow is None or stamp != self._flow_stamp:
            try:
                self._flow = load_flow(path, registry=self.registry())
                self._flow_stamp = stamp
            except (FlowError, OSError, SyntaxError) as error:
                self.execution_line.emit(f"{path}: {error}")
        return self._flow

    def registry(self):
        if self.flow_document is not None:
            return self.flow_document.registry
        return self.project.registry() if self.project is not None else None

    def open_flow(self) -> bool:
        """Show the flow of the panel (the flow document it was made for while that is open)."""
        target = self.flow_document or self.flow_file()
        if target is None:
            return False
        self.flow_requested.emit(target)
        return True

    def set_flow_file(self, path: str) -> None:
        """Bind the panel to the flow in ``path``."""
        before = self.panel.to_data()
        relative = path
        if self._path:
            relative = os.path.relpath(path, os.path.dirname(os.path.abspath(self._path)))
        self.panel.flow = relative
        self._flow = None
        self._flow_stamp = None
        self._flow_document = None
        self._push("Set the flow", before)

    def ports(self, role: str, kind: str = "") -> list[str]:
        """``node.port`` a widget of ``role`` can bind to (displays: outputs; controls: both), only
        those of a type the ``kind`` of widget shows (an LED a bool, a scope a capture, ...)."""
        flow = self.flow()
        if flow is None:
            return []
        result = []
        for node_id, node in flow.nodes.items():
            try:
                inputs, outputs = flow.spec(node, self.registry()).resolve_ports(node.params)
            except Exception as error:  # noqa: BLE001 - an unknown node type has no ports to offer
                log.debug("No ports of %s: %s", node_id, error)
                continue
            accepted = KIND_TYPES.get(kind)

            def fitting(port, accepted=accepted) -> bool:
                return accepted is None or port.type in accepted or port.type == signals.ANY

            if role == "control":
                result += [f"{node_id}.{port.name}" for port in inputs if fitting(port)]
            result += [f"{node_id}.{port.name}" for port in outputs if fitting(port)]
        return result

    # ------------------------------------------------------------- editing
    def _push(self, text: str, before: dict, merge_key: Optional[tuple] = None) -> None:
        after = self.panel.to_data()
        if after != before:
            self.undo.push(PanelEdit(self, text, before, after, merge_key))

    def _restore(self, data: dict) -> None:
        self.panel = Panel.from_data(data)
        if self.selected and self.selected not in {widget.id for widget in self.panel.widgets}:
            self.selected = None
        self.rebuild()
        self.document_changed.emit()

    def add_widget(self, kind: str, bind: str = "", **settings) -> PanelWidget:
        before = self.panel.to_data()
        if not bind and kind != "label":
            ports = self.ports(WIDGET_KINDS[kind][0], kind)
            bind = ports[0] if len(ports) == 1 else ""
        widget = self.panel.add(kind, bind, **settings)
        self.selected = widget.id
        self._push(f"Add {kind}", before)
        self.selection_changed.emit()
        return widget

    def update_widget(self, widget_id: str, **changes) -> None:
        """Change settings of a widget (``options`` replaces the options)."""
        before = self.panel.to_data()
        widget = self.panel.widget(widget_id)
        for name, value in changes.items():
            setattr(widget, name, value)
        if widget.tab and widget.tab not in self.panel.tabs:
            self.panel.tabs.append(widget.tab)
        self._push(f"Change {widget_id}", before, merge_key=(widget_id, tuple(sorted(changes))))

    def _inspector_update(self, widget_id: str, **changes) -> None:
        """A change typed into the inspector: the inspector stays (with the cursor in it)."""
        self._keep_inspector = True
        try:
            self.update_widget(widget_id, **changes)
        finally:
            self._keep_inspector = False

    def remove_selected(self) -> None:
        if self.selected is None or self.operating:
            return
        before = self.panel.to_data()
        self.panel.remove(self.selected)
        self.selected = None
        self._push("Remove widget", before)
        self.selection_changed.emit()

    def move_selected(self, rows: int, columns: int) -> None:
        if self.selected is None:
            return
        widget = self.panel.widget(self.selected)
        self.update_widget(widget.id, row=max(widget.row + rows, 0),
                           column=min(max(widget.column + columns, 0), self.panel.columns - widget.columns))

    def select(self, widget_id: Optional[str]) -> None:
        self.selected = widget_id
        self._highlight(widget_id)
        if not self._keep_inspector:
            self._inspector = None
        self.selection_changed.emit()
        self.document_changed.emit()

    def _highlight(self, widget_id: Optional[str]) -> None:
        for item_id, item in self.items.items():
            item.setStyleSheet(f"#{item.objectName()} {{ border: 2px solid {ACCENT}; }}" if item_id == widget_id else "")

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if not self.operating and self.selected is not None and event.modifiers() & Qt.AltModifier:
            moves = {Qt.Key_Up: (-1, 0), Qt.Key_Down: (1, 0), Qt.Key_Left: (0, -1), Qt.Key_Right: (0, 1)}
            if event.key() in moves:
                self.move_selected(*moves[event.key()])
                return
        super().keyPressEvent(event)

    # ------------------------------------------------------------ building
    def rebuild(self) -> None:
        """Lay the widgets out again (in the editor or for operating)."""
        operating = self.operating
        area = self.operate_area if operating else self.edit_area
        self.items.clear()
        content = self._build_content(operating)
        old = area.takeWidget()
        area.setWidget(content)
        if old is not None:
            old.deleteLater()
        if not operating and self.selected:
            self.select(self.selected)
        flow = self.flow_file()
        problems = self.panel.problems(self.flow(), self.registry())
        self.flow_label.setText(("  Flow: " + os.path.basename(flow) if flow else "  No flow")
                                + (f"  ·  {len(problems)} problem(s)" if problems else ""))
        self.flow_label.setToolTip("\n".join(problems))
        self.action_open_flow.setEnabled(bool(flow) or self.flow_document is not None)
        self.action_delete.setEnabled(not operating)

    def _build_content(self, operating: bool) -> QWidget:
        tabs = self.panel.tab_names()
        if len(tabs) == 1:
            return self._build_grid(tabs[0], operating)
        widget = QTabWidget()
        for tab in tabs:
            widget.addTab(self._build_grid(tab, operating), tab or "Main")
        return widget

    def _build_grid(self, tab: str, operating: bool) -> QWidget:
        page = QWidget() if operating else _EditGrid(self, tab)
        grid = QGridLayout(page)
        grid.setSpacing(8)
        grid.setContentsMargins(12, 12, 12, 12)
        widgets = [widget for widget in self.panel.widgets if widget.tab == tab]
        groups: dict[str, list[PanelWidget]] = {}
        for widget in widgets:
            if widget.group:
                groups.setdefault(widget.group, []).append(widget)
            else:
                grid.addWidget(self._item(widget, operating, page), widget.row, widget.column, widget.rows, widget.columns)
        for name, members in groups.items():
            top = min(widget.row for widget in members)
            left = min(widget.column for widget in members)
            bottom = max(widget.row + widget.rows for widget in members)
            right = max(widget.column + widget.columns for widget in members)
            box = QGroupBox(name, page)
            inner = QGridLayout(box)
            for widget in members:
                inner.addWidget(self._item(widget, operating, box), widget.row - top, widget.column - left,
                                widget.rows, widget.columns)
            grid.addWidget(box, top, left, bottom - top, right - left)
        for column in range(self.panel.columns):
            grid.setColumnStretch(column, 1)
        rows = max((widget.row + widget.rows for widget in widgets), default=0)
        if not operating:
            # free cells to drop widgets into: every row has a height, three more below
            for row in range(rows + EMPTY_ROWS):
                grid.setRowMinimumHeight(row, ROW_HEIGHT)
            rows += EMPTY_ROWS
            page.grid = grid
            page.rows = rows
        grid.setRowStretch(rows, 1)
        if not widgets and not operating:
            hint = QLabel("Drag a widget from the palette onto the grid (or double-click it); drag widgets to "
                          "move them, their corner to resize them. The inspector binds them to a port.", page)
            hint.setWordWrap(True)
            set_role(hint, "hint")
            grid.addWidget(hint, 0, 0, 1, self.panel.columns)
        return page

    def _item(self, widget: PanelWidget, operating: bool, parent: QWidget) -> PanelItem:
        item = make_item(widget, parent)
        self.items[widget.id] = item
        if operating:
            item.sent.connect(lambda value, model=widget: self.send(model, value))
        else:
            selector = _Selector(self, widget.id)
            for child in [item] + item.findChildren(QWidget):
                child.installEventFilter(selector)
            _Grip(self, widget.id, item)
            problem = self.binding_problem(widget)
            if problem:
                item.setToolTip(problem)
                warning = QLabel("⚠", item)
                warning.setObjectName("binding-warning")
                set_role(warning, "warning")
                warning.setToolTip(problem)
                warning.move(item.width() - 22, 4)
                warning.show()
        return item

    # ---------------------------------------------------- moving with the mouse
    def occupied(self, tab: str, skip: str = "") -> set[tuple[int, int]]:
        cells = set()
        for widget in self.panel.widgets:
            if widget.tab != tab or widget.id == skip:
                continue
            for row in range(widget.row, widget.row + widget.rows):
                for column in range(widget.column, widget.column + widget.columns):
                    cells.add((row, column))
        return cells

    def fits(self, tab: str, place: tuple[int, int, int, int], skip: str = "") -> bool:
        row, column, rows, columns = place
        if row < 0 or column < 0 or column + columns > self.panel.columns:
            return False
        taken = self.occupied(tab, skip)
        return not any((r, c) in taken for r in range(row, row + rows) for c in range(column, column + columns))

    def _grid_page(self, tab: str) -> Optional["_EditGrid"]:
        area = self.edit_area.widget()
        pages = [area] if isinstance(area, _EditGrid) else (area.findChildren(_EditGrid) if area else [])
        return next((page for page in pages if page.tab == tab), None)

    def begin_drag(self, widget_id: str, mode: str) -> None:
        if self.operating:
            return
        self.dragging = (widget_id, mode)
        self._drag_target = None

    def drag_to(self, global_position) -> None:
        if self.dragging is None:
            return
        widget_id, mode = self.dragging
        widget = self.panel.widget(widget_id)
        page = self._grid_page(widget.tab)
        if page is None:
            return
        cell = page.cell_at(page.mapFromGlobal(global_position))
        if cell is None:
            return
        row, column = cell
        if mode == "move":
            column = min(column, self.panel.columns - widget.columns)
            place = (row, column, widget.rows, widget.columns)
        else:
            place = (widget.row, widget.column, max(row - widget.row + 1, 1), max(column - widget.column + 1, 1))
        self._drag_target = place if self.fits(widget.tab, place, widget.id) else None
        page.show_target(place, self._drag_target is not None)

    def end_drag(self) -> bool:
        if self.dragging is None:
            return False
        widget_id, _mode = self.dragging
        target, self.dragging, self._drag_target = self._drag_target, None, None
        widget = self.panel.widget(widget_id)
        page = self._grid_page(widget.tab)
        if page is not None:
            page.show_target(None, True)
        if target is None or target == (widget.row, widget.column, widget.rows, widget.columns):
            return False
        row, column, rows, columns = target
        self.update_widget(widget_id, row=row, column=column, rows=rows, columns=columns)
        return True

    def drop_new(self, kind: str, tab: str, cell: tuple[int, int]) -> Optional[PanelWidget]:
        """A widget dragged from the palette onto ``cell`` of ``tab``."""
        place = (cell[0], min(cell[1], self.panel.columns - 1), 1, 1)
        if not self.fits(tab, place):
            self.flow_label.setText("  That place is taken: drop the widget on a free cell")
            return None
        return self.add_widget(kind, row=place[0], column=place[1], tab=tab)

    def binding_problem(self, widget: PanelWidget) -> str:
        """Why the widget's port does not work (empty when it does)."""
        if widget.role == "static":
            return ""
        if not widget.bind:
            return "Not bound to a port of the flow yet (Port in the inspector)"
        if self.flow() is not None and widget.bind not in self.ports(widget.role):
            return f"The flow has no port {widget.bind}"
        return ""

    # ----------------------------------------------------------- operating
    def send(self, widget: PanelWidget, value: Any) -> bool:
        """A control changed: its value goes into the running flow."""
        engine = self.runner.engine
        if not widget.bind or engine is None or not self.runner.running:
            return False
        try:
            engine.inject(widget.node, widget.port, panel_model.control_value(widget, value, engine.clock.now()))
        except FlowError as error:
            # bound to a node the flow no longer has: said once in the log, the run goes on
            if widget.id not in self._unbound:
                self._unbound.add(widget.id)
                self.execution_line.emit(f"Panel: {widget.label or widget.id}: {error}")
            return False
        return True

    def _on_values(self, node: str, port: str, values: list) -> None:
        """The values an output sent since the last delivery: every one of them reaches the
        displays bound to it (a chart adds a point for each)."""
        binding = f"{node}.{port}"
        for widget in self.panel.widgets:
            if widget.role == "display" and widget.bind == binding:
                item = self.items.get(widget.id)
                if item is not None:
                    for value in values:
                        item.show_value(value)

    @property
    def run_state(self) -> str:
        return self.runner.state if self.runner.running else "idle"

    def start_run(self) -> bool:
        if self.runner.running:
            return True
        flow = self.flow()
        if flow is None:
            messages.warning(self, "Run", "The panel has no flow.", "Choose its flow in the inspector.")
            return False
        problems = self.panel.problems(flow, self.registry())
        if problems:
            self.execution_line.emit("Panel: " + "; ".join(problems))
        if not self.operating:
            self.set_view("Operate")
        for item in self.items.values():
            item.reset()
        project = self.project
        flow = project.complete(flow.copy()) if project is not None else flow.copy()
        file = self.flow_file() or self._path
        if file:
            base = os.path.dirname(file)
        else:  # an unsaved flow writes where its document writes (a scratch folder)
            base = self.flow_document.data_dir if self.flow_document is not None else os.getcwd()
        self._unbound.clear()
        try:
            self.runner.start(flow, registry=self.registry(), mode="virtual" if self.fast_box.isChecked() else "real",
                              hub=self.hub, project=project,
                              base_dir=project.root if project else base,
                              data_dir=project.data_dir if project else base,
                              interactive=True, bound=self.panel.bindings())
        except FlowError as error:
            messages.warning(self, "Run", "The flow cannot run.", str(error))
            return False
        # the controls' settings go in once at the start
        for widget in self.panel.widgets:
            item = self.items.get(widget.id)
            if widget.kind in CONTROL_KINDS and widget.kind != "button" and item is not None \
                    and item.current() is not None and widget.bind:
                self.send(widget, item.current())
        self.execution_line.emit(f"▶ {self.panel.name}: {flow.name}")
        self._update_run_actions()
        self.run_state_changed.emit("running")
        return True

    def stop_run(self) -> None:
        self.runner.stop()

    def pause_run(self) -> None:
        self.runner.pause()

    def step_run(self) -> None:
        self.runner.step()

    def _run_state(self, state: str, message: str) -> None:
        self._update_run_actions()
        self.run_state_changed.emit(state)

    def _update_run_actions(self) -> None:
        running = self.runner.running and self.runner.state not in ("finished", "stopped", "error")
        self.action_run.setEnabled(not running)
        self.action_stop.setEnabled(running)

    def toggle_kiosk(self) -> None:
        """The operating panel full screen, as an application of its own (F11 or Esc back)."""
        if self.kiosk is not None:
            self.kiosk.close()
            return
        if not self.operating:
            self.set_view("Operate")
        content = self.operate_area.takeWidget()
        window = QWidget(None, Qt.Window)
        window.setWindowTitle(self.panel.name)
        window_layout = QVBoxLayout(window)
        window_layout.addWidget(content)
        content.show()
        for key in (Qt.Key_F11, Qt.Key_Escape):
            QShortcut(QKeySequence(key), window, activated=window.close)

        def closed(event, window=window, content=content) -> None:
            self.kiosk = None
            content.setParent(None)
            self.operate_area.setWidget(content)
            event.accept()

        window.closeEvent = closed
        self.kiosk = window
        window.showFullScreen()

    # ----------------------------------------------------------- inspector
    def inspector_widget(self, selection=None) -> Optional[QWidget]:
        if self._inspector is not None:
            return self._inspector
        holder = QWidget(self)
        holder.hide()
        form = QFormLayout(holder)
        form.setContentsMargins(10, 6, 10, 6)
        if self.selected is None or self.selected not in {widget.id for widget in self.panel.widgets}:
            name = QLineEdit(self.panel.name, holder)
            name.editingFinished.connect(lambda: self._set_panel_name(name.text()))
            form.addRow("Panel", name)
            unsaved = self.flow_document is not None and not self.flow_file()
            flow = QLabel(f"{self.flow_document.title} (not saved yet)" if unsaved else self.panel.flow or "–", holder)
            flow.setWordWrap(True)
            form.addRow("Flow", flow)
            row = QHBoxLayout()
            browse = QPushButton(holder)
            set_icon(browse, "folder")
            browse.setToolTip("Choose another flow for this panel...")
            browse.clicked.connect(self._choose_flow)
            row.addWidget(browse)
            show = QPushButton("Open flow", holder)
            set_icon(show, "nodes")
            show.setToolTip("Open the flow this panel works with")
            show.setEnabled(bool(self.flow_file()) or self.flow_document is not None)
            show.clicked.connect(self.open_flow)
            row.addWidget(show)
            row.addStretch(1)
            form.addRow("", row)
            columns = QSpinBox(holder)
            columns.setRange(1, 24)
            columns.setValue(self.panel.columns)
            columns.valueChanged.connect(lambda value: self._set_columns(value))
            form.addRow("Columns", columns)
        else:
            widget = self.panel.widget(self.selected)
            form.addRow("Widget", QLabel(f"<b>{widget.id}</b> · {WIDGET_KINDS[widget.kind][1]}", holder))
            title = QLineEdit(widget.title, holder)
            title.editingFinished.connect(lambda: self._inspector_update(widget.id, title=title.text()))
            form.addRow("Title", title)
            if widget.role != "static":
                bind = QComboBox(holder)
                bind.setEditable(True)
                bind.addItems(self.ports(widget.role, widget.kind))
                bind.setCurrentText(widget.bind)
                bind.lineEdit().editingFinished.connect(lambda: self._inspector_update(widget.id, bind=bind.currentText()))
                bind.activated.connect(lambda _index: self._inspector_update(widget.id, bind=bind.currentText()))
                form.addRow("Port", bind)
            for name, low in (("row", 0), ("column", 0), ("rows", 1), ("columns", 1)):
                spin = QSpinBox(holder)
                spin.setRange(low, 99)
                spin.setObjectName(f"setting-{name}")
                spin.setValue(getattr(widget, name))
                spin.valueChanged.connect(lambda value, key=name: self._inspector_update(widget.id, **{key: value}))
                form.addRow(name.capitalize(), spin)
            for name in ("tab", "group"):
                choices = (self.panel.tab_names() if name == "tab" else
                           sorted({item.group for item in self.panel.widgets if item.group}))
                box = QComboBox(holder)
                box.setEditable(True)  # a new name makes a new tab or group
                box.addItem("" if name == "group" else "")
                for choice in choices:
                    if choice:
                        box.addItem(choice)
                box.setCurrentText(getattr(widget, name))
                box.lineEdit().setPlaceholderText("Main" if name == "tab" else "none")
                box.setToolTip(f"Choose a {name}, or type a new name")
                box.setObjectName(f"setting-{name}")
                box.activated.connect(lambda _index, key=name, box=box: self._inspector_update(
                    widget.id, **{key: box.currentText().strip()}))
                box.lineEdit().editingFinished.connect(lambda key=name, box=box: self._inspector_update(
                    widget.id, **{key: box.currentText().strip()}))
                form.addRow(name.capitalize(), box)
            options = panel_model.OPTIONS.get(widget.kind, ())
            if options:
                text = QLineEdit(", ".join(f"{key}: {value}" for key, value in widget.options.items()), holder)
                text.setPlaceholderText(", ".join(f"{key}: …" for key in options))
                text.editingFinished.connect(lambda box=text: self._set_options(widget.id, box.text()))
                form.addRow("Options", text)
        self._inspector = holder
        return holder

    def _set_panel_name(self, name: str) -> None:
        before = self.panel.to_data()
        self.panel.name = name.strip() or self.panel.name
        self._push("Rename the panel", before)

    def _set_columns(self, value: int) -> None:
        before = self.panel.to_data()
        self.panel.columns = value
        self._push("Columns", before)

    def _set_options(self, widget_id: str, text: str) -> None:
        import yaml

        try:
            options = yaml.safe_load("{" + text + "}") if text.strip() else {}
            PanelWidget.from_data(widget_id, {"kind": self.panel.widget(widget_id).kind, **options})
        except (yaml.YAMLError, PanelError) as error:
            messages.warning(self, "Options", "The options are not valid.", str(error))
            return
        self._inspector_update(widget_id, options=dict(options))

    def _choose_flow(self) -> None:
        start = os.path.dirname(self._path) if self._path else ""
        path, _ = QFileDialog.getOpenFileName(self, "Flow of the panel", start, "Flows (*.flow.yaml *.py)")
        if path:
            self.set_flow_file(path)

    # --------------------------------------------------------------- files
    def can_save(self) -> bool:
        return True

    def save(self) -> bool:
        if not self._path:
            return self.save_as()
        return self._write(self._path)

    def save_as(self) -> bool:
        start = self._path or os.path.join(self.project.root, "panels", f"{self.panel.name}.panel.yaml") \
            if self.project is not None else (self._path or f"{self.panel.name}.panel.yaml")
        path, _ = QFileDialog.getSaveFileName(self, "Save panel", start, PANEL_FILE_FILTER)
        if not path:
            return False
        if not path.endswith(".panel.yaml"):
            path += ".panel.yaml"
        return self._write(path)

    def _write(self, path: str) -> bool:
        flow = self.flow_file()
        if not flow and self.flow_document is not None:
            # the panel keeps the file of its flow: the flow is saved first
            choice = messages.choose(self, "Save panel", "The flow of the panel is not saved yet.",
                                     ["Save the flow"],
                                     "The panel file names the file of its flow; save the flow to give it one.")
            if choice != 0 or not self.flow_document.save():
                return False
            flow = self.flow_file()
        if flow and not self.panel.flow:
            self.panel.flow = os.path.abspath(flow)
        if flow and self._path != path:
            # the flow stays the same file: relative to the new place of the panel
            self.panel.flow = os.path.relpath(flow, os.path.dirname(os.path.abspath(path)))
        try:
            panel_model.save(self.panel, path)
        except OSError as error:
            messages.error(self, "Save panel", f"{os.path.basename(path)} could not be written.", str(error))
            return False
        self._path = path
        self.undo.setClean()
        self.document_changed.emit()
        return True

    def shutdown(self) -> None:
        if self.kiosk is not None:
            self.kiosk.close()
        running = self.runner.running
        self.runner.detach()
        if running:
            self.runner.stop()
            self.runner.wait(5)


def open_panel_file(path: str, hub: Optional[Hub] = None) -> PanelDocument:
    """A panel document of a ``*.panel.yaml`` file (raises ``PanelError``/``OSError``)."""
    from ...lab.project import Project

    root = Project.find(path)
    project = Project.open(root) if root else None
    return PanelDocument(panel_model.load(path), hub=hub, path=path, project=project)
