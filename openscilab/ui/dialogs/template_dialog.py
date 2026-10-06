# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Saving a project as a template of the user: its name and what it is for."""

from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import QDialog, QFormLayout, QLineEdit, QPlainTextEdit, QWidget

from .common import InlineMessage, button_box, dialog_layout, hint


class TemplateDialog(QDialog):
    """The name and the description of a new template; :attr:`title` and :attr:`description`
    after it was accepted."""

    def __init__(self, title: str = "", description: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Save project as template")
        self.setMinimumWidth(460)
        layout = dialog_layout(self)
        layout.addWidget(hint("A copy of the project - its flows, panels, nodes, waveforms and devices, without "
                              "the captures and results in its data folder - becomes a template: it is listed "
                              "first under Templates and on the start page, and every project made from it "
                              "starts as a copy.", self))
        form = QFormLayout()
        self.title_edit = QLineEdit(title, self)
        self.title_edit.setPlaceholderText("e.g. Sensor test bench")
        form.addRow("Name", self.title_edit)
        self.description_edit = QPlainTextEdit(description, self)
        self.description_edit.setPlaceholderText("What a project made from it is for (shown on its tile)")
        self.description_edit.setFixedHeight(80)
        form.addRow("Description", self.description_edit)
        layout.addLayout(form)
        self.message = InlineMessage(self)
        layout.addWidget(self.message)
        buttons = button_box(self, "Save template", icon="bookmark")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.title_edit.selectAll()

    @property
    def title(self) -> str:
        return " ".join(self.title_edit.text().split())

    @property
    def description(self) -> str:
        return self.description_edit.toPlainText().strip()

    def _accept(self) -> None:
        if not self.title:
            self.message.show_error("Give the template a name.")
            return
        self.accept()
