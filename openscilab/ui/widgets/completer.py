# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Completions while typing in a text field (``QPlainTextEdit`` or ``QLineEdit``).

The words come from a *provider* ``(text before the cursor) -> (start of the word, completions)``
(:mod:`openscilab.lab.completion`). The list opens by itself after a letter, a dot, a colon and
the like, or with ``Ctrl+Space``; ``Enter`` or ``Tab`` takes the chosen completion, ``Esc``
closes the list.
"""

from __future__ import annotations

from typing import Callable, Union

from PySide6.QtCore import QEvent, QObject, QRect, Qt, QTimer
from PySide6.QtGui import QStandardItem, QStandardItemModel, QTextCursor
from PySide6.QtWidgets import QCompleter, QLineEdit, QPlainTextEdit

#: typed characters that open the list even when no word is begun (a value after ``key: ``, a port
#: after ``node.``, a list item after ``[`` or ``,``)
TRIGGERS = set(".:[,{ \"'")
TEXT_ROLE = Qt.UserRole + 1
#: at most this many completions are listed
LIMIT = 200

Provider = Callable[[str], tuple[int, list]]


class Completer(QObject):
    """Offers completions in ``editor`` from ``provider``."""

    def __init__(self, editor: Union[QPlainTextEdit, QLineEdit], provider: Provider) -> None:
        super().__init__(editor)
        self.editor = editor
        self.provider = provider
        self.model = QStandardItemModel(self)
        self.completer = QCompleter(self.model, self)
        self.completer.setWidget(editor)
        self.completer.setCompletionMode(QCompleter.UnfilteredPopupCompletion)
        self.completer.setCaseSensitivity(Qt.CaseInsensitive)
        self.completer.setMaxVisibleItems(12)
        self.completer.popup().clicked.connect(self._chosen)
        self._start = 0
        self._typing = False
        #: a key with text was pressed: the next change of the text was typed
        self._keyed = False
        editor.installEventFilter(self)
        if isinstance(editor, QPlainTextEdit):
            editor.textChanged.connect(self._text_changed)
        else:
            editor.textEdited.connect(lambda _text: self._text_changed())
        self._last_key = ""

    # ------------------------------------------------------------------ text
    def _text_before_cursor(self) -> str:
        if isinstance(self.editor, QPlainTextEdit):
            cursor = self.editor.textCursor()
            return self.editor.toPlainText()[:cursor.position()]
        return self.editor.text()[:self.editor.cursorPosition()]

    def _cursor_rect(self) -> QRect:
        rect = self.editor.cursorRect()
        rect.setWidth(max(self.completer.popup().sizeHintForColumn(0), 240)
                      + self.completer.popup().verticalScrollBar().sizeHint().width())
        return rect

    @property
    def visible(self) -> bool:
        return self.completer.popup().isVisible()

    def items(self) -> list[str]:
        """The completions listed now (what is inserted for each)."""
        return [self.model.item(row).data(TEXT_ROLE) for row in range(self.model.rowCount())]

    # ----------------------------------------------------------------- show
    def update(self, forced: bool = False) -> bool:
        """Ask the provider and show the list (``forced``: also without a begun word)."""
        text = self._text_before_cursor()
        try:
            start, completions = self.provider(text)
        except Exception:  # noqa: BLE001 - a provider that fails offers nothing; typing goes on
            completions = []
            start = len(text)
        word = text[start:]
        if not completions or not (forced or word or self._last_key in TRIGGERS):
            self.completer.popup().hide()
            self.model.clear()
            return False
        self._start = start
        self.model.clear()
        for completion in completions[:LIMIT]:
            label = completion.text + (f"    {completion.detail}" if completion.detail else "")
            item = QStandardItem(label)
            item.setData(completion.inserted, TEXT_ROLE)
            item.setToolTip(completion.detail)
            self.model.appendRow(item)
        popup = self.completer.popup()
        popup.setCurrentIndex(self.model.index(0, 0))
        self.completer.complete(self._cursor_rect())
        return True

    def _text_changed(self) -> None:
        if self._typing or not self._keyed:
            return  # text set by the program (another view, undo), not typed
        self._keyed = False
        self.update()

    # --------------------------------------------------------------- choose
    def _chosen(self, index) -> None:
        text = index.data(TEXT_ROLE) if hasattr(index, "data") else None
        if text is None:
            return
        self.insert(text)

    def insert(self, text: str) -> None:
        """Replace the begun word with ``text``."""
        self._typing = True
        try:
            if isinstance(self.editor, QPlainTextEdit):
                cursor = self.editor.textCursor()
                position = cursor.position()
                block_start = position - len(self._text_before_cursor()) + self._start
                cursor.setPosition(block_start)
                cursor.setPosition(position, QTextCursor.KeepAnchor)
                cursor.insertText(text)
                self.editor.setTextCursor(cursor)
            else:
                position = self.editor.cursorPosition()
                current = self.editor.text()
                self.editor.setText(current[:self._start] + text + current[position:])
                self.editor.setCursorPosition(self._start + len(text))
        finally:
            self._typing = False
        self.completer.popup().hide()
        if isinstance(self.editor, QLineEdit):
            self.editor.textEdited.emit(self.editor.text())  # (a text edit told its own handlers itself)
        if text.endswith((" ", ".", "-> ")):
            QTimer.singleShot(0, lambda: self.update(forced=True))  # ``rate: `` → its values

    # ---------------------------------------------------------------- keys
    def eventFilter(self, watched, event) -> bool:
        if watched is not self.editor or event.type() != QEvent.KeyPress:
            return False
        key = event.key()
        if self.visible:
            if key in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Tab):
                index = self.completer.popup().currentIndex()
                if index.isValid():
                    self._chosen(index)
                    return True
            if key == Qt.Key_Escape:
                self.completer.popup().hide()
                return True
            if key in (Qt.Key_Up, Qt.Key_Down, Qt.Key_PageUp, Qt.Key_PageDown):
                popup = self.completer.popup()
                rows = self.model.rowCount()
                step = {Qt.Key_Up: -1, Qt.Key_Down: 1, Qt.Key_PageUp: -10, Qt.Key_PageDown: 10}[key]
                row = min(max(popup.currentIndex().row() + step, 0), rows - 1)
                popup.setCurrentIndex(self.model.index(row, 0))
                return True
        if key == Qt.Key_Space and event.modifiers() & (Qt.ControlModifier | Qt.MetaModifier):
            self._last_key = ""
            self.update(forced=True)
            return True
        self._last_key = event.text()[-1:] if event.text() else ""
        self._keyed = bool(event.text()) or key in (Qt.Key_Backspace, Qt.Key_Delete)
        return False


def attach(editor: Union[QPlainTextEdit, QLineEdit], provider: Provider) -> Completer:
    """Completions from ``provider`` while typing in ``editor``."""
    return Completer(editor, provider)

