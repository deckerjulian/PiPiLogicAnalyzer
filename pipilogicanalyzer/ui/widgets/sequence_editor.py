# Copyright (C) 2026 Julian Decker
#
# Part of PiPiLogicAnalyzer.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Editor of a trigger sequence: stages of patterns, edges, pulse widths and gaps."""

from __future__ import annotations

from typing import Optional, Sequence

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ...core.formatting import parse_time, to_small_time
from ...driver.models import ConditionKind, EdgeKind, TriggerCondition, TriggerSequence, TriggerStage
from ..icons import set_icon
from ..theme import set_role

KIND_LABELS = {
    ConditionKind.EDGE: "Edge",
    ConditionKind.PATTERN: "Pattern",
    ConditionKind.PULSE: "Pulse width",
    ConditionKind.GAP: "Gap (no edge for)",
}
EDGE_LABELS = {EdgeKind.RISING: "Rising ↑", EdgeKind.FALLING: "Falling ↓", EdgeKind.ANY: "Any ↕"}
LEVEL_LABELS = {EdgeKind.RISING: "High", EdgeKind.FALLING: "Low", EdgeKind.ANY: "High or low"}


def _time_text(nanoseconds: Optional[int]) -> str:
    return "" if nanoseconds is None else to_small_time(nanoseconds / 1e9)


def _nanoseconds(text: str) -> Optional[int]:
    text = text.strip()
    return None if not text else int(round(parse_time(text) * 1e9))


class StageEditor(QFrame):
    removed = Signal(object)
    moved = Signal(object, int)

    def __init__(self, channels: Sequence[tuple[int, str]], kinds: Sequence[ConditionKind], parent=None) -> None:
        super().__init__(parent)
        set_role(self, "card")
        self.channels = list(channels)
        grid = QGridLayout(self)
        grid.setContentsMargins(10, 6, 10, 8)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(4)

        self.title = QLabel(self)
        set_role(self.title, "heading")
        grid.addWidget(self.title, 0, 0)
        self.kind_combo = QComboBox(self)
        for kind in kinds:
            self.kind_combo.addItem(KIND_LABELS[kind], kind)
        self.kind_combo.currentIndexChanged.connect(self._update_fields)
        grid.addWidget(self.kind_combo, 0, 1)
        self.channel_combo = QComboBox(self)
        for number, name in self.channels:
            self.channel_combo.addItem(name, number)
        grid.addWidget(self.channel_combo, 0, 2)
        self.edge_combo = QComboBox(self)
        grid.addWidget(self.edge_combo, 0, 3)
        self.pattern_edit = QLineEdit(self)
        self.pattern_edit.setPlaceholderText("Pattern from the channel, e.g. 10X1")
        self.pattern_edit.setMinimumWidth(180)
        grid.addWidget(self.pattern_edit, 0, 3)

        buttons = QHBoxLayout()
        for icon_name, tip, step in (("up", "Earlier stage", -1), ("down", "Later stage", 1)):
            button = QPushButton(self)
            set_icon(button, icon_name)
            button.setToolTip(tip)
            button.clicked.connect(lambda _checked=False, step=step: self.moved.emit(self, step))
            buttons.addWidget(button)
        remove = QPushButton(self)
        set_icon(remove, "trash")
        remove.setToolTip("Remove the stage")
        remove.clicked.connect(lambda: self.removed.emit(self))
        buttons.addWidget(remove)
        grid.addLayout(buttons, 0, 5)

        self.min_edit = QLineEdit(self)
        self.min_edit.setPlaceholderText("at least, e.g. 1 µs")
        grid.addWidget(self.min_edit, 1, 1)
        self.max_edit = QLineEdit(self)
        self.max_edit.setPlaceholderText("at most, e.g. 2 µs")
        grid.addWidget(self.max_edit, 1, 2)

        count_row = QHBoxLayout()
        count_row.addWidget(QLabel("Times", self))
        self.count_box = QSpinBox(self)
        self.count_box.setRange(1, 65535)
        count_row.addWidget(self.count_box)
        count_row.addWidget(QLabel("within", self))
        self.within_edit = QLineEdit(self)
        self.within_edit.setPlaceholderText("any time")
        self.within_edit.setToolTip("The stage has to follow the previous one within this time, else the sequence starts over")
        count_row.addWidget(self.within_edit, 1)
        grid.addLayout(count_row, 1, 3, 1, 2)
        grid.setColumnStretch(4, 1)
        self._update_fields()

    def set_index(self, index: int) -> None:
        self.title.setText(f"Stage {index + 1}")
        self.within_edit.setEnabled(index > 0)

    def _update_fields(self) -> None:
        kind = self.kind_combo.currentData()
        pattern = kind == ConditionKind.PATTERN
        self.pattern_edit.setVisible(pattern)
        self.edge_combo.setVisible(kind in (ConditionKind.EDGE, ConditionKind.PULSE))
        current = self.edge_combo.currentData()
        self.edge_combo.clear()
        for edge, label in (LEVEL_LABELS if kind == ConditionKind.PULSE else EDGE_LABELS).items():
            self.edge_combo.addItem(label, edge)
        self.edge_combo.setCurrentIndex(max(self.edge_combo.findData(current), 0))
        self.min_edit.setVisible(kind in (ConditionKind.PULSE, ConditionKind.GAP))
        self.max_edit.setVisible(kind == ConditionKind.PULSE)
        self.min_edit.setPlaceholderText("no edge for, e.g. 10 ms" if kind == ConditionKind.GAP else "at least, e.g. 1 µs")

    def set_stage(self, stage: TriggerStage) -> None:
        condition = stage.condition
        self.kind_combo.setCurrentIndex(max(self.kind_combo.findData(condition.kind), 0))
        if condition.kind == ConditionKind.PATTERN:
            numbers = [n for n in range(64) if condition.mask >> n & 1]
            first = numbers[0] if numbers else 0
            self.channel_combo.setCurrentIndex(max(self.channel_combo.findData(first), 0))
            last = numbers[-1] if numbers else first
            self.pattern_edit.setText(
                "".join(
                    ("1" if condition.value >> n & 1 else "0") if condition.mask >> n & 1 else "X"
                    for n in range(first, last + 1)
                )
            )
        else:
            self.channel_combo.setCurrentIndex(max(self.channel_combo.findData(condition.channel), 0))
            self.edge_combo.setCurrentIndex(max(self.edge_combo.findData(condition.edge), 0))
        self.min_edit.setText(_time_text(condition.min_ns))
        self.max_edit.setText(_time_text(condition.max_ns))
        self.count_box.setValue(stage.count)
        self.within_edit.setText(_time_text(stage.within_ns))

    def stage(self) -> TriggerStage:
        """The stage; raises ``ValueError`` with a message for invalid input."""
        from ..panels.search_panel import parse_pattern

        kind = self.kind_combo.currentData()
        channel = self.channel_combo.currentData()
        condition = TriggerCondition(kind=kind, channel=channel, edge=self.edge_combo.currentData() or EdgeKind.RISING)
        if kind == ConditionKind.PATTERN:
            condition.mask, condition.value = parse_pattern(self.pattern_edit.text(), channel)
        if kind == ConditionKind.PULSE:
            condition.min_ns = _nanoseconds(self.min_edit.text())
            condition.max_ns = _nanoseconds(self.max_edit.text())
            if condition.min_ns is None and condition.max_ns is None:
                raise ValueError("A pulse width condition needs a minimum or a maximum width.")
        if kind == ConditionKind.GAP:
            condition.min_ns = _nanoseconds(self.min_edit.text())
            if not condition.min_ns:
                raise ValueError("A gap condition needs the time without an edge.")
        within = _nanoseconds(self.within_edit.text()) if self.within_edit.isEnabled() else None
        return TriggerStage(condition=condition, count=self.count_box.value(), within_ns=within)


