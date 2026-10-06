# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Inspector content of the flow document: the parameters of a node (built from its type), or the
flow itself (name, settings) when nothing is selected. Changes go to the model at once."""

from __future__ import annotations

from typing import Any, Callable, Optional

import yaml
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ...core import units
from ...lab.model import Flow, Node
from ...lab.nodes.registry import NodeSpec, ParamSpec
from ...lab.yaml_io import safe_load as yaml_safe_load
from ..icons import set_icon
from ..theme import set_role


def to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return yaml.safe_dump(value, default_flow_style=True, sort_keys=False).strip().removesuffix("...").strip()


def to_block_text(value: Any) -> str:
    """``value`` as YAML in block style, the innermost collections on one line (a big text field)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    from ...lab.yaml_io import Dumper

    return yaml.dump(value, Dumper=Dumper, default_flow_style=None, sort_keys=False, allow_unicode=True,
                     width=72).strip().removesuffix("...").strip()


#: values longer than this get a big text field (when they are mappings or lists)
LONG_TEXT = 48


class YamlEdit(QPlainTextEdit):
    """A big text field for a mapping or a list in YAML: ``committed(text)`` when it loses the focus
    or on Ctrl+Return."""

    committed = Signal(str)

    def __init__(self, text: str, parent: Optional[QWidget] = None, lines: int = 6) -> None:
        super().__init__(text, parent)
        from PySide6.QtGui import QFontDatabase

        self.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self.setTabStopDistance(24)
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        height = self.fontMetrics().lineSpacing() * lines + 12
        self.setMinimumHeight(height)
        self._last = text

    def commit(self) -> None:
        text = self.toPlainText()
        if text != self._last:
            self._last = text
            self.committed.emit(text)

    def focusOutEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self.commit()
        super().focusOutEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and event.modifiers() & (Qt.ControlModifier | Qt.MetaModifier):
            self.commit()
            return
        super().keyPressEvent(event)


def from_text(text: str, kind: str) -> Any:
    """The value a text field means for a parameter of ``kind`` (``None``: not set)."""
    text = text.strip()
    if text == "":
        return None
    if kind in ("str", "path", "pin", "quantity", "address", "choice", "code"):
        if kind == "quantity":
            try:
                return yaml_safe_load(text) if not any(char.isalpha() or char == "%" for char in text) else text
            except yaml.YAMLError:
                return text
        return text
    if kind == "int":
        return int(text)
    if kind == "float":
        return float(text)
    if kind == "bool":
        return text.lower() in ("1", "true", "yes", "on")
    return yaml_safe_load(text)


def flow_path(path: str, folder: str, base: str = "") -> str:
    """``path`` as a node parameter, so that the flow finds the same file again: a plain name
    for a file in the data folder ``folder`` (where the flow looks for plain names), a path
    relative to ``base`` (the project folder, where it looks for paths with a folder) for a file
    below it, else the absolute path."""
    import os

    absolute = os.path.abspath(path)
    base = base or folder
    if folder and os.path.dirname(absolute) == os.path.abspath(folder):
        return os.path.basename(absolute)
    if base and absolute.startswith(os.path.abspath(base) + os.sep):
        relative = os.path.relpath(absolute, base).replace(os.sep, "/")
        return relative if "/" in relative else f"./{relative}"
    return path


