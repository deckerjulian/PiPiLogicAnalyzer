# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of PiPiLogicAnalyzer, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Channel shifting (port of ``Dialogs/ShiftChannelsDialog.axaml.cs``)."""

from __future__ import annotations

from enum import Enum
from typing import Optional, Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ...driver.models import AnalyzerChannel
from ..icons import set_icon
from .common import InlineMessage, button_box, dialog_layout, hint


class ShiftDirection(Enum):
    LEFT = "left"
    RIGHT = "right"


class ShiftMode(Enum):
    HIGH = "high"
    LOW = "low"
    ROTATE = "rotate"


class ShiftChannelsDialog(QDialog):
    """Shifts the samples of the selected channels left or right."""

    def __init__(
        self,
        channels: Sequence[AnalyzerChannel],
        max_shift: int,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Shift channels")
        self.resize(420, 540)

        self.channels = list(channels)
        self.shifted_channels: list[AnalyzerChannel] = []
        self.shift_amount = 1
        self.direction = ShiftDirection.LEFT
        self.mode = ShiftMode.LOW

        layout = dialog_layout(self)
        layout.addWidget(
            hint("Moves the samples of the selected channels in time, e.g. to compensate a delay.", self)
        )

        channels_group = QGroupBox("Channels", self)
        channels_layout = QVBoxLayout(channels_group)
        self.list = QListWidget(channels_group)
        self.list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        for channel in self.channels:
            item = QListWidgetItem(channel.display_name)
            item.setData(Qt.UserRole, channel)
            self.list.addItem(item)
        self.list.itemSelectionChanged.connect(lambda: self.message.clear())
        channels_layout.addWidget(self.list, 1)

        select_row = QHBoxLayout()
        select_all = QPushButton("Select all", channels_group)
        set_icon(select_all, "check-all")
        select_all.clicked.connect(self.list.selectAll)
        select_none = QPushButton("Clear", channels_group)
        set_icon(select_none, "clear")
        select_none.clicked.connect(self.list.clearSelection)
        select_row.addWidget(select_all)
        select_row.addWidget(select_none)
        select_row.addStretch(1)
        channels_layout.addLayout(select_row)
        layout.addWidget(channels_group, 1)

        options_group = QGroupBox("Shift", self)
        form = QFormLayout(options_group)
        self.amount_box = QSpinBox(options_group)
        self.amount_box.setRange(1, max(max_shift, 1))
        self.amount_box.setValue(1)
        self.amount_box.setGroupSeparatorShown(True)
        form.addRow("Samples:", self.amount_box)

        direction_row = QHBoxLayout()
        self.left_radio = QRadioButton("Left (earlier)", options_group)
        self.left_radio.setChecked(True)
        self.right_radio = QRadioButton("Right (later)", options_group)
        direction_row.addWidget(self.left_radio)
        direction_row.addWidget(self.right_radio)
        direction_row.addStretch(1)
        buttons_direction = QButtonGroup(options_group)
        buttons_direction.addButton(self.left_radio)
        buttons_direction.addButton(self.right_radio)
        form.addRow("Direction:", direction_row)

        mode_row = QHBoxLayout()
        self.low_radio = QRadioButton("Low", options_group)
        self.low_radio.setChecked(True)
        self.high_radio = QRadioButton("High", options_group)
        self.rotate_radio = QRadioButton("Wrap around", options_group)
        self.rotate_radio.setToolTip("The samples pushed out at one end come back at the other")
        buttons_mode = QButtonGroup(options_group)
        for radio in (self.low_radio, self.high_radio, self.rotate_radio):
            mode_row.addWidget(radio)
            buttons_mode.addButton(radio)
        mode_row.addStretch(1)
        form.addRow("Fill with:", mode_row)
        layout.addWidget(options_group)

        self.message = InlineMessage(self)
        layout.addWidget(self.message)

        buttons = button_box(self, "Shift channels", icon="shift")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _accept(self) -> None:
        selected = [item.data(Qt.UserRole) for item in self.list.selectedItems()]
        if not selected:
            self.message.show_error("Select at least one channel to shift.")
            return

        self.shifted_channels = selected
        self.shift_amount = self.amount_box.value()
        self.direction = ShiftDirection.LEFT if self.left_radio.isChecked() else ShiftDirection.RIGHT
        if self.high_radio.isChecked():
            self.mode = ShiftMode.HIGH
        elif self.rotate_radio.isChecked():
            self.mode = ShiftMode.ROTATE
        else:
            self.mode = ShiftMode.LOW
        self.accept()
