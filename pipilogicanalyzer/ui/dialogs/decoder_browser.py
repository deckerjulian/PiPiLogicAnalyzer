# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of PiPiLogicAnalyzer, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Decoder picker (port of ``Controls/SigrokDecoderManager.axaml.cs``'s tree)."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QLabel,
    QLineEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QWidget,
)

from ...sigrok.engine import DecoderInfo, DecoderRegistry
from ..theme import TEXT_MUTED
from .common import accept_button, button_box, dialog_layout


class DecoderBrowserDialog(QDialog):
    """Lets the user pick one decoder, grouped by category."""

    def __init__(
        self,
        registry: DecoderRegistry,
        parent: Optional[QWidget] = None,
        only_inputs: Optional[set[str]] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Stack protocol decoder" if only_inputs is not None else "Add protocol decoder")
        self.resize(560, 600)

        self.registry = registry
        self.only_inputs = only_inputs
        self.selected: Optional[DecoderInfo] = None

        layout = dialog_layout(self)

        self.search = QLineEdit(self)
        self.search.setPlaceholderText("Search by name or description...")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._populate)
        layout.addWidget(self.search)

        self.tree = QTreeWidget(self)
        self.tree.setHeaderLabels(["Decoder", "Description"])
        self.tree.setColumnWidth(0, 200)
        self.tree.itemSelectionChanged.connect(self._on_selection_changed)
        self.tree.itemDoubleClicked.connect(lambda *_: self._accept_if_valid())
        layout.addWidget(self.tree, 1)

        self.details = QLabel(self)
        self.details.setWordWrap(True)
        self.details.setMinimumHeight(64)
        self.details.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        layout.addWidget(self.details)

        stacking = only_inputs is not None
        buttons = button_box(
            self, "Stack decoder" if stacking else "Add decoder", icon="layers" if stacking else "plus"
        )
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._buttons = buttons
        self._accept_button = accept_button(buttons)
        self._accept_button.setEnabled(False)

        self._populate()
        self.search.setFocus()

    def _matching_decoders(self) -> list[DecoderInfo]:
        text = self.search.text().strip().lower()
        result = []
        for info in self.registry.decoders:
            # Every decoder is listed when adding; for stacked ones the decoder
            # manager picks or creates the decoder that feeds them.
            if self.only_inputs is not None and not (set(info.inputs) & self.only_inputs):
                continue
            if text and text not in f"{info.id} {info.name} {info.longname} {info.desc}".lower():
                continue
            result.append(info)
        return result

    def _populate(self) -> None:
        self.tree.clear()
        grouped: dict[str, list[DecoderInfo]] = {}
        for info in self._matching_decoders():
            for tag in info.tags or ("Uncategorized",):
                grouped.setdefault(str(tag), []).append(info)

        for tag in sorted(grouped, key=str.lower):
            parent = QTreeWidgetItem(self.tree, [f"{tag} ({len(grouped[tag])})", ""])
            parent.setFirstColumnSpanned(True)
            parent.setFlags(parent.flags() & ~Qt.ItemIsSelectable)
            for info in sorted(grouped[tag], key=lambda item: item.longname.lower()):
                description = info.longname
                if not info.is_base_decoder and self.only_inputs is None:
                    description += f"  (decodes {', '.join(info.inputs)})"
                child = QTreeWidgetItem(parent, [info.name, description])
                child.setData(0, Qt.UserRole, info)
                child.setToolTip(1, info.desc)
            parent.setExpanded(bool(self.search.text().strip()))

        if not self.registry.decoders:
            item = QTreeWidgetItem(self.tree, ["No decoders found", ""])
            item.setFirstColumnSpanned(True)
        elif not grouped:
            item = QTreeWidgetItem(self.tree, ["No decoder matches the search", ""])
            item.setFirstColumnSpanned(True)

    def _current_info(self) -> Optional[DecoderInfo]:
        items = self.tree.selectedItems()
        if not items:
            return None
        return items[0].data(0, Qt.UserRole)

    def _on_selection_changed(self) -> None:
        info = self._current_info()
        self._accept_button.setEnabled(info is not None)
        if info is None:
            self.details.setText(
                f"<span style='color:{TEXT_MUTED}'>Select a decoder to see its channels and outputs.</span>"
            )
            return

        channels = ", ".join(channel.name for channel in info.channels) or "none"
        self.details.setText(
            f"<b>{info.longname}</b><br>{info.desc}<br>"
            f"<span style='color:{TEXT_MUTED}'>Channels:</span> {channels}<br>"
            f"<span style='color:{TEXT_MUTED}'>Inputs:</span> {', '.join(info.inputs) or '-'} &nbsp; "
            f"<span style='color:{TEXT_MUTED}'>Outputs:</span> {', '.join(info.outputs) or '-'}"
        )

    def _accept_if_valid(self) -> None:
        info = self._current_info()
        if info is None:
            return
        self.selected = info
        self.accept()
