# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of PiPiLogicAnalyzer, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Measurements of a sample selection.

Port of ``Dialogs/MeasureDialog.axaml.cs`` and ``Controls/ChannelMeasures``.
The pulse statistics are computed by :mod:`pipilogicanalyzer.core.analysis`; the
dialog presents them as a table instead of a stack of panels, and adds the duty
cycle and the edge count.
"""

from __future__ import annotations

from typing import Optional, Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from ...core.analysis import measure_channel
from ...core.formatting import to_large_frequency, to_small_time, to_thousands
from ...driver.models import AnalyzerChannel
from ..theme import TEXT_MUTED
from .common import button_box, dialog_layout

COLUMNS = (
    "Channel",
    "Positive pulses",
    "Negative pulses",
    "Avg. high",
    "Avg. low",
    "Predicted high",
    "Predicted low",
    "Avg. frequency",
    "Predicted frequency",
    "Duty cycle",
)


class MeasureDialog(QDialog):
    """Shows the pulse statistics of every channel over a selection."""

    def __init__(
        self,
        channels: Sequence[AnalyzerChannel],
        first_sample: int,
        sample_count: int,
        frequency: int,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Measurements")
        self.resize(1120, 440)

        layout = dialog_layout(self)

        period = sample_count / frequency if frequency else 0.0
        sample_period = 1.0 / frequency if frequency else 0.0
        facts = (
            ("Samples", f"{to_thousands(sample_count)}"),
            ("Range", f"{to_thousands(first_sample)} – {to_thousands(first_sample + sample_count - 1)}"),
            ("Duration", to_small_time(period)),
            ("Sample period", to_small_time(sample_period)),
        )
        summary = QLabel(
            " &nbsp;&nbsp;·&nbsp;&nbsp; ".join(
                f"<span style='color:{TEXT_MUTED}'>{name}</span> <b>{value}</b>" for name, value in facts
            ),
            self,
        )
        layout.addWidget(summary)

        table = QTableWidget(len(channels), len(COLUMNS), self)
        table.setHorizontalHeaderLabels(COLUMNS)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectRows)
        table.setAlternatingRowColors(True)
        table.setShowGrid(False)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        table.horizontalHeader().setStretchLastSection(True)

        for row, channel in enumerate(channels):
            samples = None
            if channel.samples is not None:
                samples = channel.samples[first_sample : first_sample + sample_count]
            measures = measure_channel(samples, frequency, channel.display_name)

            values = (
                channel.display_name,
                to_thousands(measures.positive_pulses),
                to_thousands(measures.negative_pulses),
                to_small_time(measures.average_positive_duration),
                to_small_time(measures.average_negative_duration),
                to_small_time(measures.predicted_positive_duration),
                to_small_time(measures.predicted_negative_duration),
                to_large_frequency(measures.average_frequency),
                to_large_frequency(measures.predicted_frequency),
                f"{measures.duty_cycle:.1f} %",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                table.setItem(row, column, item)

        layout.addWidget(table, 1)
        layout.addWidget(button_box(self, None))
