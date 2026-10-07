# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of openSciLab, a port and extension of his software;
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

import logging
import os
from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QColorDialog,
    QDoubleSpinBox,
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
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ...core import settings
from .. import colors
from ...core.capture_io import session_from_dict, session_to_dict
from ...core.formatting import to_bytes, to_large_frequency, to_small_time, to_thousands
from ...core.sample_store import disk_sample_bytes
from ...core.profiles import (
    PROFILE_FILE_FILTER,
    Profile,
    ProfileStore,
    fit_session,
    profile_problem,
    read_profiles_file,
    write_profiles_file,
)
from ...driver.base import (
    ACQUISITION_BUFFER,
    ACQUISITION_STREAM,
    CAPABILITY_CONTINUOUS_STREAM,
    CAPABILITY_IMMEDIATE_TRIGGER,
    CAPABILITY_STREAM_IMMEDIATE_ONLY,
    CAPABILITY_STATE_MODE,
    CAPABILITY_STREAM_STATE,
    CAPABILITY_THRESHOLD,
    CAPABILITY_TRIGGER_CONDITIONS,
    CAPABILITY_TRIGGER_SEQUENCE,
    MAX_MEASURED_LOOP_COUNT,
    MIN_MEASURED_POST_SAMPLES,
    AnalyzerDriverBase,
    AnalyzerDriverType,
    CaptureLimits,
    pattern_fits,
)
from ...driver.models import AnalogChannel, AnalyzerChannel, CaptureSession, ConditionKind, EdgeKind, TriggerType
from .. import messages
from ..theme import BORDER, ERROR, set_role
from ..icons import icon, set_icon
from .common import InlineMessage, button_box, dialog_layout, hint

log = logging.getLogger(__name__)

#: Labels of the acquisition modes of devices that have several
ACQUISITION_LABELS = {
    ACQUISITION_BUFFER: "Buffer (device memory)",
    ACQUISITION_STREAM: "Stream (over USB)",
}
CHANNELS_PER_ROW = 8


#: Kinds of devices whose last capture settings a loaded profile replaces, besides those stored already
BUILT_IN_DRIVER_IDS = tuple(driver_type.value.lower() for driver_type in AnalyzerDriverType)


#: Stages of a trigger sequence the application evaluates on a stream
SOFTWARE_SEQUENCE_STAGES = 8


def hardware_sequence_limits(capabilities: frozenset[str]) -> Optional[tuple[int, tuple[ConditionKind, ...]]]:
    """Stages and condition kinds of the device's own trigger sequences (``None``: it has none)."""
    stages = 0
    kinds: tuple[ConditionKind, ...] = tuple(ConditionKind)
    for item in capabilities:
        if item == CAPABILITY_TRIGGER_SEQUENCE:
            stages = stages or 4
        elif item.startswith(CAPABILITY_TRIGGER_SEQUENCE + "="):
            try:
                stages = int(item.partition("=")[2].split(",")[0])
            except ValueError:
                stages = 4
        elif item.startswith(CAPABILITY_TRIGGER_CONDITIONS):
            names = item[len(CAPABILITY_TRIGGER_CONDITIONS):].replace(",", "/").split("/")
            kinds = tuple(kind for kind in ConditionKind if kind.value in names)
    return (stages, kinds) if stages > 0 else None


def capture_settings_file(driver: Optional[AnalyzerDriverBase]) -> str:
    """Settings file holding the last capture settings of a kind of device (a Pico board without one)."""
    return f"capture-settings-{driver.driver_id if driver is not None else 'serial'}.json"


def all_capture_settings_files() -> list[str]:
    """The capture settings files of every kind of device, stored or not."""
    names = {f"capture-settings-{driver_id}.json" for driver_id in BUILT_IN_DRIVER_IDS}
    try:
        names.update(
            name for name in os.listdir(settings.settings_directory())
            if name.startswith("capture-settings-") and name.endswith(".json")
        )
    except OSError:
        pass
    return sorted(names)