class SequenceEditor(QWidget):
    """Stages of a trigger sequence, limited to what the device supports."""

    changed = Signal()

    def __init__(
        self,
        channels: Sequence[tuple[int, str]],
        max_stages: int = 8,
        kinds: Sequence[ConditionKind] = tuple(ConditionKind),
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.channels = list(channels)
        self.max_stages = max_stages
        self.kinds = [kind for kind in KIND_LABELS if kind in kinds]
        self.stages: list[StageEditor] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        holder = QWidget(scroll)
        self.stage_layout = QVBoxLayout(holder)
        self.stage_layout.setContentsMargins(0, 0, 0, 0)
        self.stage_layout.setSpacing(6)
        self.stage_layout.addStretch(1)
        scroll.setWidget(holder)
        scroll.setMinimumHeight(170)
        layout.addWidget(scroll, 1)

        row = QHBoxLayout()
        self.add_button = QPushButton("Add stage", self)
        set_icon(self.add_button, "plus")
        self.add_button.clicked.connect(lambda: self.add_stage())
        row.addWidget(self.add_button)
        self.limit_label = QLabel(self)
        set_role(self.limit_label, "hint")
        row.addWidget(self.limit_label, 1)
        layout.addLayout(row)
        self.add_stage()

    def add_stage(self, stage: Optional[TriggerStage] = None) -> None:
        if len(self.stages) >= self.max_stages:
            return
        editor = StageEditor(self.channels, self.kinds, self)
        if stage is not None:
            editor.set_stage(stage)
        editor.removed.connect(self._remove)
        editor.moved.connect(self._move)
        self.stages.append(editor)
        self._relayout()

    def _remove(self, editor: StageEditor) -> None:
        if len(self.stages) <= 1:
            return
        self.stages.remove(editor)
        editor.deleteLater()
        self._relayout()

    def _move(self, editor: StageEditor, step: int) -> None:
        index = self.stages.index(editor)
        target = index + step
        if 0 <= target < len(self.stages):
            self.stages[index], self.stages[target] = self.stages[target], self.stages[index]
            self._relayout()

    def _relayout(self) -> None:
        for editor in self.stages:
            self.stage_layout.removeWidget(editor)
        for index, editor in enumerate(self.stages):
            self.stage_layout.insertWidget(index, editor)
            editor.set_index(index)
        self.add_button.setEnabled(len(self.stages) < self.max_stages)
        self.limit_label.setText(
            f"{len(self.stages)} of {self.max_stages} stages; the capture triggers when the last stage completes."
        )
        self.changed.emit()

    def set_sequence(self, sequence: Optional[TriggerSequence]) -> None:
        if not sequence or not sequence.stages:
            return
        for editor in self.stages:
            editor.deleteLater()
        self.stages = []
        for stage in sequence.stages[: self.max_stages]:
            self.add_stage(stage)

    def sequence(self) -> TriggerSequence:
        """Raises ``ValueError`` naming the stage with invalid input."""
        stages = []
        for index, editor in enumerate(self.stages):
            try:
                stages.append(editor.stage())
            except ValueError as error:
                raise ValueError(f"Stage {index + 1}: {error}") from None
        return TriggerSequence(stages)
