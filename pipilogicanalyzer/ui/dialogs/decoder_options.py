# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of PiPiLogicAnalyzer 7, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Channel assignment and option editor of a decoder instance.

Port of ``Controls/SigrokDecoderOptions.axaml.cs``.  Channels the capture does
not contain simply cannot be picked, and required channels that are still
unassigned are highlighted instead of silently producing no annotations.
"""

from __future__ import annotations

from typing import Optional, Sequence

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ...driver.models import AnalyzerChannel
from ...sigrok.engine import DecoderInfo, OptionType
from ...sigrok.provider import DecoderInstance
from .common import InlineMessage, accept_button, button_box, dialog_layout, heading, hint

UNASSIGNED = "Not assigned"


class DecoderOptionsDialog(QDialog):
    """Edit the channel map and the options of one decoder instance."""

    def __init__(
        self,
        info: DecoderInfo,
        instance: DecoderInstance,
        channels: Sequence[AnalyzerChannel],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"{info.longname} settings")
        self.resize(480, 580)

        self.info = info
        self.instance = instance
        self.channels = list(channels)
        self._channel_widgets: dict[int, QComboBox] = {}
        self._option_widgets: dict[str, QWidget] = {}

        layout = dialog_layout(self)
        layout.addWidget(heading(info.longname, self))
        layout.addWidget(hint(info.desc, self))

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        content = QWidget(scroll)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 4, 0)

        content_layout.addWidget(self._build_channels_group(content))
        options_group = self._build_options_group(content)
        if options_group is not None:
            content_layout.addWidget(options_group)
        content_layout.addStretch(1)

        scroll.setWidget(content)
        layout.addWidget(scroll, 1)

        self.warning = InlineMessage(self)
        layout.addWidget(self.warning)

        buttons = button_box(self, "Apply", icon="check")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._accept_button = accept_button(buttons)

        self._validate()

    # ----------------------------------------------------------------- layout
    def _build_channels_group(self, parent: QWidget) -> QGroupBox:
        group = QGroupBox("Channels", parent)
        form = QFormLayout(group)

        for channel in self.info.channels:
            combo = QComboBox(group)
            combo.addItem(UNASSIGNED, None)
            for index, capture_channel in enumerate(self.channels):
                combo.addItem(
                    f"{capture_channel.channel_number + 1}: {capture_channel.display_name}", index
                )

            assigned = self.instance.channel_map.get(channel.index)
            if assigned is not None:
                position = combo.findData(assigned)
                combo.setCurrentIndex(max(position, 0))

            combo.currentIndexChanged.connect(lambda *_: self._validate())
            combo.setToolTip(channel.desc)
            label = f"{channel.name}{' *' if channel.required else ''}:"
            form.addRow(label, combo)
            self._channel_widgets[channel.index] = combo

        if not self.info.channels:
            form.addRow(QLabel("This decoder takes its input from another decoder.", group))
        elif self.info.required_channels:
            form.addRow(hint("* required", group))

        return group

    def _build_options_group(self, parent: QWidget) -> Optional[QGroupBox]:
        if not self.info.options:
            return None

        group = QGroupBox("Options", parent)
        form = QFormLayout(group)

        for option in self.info.options:
            value = self.instance.options.get(option.id, option.default)
            widget: QWidget

            if option.option_type == OptionType.LIST:
                combo = QComboBox(group)
                for item in option.values:
                    combo.addItem(str(item), item)
                position = combo.findText(str(value))
                combo.setCurrentIndex(max(position, 0))
                widget = combo
            elif option.option_type == OptionType.BOOLEAN:
                check = QCheckBox(group)
                check.setChecked(bool(value))
                widget = check
            elif option.option_type == OptionType.INTEGER:
                spin = QSpinBox(group)
                spin.setRange(-2_147_483_647, 2_147_483_647)
                spin.setValue(int(value or 0))
                widget = spin
            elif option.option_type == OptionType.DOUBLE:
                spin_double = QDoubleSpinBox(group)
                spin_double.setDecimals(6)
                spin_double.setRange(-1e12, 1e12)
                spin_double.setValue(float(value or 0.0))
                widget = spin_double
            else:
                edit = QLineEdit(group)
                edit.setText("" if value is None else str(value))
                widget = edit

            form.addRow(f"{option.caption}:", widget)
            self._option_widgets[option.id] = widget

        return group

    # ------------------------------------------------------------- behaviour
    def _current_channel_map(self) -> dict[int, int]:
        mapping: dict[int, int] = {}
        for index, combo in self._channel_widgets.items():
            value = combo.currentData()
            if value is not None:
                mapping[index] = int(value)
        return mapping

    def _current_options(self) -> dict:
        values = {}
        for option in self.info.options:
            widget = self._option_widgets.get(option.id)
            if widget is None:
                continue
            if isinstance(widget, QComboBox):
                values[option.id] = widget.currentData()
            elif isinstance(widget, QCheckBox):
                values[option.id] = widget.isChecked()
            elif isinstance(widget, (QSpinBox, QDoubleSpinBox)):
                values[option.id] = widget.value()
            elif isinstance(widget, QLineEdit):
                values[option.id] = widget.text()
        return values

    def _validate(self) -> bool:
        mapping = self._current_channel_map()
        missing = [
            channel.name for channel in self.info.required_channels if channel.index not in mapping
        ]
        if missing:
            self.warning.show_error("Assign the required channels: " + ", ".join(missing))
            return False
        self.warning.clear()
        return True

    def _accept(self) -> None:
        if not self._validate():
            return
        self.instance.channel_map = self._current_channel_map()
        self.instance.options = self._current_options()
        self.accept()
