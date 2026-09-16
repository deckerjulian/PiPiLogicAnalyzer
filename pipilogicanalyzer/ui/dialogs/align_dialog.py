# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of PiPiLogicAnalyzer, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Choosing how the boards of a multi device capture are aligned."""

from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import QButtonGroup, QDialog, QGroupBox, QRadioButton, QVBoxLayout, QWidget

from ...core.alignment import DeviceAlignment
from .common import button_box, dialog_layout, hint


def option_text(option: DeviceAlignment) -> str:
    if option.method == "reference":
        method = f"Reference line {option.source} (exact)"
    else:
        method = f"Clock {option.source} (estimate)"
    if not option.changed:
        return f"{method}: already aligned, no correction"
    text = f"{method}: move by {option.offset:+.0f} samples"
    total = option.drift * option.sample_count
    if round(total) != 0:
        text += f", drift {total:+.1f} samples over the capture"
    return text


class AlignDialog(QDialog):
    """One group per board with every method that could measure its offset."""

    def __init__(self, options: dict[int, list[DeviceAlignment]], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Align boards")
        self.setMinimumWidth(520)
        self.options = options
        self._groups: dict[int, QButtonGroup] = {}

        layout = dialog_layout(self)
        layout.addWidget(
            hint(
                "Several methods can align these boards. A reference line (a signal of the board also "
                "connected to the first board) measures the offset exactly; the clock method estimates "
                "it from the edges of the clock on the first board.",
                self,
            )
        )

        for device, candidates in sorted(options.items()):
            group = QGroupBox(f"Board {device + 1}", self)
            group_layout = QVBoxLayout(group)
            buttons = QButtonGroup(group)
            for index, candidate in enumerate(candidates):
                radio = QRadioButton(option_text(candidate), group)
                radio.setChecked(index == 0)
                buttons.addButton(radio, index)
                group_layout.addWidget(radio)
            unchanged = QRadioButton("Leave unchanged", group)
            buttons.addButton(unchanged, len(candidates))
            group_layout.addWidget(unchanged)
            self._groups[device] = buttons
            layout.addWidget(group)

        dialog_buttons = button_box(self, "Align", icon="check")
        dialog_buttons.accepted.connect(self.accept)
        dialog_buttons.rejected.connect(self.reject)
        layout.addWidget(dialog_buttons)

    def select(self, device: int, index: int) -> None:
        self._groups[device].button(index).setChecked(True)

    @property
    def choices(self) -> dict[int, Optional[DeviceAlignment]]:
        result: dict[int, Optional[DeviceAlignment]] = {}
        for device, buttons in self._groups.items():
            index = buttons.checkedId()
            candidates = self.options[device]
            result[device] = candidates[index] if 0 <= index < len(candidates) else None
        return result
