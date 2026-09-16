# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of PiPiLogicAnalyzer, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Region editor (port of ``Dialogs/SelectedRegionDialog.axaml.cs``)."""

from __future__ import annotations

from typing import Optional

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QColorDialog,
    QDialog,
    QFormLayout,
    QLineEdit,
    QPushButton,
    QWidget,
)

from ...core.colors import DEFAULT_REGION_COLOR
from ...core.regions import SampleRegion
from ..theme import BORDER
from .common import button_box, dialog_layout, hint


class RegionDialog(QDialog):
    """Creates or edits a named, coloured sample region."""

    def __init__(self, region: SampleRegion, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        editing = bool(region.region_name)
        self.setWindowTitle("Edit region" if editing else "New region")
        self.setMinimumWidth(380)
        self.region = region
        self._color = QColor(region.region_color or DEFAULT_REGION_COLOR)

        layout = dialog_layout(self)
        form = QFormLayout()

        self.name_edit = QLineEdit(region.region_name, self)
        self.name_edit.setPlaceholderText("e.g. Reset sequence")
        form.addRow("Name:", self.name_edit)

        self.color_button = QPushButton(self)
        self.color_button.setToolTip("Choose the colour and transparency of the region")
        self.color_button.clicked.connect(self._pick_color)
        form.addRow("Colour:", self.color_button)

        form.addRow(
            "Samples:",
            hint(f"{region.start:,} – {region.end:,} ({region.sample_count:,} samples)", self),
        )
        layout.addLayout(form)

        buttons = button_box(self, "Save" if editing else "Add region", icon="check" if editing else "plus")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._update_color_button()

    def _pick_color(self) -> None:
        color = QColorDialog.getColor(
            self._color, self, "Region colour", QColorDialog.ShowAlphaChannel
        )
        if color.isValid():
            self._color = color
            self._update_color_button()

    def _update_color_button(self) -> None:
        self.color_button.setText(self._color.name(QColor.HexArgb))
        text = "#000000" if self._color.lightness() > 140 and self._color.alpha() > 120 else "#ffffff"
        self.color_button.setStyleSheet(
            f"background-color: rgba({self._color.red()},{self._color.green()},"
            f"{self._color.blue()},{self._color.alpha()}); color: {text}; border: 1px solid {BORDER};"
        )

    def _accept(self) -> None:
        self.region.region_name = self.name_edit.text()
        self.region.region_color = self._color
        self.accept()
