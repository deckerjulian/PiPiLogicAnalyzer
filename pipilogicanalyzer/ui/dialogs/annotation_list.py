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

The other rows of the same decoder are columns of their own: every entry shows what they
annotate during it, e.g. the bus cycles (the bytes read) and the memory region of an
instruction. The details below the list show every form of the selected entry, those entries
one by one, and the channel levels the decoder read.
"""

from __future__ import annotations

from typing import Optional

import numpy as np
from PySide6.QtCore import QAbstractTableModel, QItemSelectionModel, QModelIndex, QSortFilterProxyModel, Qt
from PySide6.QtGui import QFontDatabase, QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableView,
    QWidget,
)

from ...core.formatting import to_small_time, to_thousands
from ...sigrok.composition import compose
from ..icons import set_icon
from ..view_model import CaptureViewModel
from .common import button_box, dialog_layout, hint

COLUMNS = ("Sample", "Time", "Duration", "Type", "Value")
SAMPLE_COLUMN, TIME_COLUMN, DURATION_COLUMN, TYPE_COLUMN, VALUE_COLUMN = range(len(COLUMNS))
#: Separator of several entries of another row in one cell
RELATED_SEPARATOR = " · "


def compact_value(values) -> str:
    """The shortest form of an annotation with at least two characters (decoders list their
    texts from long to short: the bus cycle ``R $C000 = $A9`` ends with ``A9``)."""
    forms = [value for value in values if len(value) >= 2] or list(values)
    return min(forms, key=len) if forms else ""


class RelatedRow:
    """Another annotation row of the decoder, searchable by time."""

    def __init__(self, name: str, segments) -> None:
        self.name = name
        self.segments = sorted(segments, key=lambda segment: segment.first_sample)
        self.starts = np.array([segment.first_sample for segment in self.segments], dtype=np.int64)
        self.ends = np.array([segment.last_sample for segment in self.segments], dtype=np.int64)
        self.longest = int((self.ends - self.starts).max()) if len(self.segments) else 0

    def during(self, segment) -> list:
        """The entries overlapping ``segment``; for an instant, the entries around it."""
        first, last = segment.first_sample, max(segment.last_sample, segment.first_sample)
        low = int(np.searchsorted(self.starts, first - self.longest, side="left"))
        high = int(np.searchsorted(self.starts, last, side="right"))
        found = []
        for index in range(low, high):
            start, end = int(self.starts[index]), int(self.ends[index])
            overlaps = start <= first <= end if first == last else start < last and end > first
            if overlaps:
                found.append(self.segments[index])
        return found


class AnnotationListModel(QAbstractTableModel):
    """The segments of one annotation row, ordered by their first sample."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.segments: list = []
        self.type_names: list[str] = []
        self.related: list[RelatedRow] = []
        self._related_texts: dict[tuple[int, int], str] = {}
        self.frequency = 0
        self.trigger_sample = 0
        self._rows_by_segment: dict[int, int] = {}
        self._search_texts: dict[int, str] = {}
        self._fixed_font = QFontDatabase.systemFont(QFontDatabase.FixedFont)

    def set_segments(self, segments, type_names, frequency: int, trigger_sample: int, related=()) -> None:
        self.beginResetModel()
        self.related = list(related)
        self._related_texts.clear()
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
        """Type, every value of an entry and the other rows during it, lower case."""
        text = self._search_texts.get(row)
        if text is None:
            segment = self.segments[row]
            # The other rows in their short and their long form ("D0" and "R $C00C = $D0")
            related = [self.related_text(row, index) for index in range(len(self.related))]
            related += [
                entry.values[0]
                for index in range(len(self.related))
                for entry in self.related_entries(row, index)
                if entry.values
            ]
            text = " ".join([self.type_name(segment), *segment.values, *related]).lower()
            self._search_texts[row] = text
        return text

    @property
    def columns(self) -> list[str]:
        return list(COLUMNS) + [related.name for related in self.related]

    def related_entries(self, row: int, index: int) -> list:
        return self.related[index].during(self.segments[row])

    def related_text(self, row: int, index: int) -> str:
        key = (row, index)
        text = self._related_texts.get(key)
        if text is None:
            entries = [entry for entry in self.related_entries(row, index) if entry.values]
            # One entry in full, several (e.g. the bytes of an instruction) in their short form
            if len(entries) == 1:
                text = entries[0].values[0]
            else:
                text = " ".join(compact_value(entry.values) for entry in entries)
            self._related_texts[key] = text
        return text

    def text(self, row: int, column: int) -> str:
        if column >= len(COLUMNS):
            return self.related_text(row, column - len(COLUMNS))
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
        return 0 if parent.isValid() else len(COLUMNS) + len(self.related)

    def headerData(self, section: int, orientation, role=Qt.DisplayRole):  # noqa: N802 - Qt naming
        if orientation != Qt.Horizontal:
            return None
        if role == Qt.DisplayRole:
            return self.columns[section] if section < len(self.columns) else None
        if role == Qt.ToolTipRole and section >= len(COLUMNS):
            return f"What the row '{self.columns[section]}' of the decoder shows during each entry"
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
            if column > VALUE_COLUMN:
                return "\n".join(
                    entry.values[0] for entry in self.related_entries(row, column - len(COLUMNS)) if entry.values
                )
            return self.text(row, column)
        if role == Qt.FontRole and column >= VALUE_COLUMN:
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
        self.resize(1100, 700)

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
        self.columns_button = QPushButton("Columns", self)
        set_icon(self.columns_button, "list")
        self.columns_button.setToolTip("Show or hide the other rows of the decoder")
        self.columns_menu = QMenu(self.columns_button)
        self.columns_menu.aboutToShow.connect(self._fill_columns_menu)
        self.columns_button.setMenu(self.columns_menu)
        filter_row.addWidget(self.columns_button)
        layout.addLayout(filter_row)
        #: Names of the other rows the user hid
        self._hidden_related: set[str] = set()
        self._sized_columns: set[int] = set()

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
                                    (DURATION_COLUMN, "000.000 µs"), (TYPE_COLUMN, "Memory region  "),
                                    (VALUE_COLUMN, "$C000  LDA ($FB),Y  (undocumented)")):
            self.table.setColumnWidth(column, metrics.horizontalAdvance(sample_text) + 24)
        self.details = QPlainTextEdit(self)
        self.details.setReadOnly(True)
        self.details.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self.details.setPlaceholderText("Select an entry for its details")
        splitter = QSplitter(Qt.Vertical, self)
        splitter.addWidget(self.table)
        splitter.addWidget(self.details)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([480, 160])
        layout.addWidget(splitter, 1)

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
            related = [
                RelatedRow(other.name, other.segments)
                for other in group.annotations
                if other is not annotation and other.segments
            ]
            self.list_model.set_segments(annotation.segments, type_names, frequency, trigger, related)
            self._apply_column_visibility()
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

    def _apply_column_visibility(self) -> None:
        for index, related in enumerate(self.list_model.related):
            column = len(COLUMNS) + index
            self.table.setColumnHidden(column, related.name in self._hidden_related)
            if column not in self._sized_columns:
                self._sized_columns.add(column)
                self.table.setColumnWidth(column, 200)
        self.columns_button.setVisible(bool(self.list_model.related))

    def _fill_columns_menu(self) -> None:
        self.columns_menu.clear()
        for related in self.list_model.related:
            action = self.columns_menu.addAction(related.name)
            action.setCheckable(True)
            action.setChecked(related.name not in self._hidden_related)
            action.toggled.connect(lambda checked, name=related.name: self._toggle_related(name, checked))

    def _toggle_related(self, name: str, shown: bool) -> None:
        if shown:
            self._hidden_related.discard(name)
        else:
            self._hidden_related.add(name)
        self._apply_column_visibility()

    def details_text(self, segment) -> str:
        """Everything about ``segment``: its forms, the other rows during it, the channels read."""
        model = self.list_model
        row = model.row_of(segment)
        lines = [f"{model.type_name(segment)}  ·  sample {to_thousands(segment.first_sample)}"]
        if model.frequency:
            start = (segment.first_sample - model.trigger_sample) / model.frequency
            lines[0] += f"  ·  t = {to_small_time(start)}  ·  {to_small_time(segment.sample_count / model.frequency)}"
        lines.append("")
        for value in dict.fromkeys(segment.values):
            lines.append(f"  {value}")
        if row is not None:
            for index, related in enumerate(model.related):
                entries = model.related_entries(row, index)
                if not entries:
                    continue
                lines += ["", f"{related.name}:"]
                for entry in entries:
                    offset = ""
                    if model.frequency:
                        offset = f"{to_small_time((entry.first_sample - segment.first_sample) / model.frequency):>12}  "
                    lines.append(f"  {offset}{entry.values[0] if entry.values else ''}")
        group = self.group
        if group is not None and group.info is not None:
            composition = compose(group.info, group.instance, self.model.channels, segment)
            if composition is not None:
                described = composition.describe()
                if described:
                    lines += ["", f"Channels read at sample {to_thousands(composition.sample)}:"]
                    lines += [f"  {line}" for line in described.splitlines()]
        return "\n".join(lines)

    def _show_details(self) -> None:
        segment = self.current_segment()
        self.details.setPlainText(self.details_text(segment) if segment is not None else "")

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
        self._show_details()
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
        from ..widgets.annotation_viewer import build_hover

        if self.group is None:
            return
        model.set_hover(build_hover(model.annotation_groups, model.channels, self.group, segment))

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
            columns = [
                column for column in range(self.list_model.columnCount()) if not self.table.isColumnHidden(column)
            ]
            lines = ["\t".join(self.list_model.columns[column] for column in columns)]
            lines += [
                "\t".join(self.list_model.text(row, column) for column in columns)
                for row in source_rows
            ]
        text = "\n".join(lines)
        QGuiApplication.clipboard().setText(text)
        return text