class NodeInspector(QWidget):
    """Edits one node. ``changed(node_id, name, value)``; ``value`` ``None`` removes it."""

    param_changed = Signal(str, str, object)
    renamed = Signal(str, str)
    comment_changed = Signal(str, str)
    breakpoint_toggled = Signal(str, bool)

    def __init__(self, flow: Flow, node: Node, spec: Optional[NodeSpec], breakpoint: bool = False,
                 parent: Optional[QWidget] = None, addresses: Optional[list[tuple[str, str]]] = None,
                 problems: Optional[list[str]] = None, running: bool = False,
                 suggestions: Optional[dict[str, list[str]]] = None, folder: str = "", base: str = "",
                 devices: Optional[list[str]] = None, wide: bool = False) -> None:
        super().__init__(parent)
        #: in a window of its own (the node editor): bigger text fields
        self.wide = wide
        self.flow = flow
        self.node = node
        self.spec = spec
        #: (label, address) offered for parameters of kind ``address``
        self.addresses = list(addresses or [])
        #: values to offer per parameter (pins and channels of the device the node is wired to, the
        #: channels of the capture wired to it, ...; see :mod:`openscilab.lab.hints`)
        self.suggestions = dict(suggestions or {})
        #: the device nodes of the flow (``ctx.device("…")`` in Python code)
        self.devices = list(devices or [])
        #: where relative paths start (*Browse…* of ``path`` parameters)
        self.folder = folder
        #: where the flow resolves relative paths with a folder in them (the project folder)
        self.base = base or folder
        self.editors: dict[str, QWidget] = {}
        self.errors: dict[str, str] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 10)
        title = QLabel(spec.title if spec else node.type, self)
        set_role(title, "heading")
        layout.addWidget(title)
        kind = QLabel(node.type, self)
        set_role(kind, "hint")
        kind.setWordWrap(True)
        layout.addWidget(kind)
        if spec is not None and spec.description:
            description = QLabel(spec.description, self)
            description.setWordWrap(True)
            set_role(description, "hint")
            layout.addWidget(description)
        if problems:
            # what the check found at this node
            self.problems_label = QLabel("\n".join(f"• {problem}" for problem in problems), self)
            self.problems_label.setWordWrap(True)
            set_role(self.problems_label, "error")
            layout.addWidget(self.problems_label)
        if running:
            note = QLabel("The flow is running: changes apply on its next run.", self)
            note.setWordWrap(True)
            set_role(note, "warning")
            layout.addWidget(note)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignLeft)
        self.name_edit = QLineEdit(node.id, self)
        self.name_edit.editingFinished.connect(self._rename)
        form.addRow("Name", self.name_edit)
        for param in (spec.params if spec else []):
            editor = self._editor(param)
            self.editors[param.name] = editor
            label = param.name + (f" [{param.unit}]" if param.unit else "") + (" *" if param.required else "")
            form.addRow(label, editor)
            editor.setToolTip((param.description or param.name) + ("\nRequired." if param.required else ""))
        # parameters the type does not know (kept, shown as text)
        for name, value in node.params.items():
            if spec is not None and spec.param(name) is not None:
                continue
            editor = QLineEdit(to_text(value), self)
            editor.editingFinished.connect(lambda name=name, editor=editor: self._emit(name, editor.text(), "any"))
            self.editors[name] = editor
            form.addRow(f"{name} (unknown)", editor)
        self.comment_edit = QLineEdit(node.comment, self)
        self.comment_edit.setPlaceholderText("Comment")
        self.comment_edit.editingFinished.connect(lambda: self.comment_changed.emit(self.node.id, self.comment_edit.text()))
        form.addRow("Comment", self.comment_edit)
        layout.addLayout(form)
        self.breakpoint_box = QCheckBox("Breakpoint", self)
        self.breakpoint_box.setToolTip("Stop the run before this node handles a value")
        self.breakpoint_box.setChecked(breakpoint)
        self.breakpoint_box.toggled.connect(lambda checked: self.breakpoint_toggled.emit(self.node.id, checked))
        layout.addWidget(self.breakpoint_box)
        self.error_label = QLabel("", self)
        set_role(self.error_label, "error")
        self.error_label.setWordWrap(True)
        layout.addWidget(self.error_label)
        layout.addStretch(1)

    def _editor(self, param: ParamSpec) -> QWidget:
        value = self.node.params.get(param.name)
        if param.kind == "bool":
            box = QCheckBox(self)
            box.setChecked(bool(value if value is not None else param.default))
            box.toggled.connect(lambda checked, name=param.name: self.param_changed.emit(self.node.id, name, checked))
            return box
        if param.kind == "address":
            combo = QComboBox(self)
            combo.setEditable(True)
            combo.setToolTip("sim:<profile>, pico:<port>, dslogic, ... – or an instrument open in the device list; "
                             "empty: the project's device of this name")
            for label, address in self.addresses:
                combo.addItem(f"{label}  –  {address}", address)
            combo.setCurrentIndex(combo.findData(value) if value is not None else -1)
            combo.setEditText(str(value) if value is not None else "")

            def commit(_index=None, name=param.name, combo=combo) -> None:
                text = combo.currentText()
                data = combo.currentData() if combo.currentIndex() >= 0 and combo.itemText(combo.currentIndex()) == text \
                    else text.strip()
                if data != self.node.params.get(name):
                    self.param_changed.emit(self.node.id, name, data)

            combo.activated.connect(commit)
            combo.lineEdit().editingFinished.connect(commit)
            return combo
        offered = self.suggestions.get(param.name)
        if offered is not None and param.kind == "dict" and param.keys:
            return self._mapping_editor(param, value, offered)
        if offered is not None and param.kind == "list":
            return self._list_editor(param, value, offered)
        if offered is not None and param.kind in ("str", "pin", "any"):
            combo = QComboBox(self)
            combo.setEditable(True)
            combo.addItems([str(item) for item in offered])
            combo.setEditText(to_text(value) if value is not None else "")
            if param.default is not None:
                combo.lineEdit().setPlaceholderText(to_text(param.default))
            kind = "pin" if param.kind in ("str", "pin") else param.kind
            combo.activated.connect(lambda _index, name=param.name, combo=combo, kind=kind: self._emit(
                name, combo.currentText(), kind))
            combo.lineEdit().editingFinished.connect(lambda name=param.name, combo=combo, kind=kind: self._emit(
                name, combo.currentText(), kind))
            return combo
        if param.kind == "dict" and param.keys:
            return self._mapping_editor(param, value, [])
        if param.kind == "dict" or (param.kind in ("list", "any") and isinstance(value, (dict, list))
                                    and len(to_text(value)) > LONG_TEXT):
            # a mapping (the states of a state machine, the signals of a simulator) or a long list:
            # a big text field, one entry a line
            edit = YamlEdit(to_block_text(value), self, lines=14 if self.wide else 6)
            if param.default not in (None, {}, []):
                edit.setPlaceholderText(to_block_text(param.default))
            edit.setToolTip("YAML, one entry a line; applied when the field is left or with Ctrl+Return")
            kind = "dict" if param.kind == "dict" else "any"
            edit.committed.connect(lambda text, name=param.name, kind=kind: self._emit(name, text, kind))
            return edit
        if param.kind == "path":
            holder = QWidget(self)
            row = QHBoxLayout(holder)
            row.setContentsMargins(0, 0, 0, 0)
            edit = QLineEdit(to_text(value), holder)
            if param.default is not None:
                edit.setPlaceholderText(to_text(param.default))
            edit.editingFinished.connect(lambda name=param.name, edit=edit: self._emit(name, edit.text(), "path"))
            row.addWidget(edit, 1)
            browse = QPushButton("...", holder)
            browse.setToolTip("Choose a file")
            browse.setFixedWidth(30)
            browse.clicked.connect(lambda _checked=False, name=param.name, edit=edit: self._browse(name, edit))
            row.addWidget(browse)
            holder.edit = edit
            return holder
        if param.kind == "choice":
            combo = QComboBox(self)
            choices = list(param.choices)
            combo.addItem("", None)
            for choice in choices:
                combo.addItem(str(choice), choice)
            if value is not None and combo.findData(value) < 0:
                combo.addItem(str(value), value)
            combo.setCurrentIndex(max(combo.findData(value), 0) if value is not None else 0)
            combo.currentIndexChanged.connect(
                lambda _index, name=param.name, combo=combo: self.param_changed.emit(self.node.id, name, combo.currentData()))
            return combo
        if param.kind == "code":
            edit = QPlainTextEdit(to_text(value if value is not None else param.default), self)
            edit.setMinimumHeight(180 if self.wide else 90)
            edit.setTabStopDistance(28)
            if param.name == "code":  # Python (a comment's text is kind code too)
                from PySide6.QtGui import QFontDatabase

                from ..widgets.completer import attach
                from .highlighter import PythonHighlighter

                edit.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
                edit.setMinimumHeight(360 if self.wide else 160)
                edit.highlighter = PythonHighlighter(edit.document())
                edit.completer = attach(edit, self._python_completions)
            commit = _Debounced(lambda name=param.name, edit=edit: self.param_changed.emit(
                self.node.id, name, edit.toPlainText()))
            edit.textChanged.connect(commit)
            return edit
        edit = QLineEdit(to_text(value), self)
        if param.default is not None:
            edit.setPlaceholderText(to_text(param.default))
        edit.editingFinished.connect(lambda name=param.name, edit=edit, kind=param.kind: self._emit(name, edit.text(), kind))
        return edit

    def _python_completions(self, text: str):
        """Completions in the node's Python code: ctx, np, signals, units and its port names."""
        from ...lab.completion import python_completions

        inputs = [str(name) for name in self.node.params.get("inputs") or []]
        outputs = [str(name) for name in self.node.params.get("outputs", ["out"]) or []]
        if self.spec is not None:  # the ports as the type makes them (also those it always has)
            spec_inputs, spec_outputs = self.spec.resolve_ports(self.node.params)
            inputs = list(dict.fromkeys(inputs + [port.name for port in spec_inputs]))
            outputs = list(dict.fromkeys(outputs + [port.name for port in spec_outputs]))
        return python_completions(text, inputs=inputs, outputs=outputs, devices=self.devices)

    def _list_editor(self, param: ParamSpec, value: Any, offered: list[str]) -> QWidget:
        """A list parameter: its text, and a menu to tick the values the device has."""
        holder = QWidget(self)
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        edit = QLineEdit(to_text(value), holder)
        edit.setPlaceholderText(to_text(param.default) if param.default is not None else "choose from the list ▾")
        edit.editingFinished.connect(lambda name=param.name, edit=edit: self._emit(name, edit.text(), "list"))
        row.addWidget(edit, 1)
        button = QToolButton(holder)
        button.setText("▾")
        button.setToolTip(f"Choose {param.name}")
        button.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(button)
        current = [str(item) for item in value] if isinstance(value, (list, tuple)) else []

        def toggled(_checked: bool, name=param.name) -> None:
            chosen = [action.text() for action in menu.actions() if action.isChecked()]
            kept = [item for item in current if item not in offered]  # values the device does not tell of
            values = kept + chosen
            edit.setText(to_text(values) if values else "")
            if self.node.params.get(name) != (values or None):
                self.param_changed.emit(self.node.id, name, values or None)

        for item in offered:
            action = menu.addAction(str(item))
            action.setCheckable(True)
            action.setChecked(str(item) in current)
            action.toggled.connect(toggled)
        button.setMenu(menu)
        row.addWidget(button)
        holder.edit, holder.menu = edit, menu
        return holder

    def _mapping_editor(self, param: ParamSpec, value: Any, offered: list[str]) -> QWidget:
        """A mapping of fixed names (a decoder's channels) to values: a field for each name."""
        holder = QWidget(self)
        form = QFormLayout(holder)
        form.setContentsMargins(0, 0, 0, 0)
        mapping = dict(value) if isinstance(value, dict) else {}
        holder.boxes = {}

        def commit(name=param.name) -> None:
            values = {key: box.currentText().strip() for key, box in holder.boxes.items() if box.currentText().strip()}
            values.update({key: item for key, item in mapping.items() if key not in holder.boxes})
            if self.node.params.get(name) != (values or None):
                self.param_changed.emit(self.node.id, name, values or None)

        for key in param.keys:
            box = QComboBox(holder)
            box.setEditable(True)
            box.addItem("")
            box.addItems([str(item) for item in offered])
            box.setEditText(to_text(mapping.get(key)) if mapping.get(key) is not None else "")
            box.lineEdit().setPlaceholderText("not wired")
            box.activated.connect(lambda _index: commit())
            box.lineEdit().editingFinished.connect(commit)
            holder.boxes[key] = box
            form.addRow(key, box)
        return holder

    def _emit(self, name: str, text: str, kind: str) -> None:
        try:
            value = from_text(text, kind)
            spec = self.spec.param(name) if self.spec else None
            if spec is not None and spec.kind == "quantity" and isinstance(value, str):
                units.parse(value, spec.unit)
        except (ValueError, yaml.YAMLError, units.UnitError) as error:
            self.errors[name] = str(error)
            self.error_label.setText(f"{name}: {error}")
            return
        self.errors.pop(name, None)
        self.error_label.setText("")
        if self.node.params.get(name) != value:
            self.param_changed.emit(self.node.id, name, value)

    def _browse(self, name: str, edit: QLineEdit) -> None:
        import os

        from PySide6.QtWidgets import QFileDialog

        start = os.path.join(self.folder, edit.text()) if self.folder else edit.text()
        path, _ = QFileDialog.getSaveFileName(self, f"File for {name}", start, options=QFileDialog.DontConfirmOverwrite)
        if not path:
            return
        path = flow_path(path, self.folder, self.base)
        edit.setText(path)
        self._emit(name, path, "path")

    def _rename(self) -> None:
        new = self.name_edit.text().strip()
        if new and new != self.node.id:
            self.renamed.emit(self.node.id, new)


