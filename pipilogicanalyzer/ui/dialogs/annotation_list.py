# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of PiPiLogicAnalyzer, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The annotations of one decoder row as a list, in a window of its own.

Long rows such as a disassembly are easier to read, filter and copy as a list than in the
waveform. Selecting an entry shows it in the waveform, and hovering an annotation in the
waveform selects its entry.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QAbstractTableModel, QItemSelectionModel, QModelIndex, QSortFilterProxyModel, Qt
from PySide6.QtGui import QFontDatabase, QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableView,
    QWidget,
)

from ...core.formatting import to_small_time, to_thousands
from ...sigrok.composition import compose
from ..icons import set_icon
from ..view_model import AnnotationHover, CaptureViewModel
from .common import button_box, dialog_layout, hint

COLUMNS = ("Sample", "Time", "Duration", "Type", "Value")
SAMPLE_COLUMN, TIME_COLUMN, DURATION_COLUMN, TYPE_COLUMN, VALUE_COLUMN = range(len(COLUMNS))


class AnnotationListModel(QAbstractTableModel):
    """The segments of one annotation row, ordered by their first sample."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.segments: list = []
        self.type_names: list[str] = []
        self.frequency = 0
        self.trigger_sample = 0
        self._rows_by_segment: dict[int, int] = {}
        self._search_texts: dict[int, str] = {}
        self._fixed_font = QFontDatabase.systemFont(QFontDatabase.FixedFont)

    def set_segments(self, segments, type_names, frequency: int, trigger_sample: int) -> None:
        self.beginResetModel()
        self.segments = sorted(segments, key=lambda segment: (segment.first_sample, segment.last_sample))
        self.type_names = list(type_names)
        self.frequency = frequency
        self.trigger_sample = trigger_sample
        self._rows_by_segment = {id(segment): row for row, segment in enumerate(self.segments)}
        self._search_texts.clear()
        self.endResetModel()

    def row_of(self, segment) -> Optional[int]:
        return self._rows_by_segment.get(id(segment))

    def type_name(self, segment) -> str:
        if 0 <= segment.type_id < len(self.type_names):
            return self.type_names[segment.type_id]
        return str(segment.type_id)

    def search_text(self, row: int) -> str:
        """Type and every value of an entry (long and short form), lower case."""
        text = self._search_texts.get(row)
        if text is None:
            segment = self.segments[row]
            text = " ".join([self.type_name(segment), *segment.values]).lower()
            self._search_texts[row] = text
        return text

    def text(self, row: int, column: int) -> str:
        segment = self.segments[row]
        if column == SAMPLE_COLUMN:
            return to_thousands(segment.first_sample)
        if column == TIME_COLUMN:
            if not self.frequency:
                return "-"
            return to_small_time((segment.first_sample - self.trigger_sample) / self.frequency)
        if column == DURATION_COLUMN:
            return to_small_time(segment.sample_count / self.frequency) if self.frequency else "-"
        if column == TYPE_COLUMN:
            return self.type_name(segment)
        return segment.values[0] if segment.values else ""

    # ------------------------------------------------------------- Qt model
    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802 - Qt naming
        return 0 if parent.isValid() else len(self.segments)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802 - Qt naming
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section: int, orientation, role=Qt.DisplayRole):  # noqa: N802 - Qt naming
        if orientation != Qt.Horizontal:
            return None
        if role == Qt.DisplayRole:
            return COLUMNS[section]
        if role == Qt.ToolTipRole and section == TIME_COLUMN:
            return "Time of the first sample, relative to the trigger"
        return None

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row, column = index.row(), index.column()
        if role == Qt.DisplayRole:
            return self.text(row, column)
        if role == Qt.ToolTipRole:
            if column == VALUE_COLUMN:
                return "\n".join(self.segments[row].values)
            return self.text(row, column)
        if role == Qt.FontRole and column == VALUE_COLUMN:
            return self._fixed_font
        if role == Qt.TextAlignmentRole and column in (SAMPLE_COLUMN, TIME_COLUMN, DURATION_COLUMN):
            return int(Qt.AlignRight | Qt.AlignVCenter)
        return None


class AnnotationFilter(QSortFilterProxyModel):
    """Keeps the entries whose type or values contain the filter text."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.text = ""

    def set_text(self, text: str) -> None:
        if hasattr(self, "beginFilterChange"):  # Qt 6.9 and later
            self.beginFilterChange()
            self.text = text.strip().lower()
            self.endFilterChange()
        else:
            self.text = text.strip().lower()
            self.invalidateFilter()

    def filterAcceptsRow(self, row: int, parent: QModelIndex) -> bool:  # noqa: N802 - Qt naming
        return not self.text or self.text in self.sourceModel().search_text(row)


