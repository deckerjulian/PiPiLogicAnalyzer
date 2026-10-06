# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Undo and redo of flow edits: every edit stores the model before and after it.

Flows are small, so a copy of the model per step is cheap and every kind of edit (canvas, inspector,
YAML view) undoes the same way. Edits of one parameter in a row merge into one step.
"""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtGui import QUndoCommand

from ...lab.model import Flow

#: ids of mergeable commands
MERGE_PARAM = 1
MERGE_MOVE = 2
MERGE_YAML = 3


class FlowEdit(QUndoCommand):
    """Replace the flow by ``after`` (undo: by ``before``) through ``apply(flow)``."""

    def __init__(self, text: str, before: Flow, after: Flow, apply: Callable[[Flow], None],
                 merge_key: Optional[tuple] = None, merge_id: int = -1) -> None:
        super().__init__(text)
        self.before = before
        self.after = after
        self._apply = apply
        self.merge_key = merge_key
        self._id = merge_id if merge_key is not None else -1
        self._first = True

    def id(self) -> int:  # noqa: A003 - Qt naming
        return self._id

    def mergeWith(self, other) -> bool:  # noqa: N802 - Qt naming
        if not isinstance(other, FlowEdit) or other.merge_key != self.merge_key or self.merge_key is None:
            return False
        self.after = other.after
        return True

    def redo(self) -> None:
        if self._first:
            # The edit is already in the model when it is pushed.
            self._first = False
            return
        self._apply(self.after.copy())

    def undo(self) -> None:
        self._apply(self.before.copy())