class _Debounced:
    """Calls ``function`` when edits pause (the code editor sends every keystroke)."""

    def __init__(self, function: Callable[[], None]) -> None:
        from PySide6.QtCore import QTimer

        self.timer = QTimer()
        self.timer.setSingleShot(True)
        self.timer.setInterval(400)
        self.timer.timeout.connect(function)

    def __call__(self, *args) -> None:
        self.timer.start()


class FlowInspector(QWidget):
    """The flow when no node is selected: name, description, run settings."""

    def has_focus(self) -> bool:
        """Whether the user is typing in one of its fields."""
        focused = QApplication.focusWidget()
        return focused is not None and (focused is self or self.isAncestorOf(focused))


    name_changed = Signal(str)
    description_changed = Signal(str)
    setting_changed = Signal(str, object)
    #: a panel of the flow to open (its document or its file), a new panel for it
    panel_open_requested = Signal(object)
    new_panel_requested = Signal()

    def __init__(self, flow: Flow, parent: Optional[QWidget] = None, panels: Optional[list] = None) -> None:
        """``panels``: ``(title, target)`` of the panels that use the flow."""
        super().__init__(parent)
        self.flow = flow
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 10)
        heading = QLabel("Flow", self)
        set_role(heading, "heading")
        layout.addWidget(heading)
        form = QFormLayout()
        self.name_edit = QLineEdit(flow.name, self)
        self.name_edit.editingFinished.connect(lambda: self.name_changed.emit(self.name_edit.text().strip() or "Flow"))
        form.addRow("Name", self.name_edit)
        # several lines are kept (a single line field would drop them)
        self.description_edit = QPlainTextEdit(flow.description.strip(), self)
        self.description_edit.setFixedHeight(64)
        self._description = _Debounced(lambda: self.description_changed.emit(self.description_edit.toPlainText()))
        self.description_edit.textChanged.connect(self._description)
        form.addRow("Description", self.description_edit)
        self.seed_edit = QLineEdit(to_text(flow.settings.get("seed")), self)
        self.seed_edit.setPlaceholderText("1")
        self.seed_edit.editingFinished.connect(self._seed)
        form.addRow("Seed", self.seed_edit)
        self.duration_edit = QLineEdit(to_text(flow.settings.get("duration")), self)
        self.duration_edit.setPlaceholderText("until the flow ends")
        self.duration_edit.editingFinished.connect(self._duration)
        form.addRow("Duration", self.duration_edit)
        layout.addLayout(form)
        self.error_label = QLabel("", self)
        set_role(self.error_label, "error")
        self.error_label.setWordWrap(True)
        layout.addWidget(self.error_label)

        panels_heading = QLabel("Panels", self)
        set_role(panels_heading, "heading")
        layout.addWidget(panels_heading)
        #: a button for every panel that uses the flow
        self.panel_buttons = []
        for title, target in panels or []:
            button = QPushButton(title, self)
            set_icon(button, "panel")
            button.setToolTip(f"Open the panel {target}" if isinstance(target, str) else "Show the panel")
            button.setStyleSheet("text-align: left")
            button.clicked.connect(lambda _checked=False, target=target: self.panel_open_requested.emit(target))
            layout.addWidget(button)
            self.panel_buttons.append(button)
        if not panels:
            none = QLabel("No panel uses this flow yet.", self)
            set_role(none, "hint")
            layout.addWidget(none)
        new_panel = QPushButton("New panel for this flow", self)
        set_icon(new_panel, "plus")
        new_panel.clicked.connect(self.new_panel_requested.emit)
        layout.addWidget(new_panel)

        hint = QLabel("Devices are nodes: drag an open instrument, a simulator or a device of the project from "
                      "the palette (Devices) and wire its 'device' output to the nodes that use it.", self)
        set_role(hint, "hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addStretch(1)

    def _seed(self) -> None:
        text = self.seed_edit.text().strip()
        if text and not text.lstrip("-").isdigit():
            self.error_label.setText(f"Seed: {text!r} is not a whole number")
            return
        self.error_label.setText("")
        self.setting_changed.emit("seed", int(text) if text else None)

    def _duration(self) -> None:
        text = self.duration_edit.text().strip()
        if text:
            try:
                units.parse(text, "s")
            except (ValueError, units.UnitError) as error:
                self.error_label.setText(f"Duration: {error}")
                return
        self.error_label.setText("")
        self.setting_changed.emit("duration", text or None)


class NodeEditor(QDialog):
    """A node's parameters in a window of their own (a double-click on the node): the same editors
    as the inspector, with bigger text fields. Changes apply at once; the window stays open while
    the flow is edited."""

    def __init__(self, node_id: str, inspector_factory: Callable[[str], "NodeInspector"], title: str,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        #: the node it edits (follows a rename)
        self.node_id = node_id
        self.setWindowTitle(title)
        self.resize(720, 640)
        self._factory = inspector_factory
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 10)
        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        layout.addWidget(self.scroll, 1)
        row = QHBoxLayout()
        row.setContentsMargins(10, 0, 10, 0)
        hint_label = QLabel("Changes apply at once (text fields when you leave them, Ctrl+Return in big ones).", self)
        set_role(hint_label, "hint")
        row.addWidget(hint_label, 1)
        close = QPushButton("Close", self)
        close.clicked.connect(self.close)
        row.addWidget(close)
        layout.addLayout(row)
        self.inspector: Optional[NodeInspector] = None
        self.rebuild()

    def rebuild(self) -> None:
        """The editors again (after the node changed outside, e.g. an undo)."""
        self.inspector = self._factory(self.node_id)
        self.scroll.setWidget(self.inspector)

    def has_focus(self) -> bool:
        focus = QApplication.focusWidget()
        return focus is not None and self.isAncestorOf(focus)