class AnnotationListWindow(QDialog):
    """Lists the annotations of one row; follows new decoder runs of the same decoder."""

    def __init__(self, model: CaptureViewModel, group, annotation, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setModal(False)
        self.setWindowFlags(self.windowFlags() | Qt.WindowMinMaxButtonsHint)
        self.resize(820, 600)

        self.model = model
        self.instance = group.instance
        self.decoder_name = group.decoder_name
        self.row_name = annotation.name
        self.group = None
        self._following_hover = False

        layout = dialog_layout(self)
        self.summary = hint("", self)
        layout.addWidget(self.summary)

        filter_row = QHBoxLayout()
        self.filter_edit = QLineEdit(self)
        self.filter_edit.setPlaceholderText("Filter by type or value, e.g. JSR or $FD")
        self.filter_edit.setClearButtonEnabled(True)
        filter_row.addWidget(self.filter_edit, 1)
        self.count_label = QLabel(self)
        filter_row.addWidget(self.count_label)
        self.copy_button = QPushButton("Copy", self)
        set_icon(self.copy_button, "copy")
        self.copy_button.setToolTip("Copy the selected entries (all listed entries without a selection) as tab separated text")
        filter_row.addWidget(self.copy_button)
        self.copy_values_button = QPushButton("Copy values", self)
        self.copy_values_button.setToolTip("Copy only the values, e.g. a disassembly listing")
        filter_row.addWidget(self.copy_values_button)
        layout.addLayout(filter_row)

        self.list_model = AnnotationListModel(self)
        self.proxy = AnnotationFilter(self)
        self.proxy.setSourceModel(self.list_model)

        self.table = QTableView(self)
        self.table.setModel(self.proxy)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        metrics = self.table.fontMetrics()
        vertical = self.table.verticalHeader()
        vertical.setVisible(False)
        vertical.setSectionResizeMode(QHeaderView.Fixed)
        vertical.setDefaultSectionSize(metrics.height() + 8)
        header = self.table.horizontalHeader()
        header.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        header.setStretchLastSection(True)
        for column, sample_text in ((SAMPLE_COLUMN, "00,000,000"), (TIME_COLUMN, "-000.000 µs"),
                                    (DURATION_COLUMN, "000.000 µs"), (TYPE_COLUMN, "Memory region  ")):
            self.table.setColumnWidth(column, metrics.horizontalAdvance(sample_text) + 24)
        layout.addWidget(self.table, 1)

        buttons = button_box(self, None)
        layout.addWidget(buttons)

        self.filter_edit.textChanged.connect(self._on_filter_changed)
        self.copy_button.clicked.connect(lambda: self.copy_entries(values_only=False))
        self.copy_values_button.clicked.connect(lambda: self.copy_entries(values_only=True))
        QShortcut(QKeySequence.Copy, self.table, lambda: self.copy_entries(values_only=False))
        self.table.selectionModel().currentRowChanged.connect(self._on_current_row_changed)
        model.annotations_changed.connect(self.refresh)
        model.capture_changed.connect(self.refresh)
        model.hover_changed.connect(self._follow_hover)

        self.refresh()

    # ------------------------------------------------------------------ state
    def _find_row(self):
        """(group, annotation) of the listed row in the current decoder results."""
        groups = self.model.annotation_groups
        candidates = [group for group in groups if group.instance is self.instance]
        if not candidates:
            # The decoders were configured again (e.g. a profile was loaded): same decoder and label.
            candidates = [
                group for group in groups
                if group.instance.decoder_id == self.instance.decoder_id
                and group.instance.label == self.instance.label
            ]
        for group in candidates:
            for annotation in group.annotations:
                if annotation.name == self.row_name:
                    return group, annotation
        return None, None

    def refresh(self) -> None:
        current = self.current_segment()
        restore = (current.first_sample, current.type_id) if current is not None else None

        group, annotation = self._find_row()
        self.group = group
        if group is not None:
            self.instance = group.instance
            self.decoder_name = group.decoder_name
        self.setWindowTitle(f"{self.decoder_name}: {self.row_name}")

        session = self.model.session
        if annotation is None:
            self.list_model.set_segments([], [], 0, 0)
            self.summary.setText("The decoder does not produce this row at the moment. Decode the capture again to fill the list.")
        else:
            type_names = [entry[1] for entry in group.info.annotations] if group.info is not None else []
            frequency = session.frequency if session is not None else 0
            trigger = session.pre_trigger_samples if session is not None else 0
            self.list_model.set_segments(annotation.segments, type_names, frequency, trigger)
            self.summary.setText(
                "Select an entry to show it in the waveform; hovering an annotation in the waveform "
                "selects its entry. Times are relative to the trigger."
            )
        self._update_count()

        if restore is not None:
            for segment in self.list_model.segments:
                if (segment.first_sample, segment.type_id) == restore:
                    self.select_segment(segment, show=False)
                    break

    def _update_count(self) -> None:
        total = self.list_model.rowCount()
        listed = self.proxy.rowCount()
        if listed == total:
            self.count_label.setText(f"{to_thousands(total)} entries")
        else:
            self.count_label.setText(f"{to_thousands(listed)} of {to_thousands(total)} entries")

    def _on_filter_changed(self, text: str) -> None:
        self.proxy.set_text(text)
        self._update_count()
        current = self.table.currentIndex()
        if current.isValid():
            self.table.scrollTo(current, QAbstractItemView.PositionAtCenter)

    def current_segment(self):
        index = self.table.currentIndex()
        if not index.isValid():
            return None
        row = self.proxy.mapToSource(index).row()
        return self.list_model.segments[row] if 0 <= row < len(self.list_model.segments) else None

    # ------------------------------------------------------------- selection
    def select_segment(self, segment, show: bool = True) -> bool:
        """Select the entry of ``segment`` (clearing a filter that hides it)."""
        row = self.list_model.row_of(segment)
        if row is None:
            return False
        index = self.proxy.mapFromSource(self.list_model.index(row, 0))
        if not index.isValid():
            self.filter_edit.clear()
            index = self.proxy.mapFromSource(self.list_model.index(row, 0))
        following = self._following_hover
        self._following_hover = following or not show
        try:
            self.table.selectionModel().setCurrentIndex(
                index, QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows
            )
        finally:
            self._following_hover = following
        self.table.scrollTo(index, QAbstractItemView.PositionAtCenter)
        return True

    def _on_current_row_changed(self, current: QModelIndex, _previous: QModelIndex) -> None:
        if self._following_hover or not current.isValid():
            return
        segment = self.current_segment()
        if segment is not None:
            self.show_in_waveform(segment)

    def show_in_waveform(self, segment) -> None:
        """Scroll the waveform to ``segment`` (when it is out of view) and mark it."""
        model = self.model
        if segment.first_sample < model.first_sample or segment.last_sample > model.last_sample:
            model.center_on((segment.first_sample + max(segment.last_sample, segment.first_sample)) // 2)
        group = self.group
        composition = None
        if group is not None and group.info is not None:
            composition = compose(group.info, group.instance, model.channels, segment)
        model.set_hover(AnnotationHover(group=group, segment=segment, composition=composition))

    def _follow_hover(self) -> None:
        hover = self.model.hover
        if hover is None or hover.group is not self.group or hover.segment is self.current_segment():
            return
        self._following_hover = True
        try:
            self.select_segment(hover.segment, show=False)
        finally:
            self._following_hover = False

    # ------------------------------------------------------------------ copy
    def copy_entries(self, values_only: bool = False) -> str:
        """Copy the selected entries (or all listed ones) to the clipboard; returns the text."""
        rows = sorted({index.row() for index in self.table.selectionModel().selectedRows()})
        if not rows:
            rows = list(range(self.proxy.rowCount()))
        source_rows = [self.proxy.mapToSource(self.proxy.index(row, 0)).row() for row in rows]

        if values_only:
            lines = [self.list_model.text(row, VALUE_COLUMN) for row in source_rows]
        else:
            lines = ["\t".join(COLUMNS)]
            lines += [
                "\t".join(self.list_model.text(row, column) for column in range(len(COLUMNS)))
                for row in source_rows
            ]
        text = "\n".join(lines)
        QGuiApplication.clipboard().setText(text)
        return text
