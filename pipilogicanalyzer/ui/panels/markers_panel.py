# Copyright (C) 2026 Julian Decker
#
# Part of PiPiLogicAnalyzer.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Named markers and regions of the capture, to jump to them."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core import colors
from ...core.formatting import to_small_time, to_thousands
from ..icons import set_icon
from ..view_model import Bookmark, CaptureViewModel

COLUMNS = ("Name", "Time", "Sample", "Length")


class MarkersPanel(QWidget):
    def __init__(self, model: CaptureViewModel, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.model = model
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.tree = QTreeWidget(self)
        self.tree.setHeaderLabels(list(COLUMNS))
        self.tree.setRootIsDecorated(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.setEditTriggers(QAbstractItemView.EditKeyPressed | QAbstractItemView.SelectedClicked)
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        for column in range(1, len(COLUMNS)):
            self.tree.header().setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self.tree.itemActivated.connect(self._go_to)
        self.tree.itemChanged.connect(self._renamed)
        layout.addWidget(self.tree, 1)

        buttons = QHBoxLayout()
        self.add_button = QPushButton("Add marker", self)
        set_icon(self.add_button, "bookmark")
        self.add_button.setToolTip("Marker at cursor A, or in the middle of the view (M at the pointer)")
        self.add_button.clicked.connect(self.add_marker)
        buttons.addWidget(self.add_button)
        self.remove_button = QPushButton("Remove", self)
        set_icon(self.remove_button, "trash")
        self.remove_button.clicked.connect(self.remove_selected)
        buttons.addWidget(self.remove_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self._filling = False
        model.bookmarks_changed.connect(self.refresh)
        model.regions_changed.connect(self.refresh)
        model.capture_changed.connect(self.refresh)
        self.refresh()

    def refresh(self) -> None:
        model = self.model
        self._filling = True
        try:
            self.tree.clear()
            markers = QTreeWidgetItem(self.tree, ["Markers"])
            for bookmark in model.bookmarks:
                item = QTreeWidgetItem(
                    markers,
                    [bookmark.name, to_small_time(model.time_of(bookmark.sample)), to_thousands(bookmark.sample), ""],
                )
                item.setData(0, Qt.UserRole, bookmark)
                item.setFlags(item.flags() | Qt.ItemIsEditable)
                item.setForeground(0, QBrush(colors.BOOKMARK_COLOR))
            regions = QTreeWidgetItem(self.tree, ["Regions"])
            frequency = max(model.frequency, 1)
            for region in sorted(model.regions, key=lambda item: item.start):
                item = QTreeWidgetItem(
                    regions,
                    [
                        region.region_name or "(unnamed)",
                        to_small_time(model.time_of(region.start)),
                        to_thousands(region.start),
                        to_small_time(region.sample_count / frequency),
                    ],
                )
                item.setData(0, Qt.UserRole, region)
            for group in (markers, regions):
                group.setFirstColumnSpanned(True)
                group.setFlags(group.flags() & ~Qt.ItemIsSelectable)
                group.setExpanded(True)
        finally:
            self._filling = False
        self.add_button.setEnabled(model.sample_count > 0)

    def add_marker(self) -> None:
        model = self.model
        if not model.sample_count:
            return
        sample = model.cursor("A")
        if sample is None:
            sample = model.first_sample + model.visible_samples // 2
        model.add_bookmark(sample)

    def remove_selected(self) -> None:
        for item in self.tree.selectedItems():
            target = item.data(0, Qt.UserRole)
            if isinstance(target, Bookmark):
                self.model.remove_bookmark(target)
            elif target is not None:
                self.model.remove_region(target)

    def _go_to(self, item: QTreeWidgetItem) -> None:
        target = item.data(0, Qt.UserRole)
        if isinstance(target, Bookmark):
            self.model.center_on(target.sample)
        elif target is not None:
            span = max(target.sample_count, 4)
            self.model.set_view(target.start - span // 10, int(span * 1.2))

    def _renamed(self, item: QTreeWidgetItem, column: int) -> None:
        target = item.data(0, Qt.UserRole)
        if not self._filling and column == 0 and isinstance(target, Bookmark):
            self.model.rename_bookmark(target, item.text(0).strip() or target.name)
