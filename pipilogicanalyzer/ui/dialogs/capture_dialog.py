# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of PiPiLogicAnalyzer 7, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Capture settings dialog (port of ``Dialogs/CaptureDialog.axaml.cs``).

Fixes over the original:

* the pattern trigger range check used ``trigger - 1 + bits > 16`` and therefore
  accepted patterns that overflowed the last channel;
* the settings are written once, to the application settings directory (the
  original also dropped a copy in the working directory);
* limits are refreshed whenever the channel selection changes, including for the
  pre-trigger samples while blast mode is off;
* *Reset* and loaded settings keep a multi device setup within the triggers it can
  perform (no blast mode, no bursts);
* invalid settings are explained inside the dialog instead of a message box per
  mistake, and the total length of the capture is shown while typing.

LogicAnalyzer 6.5 additions: up to 65534 bursts (V6_5 firmware), burst
measurement limited to 254 bursts with at least 100 post-trigger samples, and
profiles -- the *Profiles* button saves the current settings as a named profile,
exports/imports them as JSON files or applies a stored profile.
"""

from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ...core import colors, settings
from ...core.capture_io import session_from_dict, session_to_dict
from ...core.formatting import to_small_time, to_thousands
from ...core.profiles import (
    PROFILE_FILE_FILTER,
    Profile,
    ProfileStore,
    read_profiles_file,
    write_profiles_file,
)
from ...driver.base import (
    MAX_MEASURED_LOOP_COUNT,
    MIN_MEASURED_POST_SAMPLES,
    AnalyzerDriverBase,
    AnalyzerDriverType,
    CaptureLimits,
    pattern_fits,
)
from ...driver.models import AnalyzerChannel, CaptureSession, TriggerType
from .. import messages
from ..theme import BORDER, set_role
from ..icons import set_icon
from .common import InlineMessage, button_box, dialog_layout, hint

MAX_TRIGGER_CHANNELS = 24
CHANNELS_PER_ROW = 8


def capture_settings_file(driver_type: AnalyzerDriverType) -> str:
    """Settings file holding the last capture settings of a driver type."""
    return f"capture-settings-{driver_type.value.lower()}.json"


class ChannelSelector(QWidget):
    """Checkbox + colour + name of one capture channel."""

    def __init__(self, channel_number: int, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.channel_number = channel_number
        self.channel_color: Optional[int] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(3)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(5)

        self.enable_box = QCheckBox(f"CH{channel_number + 1}", self)
        self.enable_box.toggled.connect(self._on_toggled)
        row.addWidget(self.enable_box, 1)

        self.color_button = QToolButton(self)
        self.color_button.setFixedSize(14, 14)
        self.color_button.setCursor(Qt.PointingHandCursor)
        self.color_button.setToolTip("Channel colour")
        self.color_button.clicked.connect(self._pick_color)
        row.addWidget(self.color_button)
        layout.addLayout(row)

        self.name_edit = QLineEdit(self)
        self.name_edit.setPlaceholderText("Name")
        self.name_edit.setEnabled(False)
        self.name_edit.setMaximumWidth(84)
        layout.addWidget(self.name_edit)

        self._update_color()

    def _on_toggled(self, checked: bool) -> None:
        self.name_edit.setEnabled(checked)

    def _pick_color(self) -> None:
        color = QColorDialog.getColor(self.color(), self, "Channel colour")
        if color.isValid():
            self.channel_color = colors.color_to_uint(color)
            self._update_color()

    def color(self) -> QColor:
        if self.channel_color is None:
            return colors.get_color(self.channel_number)
        return colors.color_from_uint(self.channel_color)

    def _update_color(self) -> None:
        self.color_button.setStyleSheet(
            f"background-color: {self.color().name()}; border: 1px solid {BORDER}; "
            "border-radius: 3px; padding: 0; min-height: 0;"
        )

    @property
    def enabled(self) -> bool:
        return self.enable_box.isChecked()

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self.enable_box.setChecked(value)

    @property
    def channel_name(self) -> str:
        return self.name_edit.text()

    @channel_name.setter
    def channel_name(self, value: str) -> None:
        self.name_edit.setText(value or "")

    def reset(self) -> None:
        self.enabled = False
        self.channel_name = ""
        self.channel_color = None
        self._update_color()


class CaptureDialog(QDialog):
    """Configures a capture for the connected device."""

    def __init__(
        self,
        driver: AnalyzerDriverBase,
        parent: Optional[QWidget] = None,
        profiles: Optional[ProfileStore] = None,
        decoder_configuration: Optional[list] = None,
        accept_text: Optional[str] = None,
        persist: bool = True,
    ) -> None:
        super().__init__(parent)
        self.driver = driver
        #: Store the accepted settings as the next defaults (not when editing a profile).
        self.persist = persist
        self.selected_settings: Optional[CaptureSession] = None
        self.settings_file = capture_settings_file(driver.driver_type)
        self.profiles = profiles if profiles is not None else ProfileStore()
        #: Decoders stored alongside profiles saved from this dialog.
        self.decoder_configuration = decoder_configuration
        self.limits: CaptureLimits = driver.get_limits([0])

        self.setWindowTitle("Capture settings")
        self.resize(1000, 720)

        layout = dialog_layout(self)
        layout.addWidget(self._build_parameters())
        layout.addWidget(self._build_channels(), 1)
        self.trigger_group = self._build_trigger()
        layout.addWidget(self.trigger_group)

        self.validation_label = InlineMessage(self)
        layout.addWidget(self.validation_label)

        if accept_text is None:
            accept_text = "Continue" if driver.driver_type == AnalyzerDriverType.EMULATED else "Start capture"
        buttons = button_box(self, accept_text, icon="record" if accept_text == "Start capture" else "arrow-right")
        self.profiles_button = buttons.addButton("Profiles", QDialogButtonBox.ResetRole)
        self.profiles_button.setToolTip("Save, export, import or apply capture profiles")
        set_icon(self.profiles_button, "bookmark")
        self.profiles_menu = QMenu(self.profiles_button)
        self.profiles_menu.aboutToShow.connect(self._rebuild_profiles_menu)
        self.profiles_button.setMenu(self.profiles_menu)
        self.reset_button = buttons.addButton("Reset", QDialogButtonBox.ResetRole)
        self.reset_button.setToolTip("Restore the default settings")
        set_icon(self.reset_button, "reset")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        self.reset_button.clicked.connect(self.reset_settings)
        layout.addWidget(buttons)

        self._apply_driver_mode()
        self._load_settings()
        self._update_limits()
        self._update_jitter()
        self._update_summary()

        for box in (self.frequency_box, self.pre_samples_box, self.post_samples_box, self.burst_count_box):
            box.valueChanged.connect(self._update_summary)
            box.valueChanged.connect(self.validation_label.clear)
        self.burst_box.toggled.connect(self._update_summary)

    # ----------------------------------------------------------------- layout
    def _build_parameters(self) -> QWidget:
        group = QGroupBox("Sampling", self)
        grid = QGridLayout(group)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)

        grid.addWidget(QLabel("Frequency:", group), 0, 0)
        self.frequency_box = QSpinBox(group)
        self.frequency_box.setGroupSeparatorShown(True)
        self.frequency_box.setSuffix(" Hz")
        self.frequency_box.setRange(max(self.driver.min_frequency, 1), self.driver.max_frequency)
        self.frequency_box.setValue(self.driver.max_frequency)
        self.frequency_box.setSingleStep(1000)
        self.frequency_box.setMinimumWidth(170)
        self.frequency_box.valueChanged.connect(self._update_jitter)
        grid.addWidget(self.frequency_box, 0, 1)

        self.jitter_label = QLabel("Jitter: 0.000%", group)
        self.jitter_label.setAlignment(Qt.AlignCenter)
        grid.addWidget(self.jitter_label, 0, 2, Qt.AlignLeft)

        grid.addWidget(QLabel("Pre-trigger samples:", group), 0, 4)
        self.pre_samples_box = QSpinBox(group)
        self.pre_samples_box.setGroupSeparatorShown(True)
        self.pre_samples_box.setRange(0, 1_000_000)
        self.pre_samples_box.setValue(512)
        self.pre_samples_box.setMinimumWidth(120)
        grid.addWidget(self.pre_samples_box, 0, 5)

        grid.addWidget(QLabel("Post-trigger samples:", group), 1, 4)
        self.post_samples_box = QSpinBox(group)
        self.post_samples_box.setGroupSeparatorShown(True)
        self.post_samples_box.setRange(0, 1_000_000)
        self.post_samples_box.setValue(1024)
        self.post_samples_box.setMinimumWidth(120)
        grid.addWidget(self.post_samples_box, 1, 5)

        self.summary_label = hint("", group, word_wrap=False)
        grid.addWidget(self.summary_label, 1, 0, 1, 3)

        grid.setColumnStretch(3, 1)
        return group

    def _build_channels(self) -> QWidget:
        group = QGroupBox("Channels", self)
        outer = QVBoxLayout(group)
        outer.setSpacing(6)

        header = QHBoxLayout()
        self.channel_count_label = hint("", group, word_wrap=False)
        header.addWidget(self.channel_count_label)
        header.addStretch(1)
        for text, value, name in (("Select all", True, "check-all"), ("Clear", False, "clear")):
            button = QPushButton(text, group)
            set_icon(button, name)
            button.clicked.connect(lambda _checked=False, v=value: self._set_all(v))
            header.addWidget(button)
        outer.addLayout(header)

        scroll = QScrollArea(group)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget(scroll)
        grid = QGridLayout(content)
        grid.setContentsMargins(0, 0, 4, 0)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(8)

        self.channel_selectors: list[ChannelSelector] = []
        channel_count = self.driver.channel_count

        for first_channel in range(0, channel_count, CHANNELS_PER_ROW):
            row = first_channel // CHANNELS_PER_ROW
            last_channel = min(first_channel + CHANNELS_PER_ROW, channel_count)

            controls = QWidget(content)
            controls_layout = QVBoxLayout(controls)
            controls_layout.setContentsMargins(0, 0, 6, 0)
            controls_layout.setSpacing(3)
            label = QLabel(f"CH{first_channel + 1}–{last_channel}", controls)
            set_role(label, "section")
            controls_layout.addWidget(label)
            buttons_row = QHBoxLayout()
            buttons_row.setSpacing(2)
            for text, tooltip, action in (
                ("All", "Select every channel of this row", True),
                ("None", "Deselect every channel of this row", False),
                ("Inv", "Invert the selection of this row", None),
            ):
                button = QToolButton(controls)
                button.setText(text)
                button.setToolTip(tooltip)
                button.clicked.connect(
                    lambda _checked=False, start=first_channel, value=action: self._set_row(start, value)
                )
                buttons_row.addWidget(button)
            controls_layout.addLayout(buttons_row)
            grid.addWidget(controls, row, 0, Qt.AlignTop)

            for offset in range(CHANNELS_PER_ROW):
                number = first_channel + offset
                if number >= channel_count:
                    break
                selector = ChannelSelector(number, content)
                selector.enable_box.toggled.connect(self._update_limits)
                selector.enable_box.toggled.connect(self._update_channel_count)
                grid.addWidget(selector, row, offset + 1, Qt.AlignTop)
                self.channel_selectors.append(selector)

        grid.setRowStretch(grid.rowCount(), 1)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)
        return group

    def _build_trigger(self) -> QWidget:
        group = QGroupBox("Trigger", self)
        layout = QVBoxLayout(group)
        layout.setSpacing(6)

        self.edge_radio = QRadioButton("Edge: start when a channel changes level", group)
        self.edge_radio.setChecked(True)
        self.edge_radio.toggled.connect(self._update_trigger_mode)
        layout.addWidget(self.edge_radio)

        self.edge_panel = QWidget(group)
        edge_layout = QGridLayout(self.edge_panel)
        edge_layout.setContentsMargins(22, 0, 0, 4)
        edge_layout.setHorizontalSpacing(10)

        edge_layout.addWidget(QLabel("Channel:", self.edge_panel), 0, 0)
        self.trigger_channel_box = QComboBox(self.edge_panel)
        self._fill_trigger_channels()
        edge_layout.addWidget(self.trigger_channel_box, 0, 1)

        self.negative_trigger_box = QCheckBox("Falling edge", self.edge_panel)
        self.negative_trigger_box.setToolTip("Trigger on a high to low change instead of low to high")
        edge_layout.addWidget(self.negative_trigger_box, 0, 2)

        self.blast_box = QCheckBox("Blast mode", self.edge_panel)
        self.blast_box.setToolTip(
            "Capture at the maximum sampling rate of the device, without pre-trigger samples"
        )
        self.blast_box.toggled.connect(self._set_blast_mode)
        edge_layout.addWidget(self.blast_box, 0, 3)

        self.burst_box = QCheckBox("Burst mode:", self.edge_panel)
        self.burst_box.setToolTip("Re-arm the trigger and capture several bursts in a row")
        self.burst_box.toggled.connect(self._update_burst_mode)
        edge_layout.addWidget(self.burst_box, 1, 0)

        self.burst_count_box = QSpinBox(self.edge_panel)
        self.burst_count_box.setRange(2, max(self.driver.max_loop_count + 1, 2))
        self.burst_count_box.setValue(2)
        self.burst_count_box.setSuffix(" bursts")
        self.burst_count_box.setEnabled(False)
        self.burst_count_box.valueChanged.connect(self._update_measure_availability)
        edge_layout.addWidget(self.burst_count_box, 1, 1)

        self.measure_box = QCheckBox("Measure delays between bursts", self.edge_panel)
        self.measure_box.setToolTip(
            f"Burst delays can be measured for up to {MAX_MEASURED_LOOP_COUNT + 1} bursts "
            f"with at least {MIN_MEASURED_POST_SAMPLES} post-trigger samples"
        )
        self.measure_box.setEnabled(False)
        edge_layout.addWidget(self.measure_box, 1, 2, 1, 2)
        edge_layout.setColumnStretch(4, 1)
        layout.addWidget(self.edge_panel)

        self.pattern_radio = QRadioButton("Pattern: start when channels match a bit pattern", group)
        self.pattern_radio.toggled.connect(self._update_trigger_mode)
        layout.addWidget(self.pattern_radio)

        self.pattern_panel = QWidget(group)
        pattern_layout = QGridLayout(self.pattern_panel)
        pattern_layout.setContentsMargins(22, 0, 0, 0)
        pattern_layout.setHorizontalSpacing(10)
        pattern_layout.addWidget(QLabel("First channel:", self.pattern_panel), 0, 0)
        self.pattern_base_box = QSpinBox(self.pattern_panel)
        self.pattern_base_box.setRange(1, max(self.driver.channel_count, 1))
        if self.driver.driver_type != AnalyzerDriverType.EMULATED:
            self.pattern_base_box.setToolTip(
                f"A pattern covers consecutive trigger inputs, usable channels: {self._pattern_groups_text()}"
            )
        pattern_layout.addWidget(self.pattern_base_box, 0, 1)

        pattern_layout.addWidget(QLabel("Pattern:", self.pattern_panel), 0, 2)
        self.pattern_edit = QLineEdit(self.pattern_panel)
        self.pattern_edit.setMaxLength(16)
        self.pattern_edit.setPlaceholderText("e.g. 1011")
        self.pattern_edit.setToolTip("Bit pattern, first channel first; only 0 and 1 are allowed")
        self.pattern_edit.textChanged.connect(lambda _text: self.validation_label.clear())
        pattern_layout.addWidget(self.pattern_edit, 0, 3)

        self.fast_trigger_box = QCheckBox("Fast matching (max. 5 bits)", self.pattern_panel)
        self.fast_trigger_box.toggled.connect(
            lambda checked: self.pattern_edit.setMaxLength(5 if checked else 16)
        )
        pattern_layout.addWidget(self.fast_trigger_box, 0, 4)
        pattern_layout.setColumnStretch(5, 1)
        layout.addWidget(self.pattern_panel)

        self.trigger_button_group = QButtonGroup(group)
        self.trigger_button_group.addButton(self.edge_radio)
        self.trigger_button_group.addButton(self.pattern_radio)

        self._update_trigger_mode()
        return group

    # ------------------------------------------------------------- behaviour
    def _apply_driver_mode(self) -> None:
        driver_type = self.driver.driver_type
        if driver_type == AnalyzerDriverType.MULTI:
            # The board of the trigger channel starts the others through the trigger line, once.
            self.burst_box.setEnabled(False)
            self.burst_box.setToolTip("Burst mode is not available for a multi device set")
            self.edge_radio.setToolTip(
                "The board of the trigger channel starts the other boards through the trigger line "
                "(needs the firmware of this project); the external trigger starts every board on "
                "its own trigger input"
            )
            self.pattern_radio.setToolTip(
                "The board of the first pattern channel compares the pattern and starts the other boards"
            )
        elif driver_type == AnalyzerDriverType.EMULATED:
            self.trigger_group.setVisible(False)
        if self.driver.blast_frequency <= 0:
            self.blast_box.setEnabled(False)
            self.blast_box.setToolTip("This device does not support blast mode")

    def _fill_trigger_channels(self) -> None:
        box = self.trigger_channel_box
        box.clear()
        channels = self.driver.edge_trigger_channels()
        multi = self.driver.driver_type == AnalyzerDriverType.MULTI
        if multi:
            per_device = self.driver.channels_per_device
            for index in channels:
                box.addItem(f"Channel {index + 1} (board {index // per_device + 1})", index)
        else:
            for index in channels[:MAX_TRIGGER_CHANNELS]:
                box.addItem(f"Channel {index + 1}", index)
        if self.driver.driver_type != AnalyzerDriverType.EMULATED:
            box.addItem(
                "External trigger (every board)" if multi else "External trigger",
                self.driver.channel_count,
            )

    def _pattern_groups_text(self) -> str:
        return ", ".join(
            f"{first + 1}–{first + count}" if count > 1 else str(first + 1)
            for first, count in self.driver.pattern_trigger_groups()
        )

    def _trigger_board_captures(self, session: CaptureSession) -> bool:
        """In a multi device set the board evaluating the trigger has to capture a channel."""
        if (
            self.driver.driver_type != AnalyzerDriverType.MULTI
            or session.trigger_channel >= self.driver.channel_count
        ):
            return True
        per_device = self.driver.channels_per_device
        board = session.trigger_channel // per_device
        if any(channel.channel_number // per_device == board for channel in session.capture_channels):
            return True
        self._reject_settings(
            f"Board {board + 1} evaluates the trigger, so it has to capture at least one channel: "
            f"select one of the channels {board * per_device + 1} to {(board + 1) * per_device}."
        )
        return False

    def _set_row(self, first_channel: int, value: Optional[bool]) -> None:
        for selector in self.channel_selectors:
            if first_channel <= selector.channel_number < first_channel + CHANNELS_PER_ROW:
                selector.enabled = (not selector.enabled) if value is None else value

    def _set_all(self, value: bool) -> None:
        for selector in self.channel_selectors:
            selector.enabled = value

    def enabled_channels(self) -> list[int]:
        return [selector.channel_number for selector in self.channel_selectors if selector.enabled]

    def _update_channel_count(self) -> None:
        count = len(self.enabled_channels())
        self.channel_count_label.setText(
            f"{count} of {len(self.channel_selectors)} channels selected"
        )
        self.validation_label.clear()

    def _update_limits(self) -> None:
        channels = self.enabled_channels() or [0]
        self.limits = self.driver.get_limits(channels)

        if not self.blast_box.isChecked():
            self.pre_samples_box.setRange(self.limits.min_pre_samples, self.limits.max_pre_samples)
        self.post_samples_box.setRange(self.limits.min_post_samples, self.limits.max_post_samples)

        self.pre_samples_box.setToolTip(
            f"Samples kept before the trigger\n"
            f"Min: {to_thousands(self.limits.min_pre_samples)}  Max: {to_thousands(self.limits.max_pre_samples)}"
        )
        self.post_samples_box.setToolTip(
            f"Samples captured after the trigger (per burst)\n"
            f"Min: {to_thousands(self.limits.min_post_samples)}  Max: {to_thousands(self.limits.max_post_samples)}"
        )
        self._update_summary()

    def _total_samples(self) -> int:
        loops = self.burst_count_box.value() - 1 if self.burst_box.isChecked() else 0
        return self.pre_samples_box.value() + self.post_samples_box.value() * (loops + 1)

    def _update_summary(self) -> None:
        if not hasattr(self, "summary_label"):
            return
        total = self._total_samples()
        maximum = self.limits.max_total_samples
        duration = to_small_time(total / max(self.frequency_box.value(), 1))
        self.summary_label.setText(
            f"{to_thousands(total)} samples in total ({duration}), "
            f"at most {to_thousands(maximum)} with these channels"
        )
        set_role(self.summary_label, "error" if total > maximum else "hint")

    def _update_jitter(self) -> None:
        frequency = self.frequency_box.value()
        if frequency <= 0:
            return
        divider = int(self.driver.max_frequency / frequency)
        if divider <= 0:
            divider = 1
        actual = self.driver.max_frequency / divider
        percentage = (actual - frequency) * 100.0 / frequency

        self.jitter_label.setText(f"Jitter {percentage:.3f}%")
        role = "chip-ok" if percentage < 1 else ("chip-medium" if percentage < 10 else "chip-high")
        set_role(self.jitter_label, role)
        self.jitter_label.setToolTip(f"Actual sampling frequency: {actual:,.0f} Hz")
        self._update_summary()

    def _update_trigger_mode(self) -> None:
        edge = self.edge_radio.isChecked()
        self.edge_panel.setEnabled(edge)
        self.pattern_panel.setEnabled(not edge)
        if not edge and self.blast_box.isChecked():
            self.blast_box.setChecked(False)
        if hasattr(self, "validation_label"):
            self.validation_label.clear()

    def _update_burst_mode(self, checked: bool) -> None:
        self.burst_count_box.setEnabled(checked)
        self._update_measure_availability()

    def _update_measure_availability(self) -> None:
        measurable = (
            self.burst_box.isChecked()
            and self.burst_count_box.value() - 1 <= MAX_MEASURED_LOOP_COUNT
        )
        self.measure_box.setEnabled(measurable)
        if not measurable:
            self.measure_box.setChecked(False)

    def _set_blast_mode(self, enabled: bool) -> None:
        if enabled:
            blast_frequency = self.driver.blast_frequency
            self.frequency_box.setRange(blast_frequency, blast_frequency)
            self.frequency_box.setValue(blast_frequency)
            self.frequency_box.setEnabled(False)

            self.pre_samples_box.setRange(0, 0)
            self.pre_samples_box.setValue(0)
            self.pre_samples_box.setEnabled(False)

            self.burst_box.setChecked(False)
            self.burst_box.setEnabled(False)
            self.jitter_label.setText("Jitter 0.000%")
            set_role(self.jitter_label, "chip-ok")
        else:
            self.frequency_box.setRange(max(self.driver.min_frequency, 1), self.driver.max_frequency)
            self.frequency_box.setEnabled(True)
            self.pre_samples_box.setEnabled(True)
            self.burst_box.setEnabled(True)
            self._update_limits()
            self._update_jitter()

    def reset_settings(self) -> None:
        self.blast_box.setChecked(False)
        self.frequency_box.setValue(self.driver.max_frequency)
        self.pre_samples_box.setValue(512)
        self.post_samples_box.setValue(1024)
        self.burst_box.setChecked(False)
        self.burst_count_box.setValue(2)
        self.trigger_channel_box.setCurrentIndex(0)
        self.negative_trigger_box.setChecked(False)
        self.measure_box.setChecked(False)
        self.pattern_edit.clear()
        self.fast_trigger_box.setChecked(False)
        self.pattern_base_box.setValue(1)
        self.edge_radio.setChecked(True)
        for selector in self.channel_selectors:
            selector.reset()
        self._update_limits()
        self._update_channel_count()

    # -------------------------------------------------------------- profiles
    def _rebuild_profiles_menu(self) -> None:
        menu = self.profiles_menu
        menu.clear()

        if not self.profiles.profiles:
            menu.addAction("No saved profiles").setEnabled(False)
        for profile in self.profiles.profiles:
            action = menu.addAction(profile.name.replace("&", "&&"))
            action.setEnabled(profile.capture_settings is not None)
            action.triggered.connect(lambda _checked=False, p=profile: self.apply_profile(p))
        menu.addSeparator()

        for text, handler in (
            ("Save as profile...", self.save_as_profile),
            ("Export settings to file...", self.export_to_file),
            ("Import settings from file...", self.import_from_file),
        ):
            action = menu.addAction(text)
            action.triggered.connect(handler)

    def apply_profile(self, profile: Profile) -> None:
        if profile.capture_settings is not None:
            self.apply_session(profile.capture_settings)

    def save_as_profile(self) -> None:
        session = self.build_session()
        if session is None:
            return

        name, ok = QInputDialog.getText(self, "Save profile", "Profile name:")
        name = name.strip()
        if not ok or not name:
            return

        existing = self.profiles.get(name)
        if existing is not None and not messages.confirm(
            self, "Save profile", f'A profile named "{name}" already exists.', "Replace"
        ):
            return

        decoders = self.decoder_configuration
        if decoders is None:
            decoders = existing.decoder_configuration if existing is not None else []
        self.profiles.add(Profile(name, session.clone_settings(), decoders))
        if not self.profiles.save():
            self.validation_label.show_error("The profiles file cannot be written.")
        else:
            self.validation_label.show_success(f'Saved as profile "{name}".')

    def export_to_file(self) -> None:
        session = self.build_session()
        if session is None:
            return

        path, _ = QFileDialog.getSaveFileName(
            self, "Export capture settings", "capture-settings.json", PROFILE_FILE_FILTER
        )
        if not path:
            return
        if not path.lower().endswith(".json"):
            path += ".json"

        name = os.path.splitext(os.path.basename(path))[0]
        profile = Profile(name, session.clone_settings(), self.decoder_configuration or [])
        try:
            write_profiles_file(path, [profile])
        except OSError as error:
            self.validation_label.show_error(f"The settings could not be exported: {error}")
            return
        self.validation_label.show_success(f"Exported to {os.path.basename(path)}.")

    def import_from_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Import capture settings", "", PROFILE_FILE_FILTER
        )
        if not path:
            return

        try:
            imported = [p for p in read_profiles_file(path) if p.capture_settings is not None]
        except (OSError, ValueError) as error:
            self.validation_label.show_error(f"The settings could not be imported: {error}")
            return
        if not imported:
            self.validation_label.show_error("The file does not contain capture settings.")
            return

        profile = imported[0]
        if len(imported) > 1:
            names = [candidate.name for candidate in imported]
            name, ok = QInputDialog.getItem(
                self, "Import capture settings", "Profile:", names, 0, False
            )
            if not ok:
                return
            profile = imported[names.index(name)]
        self.apply_profile(profile)

    # -------------------------------------------------------------- settings
    def _load_settings(self) -> None:
        data = settings.get_settings(self.settings_file)
        session: Optional[CaptureSession] = None
        if data:
            try:
                session = session_from_dict(data)
            except (ValueError, KeyError, TypeError):
                session = None

        if session is None:
            # Sensible default: capture the first eight channels.
            for selector in self.channel_selectors[:CHANNELS_PER_ROW]:
                selector.enabled = True
            self._update_channel_count()
            return
        self.apply_session(session)

    def apply_session(self, session: CaptureSession) -> None:
        """Show the settings of ``session``: channels, timing and trigger."""
        self.reset_settings()

        for channel in session.capture_channels:
            if 0 <= channel.channel_number < len(self.channel_selectors):
                selector = self.channel_selectors[channel.channel_number]
                selector.enabled = True
                selector.channel_name = channel.channel_name
                selector.channel_color = channel.channel_color
                selector._update_color()

        if session.trigger_type == TriggerType.BLAST and self.blast_box.isEnabled():
            self.blast_box.setChecked(True)
        else:
            self.frequency_box.setValue(
                max(min(session.frequency, self.driver.max_frequency), self.driver.min_frequency)
            )
            self._update_limits()
            self.pre_samples_box.setValue(session.pre_trigger_samples)
            self.post_samples_box.setValue(session.post_trigger_samples)

        self._update_channel_count()
        driver_type = self.driver.driver_type
        if driver_type == AnalyzerDriverType.EMULATED or session.trigger_type == TriggerType.SIMULATION:
            return  # no trigger settings (simulated captures are generated, not triggered)

        if session.trigger_type in (TriggerType.EDGE, TriggerType.BLAST):
            if driver_type == AnalyzerDriverType.MULTI and session.trigger_type == TriggerType.BLAST:
                return  # a multi device set has no blast mode
            self.edge_radio.setChecked(True)
            position = self.trigger_channel_box.findData(session.trigger_channel)
            self.trigger_channel_box.setCurrentIndex(max(position, 0))
            self.negative_trigger_box.setChecked(session.trigger_inverted)
            self.burst_box.setChecked(session.loop_count > 0)
            self.burst_count_box.setValue(session.loop_count + 1 if session.loop_count > 0 else 2)
            self.measure_box.setChecked(session.measure_bursts and session.loop_count > 0)
        else:
            self.pattern_radio.setChecked(True)
            self.pattern_base_box.setValue(session.trigger_channel + 1)
            self.pattern_edit.setText(session.trigger_description())
            self.fast_trigger_box.setChecked(session.trigger_type == TriggerType.FAST)

    def _persist_settings(self, session: CaptureSession) -> None:
        settings.persist_settings(
            self.settings_file, session_to_dict(session.clone_settings(), include_samples=False)
        )

    # ----------------------------------------------------------------- accept
    def _accept(self) -> None:
        session = self.build_session()
        if session is None:
            return
        self.selected_settings = session
        if self.persist:
            self._persist_settings(session)
        self.accept()

    def _reject_settings(self, message: str, focus: Optional[QWidget] = None) -> None:
        self.validation_label.show_error(message)
        if focus is not None:
            focus.setFocus()

    def build_session(self) -> Optional[CaptureSession]:
        """Validate the dialog; return the configured session or ``None``."""
        self.validation_label.clear()
        channels = [
            AnalyzerChannel(
                channel_number=selector.channel_number,
                channel_name=selector.channel_name,
                channel_color=selector.channel_color,
            )
            for selector in self.channel_selectors
            if selector.enabled
        ]

        if not channels:
            self._reject_settings("Select at least one channel to capture.")
            return None

        session = CaptureSession()
        session.capture_channels = channels
        session.frequency = self.frequency_box.value()
        session.pre_trigger_samples = self.pre_samples_box.value()
        session.post_trigger_samples = self.post_samples_box.value()

        edge_mode = self.edge_radio.isChecked()
        bursts = self.burst_box.isChecked() and self.burst_box.isEnabled() and edge_mode
        loops = (self.burst_count_box.value() - 1) if bursts else 0
        session.loop_count = loops
        session.measure_bursts = bursts and self.measure_box.isChecked()

        if session.measure_bursts and loops > 0:
            if loops > MAX_MEASURED_LOOP_COUNT:
                self._reject_settings(
                    f"Too many bursts to measure: reduce the burst count to "
                    f"{MAX_MEASURED_LOOP_COUNT + 1} or disable the delay measurement.",
                    self.burst_count_box,
                )
                return None
            if session.post_trigger_samples < MIN_MEASURED_POST_SAMPLES:
                self._reject_settings(
                    f"Post-trigger samples too low: increase them to {MIN_MEASURED_POST_SAMPLES} "
                    "or disable the delay measurement.",
                    self.post_samples_box,
                )
                return None

        maximum = self.driver.get_limits([c.channel_number for c in channels]).max_total_samples
        if session.pre_trigger_samples + session.post_trigger_samples * (loops + 1) > maximum:
            self._reject_settings(
                f"The capture is too long: at most {to_thousands(maximum)} samples fit into the "
                "buffer with the selected channels.",
                self.post_samples_box,
            )
            return None

        if self.driver.driver_type == AnalyzerDriverType.EMULATED:
            session.trigger_type = TriggerType.EDGE
            return session

        if edge_mode:
            trigger_channel = self.trigger_channel_box.currentData()
            if trigger_channel is None:
                self._reject_settings("Choose the channel that triggers the capture.", self.trigger_channel_box)
                return None
            session.trigger_channel = int(trigger_channel)
            session.trigger_inverted = self.negative_trigger_box.isChecked()
            session.trigger_type = TriggerType.BLAST if self.blast_box.isChecked() else TriggerType.EDGE
        else:
            pattern = self.pattern_edit.text().strip()
            if not pattern:
                self._reject_settings("Enter a trigger pattern of at least one bit.", self.pattern_edit)
                return None
            if any(character not in "01" for character in pattern):
                self._reject_settings("The trigger pattern can only contain 0 and 1.", self.pattern_edit)
                return None

            base_channel = self.pattern_base_box.value() - 1
            fast = self.fast_trigger_box.isChecked()
            max_bits = 5 if fast else 16
            if len(pattern) > max_bits:
                self._reject_settings(
                    f"A {'fast' if fast else 'complex'} pattern trigger compares at most {max_bits} bits.",
                    self.pattern_edit,
                )
                return None
            if not pattern_fits(self.driver.pattern_trigger_groups(), base_channel, len(pattern)):
                self._reject_settings(
                    f"The pattern does not fit: channels {base_channel + 1} to "
                    f"{base_channel + len(pattern)} are not consecutive trigger inputs of one board. "
                    f"Usable channels: {self._pattern_groups_text()}.",
                    self.pattern_base_box,
                )
                return None

            value = 0
            for index, character in enumerate(pattern):
                if character == "1":
                    value |= 1 << index

            session.trigger_channel = base_channel
            session.trigger_bit_count = len(pattern)
            session.trigger_pattern = value
            session.trigger_type = TriggerType.FAST if fast else TriggerType.COMPLEX

        if not self._trigger_board_captures(session):
            return None
        return session