class LargeSpinBox(QDoubleSpinBox):
    """A whole number spin box beyond the 32 bits of QSpinBox (exact up to 2^53).

    A stream recorded to disk can keep tens of billions of samples.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setDecimals(0)

    def value(self) -> int:  # noqa: D401 - Qt override
        return int(round(super().value()))

    def minimum(self) -> int:  # noqa: D401 - Qt override
        return int(super().minimum())

    def maximum(self) -> int:  # noqa: D401 - Qt override
        return int(super().maximum())

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
        self.name_edit.setObjectName("channel-name")
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
        allow_apply: bool = False,
    ) -> None:
        super().__init__(parent)
        self.driver = driver
        #: Store the accepted settings as the next defaults (not when editing a profile).
        self.persist = persist
        self.selected_settings: Optional[CaptureSession] = None
        #: ``True`` when the settings were only applied (stored for the device), not to capture now
        self.applied_only = False
        self.settings_file = capture_settings_file(driver)
        self.profiles = profiles if profiles is not None else ProfileStore()
        #: Decoders stored alongside profiles saved from this dialog.
        self.decoder_configuration = decoder_configuration
        try:
            self.capabilities = driver.capabilities()
        except Exception:  # noqa: BLE001 - optional functions only
            log.debug("self.capabilities = driver.capabilities() failed: optional functions only", exc_info=True)
            self.capabilities = frozenset()
        self.limits: CaptureLimits = driver.get_limits([0])
        #: settings are being restored: keep their values instead of the defaults of the controls
        self._loading_session = False

        self.setWindowTitle("Capture settings")
        self.resize(900, 620)

        layout = dialog_layout(self)
        # pages: what to sample, which channels, when to start; a list of them on the left
        body = QHBoxLayout()
        self.page_list = QListWidget(self)
        self.page_list.setFixedWidth(150)
        self.page_list.setAccessibleName("Pages of the capture settings")
        self.pages = QStackedWidget(self)
        # built in this order: the trigger reads the sampling and channel controls
        parameters = self._build_parameters()
        channels = self._build_channels()
        self.trigger_group = self._build_trigger()
        for title, icon_name, widget in (("Sampling", "clock", parameters), ("Channels", "channels", channels),
                                         ("Trigger", "target", self.trigger_group)):
            self.page_list.addItem(QListWidgetItem(icon(icon_name), title))
            page = QWidget(self.pages)
            page_layout = QVBoxLayout(page)
            page_layout.setContentsMargins(0, 0, 0, 0)
            page_layout.addWidget(widget, 1 if title == "Channels" else 0)
            if title != "Channels":
                page_layout.addStretch(1)
            self.pages.addWidget(page)
        self.page_list.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.page_list.setCurrentRow(0)
        body.addWidget(self.page_list)
        body.addWidget(self.pages, 1)
        layout.addLayout(body, 1)
        # what the capture will be, whatever page is shown
        self.overview_label = QLabel(self)
        set_role(self.overview_label, "hint")
        self.overview_label.setWordWrap(True)
        layout.addWidget(self.overview_label)
        self._summary_timer = QTimer(self)
        self._summary_timer.setInterval(300)
        self._summary_timer.timeout.connect(self._update_overview)
        self._summary_timer.start()

        self.validation_label = InlineMessage(self)
        layout.addWidget(self.validation_label)

        if accept_text is None:
            accept_text = "Start capture" if driver.is_hardware else "Continue"
        buttons = button_box(self, accept_text, icon="record" if accept_text == "Start capture" else "arrow-right")
        self.profiles_button = buttons.addButton("Profiles", QDialogButtonBox.ResetRole)
        self.profiles_button.setToolTip("Save, export, import or apply capture profiles")
        set_icon(self.profiles_button, "bookmark")
        self.profiles_menu = QMenu(self.profiles_button)
        self.profiles_menu.aboutToShow.connect(self._rebuild_profiles_menu)
        self.profiles_button.setMenu(self.profiles_menu)
        self.apply_button = None
        if allow_apply:
            # Keep the settings (channel names, rate, trigger) for the device without capturing now
            self.apply_button = buttons.addButton("Use settings", QDialogButtonBox.ApplyRole)
            self.apply_button.setToolTip("Keep these settings for the device's next capture (names, rate, "
                                         "trigger) and close, without capturing now")
            # the device card has the profiles; one menu for them is enough
            self.profiles_button.setVisible(False)
            set_icon(self.apply_button, "check")
            self.apply_button.clicked.connect(self._apply_only)
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
    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        """Enter in a channel name goes to the next name instead of starting the capture."""
        focus = self.focusWidget()
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and focus is not None \
                and focus.objectName() == "channel-name":
            names = [edit for edit in self.findChildren(QLineEdit, "channel-name") if edit.isEnabled()]
            if focus in names and names.index(focus) + 1 < len(names):
                names[names.index(focus) + 1].setFocus()
                names[names.index(focus) + 1].selectAll()
            return
        super().keyPressEvent(event)

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

        self.post_label = QLabel("Post-trigger samples:", group)
        grid.addWidget(self.post_label, 1, 4)
        self.post_samples_box = LargeSpinBox(group)
        self.post_samples_box.setGroupSeparatorShown(True)
        self.post_samples_box.setRange(0, 1_000_000)
        self.post_samples_box.setValue(1024)
        self.post_samples_box.setMinimumWidth(120)
        grid.addWidget(self.post_samples_box, 1, 5)

        self.max_samples_button = QToolButton(group)
        self.max_samples_button.setText("Max")
        self.max_samples_button.setToolTip("The longest capture with these channels and this mode")
        self.max_samples_button.clicked.connect(self.use_max_samples)
        grid.addWidget(self.max_samples_button, 1, 6)

        self.summary_label = hint("", group, word_wrap=False)
        grid.addWidget(self.summary_label, 1, 0, 1, 3)

        # Devices with a fixed list of rates (DSLogic): a list instead of the free value
        self.rate_box = QComboBox(group)
        self.rate_box.setMinimumWidth(170)
        self.rate_box.currentIndexChanged.connect(self._on_rate_selected)
        grid.addWidget(self.rate_box, 0, 1)
        self.fixed_rates = self.driver.sample_rates([0], self._acquisition_mode_default()) is not None
        if self.fixed_rates:
            all_rates = self.driver.sample_rates(
                range(self.driver.channel_count), None
            ) or [self.driver.max_frequency]
            self.frequency_box.setRange(1, max(self.driver.max_frequency, max(all_rates)))
            self.frequency_box.setVisible(False)
            self.jitter_label.setVisible(False)
        else:
            self.rate_box.setVisible(False)

        self.acquisition_label = QLabel("Acquisition:", group)
        grid.addWidget(self.acquisition_label, 2, 0)
        self.acquisition_box = QComboBox(group)
        for mode in self.driver.acquisition_modes():
            self.acquisition_box.addItem(ACQUISITION_LABELS.get(mode, mode), mode)
        self.acquisition_box.setToolTip(
            "Buffer: the capture is kept in the memory of the device, the fastest rates.\n"
            "Stream: the samples are sent over USB while capturing, long captures at lower rates."
        )
        self.acquisition_box.currentIndexChanged.connect(lambda _index: self._update_limits())
        self.acquisition_box.currentIndexChanged.connect(lambda _index: self._update_state_availability())
        grid.addWidget(self.acquisition_box, 2, 1)
        has_modes = self.acquisition_box.count() > 1
        self.acquisition_label.setVisible(has_modes)
        self.acquisition_box.setVisible(has_modes)

        self.continuous_box = QCheckBox("Until stopped", group)
        self.continuous_box.setToolTip(
            "Stream until Stop is pressed, keeping only the latest samples in memory:\n"
            "the sample count sets how many are kept."
        )
        self.continuous_box.toggled.connect(self._on_continuous_toggled)
        grid.addWidget(self.continuous_box, 2, 2)
        self.continuous_box.setVisible(False)

        self.disk_box = QCheckBox("Record to disk", group)
        self.disk_box.setToolTip(
            "Keep the stream in memory-mapped files on the disk instead of in memory:\n"
            "much longer captures, limited by the free disk space. Decoders then run on request."
        )
        self.disk_box.toggled.connect(self._on_disk_toggled)
        grid.addWidget(self.disk_box, 2, 3)
        self.disk_box.setVisible(False)

        self.threshold_label = QLabel("Threshold:", group)
        grid.addWidget(self.threshold_label, 2, 4)
        self.threshold_box = QDoubleSpinBox(group)
        self.threshold_box.setRange(0.0, 5.0)
        self.threshold_box.setSingleStep(0.1)
        self.threshold_box.setDecimals(2)
        self.threshold_box.setSuffix(" V")
        self.threshold_box.setValue(1.0)
        self.threshold_box.setToolTip("Input voltage above which a sample reads as 1")
        grid.addWidget(self.threshold_box, 2, 5)
        has_threshold = CAPABILITY_THRESHOLD in self.capabilities
        self.threshold_label.setVisible(has_threshold)
        self.threshold_box.setVisible(has_threshold)

        # State mode: the samples are taken on the edges of an input instead of the internal clock
        self.clock_box = QCheckBox("External clock:", group)
        self.clock_box.setToolTip(
            "State mode: take a sample on every edge of a clock input instead of at the sample rate"
        )
        self.clock_box.toggled.connect(self._update_clock_mode)
        grid.addWidget(self.clock_box, 3, 0)
        self.clock_channel_box = QComboBox(group)
        # Only channels whose pins can clock the state mode (CLOCK, CLOCK_SW).
        try:
            clock_channels = list(self.driver.state_clock_channels())
        except Exception:  # noqa: BLE001 - a driver that cannot say: every channel
            log.debug("clock_channels = list(self.driver.state_clock_channels()) failed: a driver that cannot say: every channel", exc_info=True)
            clock_channels = list(range(self.driver.channel_count))
        for number in clock_channels:
            self.clock_channel_box.addItem(f"Channel {number + 1}", number)
        grid.addWidget(self.clock_channel_box, 3, 1)
        self.clock_edge_box = QComboBox(group)
        self.clock_edge_box.addItem("Rising ↑", EdgeKind.RISING)
        self.clock_edge_box.addItem("Falling ↓", EdgeKind.FALLING)
        grid.addWidget(self.clock_edge_box, 3, 2)
        has_state_mode = CAPABILITY_STATE_MODE in self.capabilities
        for widget in (self.clock_box, self.clock_channel_box, self.clock_edge_box):
            widget.setVisible(has_state_mode)
        self.clock_channel_box.setEnabled(False)
        self.clock_edge_box.setEnabled(False)

        grid.setColumnStretch(3, 1)
        return group

    def _update_clock_mode(self, checked: bool) -> None:
        self.clock_channel_box.setEnabled(checked)
        self.clock_edge_box.setEnabled(checked)
        self.frequency_box.setEnabled(not checked)
        self.rate_box.setEnabled(not checked)

    def _update_state_availability(self) -> None:
        """State mode on a stream only with ``STREAM_STATE``."""
        if not hasattr(self, "clock_box"):
            return
        allowed = self.acquisition_mode() != ACQUISITION_STREAM or CAPABILITY_STREAM_STATE in self.capabilities
        self.clock_box.setEnabled(allowed)
        self.clock_box.setToolTip(
            "State mode: take a sample on every edge of a clock input instead of at the sample rate"
            if allowed else "This device has no state mode on a stream (STREAM_STATE)")
        if not allowed and self.clock_box.isChecked():
            self.clock_box.setChecked(False)

    def _acquisition_mode_default(self) -> Optional[str]:
        modes = self.driver.acquisition_modes()
        return modes[0] if modes else None

    def _on_continuous_toggled(self, checked: bool) -> None:
        self._update_limits()
        # An endless stream keeps as many of its latest samples as fit, unless the user says less.
        if checked and not self._loading_session:
            self.use_max_samples()

    def use_max_samples(self) -> None:
        """Sets the samples after the trigger to the most that fit with the samples before it."""
        loops = self.burst_count_box.value() - 1 if self.burst_box.isChecked() else 0
        # Without a trigger the samples before it are not captured (see build_session)
        pre = 0 if self.immediate_radio.isChecked() else self.pre_samples_box.value()
        room = (self.limits.max_total_samples - pre) // (loops + 1)
        self.post_samples_box.setValue(max(min(room, self.post_samples_box.maximum()), self.post_samples_box.minimum()))

    def _on_disk_toggled(self, checked: bool) -> None:
        was_at_maximum = self.post_samples_box.value() >= self.post_samples_box.maximum()
        self._update_limits()
        # On disk a capture that filled the memory may grow to what the disk holds
        if checked and was_at_maximum and not self._loading_session:
            self.use_max_samples()

    def to_disk(self) -> bool:
        """The stream is recorded into memory-mapped files."""
        return hasattr(self, "disk_box") and self.disk_box.isChecked() and self._stream_selected()

    def _stream_selected(self) -> bool:
        return self.acquisition_mode() == ACQUISITION_STREAM and ACQUISITION_STREAM in self.driver.acquisition_modes()

    def continuous(self) -> bool:
        """An endless stream is selected."""
        return (
            hasattr(self, "continuous_box")
            and self.continuous_box.isChecked()
            and self.acquisition_mode() == ACQUISITION_STREAM
            and CAPABILITY_CONTINUOUS_STREAM in self.capabilities
        )

    def acquisition_mode(self) -> Optional[str]:
        if not hasattr(self, "acquisition_box") or self.acquisition_box.count() == 0:
            return None
        return self.acquisition_box.currentData()

    def _refresh_rates(self) -> None:
        """Rates of devices with a fixed list, for the selected channels and acquisition mode."""
        if not getattr(self, "fixed_rates", False):
            return
        rates = self.driver.sample_rates(self.enabled_channels() or [0], self.acquisition_mode()) or []
        current = self.frequency_box.value()
        self.rate_box.blockSignals(True)
        self.rate_box.clear()
        for rate in rates:
            self.rate_box.addItem(to_large_frequency(rate), rate)
        # keep the rate, or the fastest one below it, or the slowest one
        index = max((i for i, rate in enumerate(rates) if rate <= current), default=0)
        self.rate_box.setCurrentIndex(index if rates else -1)
        self.rate_box.blockSignals(False)
        if rates:
            self.frequency_box.setValue(rates[index])

    def _on_rate_selected(self, index: int) -> None:
        rate = self.rate_box.itemData(index)
        if rate is not None:
            self.frequency_box.setValue(int(rate))

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

        # Analog inputs (devices with CAPABILITY_ANALOG): a check box each.
        self.analog_boxes: list[QCheckBox] = []
        names = self.driver.analog_channel_names() if self.driver.analog_channel_count else []
        if names:
            row = QHBoxLayout()
            label = QLabel("Analog:", group)
            set_role(label, "section")
            row.addWidget(label)
            for number, name in enumerate(names):
                box = QCheckBox(name, group)
                box.setProperty("analogNumber", number)
                box.toggled.connect(self._update_limits)
                box.toggled.connect(self._update_channel_count)
                row.addWidget(box)
                self.analog_boxes.append(box)
            row.addStretch(1)
            outer.addLayout(row)
        return group

    def enabled_analog(self) -> list[int]:
        return [int(box.property("analogNumber")) for box in self.analog_boxes if box.isChecked()]

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
        if self.driver.is_hardware:
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

        self.immediate_radio = QRadioButton("None: start capturing at once", group)
        self.immediate_radio.toggled.connect(self._update_trigger_mode)
        self.immediate_radio.setVisible(CAPABILITY_IMMEDIATE_TRIGGER in self.capabilities)
        layout.addWidget(self.immediate_radio)

        self.hardware_sequence = hardware_sequence_limits(self.capabilities)
        self.can_stream = ACQUISITION_STREAM in self.driver.acquisition_modes()
        self.sequence_radio = QRadioButton(
            "Sequence: stages of patterns, edges, pulse widths and gaps", group
        )
        self.sequence_radio.toggled.connect(self._update_trigger_mode)
        layout.addWidget(self.sequence_radio)
        from ..widgets.sequence_editor import SequenceEditor

        # On a stream the application evaluates any sequence; the device only its own kinds
        hardware_stages, hardware_kinds = self.hardware_sequence or (0, ())
        self.sequence_editor = SequenceEditor(
            [(number, f"Channel {number + 1}") for number in range(self.driver.channel_count)],
            max(hardware_stages, SOFTWARE_SEQUENCE_STAGES if self.can_stream else 0) or 1,
            tuple(ConditionKind) if self.can_stream else hardware_kinds,
            group,
        )
        self.sequence_editor.setContentsMargins(22, 0, 0, 0)
        layout.addWidget(self.sequence_editor)
        has_sequences = self.driver.is_hardware and (self.hardware_sequence is not None or self.can_stream)
        self.sequence_radio.setVisible(has_sequences)
        self.sequence_editor.setVisible(False)

        self.software_box = QCheckBox(
            "Evaluate the trigger in the application (on a stream, at the stream rates)", group
        )
        self.software_box.setToolTip(
            "The device streams without a trigger and the application looks for the trigger in the\n"
            "samples; it keeps the samples before and after it. Works with every trigger, also when\n"
            "the device itself has none in stream mode."
        )
        self.software_box.toggled.connect(self._on_software_toggled)
        self.software_box.setVisible(self.driver.is_hardware and self.can_stream)
        layout.addWidget(self.software_box)

        self.trigger_button_group = QButtonGroup(group)
        self.trigger_button_group.addButton(self.edge_radio)
        self.trigger_button_group.addButton(self.pattern_radio)
        self.trigger_button_group.addButton(self.immediate_radio)
        self.trigger_button_group.addButton(self.sequence_radio)

        self._update_trigger_mode()
        return group

    # ------------------------------------------------------------- behaviour
    def _apply_driver_mode(self) -> None:
        multi = self.driver.board_count > 1
        if multi:
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
        elif not self.driver.is_hardware:
            self.trigger_group.setVisible(False)
        if self.driver.blast_frequency <= 0:
            self.blast_box.setEnabled(False)
            if multi:
                self.blast_box.setToolTip("Blast mode is not available for a multi device set")
            else:
                self.blast_box.setVisible(False)
        if self.driver.max_loop_count <= 0:
            for widget in (self.burst_box, self.burst_count_box, self.measure_box):
                widget.setVisible(False)

    def _fill_trigger_channels(self) -> None:
        box = self.trigger_channel_box
        box.clear()
        channels = self.driver.edge_trigger_channels()
        multi = self.driver.board_count > 1
        if multi:
            per_device = self.driver.channels_per_device
            for index in channels:
                box.addItem(f"Channel {index + 1} (board {index // per_device + 1})", index)
        else:
            for index in channels:
                box.addItem(f"Channel {index + 1}", index)
        if self.driver.is_hardware and self.driver.has_external_trigger():
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
        if self.driver.board_count == 1 or session.trigger_channel >= self.driver.channel_count:
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
        text = f"{count} of {len(self.channel_selectors)} channels selected"
        analog = len(self.enabled_analog()) if hasattr(self, "analog_boxes") else 0
        if analog:
            text += f", {analog} analog"
        self.channel_count_label.setText(text)
        self.validation_label.clear()

    def _update_limits(self) -> None:
        channels = self.enabled_channels() or [0]
        self.limits = self.driver.get_limits(
            channels, self.acquisition_mode(), to_disk=self.to_disk(), continuous=self.continuous()
        )
        if hasattr(self, "software_box") and self.software_trigger():
            from ...driver.software_trigger import software_trigger_limits

            self.limits = software_trigger_limits(self.limits)
        self.limits = self._with_memory_depth(self.limits, channels)
        self._refresh_rates()
        if not self.fixed_rates and not self.blast_box.isChecked():
            # A stream is limited by its link (the Pico: USB full speed)
            self.frequency_box.setMaximum(self.driver.max_frequency_for(channels, self.acquisition_mode()))
        self._apply_stream_trigger()

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
        if hasattr(self, "disk_box"):
            self.disk_box.setVisible(self._stream_selected())
        if hasattr(self, "continuous_box"):
            self.continuous_box.setVisible(
                CAPABILITY_CONTINUOUS_STREAM in self.capabilities
                and self.acquisition_mode() == ACQUISITION_STREAM
            )
            continuous = self.continuous()
            self.post_label.setText("Keep last samples:" if continuous else "Post-trigger samples:")
            if continuous:
                self.post_samples_box.setToolTip(
                    "The stream runs until stopped; this many of the latest samples are kept\n"
                    f"Max: {to_thousands(self.limits.max_post_samples)}"
                )
        self._update_summary()

    def _with_memory_depth(self, limits, channels):
        """The buffer of devices whose depth depends on the analog channels in use."""
        if self.acquisition_mode() == ACQUISITION_STREAM or not hasattr(self, "analog_boxes"):
            return limits
        depth = self.driver.memory_depth(len(self.enabled_channels()), len(self.enabled_analog()))
        if not depth:
            return limits
        from ...driver.base import CaptureLimits

        return CaptureLimits(limits.min_pre_samples, min(limits.max_pre_samples, depth // 2),
                             limits.min_post_samples, min(limits.max_post_samples, depth))

    def _total_samples(self) -> int:
        loops = self.burst_count_box.value() - 1 if self.burst_box.isChecked() else 0
        return self.pre_samples_box.value() + self.post_samples_box.value() * (loops + 1)

    def _update_summary(self) -> None:
        if not hasattr(self, "summary_label"):
            return
        total = self._total_samples()
        maximum = self.limits.max_total_samples
        duration = to_small_time(total / max(self.frequency_box.value(), 1))
        if self.continuous():
            text = (
                f"Runs until stopped, keeps the last {to_thousands(total)} samples ({duration}), "
                f"at most {to_thousands(maximum)}"
            )
        else:
            analog = len(self.enabled_analog()) if hasattr(self, "analog_boxes") else 0
            text = (
                f"{to_thousands(total)} samples in total ({duration}), "
                f"at most {to_thousands(maximum)} with these channels"
                + (f" and {analog} analog" if analog else "")
            )
        if self.to_disk():
            channels = max(len(self.enabled_channels()), 1)
            text += f"; on disk: {to_bytes(total * channels * (2 if self.continuous() else 1))} of {to_bytes(disk_sample_bytes())} free"
        self.summary_label.setText(text)
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

    def _apply_stream_trigger(self) -> None:
        """Devices whose stream starts at once offer only "None" as its trigger."""
        if not hasattr(self, "immediate_radio"):
            return
        only_immediate = (
            self.acquisition_mode() == ACQUISITION_STREAM
            and CAPABILITY_STREAM_IMMEDIATE_ONLY in self.capabilities
            and not (hasattr(self, "software_box") and self.software_box.isChecked())
        )
        self.immediate_radio.setVisible(only_immediate or CAPABILITY_IMMEDIATE_TRIGGER in self.capabilities)
        self.edge_radio.setEnabled(not only_immediate)
        self.pattern_radio.setEnabled(not only_immediate)
        if only_immediate:
            self.immediate_radio.setChecked(True)
        elif self.immediate_radio.isChecked() and self.immediate_radio.isHidden():
            self.edge_radio.setChecked(True)

    def _on_software_toggled(self, checked: bool) -> None:
        if checked:
            position = self.acquisition_box.findData(ACQUISITION_STREAM)
            if position >= 0:
                self.acquisition_box.setCurrentIndex(position)
        self._apply_stream_trigger()
        self._update_trigger_mode()
        self._update_limits()

    def software_trigger(self) -> bool:
        """The application evaluates the trigger (only devices that stream)."""
        return self.can_stream and self.driver.is_hardware and self.software_box.isChecked()

    def _fits_hardware(self, sequence) -> bool:
        if self.hardware_sequence is None:
            return False
        stages, kinds = self.hardware_sequence
        return len(sequence.stages) <= stages and all(stage.condition.kind in kinds for stage in sequence.stages)

    def _update_trigger_mode(self) -> None:
        edge = self.edge_radio.isChecked()
        immediate = getattr(self, "immediate_radio", None) is not None and self.immediate_radio.isChecked()
        sequence = getattr(self, "sequence_radio", None) is not None and self.sequence_radio.isChecked()
        self.edge_panel.setEnabled(edge)
        self.pattern_panel.setEnabled(not edge and not immediate and not sequence)
        if hasattr(self, "sequence_editor"):
            self.sequence_editor.setVisible(sequence)
            if sequence and self.hardware_sequence is None and not self.software_box.isChecked():
                self.software_box.setChecked(True)
        # Without a trigger every sample follows the start.
        self.pre_samples_box.setEnabled(not immediate and not self.blast_box.isChecked())
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
            channels = self.enabled_channels() or [0]
            self.frequency_box.setRange(
                max(self.driver.min_frequency, 1), self.driver.max_frequency_for(channels, self.acquisition_mode())
            )
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
        self.continuous_box.setChecked(False)
        self.disk_box.setChecked(False)
        self.pattern_base_box.setValue(1)
        self.edge_radio.setChecked(True)
        if self.acquisition_box.count():
            self.acquisition_box.setCurrentIndex(0)
        self.threshold_box.setValue(1.0)
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
            problem = profile_problem(profile, self.driver)
            action = menu.addAction(profile.name.replace("&", "&&") + (f"  ({problem})" if problem else ""))
            action.setEnabled(profile.capture_settings is not None and not problem)
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
            self.apply_session(fit_session(profile.capture_settings, self.driver)[0])

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
        self._loading_session = True
        try:
            self._apply_session(session)
        finally:
            self._loading_session = False

    def _apply_session(self, session: CaptureSession) -> None:
        self.reset_settings()

        for channel in session.capture_channels:
            if 0 <= channel.channel_number < len(self.channel_selectors):
                selector = self.channel_selectors[channel.channel_number]
                selector.enabled = True
                selector.channel_name = channel.channel_name
                selector.channel_color = channel.channel_color
                selector._update_color()

        position = self.acquisition_box.findData(session.acquisition_mode)
        if position >= 0:
            self.acquisition_box.setCurrentIndex(position)
        if session.threshold_voltage is not None:
            self.threshold_box.setValue(session.threshold_voltage)
        self.continuous_box.setChecked(session.continuous)
        self.disk_box.setChecked(session.to_disk)
        if not self.software_box.isHidden():
            self.software_box.setChecked(session.software_trigger and self.can_stream)
        for box in getattr(self, "analog_boxes", []):
            box.setChecked(any(channel.channel_number == box.property("analogNumber")
                               for channel in session.analog_channels))
        if session.clock_channel is not None and CAPABILITY_STATE_MODE in self.capabilities:
            self.clock_box.setChecked(True)
            self.clock_channel_box.setCurrentIndex(max(self.clock_channel_box.findData(session.clock_channel), 0))
            self.clock_edge_box.setCurrentIndex(max(self.clock_edge_box.findData(session.clock_edge), 0))

        if session.trigger_type == TriggerType.BLAST and self.blast_box.isEnabled():
            self.blast_box.setChecked(True)
            self._update_limits()
            self.post_samples_box.setValue(session.post_trigger_samples)
        else:
            self.frequency_box.setValue(
                max(min(session.frequency, self.driver.max_frequency), self.driver.min_frequency)
            )
            self._update_channel_count()
            self._update_limits()
            self.pre_samples_box.setValue(session.pre_trigger_samples)
            self.post_samples_box.setValue(session.post_trigger_samples)

        self._update_channel_count()
        if not self.driver.is_hardware or session.trigger_type == TriggerType.SIMULATION:
            return  # no trigger settings (simulated captures are generated, not triggered)

        if session.trigger_type == TriggerType.SEQUENCE:
            if not self.sequence_radio.isHidden():
                self.sequence_editor.set_sequence(session.trigger_sequence)
                self.sequence_radio.setChecked(True)
        elif session.trigger_type == TriggerType.IMMEDIATE:
            if self.immediate_radio.isVisible() or CAPABILITY_IMMEDIATE_TRIGGER in self.capabilities:
                self.immediate_radio.setChecked(True)
        elif session.trigger_type in (TriggerType.EDGE, TriggerType.BLAST):
            if session.trigger_type == TriggerType.BLAST and self.driver.blast_frequency <= 0:
                return  # e.g. a multi device set has no blast mode
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

    def _apply_only(self) -> None:
        self.applied_only = True
        session = self.build_session()
        if session is None:
            self.applied_only = False
            return
        self.selected_settings = session
        if self.persist:
            self._persist_settings(session)
        self.accept()

    def _reject_settings(self, message: str, focus: Optional[QWidget] = None) -> None:
        """What is wrong, on the page where it can be fixed (marked in the list)."""
        self.validation_label.show_error(message)
        page = None
        if focus is not None:
            page = next((index for index in range(self.pages.count())
                         if self.pages.widget(index).isAncestorOf(focus)), None)
        elif "channel" in message.lower():
            page = 1
        for index in range(self.page_list.count()):
            item = self.page_list.item(index)
            item.setIcon(icon("info", ERROR) if index == page else icon(("clock", "channels", "target")[index]))
        if page is not None:
            self.page_list.setCurrentRow(page)
        if focus is not None:
            focus.setFocus()

    def _update_overview(self) -> None:
        channels = sum(1 for selector in self.channel_selectors if selector.enabled)
        total = self.pre_samples_box.value() + self.post_samples_box.value()
        trigger = next((name for radio, name in ((self.edge_radio, "edge"), (self.pattern_radio, "pattern"),
                                                 (self.immediate_radio, "none"), (self.sequence_radio, "sequence"))
                        if radio.isChecked()), "")
        self.overview_label.setText(
            f"{channels} channel{'s' if channels != 1 else ''} · {self.frequency_box.value():,} Hz · "
            f"{total:,} samples" + (f" · trigger: {trigger}" if trigger else ""))

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

        analog = [
            AnalogChannel(channel_number=number, channel_name=self.driver.analog_channel_names()[number])
            for number in (self.enabled_analog() if hasattr(self, "analog_boxes") else [])
        ]
        if not channels and not analog:
            self._reject_settings("Select at least one channel to capture.")
            return None

        session = CaptureSession()
        session.capture_channels = channels
        session.analog_channels = analog
        session.frequency = self.frequency_box.value()
        session.pre_trigger_samples = self.pre_samples_box.value()
        session.post_trigger_samples = self.post_samples_box.value()
        session.acquisition_mode = self.acquisition_mode() or ACQUISITION_BUFFER
        session.continuous = self.continuous()
        session.to_disk = self.to_disk()
        if CAPABILITY_THRESHOLD in self.capabilities:
            session.threshold_voltage = round(self.threshold_box.value(), 2)
        immediate = self.immediate_radio.isChecked()
        if immediate:
            session.pre_trigger_samples = 0

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

        channel_numbers = [c.channel_number for c in channels] or [0]
        maximum = self._with_memory_depth(self.driver.get_limits(
            channel_numbers, self.acquisition_mode(), to_disk=session.to_disk, continuous=session.continuous
        ), channel_numbers).max_total_samples
        if session.pre_trigger_samples + session.post_trigger_samples * (loops + 1) > maximum:
            self._reject_settings(
                f"The capture is too long: at most {to_thousands(maximum)} samples fit into the "
                "buffer with the selected channels.",
                self.post_samples_box,
            )
            return None

        if self.fixed_rates and session.frequency not in (
            self.driver.sample_rates(channel_numbers, self.acquisition_mode()) or []
        ):
            self._reject_settings(
                "The device cannot sample these channels at this rate: choose fewer channels, "
                "lower channel numbers or another rate.",
                self.rate_box,
            )
            return None

        if not self.driver.is_hardware:
            session.trigger_type = TriggerType.EDGE
            return session

        session.software_trigger = self.software_trigger()
        if session.software_trigger and session.acquisition_mode != ACQUISITION_STREAM:
            self._reject_settings(
                "A trigger evaluated by the application needs the stream acquisition.", self.acquisition_box
            )
            return None
        if self.clock_box.isVisible() and self.clock_box.isChecked():
            session.clock_channel = self.clock_channel_box.currentData()
            session.clock_edge = self.clock_edge_box.currentData()

        if self.sequence_radio.isChecked():
            try:
                sequence = self.sequence_editor.sequence()
            except ValueError as error:
                self._reject_settings(str(error), self.sequence_editor)
                return None
            limits = getattr(self.driver, "trigger_sequence_limits", lambda: None)()
            if not session.software_trigger and limits and session.clock_channel is None and session.frequency > limits[2]:
                self._reject_settings(
                    f"The device evaluates trigger sequences up to {to_large_frequency(limits[2])}: lower the "
                    "rate, or tick 'Evaluate the trigger in the application' to look for it in a stream.",
                    self.frequency_box,
                )
                return None
            if not session.software_trigger and not self._fits_hardware(sequence):
                self._reject_settings(
                    "The device cannot evaluate this sequence itself: tick 'Evaluate the trigger in the "
                    "application', or use fewer stages and the conditions it supports.",
                    self.software_box if self.can_stream else self.sequence_editor,
                )
                return None
            session.trigger_type = TriggerType.SEQUENCE
            session.trigger_sequence = sequence
            return session
        if immediate:
            session.trigger_type = TriggerType.IMMEDIATE
        elif edge_mode:
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
            if not session.software_trigger and not pattern_fits(
                self.driver.pattern_trigger_groups(), base_channel, len(pattern)
            ):
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
