# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of PiPiLogicAnalyzer, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Editing a stored profile: name, notes, capture settings and decoders."""

from __future__ import annotations

import copy
from typing import Optional, Sequence

from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core.formatting import to_large_frequency, to_thousands
from ...core.profiles import Profile
from ...driver.base import AnalyzerDriverType
from ...driver.emulated import EmulatedAnalyzerDriver
from ...driver.models import CaptureSession
from ..icons import set_icon
from .capture_dialog import CaptureDialog
from .common import InlineMessage, button_box, dialog_layout, hint

CHANNELS_PER_BOARD = 24


class ProfileSettingsDriver(EmulatedAnalyzerDriver):
    """Stands in for the analyzer while the capture settings of a profile are edited.

    Settings with channels beyond the first board belong to a multi device set; the dialog then
    offers what such a set can do (no blast mode, no bursts). The boards are assumed to be Pico
    boards with the firmware of this project, the capture itself checks the real devices.
    """

    def __init__(self, settings: Optional[CaptureSession]) -> None:
        super().__init__(1)
        highest = max((channel.channel_number for channel in settings.capture_channels), default=0) if settings else 0
        self.boards = highest // CHANNELS_PER_BOARD + 1

    @property
    def driver_type(self) -> AnalyzerDriverType:
        return AnalyzerDriverType.MULTI if self.boards > 1 else AnalyzerDriverType.SERIAL

    @property
    def channel_count(self) -> int:
        return CHANNELS_PER_BOARD * max(self.boards, 1)

    @property
    def channels_per_device(self) -> int:
        return CHANNELS_PER_BOARD

    def pattern_trigger_groups(self) -> tuple[tuple[int, int], ...]:
        # Channels 1-21 and 22-24 of every board (consecutive GPIOs of a Pico board)
        return tuple(
            group
            for board in range(max(self.boards, 1))
            for group in ((board * CHANNELS_PER_BOARD, 21), (board * CHANNELS_PER_BOARD + 21, 3))
        )

    @property
    def max_frequency(self) -> int:
        return 100_000_000

    @property
    def blast_frequency(self) -> int:
        return 0

    @property
    def device_version(self) -> str:
        return "PROFILE_EDITOR"


def describe_settings(settings: Optional[CaptureSession]) -> str:
    if settings is None:
        return "No capture settings stored."
    return (
        f"{len(settings.capture_channels)} channels · {to_large_frequency(settings.frequency)} · "
        f"{to_thousands(settings.pre_trigger_samples)} + {to_thousands(settings.post_trigger_samples)} samples · "
        f"{settings.trigger_type.label} trigger"
    )


def decoder_names(configuration) -> list[str]:
    if isinstance(configuration, dict):
        return ["Decoder tree of the original software"]
    names = []
    for item in configuration or []:
        if isinstance(item, dict):
            names.append(str(item.get("label") or item.get("decoder_id") or "decoder"))
    return names


