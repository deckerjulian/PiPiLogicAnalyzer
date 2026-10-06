# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""*Simulated multi device…*: several simulated boards captured together as one device, like a
multi device set of real boards (e.g. two Picos with 48 channels for the C64 expansion port)."""

from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import QComboBox, QDialog, QFormLayout, QLabel, QSpinBox, QWidget

from ...driver.simulated.profiles import MAX_BOARDS, available_profiles, load_profile
from ..theme import set_role
from .common import button_box, dialog_layout, hint


class SimulatedMultiDialog(QDialog):
    """Choose the board and how many of it; ``spec`` after *Connect* (``pico*2``)."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Simulated multi device")
        self.setMinimumWidth(420)
        self.spec = ""
        layout = dialog_layout(self)
        layout.addWidget(hint("Several simulated boards captured together as one device: their channels one "
                              "board after the other, as with a multi device set of real boards.", self))
        form = QFormLayout()
        self.profile_box = QComboBox(self)
        for name in available_profiles():
            try:
                profile = load_profile(name)
            except (OSError, ValueError, KeyError):
                continue
            if profile["digital"]:
                self.profile_box.addItem(f"{profile['title']} ({len(profile['digital'])} channels)", name)
        preferred = self.profile_box.findData("pico")
        self.profile_box.setCurrentIndex(max(preferred, 0))
        form.addRow("Board", self.profile_box)
        self.boards_box = QSpinBox(self)
        self.boards_box.setRange(2, MAX_BOARDS)
        form.addRow("Boards", self.boards_box)
        layout.addLayout(form)
        self.total_label = QLabel(self)
        set_role(self.total_label, "hint")
        layout.addWidget(self.total_label)
        self.profile_box.currentIndexChanged.connect(self._update)
        self.boards_box.valueChanged.connect(self._update)
        buttons = button_box(self, "Connect", icon="plug")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._update()

    def _update(self) -> None:
        name = self.profile_box.currentData()
        if name is None:
            self.total_label.setText("No simulator has digital channels.")
            return
        channels = len(load_profile(name)["digital"]) * self.boards_box.value()
        self.total_label.setText(f"{channels} channels in total" + (
            " – enough for the C64 bus (48)" if channels >= 48 else ""))

    def _accept(self) -> None:
        name = self.profile_box.currentData()
        if name is None:
            return
        self.spec = f"{name}*{self.boards_box.value()}"
        self.accept()
