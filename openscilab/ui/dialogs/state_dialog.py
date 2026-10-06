# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""State analysis: a timing capture resampled on the edges of a clock channel."""

from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import QCheckBox, QComboBox, QDialog, QFormLayout, QLabel, QLineEdit, QSpinBox, QWidget

from ...driver.models import CaptureSession, EdgeKind
from ..theme import set_role
from .common import InlineMessage, button_box, dialog_layout, hint


class StateDialog(QDialog):
    """:attr:`result_capture` holds the states after *Resample*."""

    def __init__(self, session: CaptureSession, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.session = session
        self.result_capture = None
        self.clock_channel = 0
        self.setWindowTitle("State analysis")
        self.setMinimumWidth(460)
        layout = dialog_layout(self)
        layout.addWidget(
            hint(
                "Every edge of the clock channel becomes one state holding the levels of the "
                "channels, like a logic analyzer clocked by the device under test. The states "
                "replace the loaded capture; Analyze > Back to the timing capture returns to it. "
                "Decoders that follow the clock themselves (such as the C64 bus) need the timing "
                "capture.",
                self,
            )
        )
        form = QFormLayout()
        self.clock_combo = QComboBox(self)
        for channel in session.capture_channels:
            self.clock_combo.addItem(channel.display_name, channel.channel_number)
        form.addRow("Clock", self.clock_combo)
        self.suggestion_label = QLabel(self)
        self.suggestion_label.setWordWrap(True)
        form.addRow("", self.suggestion_label)
        self.edge_combo = QComboBox(self)
        for label, edge in (("Rising ↑", EdgeKind.RISING), ("Falling ↓", EdgeKind.FALLING), ("Both ↕", EdgeKind.ANY)):
            self.edge_combo.addItem(label, edge)
        form.addRow("Edge", self.edge_combo)
        self.delay_box = QSpinBox(self)
        self.delay_box.setRange(-1000, 1000)
        self.delay_box.setToolTip(
            "Read the levels this many samples after the clock edge (negative: before it), e.g. to "
            "respect the setup time of the data"
        )
        form.addRow("Sample offset", self.delay_box)
        self.qualifier_edit = QLineEdit(self)
        self.qualifier_edit.setPlaceholderText("optional, e.g. CH3=0 CH4=1")
        self.qualifier_edit.setToolTip("Keep only the states where these channels have these levels")
        form.addRow("Only when", self.qualifier_edit)
        self.keep_clock_box = QCheckBox("Keep the clock channel", self)
        self.keep_clock_box.setChecked(True)
        self.keep_clock_box.setToolTip(
            "Keeps every channel in its place, so the decoders still find their channels"
        )
        form.addRow("", self.keep_clock_box)
        layout.addLayout(form)
        self.message = InlineMessage(self)
        layout.addWidget(self.message)
        self.clock_combo.currentIndexChanged.connect(self.suggest)
        self.suggest()

        buttons = button_box(self, "Resample", icon="clock")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def suggest(self) -> None:
        """Edge and offset at which the other channels are stable for the chosen clock."""
        from ...core.state_mode import suggest_sampling

        clock = self.clock_combo.currentData()
        if clock is None:
            return
        try:
            suggestion = suggest_sampling(self.session, clock)
        except ValueError:
            self.suggestion_label.setText("")
            return
        self.edge_combo.setCurrentIndex(max(self.edge_combo.findData(suggestion.edge), 0))
        self.delay_box.setValue(suggestion.delay)
        unstable = suggestion.unstable[(suggestion.edge, suggestion.delay)]
        edge = "falling" if suggestion.edge == EdgeKind.FALLING else "rising"
        offset = f"{suggestion.delay:+d} sample{'s' if abs(suggestion.delay) != 1 else ''}" if suggestion.delay else "at the edge"
        stability = "no line changes there" if not unstable else f"{unstable} line changes next to it"
        self.suggestion_label.setText(f"Suggested: the {edge} edge, {offset} ({stability}).")
        set_role(self.suggestion_label, "success" if not unstable else "hint")

    def _qualifier(self) -> tuple[int, int]:
        """``(mask, value)`` of ``CH3=0 CH4=1`` (channel numbers counted from 1)."""
        mask = value = 0
        for part in self.qualifier_edit.text().replace(",", " ").split():
            name, _, level = part.partition("=")
            name = name.strip().upper().removeprefix("CH")
            if not name.isdigit() or level.strip() not in ("0", "1"):
                raise ValueError(f"'{part}' is not a condition such as CH3=0")
            number = int(name) - 1
            mask |= 1 << number
            if level.strip() == "1":
                value |= 1 << number
        return mask, value

    def _accept(self) -> None:
        from ...core.state_mode import resample_on_clock

        try:
            mask, value = self._qualifier()
            self.clock_channel = self.clock_combo.currentData()
            capture = resample_on_clock(
                self.session,
                self.clock_channel,
                self.edge_combo.currentData(),
                self.delay_box.value(),
                mask,
                value,
                self.keep_clock_box.isChecked(),
            )
        except ValueError as error:
            self.message.show_error(str(error))
            return
        if not capture.state_count:
            self.message.show_error("The clock channel has no such edges (or no state passed the condition).")
            return
        self.result_capture = capture
        self.accept()