class ProfileEditDialog(QDialog):
    def __init__(
        self,
        profile: Profile,
        current_decoders: Sequence[dict],
        other_names: Sequence[str],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit profile")
        self.resize(600, 620)
        self.original = profile
        self.profile: Optional[Profile] = None
        self._settings = profile.capture_settings.clone_settings() if profile.capture_settings else None
        self._decoders = copy.deepcopy(profile.decoder_configuration)
        self._current_decoders = copy.deepcopy(list(current_decoders))
        self._other_names = {name.strip().lower() for name in other_names}

        layout = dialog_layout(self)

        form = QFormLayout()
        self.name_edit = QLineEdit(profile.name, self)
        form.addRow("Name:", self.name_edit)
        layout.addLayout(form)

        notes_group = QGroupBox("Notes", self)
        notes_layout = QVBoxLayout(notes_group)
        self.notes_edit = QPlainTextEdit(profile.notes, notes_group)
        self.notes_edit.setPlaceholderText("How to connect the probes, what to look for, ...")
        notes_layout.addWidget(self.notes_edit)
        layout.addWidget(notes_group, 1)

        settings_group = QGroupBox("Capture settings", self)
        settings_layout = QVBoxLayout(settings_group)
        self.settings_label = hint("", settings_group)
        settings_layout.addWidget(self.settings_label)
        settings_buttons = QHBoxLayout()
        self.edit_settings_button = QPushButton("Edit capture settings...", settings_group)
        set_icon(self.edit_settings_button, "sliders")
        self.edit_settings_button.clicked.connect(self.edit_capture_settings)
        settings_buttons.addWidget(self.edit_settings_button)
        self.clear_settings_button = QPushButton("Remove", settings_group)
        set_icon(self.clear_settings_button, "clear")
        self.clear_settings_button.clicked.connect(self.clear_capture_settings)
        settings_buttons.addWidget(self.clear_settings_button)
        settings_buttons.addStretch(1)
        settings_layout.addLayout(settings_buttons)
        layout.addWidget(settings_group)

        decoders_group = QGroupBox("Protocol decoders", self)
        decoders_layout = QVBoxLayout(decoders_group)
        self.decoder_list = QListWidget(decoders_group)
        self.decoder_list.setMaximumHeight(110)
        decoders_layout.addWidget(self.decoder_list)
        decoder_buttons = QHBoxLayout()
        self.use_current_button = QPushButton("Use current decoders", decoders_group)
        self.use_current_button.setToolTip("Replace the decoders of the profile with the decoders configured now")
        set_icon(self.use_current_button, "import")
        self.use_current_button.clicked.connect(self.use_current_decoders)
        decoder_buttons.addWidget(self.use_current_button)
        self.clear_decoders_button = QPushButton("Remove all", decoders_group)
        set_icon(self.clear_decoders_button, "clear")
        self.clear_decoders_button.clicked.connect(self.clear_decoders)
        decoder_buttons.addWidget(self.clear_decoders_button)
        decoder_buttons.addStretch(1)
        decoders_layout.addLayout(decoder_buttons)
        layout.addWidget(decoders_group)

        self.message = InlineMessage(self)
        layout.addWidget(self.message)

        buttons = button_box(self, "Save profile", icon="save")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._refresh()

    # ----------------------------------------------------------------- state
    @property
    def capture_settings(self) -> Optional[CaptureSession]:
        return self._settings

    @property
    def decoder_configuration(self):
        return self._decoders

    def _refresh(self) -> None:
        self.settings_label.setText(describe_settings(self._settings))
        self.clear_settings_button.setEnabled(self._settings is not None)
        self.decoder_list.clear()
        names = decoder_names(self._decoders)
        if names:
            self.decoder_list.addItems(names)
        else:
            self.decoder_list.addItem("No decoders stored")
        self.clear_decoders_button.setEnabled(bool(names))
        self.use_current_button.setEnabled(bool(self._current_decoders))

    # --------------------------------------------------------------- actions
    def edit_capture_settings(self) -> None:
        dialog = CaptureDialog(
            ProfileSettingsDriver(self._settings),
            self,
            decoder_configuration=self._decoders if isinstance(self._decoders, list) else None,
            accept_text="Apply",
            persist=False,
        )
        dialog.setWindowTitle(f"Capture settings of {self.name_edit.text().strip() or 'the profile'}")
        if self._settings is not None:
            dialog.apply_session(self._settings)
        if dialog.exec() and dialog.selected_settings is not None:
            self._settings = dialog.selected_settings
            self._refresh()

    def clear_capture_settings(self) -> None:
        self._settings = None
        self._refresh()

    def use_current_decoders(self) -> None:
        self._decoders = copy.deepcopy(self._current_decoders)
        self._refresh()

    def clear_decoders(self) -> None:
        self._decoders = []
        self._refresh()

    def _accept(self) -> None:
        name = self.name_edit.text().strip()
        if not name:
            self.message.show_error("The profile needs a name.")
            self.name_edit.setFocus()
            return
        if name.lower() in self._other_names:
            self.message.show_error(f'A profile named "{name}" already exists.')
            self.name_edit.setFocus()
            return
        if self._settings is None and not decoder_names(self._decoders):
            self.message.show_error("A profile needs capture settings or decoders.")
            return
        self.profile = Profile(
            name=name,
            capture_settings=self._settings,
            decoder_configuration=self._decoders,
            notes=self.notes_edit.toPlainText().strip(),
        )
        self.accept()
