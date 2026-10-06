# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The console below the documents: problems, execution, log and a Python console."""

from __future__ import annotations

import code
import contextlib
import io
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..icons import icon
from ..theme import ERROR, TEXT_MUTED, WARNING


@dataclass(frozen=True)
class Problem:
    severity: str  # "error", "warning" or "info"
    text: str
    source: str = ""
    #: the node it is about (a double-click selects it)
    node: str = ""
    #: the document that reported it
    owner: object = field(default=None, compare=False)


class ProblemsView(QListWidget):
    """Problems of the project: type errors in a flow, a device without connection, ...
    A double-click (or Enter) on one shows where it is."""

    #: a problem was activated: show it
    problem_activated = Signal(object)
    #: the list changed (counts in the status bar)
    changed = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.NoFrame)
        self._problems: dict[str, list[Problem]] = {}
        self.itemActivated.connect(lambda item: item.data(Qt.UserRole) is not None
                                   and self.problem_activated.emit(item.data(Qt.UserRole)))

    def counts(self) -> tuple[int, int]:
        """``(errors, warnings)``."""
        problems = self.problems()
        return (sum(problem.severity == "error" for problem in problems),
                sum(problem.severity == "warning" for problem in problems))

    def set_problems(self, source: str, problems: list[Problem]) -> None:
        """Replace the problems reported by ``source``."""
        self._problems[source] = list(problems)
        self._refresh()

    def problems(self) -> list[Problem]:
        return [problem for group in self._problems.values() for problem in group]

    def _refresh(self) -> None:
        self.clear()
        colors = {"error": ERROR, "warning": WARNING, "info": TEXT_MUTED}
        for problem in self.problems():
            text = problem.text if not problem.source else f"{problem.text}    ({problem.source})"
            item = QListWidgetItem(icon("info", colors.get(problem.severity, TEXT_MUTED)), text, self)
            item.setData(Qt.UserRole, problem)
            if problem.node:
                item.setToolTip("Double-click to show the node")
        self.changed.emit()


class ExecutionView(QListWidget):
    """What a running flow does: one line per event, newest last (filled by the engine, 0c)."""

    MAX_LINES = 2000

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.NoFrame)

    def add(self, text: str) -> None:
        self.addItem(text)
        while self.count() > self.MAX_LINES:
            self.takeItem(0)
        self.scrollToBottom()


class _LogBridge(QObject):
    message = Signal(str)


class QtLogHandler(logging.Handler):
    """Sends log records to the log view (from any thread)."""

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.bridge = _LogBridge()
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.bridge.message.emit(self.format(record))
        except RuntimeError:  # the console is gone
            pass


class LogView(QPlainTextEdit):
    MAX_BLOCKS = 5000

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setMaximumBlockCount(self.MAX_BLOCKS)
        font = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        font.setStyleHint(QFont.Monospace)
        self.setFont(font)

    def add(self, text: str) -> None:
        self.appendPlainText(text)


class PythonConsole(QWidget):
    """An interactive Python prompt with access to the shell, its documents and the project."""

    def __init__(self, namespace: dict, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.namespace = namespace
        self.interpreter = code.InteractiveInterpreter(namespace)
        self._buffer: list[str] = []
        self._history: list[str] = []
        self._history_index = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.output = LogView(self)
        self.output.add("Python console: shell, documents(), active() and the module openscilab are available.")
        layout.addWidget(self.output, 1)
        row = QHBoxLayout()
        row.setContentsMargins(6, 2, 6, 4)
        self.prompt = QLabel(">>>", self)
        row.addWidget(self.prompt)
        self.input = QLineEdit(self)
        self.input.setFont(self.output.font())
        self.input.returnPressed.connect(lambda: self.execute(self.input.text()))
        self.input.installEventFilter(self)
        from ..widgets.completer import attach

        # names of the namespace and their attributes (shell., documents(), openscilab.)
        self.completer = attach(self.input, self._completions)
        row.addWidget(self.input, 1)
        layout.addLayout(row)

    def _completions(self, text: str):
        from ...lab.completion import python_completions

        return python_completions(text, namespace=self.namespace)

    def execute(self, line: str) -> str:
        """Run one line as typed at the prompt; returns what it printed."""
        self.input.clear()
        if line.strip():
            self._history.append(line)
        self._history_index = len(self._history)
        self.output.add(f"{self.prompt.text()} {line}")
        self._buffer.append(line)
        source = "\n".join(self._buffer)
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
            more = self.interpreter.runsource(source, "<console>")
        if more:
            self.prompt.setText("...")
        else:
            self._buffer.clear()
            self.prompt.setText(">>>")
        text = stream.getvalue()
        if text:
            self.output.add(text.rstrip("\n"))
        return text

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt naming
        if watched is self.input and event.type() == event.Type.KeyPress and self._history:
            if event.key() == Qt.Key_Up:
                self._history_index = max(0, self._history_index - 1)
                self.input.setText(self._history[self._history_index])
                return True
            if event.key() == Qt.Key_Down:
                self._history_index = min(len(self._history), self._history_index + 1)
                self.input.setText(self._history[self._history_index] if self._history_index < len(self._history) else "")
                return True
        return super().eventFilter(watched, event)


class Console(QTabWidget):
    """Problems · Execution · Log · Python."""

    def __init__(self, namespace: dict, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("console")
        self.setDocumentMode(True)
        self.problems = ProblemsView(self)
        self.addTab(self.problems, "Problems")
        self.execution = ExecutionView(self)
        self.addTab(self.execution, "Execution")
        self.log = LogView(self)
        self.addTab(self.log, "Log")
        self.python = PythonConsole(namespace, self)
        self.addTab(self.python, "Python")

        self.log_handler = QtLogHandler()
        self.log_handler.bridge.message.connect(self.log.add)
        logging.getLogger("openscilab").addHandler(self.log_handler)
        self.destroyed.connect(lambda: logging.getLogger("openscilab").removeHandler(self.log_handler))

    def detach_logging(self) -> None:
        logging.getLogger("openscilab").removeHandler(self.log_handler)

    def message(self, text: str) -> None:
        """A line in the log (status messages of the shell)."""
        self.log.add(f"{time.strftime('%H:%M:%S')} {text}")

    def show_tab(self, name: str) -> None:
        for index in range(self.count()):
            if self.tabText(index).lower() == name.lower():
                self.setCurrentIndex(index)
                return
