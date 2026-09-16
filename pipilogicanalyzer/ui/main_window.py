# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of PiPiLogicAnalyzer, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Main window (port of ``MainWindow.axaml`` and ``MainWindow.axaml.cs``)."""

from __future__ import annotations

import html
import math
import os
import webbrowser
from typing import Optional

import numpy as np
from PySide6.QtCore import QObject, QTimer, Qt, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QScrollBar,
    QSizePolicy,
    QSlider,
    QSplitter,
    QStackedWidget,
    QStatusBar,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..core import alignment, capture_io, settings
from ..core import firmware as firmware_images
from ..core import simulation as simulation_signals
from ..core.formatting import to_large_frequency, to_small_time, to_thousands
from ..core.profiles import (
    PROFILE_FILE_FILTER,
    Profile,
    ProfileStore,
    read_profiles_file,
    write_profiles_file,
)
from ..core.regions import SampleRegion
from ..driver import detector
from ..driver.analyzer import PiPiLogicAnalyzerDriver
from ..driver.base import (
    CAPABILITY_SIMULATION,
    AnalyzerDriverBase,
    AnalyzerDriverType,
    CaptureCompletedArgs,
    CaptureError,
    DeviceConnectionError,
)
from ..driver.emulated import EmulatedAnalyzerDriver
from ..driver.models import CaptureSession, TriggerType
from ..driver.multi import MultiAnalyzerDriver
from ..sigrok.provider import SigrokProvider
from . import messages
from .dialogs.align_dialog import AlignDialog
from .dialogs.annotation_list import AnnotationListWindow
from .dialogs.board_test_dialog import BoardTestDialog
from .dialogs.capture_dialog import CaptureDialog, capture_settings_file
from .dialogs.common import Banner, hint
from .dialogs.firmware_dialog import ConnectedDevice, FirmwareDialog
from .dialogs.simulation_dialog import SimulationDialog
from .dialogs.create_samples_dialog import CreateSamplesDialog
from .dialogs.device_dialogs import (
    AboutDialog,
    DeviceInfoDialog,
    MultiComposeDialog,
    MultiConnectDialog,
    NetworkConnectDialog,
    NetworkSettingsDialog,
)
from .dialogs.measure_dialog import MeasureDialog
from .dialogs.profile_dialog import ProfileEditDialog
from .dialogs.region_dialog import RegionDialog
from .dialogs.shift_dialog import ShiftChannelsDialog, ShiftDirection, ShiftMode
from .icons import icon, set_icon
from .theme import ACCENT, STYLESHEET, TEXT_MUTED, apply_palette, set_role, set_variant
from .view_model import (
    CHANNEL_HEIGHT_STEP,
    DEFAULT_CHANNEL_HEIGHT,
    LARGEST_CHANNEL_HEIGHT,
    SMALLEST_CHANNEL_HEIGHT,
    CaptureViewModel,
)
from .widgets.annotation_viewer import AnnotationViewer
from .widgets.channel_viewer import CHANNEL_COLUMN_WIDTH, ChannelViewer
from .widgets.decoder_manager import DecoderManager
from .widgets.sample_marker import SampleMarker
from .widgets.sample_previewer import SamplePreviewer
from .widgets.sample_viewer import SampleViewer

DOCUMENTATION_URL = "https://github.com/deckerjulian/PiPiLogicAnalyzer/wiki"
#: Wiki of the original LogicAnalyzer, which documents the hardware.
UPSTREAM_DOCUMENTATION_URL = "https://github.com/gusmanb/logicanalyzer/wiki"
POWER_POLL_INTERVAL_MS = 30_000
BOARD_WATCH_INTERVAL_MS = 2_000
WINDOW_STATE_FILE = "window-state.json"
ZOOM_SLIDER_STEPS = 1000
CAPTURE_FILE_FILTER = "Logic analyzer captures (*.lac *.lac.gz);;All files (*)"


class CaptureBridge(QObject):
    """Marshals capture completion from the driver thread to the UI thread."""

    completed = Signal(object)


class MainWindow(QMainWindow):
    def __init__(self, decoder_paths: tuple[str, ...] = ()) -> None:
        super().__init__()
        self.resize(1280, 820)
        apply_palette(self)
        self.setStyleSheet(STYLESHEET)

        self.model = CaptureViewModel(self)
        #: Open annotation list windows by (decoder instance, row name).
        self._annotation_lists: dict[tuple[int, str], AnnotationListWindow] = {}
        self.driver: Optional[AnalyzerDriverBase] = None
        self.provider = SigrokProvider()
        if decoder_paths:
            self.provider.registry.search_paths = list(decoder_paths) + [
                path for path in self.provider.registry.search_paths if path not in decoder_paths
            ]
        self.profiles = ProfileStore()
        #: Settings of the simulated capture in progress (decoders to add).
        self._pending_simulation: Optional[dict] = None
        self.clipboard_samples: Optional[list[np.ndarray]] = None
        self.current_file: Optional[str] = None
        self._updating_view_controls = False
        #: (session, samples before any alignment by channel id) to align again with another method.
        self._unaligned: Optional[tuple[CaptureSession, dict[int, np.ndarray]]] = None
        #: (session, how its boards were aligned) for the capture information.
        self._alignment_report: Optional[tuple[CaptureSession, str]] = None

        self.bridge = CaptureBridge(self)
        self.bridge.completed.connect(self._on_capture_completed)

        self._build_ui()
        self._build_menu()

        self.power_timer = QTimer(self)
        self.power_timer.setInterval(POWER_POLL_INTERVAL_MS)
        self.power_timer.timeout.connect(self._update_power_status)

        # Looks for boards without the firmware while no device is connected.
        self.board_watch_timer = QTimer(self)
        self.board_watch_timer.setInterval(BOARD_WATCH_INTERVAL_MS)
        self.board_watch_timer.timeout.connect(self._check_for_new_boards)
        self.board_watch_timer.start()

        self.model.view_changed.connect(self._sync_view_controls)
        self.model.capture_changed.connect(self._on_capture_changed)

        self._update_title()
        self.refresh_ports()
        self._update_actions()
        self._update_info_panel()
        self._sync_view_controls()
        self._restore_window_state()

    # -------------------------------------------------------- window geometry
    def _restore_window_state(self) -> None:
        """Reopen where the window was last closed (``PersistableWindowBase``)."""
        state = settings.get_settings(WINDOW_STATE_FILE)
        if not isinstance(state, dict):
            return
        try:
            width = int(state.get("width", 0))
            height = int(state.get("height", 0))
            if width > 200 and height > 200:
                self.resize(width, height)
            if "x" in state and "y" in state:
                self.move(int(state["x"]), int(state["y"]))
            if state.get("maximized"):
                self.showMaximized()
            sizes = state.get("splitter")
            if isinstance(sizes, list) and len(sizes) == 2:
                self.splitter.setSizes([int(size) for size in sizes])
            if "preview" in state:
                # The pinned state of the preview is persisted since V6_5.
                visible = bool(state["preview"])
                self.action_toggle_preview.setChecked(visible)
                self.previewer.setVisible(visible)
            if "channel_height" in state:
                self.model.set_channel_height(int(state["channel_height"]))
        except (TypeError, ValueError):
            return

    def _save_window_state(self) -> None:
        geometry = self.normalGeometry()
        settings.persist_settings(
            WINDOW_STATE_FILE,
            {
                "x": geometry.x(),
                "y": geometry.y(),
                "width": geometry.width(),
                "height": geometry.height(),
                "maximized": self.isMaximized(),
                "splitter": self.splitter.sizes(),
                "preview": self.action_toggle_preview.isChecked(),
                "channel_height": self.model.channel_height,
            },
        )

    # ------------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        self.addToolBar(Qt.TopToolBarArea, self._build_toolbar())

        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Boards without the firmware, or with an outdated one.
        banner_holder = QWidget(central)
        banner_layout = QVBoxLayout(banner_holder)
        banner_layout.setContentsMargins(8, 8, 8, 0)
        self.firmware_banner = Banner("warning", banner_holder)
        self.firmware_notice = self.firmware_banner.label
        self.firmware_notice_button = self.firmware_banner.button
        self.firmware_notice_button.clicked.connect(self.install_firmware)
        set_icon(self.firmware_notice_button, "chip")
        banner_layout.addWidget(self.firmware_banner)
        self.firmware_banner.setVisible(False)
        self._banner_holder = banner_holder
        banner_holder.setVisible(False)
        layout.addWidget(banner_holder)

        self.splitter = QSplitter(Qt.Horizontal, central)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(self._build_capture_area())
        self.splitter.addWidget(self._build_side_panel())
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setSizes([940, 340])
        layout.addWidget(self.splitter, 1)

        self.setCentralWidget(central)
        status = QStatusBar(self)
        self.power_label = QLabel(status)
        self.power_label.setVisible(False)
        status.addPermanentWidget(self.power_label)
        self.setStatusBar(status)
        self.statusBar().showMessage("Ready")

    def _build_toolbar(self) -> QToolBar:
        bar = QToolBar("Main toolbar", self)
        bar.setObjectName("main-toolbar")
        bar.setMovable(False)
        bar.setFloatable(False)
        bar.toggleViewAction().setVisible(False)

        self.port_combo = QComboBox(bar)
        self.port_combo.setMinimumWidth(260)
        self.port_combo.setToolTip("Analyzer to connect to")
        bar.addWidget(self.port_combo)

        self.refresh_button = QPushButton("Refresh", bar)
        set_icon(self.refresh_button, "refresh")
        self.refresh_button.setToolTip("Search for connected analyzers again")
        self.refresh_button.clicked.connect(self.refresh_ports)
        bar.addWidget(self.refresh_button)

        self.connect_button = QPushButton("Connect", bar)
        self.connect_button.clicked.connect(self.toggle_connection)
        bar.addWidget(self.connect_button)

        bar.addSeparator()

        self.device_label = QLabel(bar)
        # Clicking the connected device opens the board information.
        self.device_label.setTextInteractionFlags(Qt.LinksAccessibleByMouse)
        self.device_label.linkActivated.connect(lambda _link: self.show_device_info())
        self._set_device_label(None)
        bar.addWidget(self.device_label)

        spacer = QWidget(bar)
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        bar.addWidget(spacer)

        self.capture_button = QPushButton("Capture...", bar)
        self.capture_button.setToolTip("Configure and start a capture (F5)")
        set_variant(self.capture_button, "primary")
        set_icon(self.capture_button, "record")
        self.capture_button.clicked.connect(self.start_capture)
        bar.addWidget(self.capture_button)

        self.repeat_button = QPushButton("Repeat", bar)
        set_icon(self.repeat_button, "repeat")
        self.repeat_button.setToolTip("Capture again with the settings of the last capture (Ctrl+R)")
        self.repeat_button.clicked.connect(self.repeat_capture)
        bar.addWidget(self.repeat_button)

        self.abort_button = QPushButton("Stop", bar)
        self.abort_button.setToolTip("Abort the running capture (Shift+F5)")
        set_variant(self.abort_button, "danger")
        set_icon(self.abort_button, "stop")
        self.abort_button.clicked.connect(self.abort_capture)
        bar.addWidget(self.abort_button)

        return bar

    def _build_capture_area(self) -> QWidget:
        self.capture_stack = QStackedWidget(self)
        self.capture_stack.addWidget(self._build_empty_page())

        container = QWidget(self.capture_stack)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.annotation_viewer = AnnotationViewer(self.model, container)
        self.annotation_viewer.row_clicked.connect(self.show_annotation_list)
        layout.addWidget(self.annotation_viewer)

        ruler_row = QWidget(container)
        ruler_layout = QHBoxLayout(ruler_row)
        ruler_layout.setContentsMargins(0, 0, 0, 0)
        ruler_layout.setSpacing(0)

        show_all_button = QPushButton("Show all", ruler_row)
        set_icon(show_all_button, "eye")
        show_all_button.setFixedWidth(CHANNEL_COLUMN_WIDTH)
        show_all_button.setToolTip("Show every hidden channel again")
        show_all_button.clicked.connect(lambda: self.channel_viewer.show_all_channels())
        ruler_layout.addWidget(show_all_button)

        self.sample_marker = SampleMarker(self.model, ruler_row)
        self.sample_marker.setToolTip(
            "Drag to select samples, right-click for editing, measuring and regions"
        )
        ruler_layout.addWidget(self.sample_marker, 1)
        layout.addWidget(ruler_row)

        # Pinned channels: outside the scroll area, so they stay in view.
        self.pinned_area = QWidget(container)
        pinned_layout = QHBoxLayout(self.pinned_area)
        pinned_layout.setContentsMargins(0, 0, 0, 0)
        pinned_layout.setSpacing(0)
        self.pinned_channel_viewer = ChannelViewer(self.model, self.pinned_area, section="pinned")
        pinned_layout.addWidget(self.pinned_channel_viewer)
        self.pinned_sample_viewer = SampleViewer(self.model, self.pinned_area, section="pinned")
        self.pinned_sample_viewer.setMinimumHeight(0)
        pinned_layout.addWidget(self.pinned_sample_viewer, 1)
        layout.addWidget(self.pinned_area)
        self.pinned_separator = QFrame(container)
        self.pinned_separator.setFixedHeight(3)
        self.pinned_separator.setStyleSheet(f"background-color: {ACCENT};")
        layout.addWidget(self.pinned_separator)
        self.pinned_area.setVisible(False)
        self.pinned_separator.setVisible(False)

        self.scroll_area = QScrollArea(container)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll_area.setFrameShape(QFrame.NoFrame)

        waveform_container = QWidget(self.scroll_area)
        waveform_layout = QHBoxLayout(waveform_container)
        waveform_layout.setContentsMargins(0, 0, 0, 0)
        waveform_layout.setSpacing(0)

        self.channel_viewer = ChannelViewer(self.model, waveform_container, section="scrolling")
        waveform_layout.addWidget(self.channel_viewer)

        self.sample_viewer = SampleViewer(self.model, waveform_container, section="scrolling")
        waveform_layout.addWidget(self.sample_viewer, 1)

        self.scroll_area.setWidget(waveform_container)
        layout.addWidget(self.scroll_area, 1)

        self.previewer = SamplePreviewer(self.model, container)
        layout.addWidget(self.previewer)

        self.position_scrollbar = QScrollBar(Qt.Horizontal, container)
        self.position_scrollbar.valueChanged.connect(self._on_scrollbar_moved)
        layout.addWidget(self.position_scrollbar)

        self.sample_marker.copy_requested.connect(self.copy_samples)
        self.sample_marker.cut_requested.connect(self.cut_samples)
        self.sample_marker.paste_requested.connect(self.paste_samples)
        self.sample_marker.insert_requested.connect(self.insert_samples_dialog)
        self.sample_marker.delete_requested.connect(self.delete_samples)
        self.sample_marker.measure_requested.connect(self.measure_samples)
        self.sample_marker.shift_requested.connect(self.shift_channels)
        self.sample_marker.create_region_requested.connect(self.create_region)
        self.sample_marker.delete_region_requested.connect(self.model.remove_region)
        self.model.channels_changed.connect(self._update_pinned_area)
        self.model.capture_changed.connect(self._update_pinned_area)
        self.model.channel_height_changed.connect(self._update_pinned_area)

        self.capture_stack.addWidget(container)
        return self.capture_stack

    def _build_empty_page(self) -> QWidget:
        """Shown while there is no capture: explains the three ways to get one."""
        page = QWidget(self)
        outer = QVBoxLayout(page)
        outer.addStretch(2)

        card = QFrame(page)
        set_role(card, "card")
        card.setMaximumWidth(560)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(28, 24, 28, 24)
        card_layout.setSpacing(10)

        title = QLabel("No capture loaded", card)
        set_role(title, "title")
        card_layout.addWidget(title)
        card_layout.addWidget(
            hint(
                "Connect an analyzer and start a capture, open a saved capture file, or try "
                "the application with simulated test signals.",
                card,
            )
        )
        card_layout.addSpacing(6)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.empty_primary_button = QPushButton("Connect device", card)
        set_variant(self.empty_primary_button, "primary")
        self.empty_primary_button.clicked.connect(self._empty_page_primary_action)
        buttons.addWidget(self.empty_primary_button)
        open_button = QPushButton("Open capture...", card)
        set_icon(open_button, "folder")
        open_button.clicked.connect(self.open_capture)
        buttons.addWidget(open_button)
        simulate_button = QPushButton("Simulated capture...", card)
        set_icon(simulate_button, "wave")
        simulate_button.clicked.connect(self.simulated_capture)
        buttons.addWidget(simulate_button)
        buttons.addStretch(1)
        card_layout.addLayout(buttons)

        card_layout.addSpacing(6)
        card_layout.addWidget(
            hint("F5 capture  ·  Ctrl+O open  ·  Ctrl+0 zoom to fit  ·  Ctrl+T go to trigger", card)
        )

        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(card, 3)
        row.addStretch(1)
        outer.addLayout(row)
        outer.addStretch(3)
        return page

    def _update_pinned_area(self) -> None:
        count = len(self.model.section_channels("pinned"))
        self.pinned_area.setFixedHeight(count * self.model.channel_height)
        self.pinned_area.setVisible(count > 0)
        self.pinned_separator.setVisible(count > 0)
        self.pinned_sample_viewer.update()

    def _sync_channel_height(self) -> None:
        height = self.model.channel_height
        if self.channel_height_slider.value() != height:
            self.channel_height_slider.blockSignals(True)
            self.channel_height_slider.setValue(height)
            self.channel_height_slider.blockSignals(False)
        self.channel_height_label.setText(f"{height} px")

    def _empty_page_primary_action(self) -> None:
        if self._has_real_device():
            self.start_capture()
        else:
            self.connect_device()

    def _build_side_panel(self) -> QWidget:
        panel = QWidget(self)
        panel.setMinimumWidth(300)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        self.decoder_manager = DecoderManager(self.model, self.provider, panel)
        layout.addWidget(self.decoder_manager, 1)

        zoom_box = QFrame(panel)
        set_role(zoom_box, "card")
        zoom_layout = QVBoxLayout(zoom_box)
        zoom_layout.setContentsMargins(10, 8, 10, 8)

        header = QHBoxLayout()
        title = QLabel("View", zoom_box)
        set_role(title, "heading")
        header.addWidget(title)
        header.addStretch(1)
        self.fit_button = QPushButton("Fit", zoom_box)
        set_icon(self.fit_button, "fit")
        self.fit_button.setToolTip("Show the whole capture (Ctrl+0)")
        self.fit_button.clicked.connect(self.model.zoom_to_fit)
        header.addWidget(self.fit_button)
        self.trigger_button = QPushButton("Trigger", zoom_box)
        set_icon(self.trigger_button, "target")
        self.trigger_button.setToolTip("Center the view on the trigger (Ctrl+T)")
        self.trigger_button.clicked.connect(self.go_to_trigger)
        header.addWidget(self.trigger_button)
        zoom_layout.addLayout(header)

        slider_row = QHBoxLayout()
        self.min_samples_label = QLabel("4", zoom_box)
        set_role(self.min_samples_label, "hint")
        slider_row.addWidget(self.min_samples_label)
        self.zoom_slider = QSlider(Qt.Horizontal, zoom_box)
        self.zoom_slider.setRange(0, ZOOM_SLIDER_STEPS)
        self.zoom_slider.setToolTip("Samples shown on screen")
        self.zoom_slider.valueChanged.connect(self._on_zoom_slider)
        slider_row.addWidget(self.zoom_slider, 1)
        self.max_samples_label = QLabel("0", zoom_box)
        set_role(self.max_samples_label, "hint")
        slider_row.addWidget(self.max_samples_label)
        zoom_layout.addLayout(slider_row)

        self.visible_samples_label = QLabel("-", zoom_box)
        self.visible_samples_label.setAlignment(Qt.AlignCenter)
        set_role(self.visible_samples_label, "hint")
        zoom_layout.addWidget(self.visible_samples_label)

        height_row = QHBoxLayout()
        height_title = QLabel("Channel height", zoom_box)
        set_role(height_title, "hint")
        height_row.addWidget(height_title)
        self.channel_height_slider = QSlider(Qt.Horizontal, zoom_box)
        self.channel_height_slider.setRange(SMALLEST_CHANNEL_HEIGHT, LARGEST_CHANNEL_HEIGHT)
        self.channel_height_slider.setValue(self.model.channel_height)
        self.channel_height_slider.setToolTip(
            "Height of the channels: lower channels fit more of them on the screen "
            "(Alt + mouse wheel, Ctrl+Shift+Up/Down)"
        )
        self.channel_height_slider.valueChanged.connect(self.model.set_channel_height)
        height_row.addWidget(self.channel_height_slider, 1)
        self.channel_height_label = QLabel(f"{self.model.channel_height} px", zoom_box)
        self.channel_height_label.setMinimumWidth(self.channel_height_label.fontMetrics().horizontalAdvance("000 px"))
        set_role(self.channel_height_label, "hint")
        height_row.addWidget(self.channel_height_label)
        zoom_layout.addLayout(height_row)
        self.model.channel_height_changed.connect(self._sync_channel_height)
        layout.addWidget(zoom_box)

        info_box = QFrame(panel)
        set_role(info_box, "card")
        info_layout = QVBoxLayout(info_box)
        info_layout.setContentsMargins(10, 8, 10, 8)
        info_title = QLabel("Capture", info_box)
        set_role(info_title, "heading")
        info_layout.addWidget(info_title)
        self.info_label = QLabel("No capture loaded", info_box)
        self.info_label.setTextFormat(Qt.RichText)
        self.info_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        info_layout.addWidget(self.info_label)
        layout.addWidget(info_box)

        return panel

    def _build_menu(self) -> None:
        menu = self.menuBar()

        # ------------------------------------------------------------ File
        file_menu = menu.addMenu("&File")
        self.action_new = QAction("&New capture...", self)
        self.action_new.setShortcut(QKeySequence.New)
        self.action_new.setStatusTip("Compose a capture from signal descriptions")
        self.action_new.triggered.connect(self.new_capture)
        file_menu.addAction(self.action_new)

        self.action_open = QAction("&Open capture...", self)
        self.action_open.setShortcut(QKeySequence.Open)
        self.action_open.triggered.connect(self.open_capture)
        file_menu.addAction(self.action_open)

        file_menu.addSeparator()
        self.action_save = QAction("&Save", self)
        self.action_save.setShortcut(QKeySequence.Save)
        self.action_save.triggered.connect(self.save_capture)
        file_menu.addAction(self.action_save)

        self.action_save_as = QAction("Save &as...", self)
        self.action_save_as.setShortcut(QKeySequence("Ctrl+Shift+S"))
        self.action_save_as.triggered.connect(self.save_capture_as)
        file_menu.addAction(self.action_save_as)

        export_menu = file_menu.addMenu("&Export")
        self.action_export_csv = QAction("Comma separated values (.csv)...", self)
        self.action_export_csv.triggered.connect(lambda: self.export_capture("csv"))
        export_menu.addAction(self.action_export_csv)
        self.action_export_vcd = QAction("Value change dump (.vcd)...", self)
        self.action_export_vcd.triggered.connect(lambda: self.export_capture("vcd"))
        export_menu.addAction(self.action_export_vcd)

        file_menu.addSeparator()
        action_exit = QAction("E&xit", self)
        action_exit.setShortcut(QKeySequence.Quit)
        action_exit.triggered.connect(self.close)
        file_menu.addAction(action_exit)

        # --------------------------------------------------------- Capture
        capture_menu = menu.addMenu("&Capture")
        self.action_capture = QAction("&Start capture...", self)
        self.action_capture.setShortcut(QKeySequence("F5"))
        self.action_capture.triggered.connect(self.start_capture)
        capture_menu.addAction(self.action_capture)

        self.action_repeat = QAction("&Repeat last capture", self)
        self.action_repeat.setShortcut(QKeySequence("Ctrl+R"))
        self.action_repeat.triggered.connect(self.repeat_capture)
        capture_menu.addAction(self.action_repeat)

        self.action_stop = QAction("S&top capture", self)
        self.action_stop.setShortcut(QKeySequence("Shift+F5"))
        self.action_stop.triggered.connect(self.abort_capture)
        capture_menu.addAction(self.action_stop)

        self.action_align = QAction("&Align boards", self)
        self.action_align.setStatusTip(
            "Align the boards of a multi device capture using a reference line or the clock"
        )
        self.action_align.triggered.connect(self.align_boards)
        capture_menu.addAction(self.action_align)

        capture_menu.addSeparator()
        self.action_simulation = QAction("Si&mulated capture...", self)
        self.action_simulation.triggered.connect(self.simulated_capture)
        capture_menu.addAction(self.action_simulation)

        # ---------------------------------------------------------- Device
        device_menu = menu.addMenu("&Device")
        self.action_connect = QAction("&Connect", self)
        self.action_connect.triggered.connect(self.toggle_connection)
        device_menu.addAction(self.action_connect)

        action_refresh = QAction("&Refresh device list", self)
        action_refresh.triggered.connect(self.refresh_ports)
        device_menu.addAction(action_refresh)

        device_menu.addSeparator()
        self.action_device_info = QAction("Device &information...", self)
        self.action_device_info.triggered.connect(self.show_device_info)
        device_menu.addAction(self.action_device_info)

        self.action_board_test = QAction("Board &self-test...", self)
        self.action_board_test.triggered.connect(self.run_board_test)
        device_menu.addAction(self.action_board_test)

        self.action_network_settings = QAction("&Network settings...", self)
        self.action_network_settings.triggered.connect(self.update_network_settings)
        device_menu.addAction(self.action_network_settings)

        device_menu.addSeparator()
        self.action_firmware = QAction("Install or update &firmware...", self)
        self.action_firmware.triggered.connect(self.install_firmware)
        device_menu.addAction(self.action_firmware)

        self.action_bootloader = QAction("Enter &bootloader mode...", self)
        self.action_bootloader.triggered.connect(self.enter_bootloader)
        device_menu.addAction(self.action_bootloader)

        device_menu.addSeparator()
        self.action_forget_devices = QAction("Forget multi device sets...", self)
        self.action_forget_devices.triggered.connect(self.forget_known_devices)
        device_menu.addAction(self.action_forget_devices)

        for action, name in (
            (self.action_new, "file-plus"),
            (self.action_open, "folder"),
            (self.action_save, "save"),
            (self.action_save_as, "save"),
            (self.action_export_csv, "export"),
            (self.action_export_vcd, "export"),
            (self.action_capture, "record"),
            (self.action_repeat, "repeat"),
            (self.action_stop, "stop"),
            (self.action_simulation, "wave"),
            (action_refresh, "refresh"),
            (self.action_device_info, "info"),
            (self.action_board_test, "checklist"),
            (self.action_network_settings, "wifi"),
            (self.action_firmware, "chip"),
            (self.action_bootloader, "power"),
        ):
            action.setIcon(icon(name))

        # -------------------------------------------------------- Profiles
        self.profiles_menu = menu.addMenu("&Profiles")
        self.profiles_menu.aboutToShow.connect(self._rebuild_profiles_menu)
        self._rebuild_profiles_menu()

        # ------------------------------------------------------------ View
        view_menu = menu.addMenu("&View")
        action_zoom_in = QAction("Zoom &in", self)
        # "=" is "+" without Shift on US keyboards.
        action_zoom_in.setShortcuts(
            QKeySequence.keyBindings(QKeySequence.ZoomIn) + [QKeySequence("+"), QKeySequence("=")]
        )
        action_zoom_in.triggered.connect(lambda: self.model.zoom(0.5))
        view_menu.addAction(action_zoom_in)

        action_zoom_out = QAction("Zoom &out", self)
        action_zoom_out.setShortcuts(QKeySequence.keyBindings(QKeySequence.ZoomOut) + [QKeySequence("-")])
        action_zoom_out.triggered.connect(lambda: self.model.zoom(2.0))
        view_menu.addAction(action_zoom_out)

        action_fit = QAction("Zoom to &fit", self)
        action_fit.setShortcut("Ctrl+0")
        action_fit.triggered.connect(self.model.zoom_to_fit)
        view_menu.addAction(action_fit)

        action_trigger = QAction("Go to &trigger", self)
        action_trigger.setShortcut("Ctrl+T")
        action_trigger.triggered.connect(self.go_to_trigger)
        view_menu.addAction(action_trigger)

        view_menu.addSeparator()
        for title, shortcut, handler in (
            ("T&aller channels", "Ctrl+Shift+Up", lambda: self.model.zoom_channels(CHANNEL_HEIGHT_STEP)),
            ("S&horter channels", "Ctrl+Shift+Down", lambda: self.model.zoom_channels(1 / CHANNEL_HEIGHT_STEP)),
            ("&Default channel height", "Ctrl+Shift+0", lambda: self.model.set_channel_height(DEFAULT_CHANNEL_HEIGHT)),
        ):
            action = QAction(title, self)
            action.setShortcut(shortcut)
            action.triggered.connect(handler)
            view_menu.addAction(action)
        view_menu.addSeparator()

        # Keyboard navigation, as in the original: Ctrl moves by a tenth of the
        # window, Ctrl+Up/Down by a full window, Shift by a single sample.
        navigate_menu = view_menu.addMenu("&Navigate")
        for title, shortcut, step in (
            ("Scroll &left", "Ctrl+Left", -0.1),
            ("Scroll &right", "Ctrl+Right", 0.1),
            ("Previous &page", "Ctrl+Down", -1.0),
            ("Next pa&ge", "Ctrl+Up", 1.0),
            ("Previous sample", "Shift+Left", None),
            ("Next sample", "Shift+Right", None),
        ):
            action = QAction(title, self)
            if shortcut in ("Ctrl+Left", "Ctrl+Right"):
                # The plain arrow keys scroll as well; text fields keep them for editing.
                action.setShortcuts([QKeySequence(shortcut), QKeySequence(shortcut.split("+")[1])])
            else:
                action.setShortcut(shortcut)
            if step is None:
                samples = -1 if "Previous" in title else 1
                action.triggered.connect(lambda _checked=False, s=samples: self.model.scroll_by(s))
            else:
                action.triggered.connect(
                    lambda _checked=False, f=step: self.model.scroll_by(
                        int(self.model.visible_samples * f) or (1 if f > 0 else -1)
                    )
                )
            navigate_menu.addAction(action)

        view_menu.addSeparator()
        action_show_channels = QAction("Show all &channels", self)
        action_show_channels.triggered.connect(lambda: self.channel_viewer.show_all_channels())
        view_menu.addAction(action_show_channels)

        action_unpin = QAction("&Unpin all channels", self)
        action_unpin.triggered.connect(self.model.unpin_all)
        view_menu.addAction(action_unpin)

        self.action_toggle_preview = QAction("Capture &overview", self)
        self.action_toggle_preview.setCheckable(True)
        self.action_toggle_preview.setChecked(True)
        self.action_toggle_preview.triggered.connect(self.previewer.setVisible)
        view_menu.addAction(self.action_toggle_preview)

        # ------------------------------------------------------------ Help
        help_menu = menu.addMenu("&Help")
        action_shortcuts = QAction("&Keyboard shortcuts", self)
        action_shortcuts.triggered.connect(self.show_shortcuts)
        help_menu.addAction(action_shortcuts)

        action_docs = QAction("Online &documentation", self)
        action_docs.setToolTip("The wiki of this project")
        action_docs.triggered.connect(lambda: webbrowser.open(DOCUMENTATION_URL))
        help_menu.addAction(action_docs)

        action_upstream_docs = QAction("Online documentation of the &original software (gusmanb)", self)
        action_upstream_docs.setToolTip("The wiki of the LogicAnalyzer by Agustín Giménez Bernad, which documents the hardware")
        action_upstream_docs.triggered.connect(lambda: webbrowser.open(UPSTREAM_DOCUMENTATION_URL))
        help_menu.addAction(action_upstream_docs)

        action_decoders = QAction("Decoder search &paths", self)
        action_decoders.triggered.connect(self.show_decoder_paths)
        help_menu.addAction(action_decoders)

        help_menu.addSeparator()
        action_about = QAction("&About PiPiLogicAnalyzer", self)
        action_about.triggered.connect(lambda: AboutDialog(self).exec())
        help_menu.addAction(action_about)

    def _update_title(self) -> None:
        name = f"PiPiLogicAnalyzer {__version__}"
        if self.current_file:
            self.setWindowTitle(f"{os.path.basename(self.current_file)} — {name}")
        else:
            self.setWindowTitle(name)

    # ------------------------------------------------------------ view state
    def _on_capture_changed(self) -> None:
        self._sync_view_controls()
        self._update_info_panel()
        self._update_actions()

    def _sync_view_controls(self) -> None:
        if self._updating_view_controls:
            return
        self._updating_view_controls = True
        try:
            total = self.model.sample_count
            visible = self.model.visible_samples

            self.position_scrollbar.setRange(0, max(total - visible, 0))
            self.position_scrollbar.setPageStep(max(visible, 1))
            self.position_scrollbar.setSingleStep(max(visible // 10, 1))
            self.position_scrollbar.setValue(self.model.first_sample)

            self.min_samples_label.setText("4" if total else "")
            self.max_samples_label.setText(to_thousands(max(total, 4)) if total else "")
            self.zoom_slider.setValue(self._zoom_to_slider(visible))

            if total:
                duration = visible / max(self.model.frequency, 1)
                self.visible_samples_label.setText(
                    f"{to_thousands(visible)} samples on screen ({to_small_time(duration)})"
                )
            else:
                self.visible_samples_label.setText("No capture")
        finally:
            self._updating_view_controls = False

    def _zoom_to_slider(self, visible: int) -> int:
        maximum = max(self.model.max_visible_samples(), 4)
        if maximum <= 4:
            return 0
        ratio = math.log(max(visible, 4) / 4) / math.log(maximum / 4)
        return int(round(ratio * ZOOM_SLIDER_STEPS))

    def _slider_to_zoom(self, value: int) -> int:
        maximum = max(self.model.max_visible_samples(), 4)
        return int(round(4 * (maximum / 4) ** (value / ZOOM_SLIDER_STEPS)))

    def _on_zoom_slider(self, value: int) -> None:
        if self._updating_view_controls:
            return
        self.model.set_view(self.model.first_sample, self._slider_to_zoom(value))

    def _on_scrollbar_moved(self, value: int) -> None:
        if self._updating_view_controls:
            return
        self.model.scroll_to(value)

    def go_to_trigger(self) -> None:
        session = self.model.session
        if session is None:
            return
        self.model.center_on(session.pre_trigger_samples)

    # --------------------------------------------------------------- devices
    def refresh_ports(self) -> None:
        """Detected analyzers followed by the network/multi device entries; other serial ports are left out."""
        self.port_combo.clear()
        detected = {device.port_name: device for device in detector.detect()}

        if detected:
            self.port_combo.addItem(
                "Autodetect" + (f" ({len(detected)} analyzers)" if len(detected) > 1 else ""),
                ("autodetect", None),
            )
            for port, device in detected.items():
                serial = f", S/N {device.serial_number}" if device.serial_number else ""
                self.port_combo.addItem(f"PiPiLogicAnalyzer on {port}{serial}", ("serial", port))
        else:
            self.port_combo.addItem("No analyzer detected", None)

        self.port_combo.insertSeparator(self.port_combo.count())
        self.port_combo.addItem("Network device...", ("network", None))
        self.port_combo.addItem("Multiple devices...", ("multi", None))
        self.port_combo.setCurrentIndex(0)
        self._check_for_new_boards()

    def toggle_connection(self) -> None:
        if self._has_real_device():
            self.disconnect_device()
        else:
            self.connect_device()

    def connect_device(self) -> None:
        data = self.port_combo.currentData()
        if not data:
            messages.warning(
                self,
                "Connect",
                "No analyzer is selected.",
                "Plug the analyzer in and press Refresh, or choose a network device or "
                "multiple devices in the device list.",
            )
            return

        kind, value = data
        try:
            if kind == "serial":
                driver: Optional[AnalyzerDriverBase] = PiPiLogicAnalyzerDriver(value)
            elif kind == "network":
                driver = self._connect_network()
            elif kind == "multi":
                driver = self._connect_multi()
            else:
                driver = self._connect_autodetect()
        except (DeviceConnectionError, OSError, ValueError) as error:
            messages.error(self, "Connect", "The device could not be opened.", str(error))
            return

        if driver is None:
            return

        if self.driver is not None:
            self.driver.dispose()  # the emulated driver of a loaded file
        self.driver = driver
        driver.add_capture_completed_handler(self.bridge.completed.emit)

        self._set_device_label(driver.device_version or "Unknown")
        self.port_combo.setEnabled(False)
        self.refresh_button.setEnabled(False)
        self.statusBar().showMessage(f"Connected to {driver.device_version}", 5000)

        if driver.is_network:
            self.power_timer.start()
            self._update_power_status()

        self.board_watch_timer.stop()
        self._check_connected_firmware(driver)

        self._update_actions()

    def _connect_network(self) -> Optional[AnalyzerDriverBase]:
        dialog = NetworkConnectDialog(parent=self)
        if not dialog.exec():
            return None
        return PiPiLogicAnalyzerDriver(f"{dialog.address}:{dialog.port}")

    def _connect_multi(self) -> Optional[AnalyzerDriverBase]:
        dialog = MultiConnectDialog([port for port in detector.list_port_infos() if port.is_analyzer], self)
        if not dialog.exec():
            return None
        return MultiAnalyzerDriver(dialog.connection_strings)

    def _connect_autodetect(self) -> Optional[AnalyzerDriverBase]:
        devices = detector.detect()
        if not devices:
            messages.warning(
                self,
                "Connect",
                "No analyzer was found.",
                "Check the USB cable. A board without the PiPiLogicAnalyzer firmware can be "
                "flashed with Device > Install or update firmware.",
            )
            return None
        if len(devices) == 1:
            return PiPiLogicAnalyzerDriver(devices[0].port_name)

        known = self._known_device_order(devices)
        if known is not None:
            return MultiAnalyzerDriver(known)

        choice = messages.choose(
            self,
            "Several analyzers found",
            f"{len(devices)} analyzers are connected. How do you want to use them?",
            ["Combine into a multi device set", f"Use only {devices[0].port_name}"],
            "A multi device set captures on all boards at once; the first board is the master "
            "and triggers the others.",
        )
        if choice is None:
            return None
        if choice == 1:
            return PiPiLogicAnalyzerDriver(devices[0].port_name)

        dialog = MultiComposeDialog(devices, self)
        if not dialog.exec():
            return None

        ports = [device.port_name for device in dialog.ordered_devices]
        self._store_known_device(dialog.ordered_devices)
        return MultiAnalyzerDriver(ports)

    def _known_device_order(self, devices) -> Optional[list[str]]:
        known_devices = settings.get_settings("known-devices.json") or []
        serials = {device.serial_number for device in devices if device.serial_number}
        for entry in known_devices:
            stored = entry.get("serial_numbers") or []
            if set(stored) == serials and len(stored) == len(devices):
                by_serial = {device.serial_number: device for device in devices}
                return [by_serial[serial].port_name for serial in stored]
        return None

    def forget_known_devices(self) -> None:
        """Drop the stored multi device sets so autodetect asks again."""
        known_devices = settings.get_settings("known-devices.json") or []
        if not known_devices:
            messages.info(self, "Multi device sets", "No multi device set has been registered yet.")
            return

        if not messages.confirm(
            self,
            "Forget multi device sets",
            f"Forget {len(known_devices)} registered multi device set(s)?",
            "Forget",
            "Autodetect asks again the next time several analyzers are connected.",
            destructive=True,
        ):
            return

        settings.persist_settings("known-devices.json", [])
        self.statusBar().showMessage("Registered multi device sets removed", 5000)

    def _store_known_device(self, devices) -> None:
        known_devices = settings.get_settings("known-devices.json") or []
        known_devices.append(
            {"serial_numbers": [device.serial_number for device in devices]}
        )
        settings.persist_settings("known-devices.json", known_devices)

    def disconnect_device(self) -> None:
        if self.driver is None:
            return
        self.power_timer.stop()
        self.power_label.setVisible(False)
        self.driver.dispose()
        self.driver = None
        self._set_device_label(None)
        self.port_combo.setEnabled(True)
        self.refresh_button.setEnabled(True)
        self.statusBar().showMessage("Disconnected", 5000)
        self._set_firmware_notice(None)
        self.board_watch_timer.start()
        self.refresh_ports()
        self._update_actions()

    def _set_device_label(self, version: Optional[str]) -> None:
        """Show the connected device as a link to its board information."""
        if version is None:
            self.device_label.setText("Not connected")
            self.device_label.setToolTip("No analyzer is connected")
            self.device_label.unsetCursor()
            set_role(self.device_label, "chip-neutral")
            return
        self.device_label.setText(
            f"<a href='device-info' style='color: #d6f5dc; text-decoration: none;'>"
            f"● {html.escape(version)}</a>"
        )
        self.device_label.setToolTip("Connected. Click for board, firmware and connection details")
        self.device_label.setCursor(Qt.PointingHandCursor)
        set_role(self.device_label, "chip-ok")

    def show_device_info(self) -> None:
        if self.driver is None:
            return
        DeviceInfoDialog(self.driver, self).exec()

    def update_network_settings(self) -> None:
        if self.driver is None:
            return
        dialog = NetworkSettingsDialog(self)
        if not dialog.exec():
            return
        if self.driver.send_network_config(
            dialog.access_point, dialog.password, dialog.address, dialog.port
        ):
            messages.info(
                self,
                "Network settings",
                "The network settings were saved on the device.",
                "Restart the device to connect it to the access point.",
            )
        else:
            messages.error(
                self,
                "Network settings",
                "The network settings could not be saved.",
                "Restart the device and try again.",
            )

    def enter_bootloader(self) -> None:
        if self.driver is None or self.driver.is_capturing:
            return
        if not messages.confirm(
            self,
            "Enter bootloader mode",
            "Restart the analyzer into bootloader mode?",
            "Restart into bootloader",
            "The device is disconnected and appears as a USB drive, ready for a new firmware.",
        ):
            return
        if self.driver.enter_bootloader():
            self.disconnect_device()
            messages.info(
                self,
                "Enter bootloader mode",
                "The device is in bootloader mode.",
                "Use Device > Install or update firmware to flash a new firmware.",
            )
        else:
            messages.error(
                self,
                "Enter bootloader mode",
                "The device did not enter bootloader mode.",
                "Unplug it and plug it in again while holding the BOOTSEL button.",
            )

    def _update_power_status(self) -> None:
        if self.driver is None or not self.driver.is_network or self.driver.is_capturing:
            return
        status = self.driver.get_voltage_status()
        if not status or status in ("UNSUPPORTED", "DISCONNECTED"):
            return
        parts = status.split("_")
        if len(parts) == 2:
            source = "external power" if parts[1] == "1" else "battery"
            self.power_label.setText(f"Power: {parts[0]} V ({source})")
            self.power_label.setVisible(True)

    # ------------------------------------------------------------- firmware
    def _set_firmware_notice(self, text: Optional[str], button: str = "Install firmware...") -> None:
        self.firmware_banner.set_message(text or "", button if text else None)
        self.firmware_notice.setVisible(bool(text))
        self._banner_holder.setVisible(bool(text))

    def _has_real_device(self) -> bool:
        return self.driver is not None and self.driver.driver_type in (
            AnalyzerDriverType.SERIAL,
            AnalyzerDriverType.NETWORK,
            AnalyzerDriverType.MULTI,
        )

    def _check_for_new_boards(self) -> None:
        """Show a notice for boards in bootloader mode or with foreign firmware."""
        if self._has_real_device():
            return
        try:
            drives = firmware_images.find_boot_drives()
            foreign = detector.detect_foreign_picos()
        except Exception:  # noqa: BLE001 - enumeration problems must not disturb the UI
            return

        if drives:
            drive = drives[0]
            self._set_firmware_notice(
                f"<b>{drive.chip} board in bootloader mode</b> ({drive.name}): "
                "install the PiPiLogicAnalyzer firmware to use it."
            )
        elif foreign:
            board = foreign[0]
            self._set_firmware_notice(
                f"<b>Raspberry Pi board with {html.escape(board.description)}</b> on "
                f"{html.escape(board.port_name)}: it does not run the PiPiLogicAnalyzer firmware."
            )
        else:
            self._set_firmware_notice(None)

    def _check_connected_firmware(self, driver: AnalyzerDriverBase) -> None:
        if driver.driver_type not in (AnalyzerDriverType.SERIAL, AnalyzerDriverType.NETWORK):
            self._set_firmware_notice(None)
            return
        try:
            capabilities = driver.capabilities()
        except Exception:  # noqa: BLE001 - the notice is optional
            capabilities = frozenset()
        if capabilities:
            self._set_firmware_notice(None)
        else:
            self._set_firmware_notice(
                "<b>Older firmware:</b> the board self-test, simulated captures on the board "
                "and the device details need the firmware of this project.",
                "Update firmware...",
            )

    def _restart_connected_into_bootloader(self) -> bool:
        if not self._has_real_device() or self.driver.is_capturing:
            return False
        if not self.driver.enter_bootloader():
            return False
        self.disconnect_device()
        return True

    def _connected_devices(self) -> list[ConnectedDevice]:
        """Installed firmware of the connected analyzer, one entry per board of a multi device set."""
        if not self._has_real_device():
            return []
        boards = self.driver.devices if self.driver.driver_type == AnalyzerDriverType.MULTI else [self.driver]
        devices = []
        for board in boards:
            try:
                details = board.device_details()
            except Exception:  # noqa: BLE001 - the details only complete the version line
                details = {}
            location = getattr(board, "connection_string", None) or "?"
            devices.append(ConnectedDevice(location, board.device_version, details))
        return devices

    def install_firmware(self) -> None:
        connected = self.driver if self._has_real_device() else None
        if connected is not None and connected.is_capturing:
            return
        # A board on the network cannot be flashed from here; it only shows its firmware.
        restartable = connected is not None and connected.driver_type != AnalyzerDriverType.NETWORK
        dialog = FirmwareDialog(
            self,
            connected_devices=self._connected_devices(),
            restart_connected=self._restart_connected_into_bootloader if restartable else None,
        )
        dialog.exec()
        if not self._has_real_device():
            self.refresh_ports()
            if dialog.new_port:
                index = self.port_combo.findData(("serial", dialog.new_port))
                if index >= 0:
                    self.port_combo.setCurrentIndex(index)
                self.statusBar().showMessage(f"Firmware installed, the analyzer is on {dialog.new_port}")

    # ----------------------------------------------------- board test / simulation
    def run_board_test(self) -> None:
        if self.driver is None or self.driver.is_capturing:
            return
        # The power poll would talk to the device in the middle of the test.
        polling = self.power_timer.isActive()
        self.power_timer.stop()
        try:
            BoardTestDialog(self.driver, self).exec()
        finally:
            if polling:
                self.power_timer.start()

    def simulated_capture(self) -> None:
        """Capture test signals: generated by the board if possible, else on the PC."""
        if self.driver is not None and self.driver.is_capturing:
            return

        board = self.driver if self.driver is not None and self.driver.driver_type in (
            AnalyzerDriverType.SERIAL,
            AnalyzerDriverType.NETWORK,
        ) else None
        on_board = False
        if board is not None:
            try:
                on_board = CAPABILITY_SIMULATION in board.capabilities()
            except Exception:  # noqa: BLE001 - fall back to the computed signals
                on_board = False

        driver = board if on_board else EmulatedAnalyzerDriver(1)
        dialog = SimulationDialog(driver, on_board=on_board, board_connected=board is not None, parent=self)
        if not dialog.exec() or dialog.session is None:
            return

        self._pending_simulation = {
            "pattern": dialog.pattern,
            "channels": dialog.channel_count,
            "frequency": dialog.frequency,
            "add_decoders": dialog.add_decoders,
        }

        if on_board:
            self._begin_capture(dialog.session)
            return

        error = driver.start_capture(dialog.session, self.bridge.completed.emit)
        if error != CaptureError.NONE:
            self._pending_simulation = None
            messages.error(self, "Simulated capture", "The simulated capture could not be started.", error.message)
            return
        self.statusBar().showMessage("Computing the simulated capture...")

    def _apply_simulation_decoders(self, session: CaptureSession) -> None:
        pending, self._pending_simulation = self._pending_simulation, None
        if pending is None or session.trigger_type != TriggerType.SIMULATION or not pending["add_decoders"]:
            return
        configuration = simulation_signals.decoder_configuration(
            pending["pattern"], pending["channels"], pending["frequency"], self.provider.registry
        )
        if configuration:
            self.provider.load_configuration(configuration)
            self.decoder_manager.refresh()

    # --------------------------------------------------------------- capture
    def start_capture(self) -> None:
        if not self._has_real_device() or self.driver.is_capturing:
            return

        dialog = self._capture_dialog(self.driver)
        if not dialog.exec() or dialog.selected_settings is None:
            return

        self._begin_capture(dialog.selected_settings)

    def _capture_dialog(self, driver: AnalyzerDriverBase, accept_text: Optional[str] = None) -> CaptureDialog:
        return CaptureDialog(
            driver,
            self,
            profiles=self.profiles,
            decoder_configuration=self.provider.to_list(),
            accept_text=accept_text,
        )

    def repeat_capture(self) -> None:
        session = self.model.session
        if session is None or not self._has_real_device():
            messages.warning(
                self,
                "Repeat capture",
                "There is no capture to repeat.",
                "Connect an analyzer and start a capture first.",
            )
            return
        if self.driver.is_capturing:
            return
        self._begin_capture(session.clone_settings())

    def _begin_capture(self, session: CaptureSession) -> None:
        if self.driver is None:
            return

        error = self.driver.start_capture(session)
        if error != CaptureError.NONE:
            messages.error(self, "Capture", "The capture could not be started.", error.message)
            return

        self.pending_session = session
        self._update_actions()
        self.statusBar().showMessage("Capturing, waiting for the trigger...")

    def abort_capture(self) -> None:
        if self.driver is None or not self.driver.is_capturing:
            return
        self.driver.stop_capture()
        self.statusBar().showMessage("Capture aborted", 5000)
        self._update_actions()

    def _on_capture_completed(self, args: CaptureCompletedArgs) -> None:
        self._update_actions()

        if not args.success:
            self._pending_simulation = None
            messages.error(
                self,
                "Capture failed",
                "No samples were received from the device.",
                "Try again. If the error persists, reconnect the device and restart the application."
                + (f"\n\n{args.error}" if args.error else ""),
            )
            self.statusBar().showMessage("Capture failed")
            return

        self.model.clear_regions()
        self.current_file = None
        self._update_title()
        aligned = ""
        if self.driver is not None and self.driver.driver_type == AnalyzerDriverType.MULTI:
            # Before loading, so the display and the decoders see the corrected samples.
            per_device = self.driver.channels_per_device
            self._remember_unaligned(args.session)
            options = alignment.alignment_options(args.session, per_device)
            if options:
                report = self._apply_alignment_choices(
                    args.session, per_device, {device: candidates[0] for device, candidates in options.items()}
                )
                if any(len(candidates) > 1 for candidates in options.values()):
                    report += " (Capture > Align boards to choose another method)"
                aligned = "; " + report
        # Before loading: the capture change decodes with the new decoders once.
        self._apply_simulation_decoders(args.session)
        self.load_session(args.session, reset_view=True)
        self.statusBar().showMessage(
            f"Captured {to_thousands(args.session.total_samples)} samples "
            f"at {to_large_frequency(args.session.frequency)}{aligned}"
        )

    def _remember_unaligned(self, session: CaptureSession) -> None:
        """Keep the samples as captured, so another method can be chosen later."""
        if self._unaligned is not None and self._unaligned[0] is session:
            originals = self._unaligned[1]
            if all(
                id(channel) in originals and channel.samples is not None
                and originals[id(channel)].size == channel.samples.size
                for channel in session.capture_channels
            ):
                return
        self._unaligned = (
            session,
            {id(channel): channel.samples for channel in session.capture_channels if channel.samples is not None},
        )

    def _apply_alignment_choices(self, session: CaptureSession, per_device: int, choices: dict) -> str:
        lines = []
        for device, choice in sorted(choices.items()):
            if choice is None:
                lines.append(f"board {device + 1} left unchanged")
                continue
            if choice.changed:
                alignment.apply_alignment(session, per_device, choice)
            lines.append(choice.describe())
        report = "; ".join(lines)
        self._alignment_report = (session, report)
        return report

    def align_boards(self) -> None:
        """Align the boards of the loaded multi device capture, choosing the method if there are several."""
        session = self.model.session
        if session is None:
            return
        if self.driver is not None and self.driver.driver_type == AnalyzerDriverType.MULTI:
            per_device: Optional[int] = self.driver.channels_per_device
        else:
            per_device = alignment.channels_per_device_of(session)

        options = {}
        current = {id(channel): channel.samples for channel in session.capture_channels}
        if per_device:
            # Measure on the samples as captured, not on an earlier correction.
            self._remember_unaligned(session)
            originals = self._unaligned[1]
            for channel in session.capture_channels:
                if id(channel) in originals:
                    channel.samples = originals[id(channel)]
            options = alignment.alignment_options(session, per_device)

        def keep_current() -> None:
            for channel in session.capture_channels:
                channel.samples = current.get(id(channel), channel.samples)

        if not options:
            keep_current()
            messages.info(
                self,
                "Align boards",
                "The boards could not be aligned.",
                "Aligning needs a capture from a multi device set and either a reference line (a "
                "channel on the first board named like a channel of another board plus ' ref', e.g. "
                "'A0 (Y) ref') or a clock channel on the first board named Φ2, PHI2, CLK or clock.",
            )
            return

        if any(len(candidates) > 1 for candidates in options.values()):
            dialog = AlignDialog(options, self)
            if not dialog.exec():
                keep_current()
                return
            choices = dialog.choices
        else:
            choices = {device: candidates[0] for device, candidates in options.items()}

        report = self._apply_alignment_choices(session, per_device, choices)
        self._after_samples_modified(self.model.first_sample)
        self.statusBar().showMessage(report, 10000)
        messages.info(self, "Align boards", "The boards were aligned.", report.replace("; ", "\n"))

    def load_session(self, session: CaptureSession, reset_view: bool = True) -> None:
        self.model.set_session(session)
        if reset_view:
            total = self.model.sample_count
            visible = max(min(total, 200), 4)
            first = max(session.pre_trigger_samples - visible // 4, 0)
            self.model.set_view(first, visible)
        self._sync_view_controls()
        self._update_info_panel()
        self._update_actions()

    # ------------------------------------------------------------ file menu
    def new_capture(self) -> None:
        driver = self.driver or EmulatedAnalyzerDriver(5)
        dialog = self._capture_dialog(driver, accept_text="Next")
        dialog.setWindowTitle("New capture - channels and timing")
        if not dialog.exec() or dialog.selected_settings is None:
            return

        session = dialog.selected_settings
        total = session.pre_trigger_samples + session.post_trigger_samples
        creator = CreateSamplesDialog(
            [channel.channel_number for channel in session.capture_channels],
            [channel.channel_name for channel in session.capture_channels],
            max_samples=total,
            initial_samples=total,
            parent=self,
        )
        if not creator.exec() or creator.samples is None:
            return

        for channel, samples in zip(session.capture_channels, creator.samples):
            channel.samples = samples

        if self.driver is None:
            self.driver = EmulatedAnalyzerDriver(5)

        self.model.clear_regions()
        self.current_file = None
        self._update_title()
        self.load_session(session)
        self.statusBar().showMessage("Created a new capture", 5000)

    def open_capture(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open capture", "", CAPTURE_FILE_FILTER)
        if path:
            self.open_capture_file(path)

    def open_capture_file(self, path: str) -> None:
        """Load a capture file, as the *Open* menu entry and the CLI do."""
        try:
            exported = capture_io.load_capture(path)
        except (OSError, ValueError) as error:
            messages.error(self, "Open capture", f"{os.path.basename(path)} could not be opened.", str(error))
            return

        if self.driver is None:
            self.driver = EmulatedAnalyzerDriver(5)

        self.current_file = path
        self.model.set_regions(exported.regions)
        self.load_session(exported.session)
        self._update_title()
        self.statusBar().showMessage(f"Opened {path}", 5000)

    def save_capture(self) -> None:
        """Save to the file the capture came from, or ask for a name."""
        if self.model.session is None:
            return
        if self.current_file and self.current_file.endswith((".lac", ".lac.gz")):
            self._write_capture(self.current_file)
        else:
            self.save_capture_as()

    def save_capture_as(self) -> None:
        if self.model.session is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save capture",
            self.current_file or "capture.lac",
            "Logic analyzer captures (*.lac);;Compressed captures (*.lac.gz)",
        )
        if not path:
            return
        if not path.endswith((".lac", ".lac.gz")):
            path += ".lac"
        self._write_capture(path)

    def _write_capture(self, path: str) -> None:
        try:
            capture_io.save_capture(path, self.model.session, self.model.regions)
        except (OSError, ValueError) as error:
            messages.error(self, "Save capture", f"{os.path.basename(path)} could not be saved.", str(error))
            return

        self.current_file = path
        self._update_title()
        self.statusBar().showMessage(f"Saved {path}", 5000)

    def export_capture(self, kind: str) -> None:
        session = self.model.session
        if session is None:
            return

        base = os.path.splitext(os.path.basename(self.current_file or "capture"))[0]
        if kind == "csv":
            path, _ = QFileDialog.getSaveFileName(
                self, "Export as CSV", f"{base}.csv", "Comma separated values (*.csv)"
            )
            exporter = capture_io.export_csv
            extension = ".csv"
        else:
            path, _ = QFileDialog.getSaveFileName(
                self, "Export as VCD", f"{base}.vcd", "Value change dump (*.vcd)"
            )
            exporter = capture_io.export_vcd
            extension = ".vcd"

        if not path:
            return
        if not path.lower().endswith(extension):
            path += extension

        progress = QProgressDialog("Exporting the capture...", "", 0, 0, self)
        progress.setWindowTitle("Export")
        progress.setCancelButton(None)
        progress.setWindowModality(Qt.WindowModal)
        progress.show()
        try:
            exporter(path, session)
        except (OSError, ValueError) as error:
            messages.error(self, "Export", f"{os.path.basename(path)} could not be written.", str(error))
            return
        finally:
            progress.close()

        self.statusBar().showMessage(f"Exported {path}", 5000)

    def show_decoder_paths(self) -> None:
        registry = self.provider.registry
        registry.load()
        paths = "\n".join(registry.search_paths) or "(none)"
        messages.info(
            self,
            "Decoder search paths",
            f"{len(registry.decoders)} protocol decoders are loaded.",
            f"Search paths:\n{paths}\n\n"
            "By default the 'decoders' folder next to the application is used. Pass "
            "--decoders, set the PIPILOGICANALYZER_DECODERS environment variable or copy "
            "decoders into the 'decoders' folder of the settings directory to add more.",
        )

    def show_shortcuts(self) -> None:
        rows = []
        for action in self.findChildren(QAction):
            if action.shortcut().isEmpty() or not action.text():
                continue
            name = action.text().replace("&", "").rstrip(".")
            keys = ", ".join(sequence.toString(QKeySequence.NativeText) for sequence in action.shortcuts())
            rows.append((name, keys))
        rows.sort()
        messages.info(
            self,
            "Keyboard shortcuts",
            "Keyboard shortcuts",
            "\n".join(f"{shortcut}\t{name}" for name, shortcut in rows),
        )

    # -------------------------------------------------------------- profiles
    def _rebuild_profiles_menu(self) -> None:
        """Profiles menu as in V6_5: add, then load/delete per stored profile."""
        menu = self.profiles_menu
        menu.clear()
        capturing = self.driver is not None and self.driver.is_capturing

        add_action = menu.addAction(icon("bookmark"), "&Save current settings as profile...")
        add_action.triggered.connect(self.add_profile)
        menu.addSeparator()

        if not self.profiles.profiles:
            menu.addAction("No saved profiles").setEnabled(False)
        for profile in self.profiles.profiles:
            submenu = menu.addMenu(profile.name.replace("&", "&&"))
            load_action = submenu.addAction(icon("check"), "Load")
            load_action.setEnabled(not capturing)
            load_action.triggered.connect(lambda _checked=False, p=profile: self.load_profile(p))
            edit_action = submenu.addAction(icon("pencil"), "Edit...")
            edit_action.triggered.connect(lambda _checked=False, p=profile: self.edit_profile(p))
            submenu.addSeparator()
            delete_action = submenu.addAction(icon("trash"), "Delete...")
            delete_action.triggered.connect(
                lambda _checked=False, p=profile: self.delete_profile(p)
            )

        menu.addSeparator()
        import_action = menu.addAction(icon("import"), "&Import profiles...")
        import_action.triggered.connect(self.import_profiles)
        export_action = menu.addAction(icon("export"), "&Export profiles...")
        export_action.triggered.connect(self.export_profiles)
        export_action.setEnabled(bool(self.profiles.profiles))

    def _current_capture_settings(self) -> Optional[CaptureSession]:
        """Settings of the loaded capture, or the last ones used for the device."""
        session = self.model.session
        if session is not None and session.capture_channels:
            return session.clone_settings()

        driver_type = self.driver.driver_type if self.driver else AnalyzerDriverType.SERIAL
        data = settings.get_settings(capture_settings_file(driver_type))
        if not data:
            return None
        try:
            return capture_io.session_from_dict(data)
        except (KeyError, TypeError, ValueError):
            return None

    def _save_profiles(self) -> None:
        if not self.profiles.save():
            messages.error(self, "Profiles", "The profiles file could not be written.")

    def add_profile(self) -> None:
        capture_settings = self._current_capture_settings()
        decoders = self.provider.to_list()
        if capture_settings is None and not decoders:
            messages.info(
                self,
                "Save profile",
                "There is nothing to store yet.",
                "Configure a capture or add a protocol decoder first.",
            )
            return

        name, ok = QInputDialog.getText(
            self, "Save profile", "Name of the profile (capture settings and decoders):"
        )
        name = name.strip()
        if not ok or not name:
            return
        if self.profiles.get(name) is not None and not messages.confirm(
            self, "Save profile", f'A profile named "{name}" already exists.', "Replace"
        ):
            return

        self.profiles.add(
            Profile(name=name, capture_settings=capture_settings, decoder_configuration=decoders)
        )
        self._save_profiles()
        self.statusBar().showMessage(f'Profile "{name}" saved', 5000)

    def load_profile(self, profile: Profile) -> None:
        if self.driver is not None and self.driver.is_capturing:
            if not messages.confirm(
                self,
                "Load profile",
                "A capture is in progress.",
                "Stop capture and load",
                "The running capture is aborted before the profile is loaded.",
            ):
                return
            self.abort_capture()

        if profile.capture_settings is not None:
            # The next capture dialog starts from the profile, whatever device
            # gets connected.
            data = capture_io.session_to_dict(
                profile.capture_settings.clone_settings(), include_samples=False
            )
            for driver_type in AnalyzerDriverType:
                settings.persist_settings(capture_settings_file(driver_type), data)

        self.decoder_manager.load_configuration(profile.decoder_configuration)
        self.statusBar().showMessage(
            f'Profile "{profile.name}" loaded'
            + (", its capture settings apply to the next capture"
               if profile.capture_settings is not None else ""),
            8000,
        )

    def delete_profile(self, profile: Profile) -> None:
        if not messages.confirm(
            self,
            "Delete profile",
            f'Delete the profile "{profile.name}"?',
            "Delete",
            "This cannot be undone.",
            destructive=True,
        ):
            return
        self.profiles.remove(profile.name)
        self._save_profiles()
        self.statusBar().showMessage(f'Profile "{profile.name}" deleted', 5000)

    def edit_profile(self, profile: Profile) -> None:
        others = [candidate.name for candidate in self.profiles.profiles if candidate is not profile]
        dialog = ProfileEditDialog(profile, self.provider.to_list(), others, self)
        if not dialog.exec() or dialog.profile is None:
            return
        self.profiles.remove(profile.name)
        self.profiles.add(dialog.profile)
        self._save_profiles()
        self.statusBar().showMessage(f'Profile "{dialog.profile.name}" saved', 5000)

    def export_profiles(self) -> None:
        if not self.profiles.profiles:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export profiles", "profiles.json", PROFILE_FILE_FILTER
        )
        if not path:
            return
        if not path.lower().endswith(".json"):
            path += ".json"
        try:
            write_profiles_file(path, self.profiles.profiles)
        except OSError as error:
            messages.error(self, "Export profiles", "The profiles could not be exported.", str(error))
            return
        self.statusBar().showMessage(f"Exported {len(self.profiles.profiles)} profile(s) to {path}", 5000)

    def import_profiles(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import profiles", "", PROFILE_FILE_FILTER)
        if not path:
            return
        try:
            imported = read_profiles_file(path)
        except (OSError, ValueError) as error:
            messages.error(self, "Import profiles", "The profiles could not be imported.", str(error))
            return

        conflicts = [profile.name for profile in imported if self.profiles.get(profile.name)]
        replace = True
        if conflicts:
            choice = messages.choose(
                self,
                "Import profiles",
                f"{len(conflicts)} imported profile(s) already exist.",
                ["Replace existing", "Keep existing"],
                "\n".join(conflicts),
            )
            if choice is None:
                return
            replace = choice == 0

        added = 0
        for profile in imported:
            if profile.name in conflicts and not replace:
                continue
            self.profiles.add(profile)
            added += 1
        self._save_profiles()
        self.statusBar().showMessage(f"Imported {added} profile(s) from {path}", 5000)

    # ---------------------------------------------------------- sample edits
    def _selection_out_of_range(self) -> None:
        messages.warning(self, "Selection", "The selection is outside the capture.")

    def create_region(self, first_sample: int, last_sample: int) -> None:
        region = SampleRegion(first_sample=first_sample, last_sample=last_sample)
        dialog = RegionDialog(region, self)
        if dialog.exec():
            self.model.add_region(region)
        self.sample_marker.clear_selection()

    def measure_samples(self, first_sample: int, sample_count: int) -> None:
        session = self.model.session
        if session is None:
            return
        if first_sample + sample_count > self.model.sample_count:
            self._selection_out_of_range()
            return
        MeasureDialog(
            session.capture_channels, first_sample, sample_count, session.frequency, self
        ).exec()

    def show_annotation_list(self, group, annotation, segment=None) -> AnnotationListWindow:
        """Open (or bring up) the list window of an annotation row, selecting ``segment``."""
        key = (id(group.instance), annotation.name)
        window = self._annotation_lists.get(key)
        if window is None:
            window = AnnotationListWindow(self.model, group, annotation, self)
            window.setAttribute(Qt.WA_DeleteOnClose)
            window.destroyed.connect(lambda *_: self._annotation_lists.pop(key, None))
            self._annotation_lists[key] = window
        window.show()
        window.raise_()
        window.activateWindow()
        if segment is not None:
            window.select_segment(segment)
        return window

    def copy_samples(self, first_sample: int, sample_count: int) -> None:
        session = self.model.session
        if session is None:
            return
        if first_sample + sample_count > self.model.sample_count:
            self._selection_out_of_range()
            return
        self.clipboard_samples = [
            channel.samples[first_sample : first_sample + sample_count].copy()
            if channel.samples is not None
            else np.zeros(sample_count, dtype=np.uint8)
            for channel in session.capture_channels
        ]
        self.sample_marker.has_clipboard = True
        self.statusBar().showMessage(f"Copied {to_thousands(sample_count)} samples", 5000)

    def cut_samples(self, first_sample: int, sample_count: int) -> None:
        self.copy_samples(first_sample, sample_count)
        self.delete_samples(first_sample, sample_count)

    def delete_samples(self, first_sample: int, sample_count: int) -> None:
        session = self.model.session
        if session is None or sample_count <= 0:
            return
        total = self.model.sample_count
        if first_sample >= total:
            self._selection_out_of_range()
            return

        sample_count = min(sample_count, total - first_sample)
        last_sample = first_sample + sample_count - 1

        for channel in session.capture_channels:
            if channel.samples is None:
                continue
            channel.samples = np.concatenate(
                (channel.samples[:first_sample], channel.samples[first_sample + sample_count :])
            )

        if first_sample < session.pre_trigger_samples:
            removed_before_trigger = min(sample_count, session.pre_trigger_samples - first_sample)
            session.pre_trigger_samples -= removed_before_trigger

        # ``sample_count`` is read back from the channels, which were already
        # trimmed above.
        session.post_trigger_samples = max(
            self.model.sample_count - session.pre_trigger_samples, 0
        )
        session.loop_count = 0
        session.measure_bursts = False
        session.bursts = None

        self._remap_regions_after_delete(first_sample, last_sample, sample_count)
        self._after_samples_modified(first_sample)
        self.statusBar().showMessage(f"Deleted {to_thousands(sample_count)} samples", 5000)

    def _remap_regions_after_delete(
        self, first_sample: int, last_sample: int, sample_count: int
    ) -> None:
        remaining = []
        for region in self.model.regions:
            start, end = region.start, region.end
            if start >= first_sample and end <= last_sample:
                continue  # fully inside the deleted range
            if end < first_sample:
                remaining.append(region)
                continue
            if start > last_sample:
                remaining.append(region.shifted(-sample_count))
                continue
            # Partially overlapping: clip and shift what is left.
            new_start = start if start < first_sample else first_sample
            new_end = end - sample_count if end > last_sample else first_sample
            if new_end - new_start < 1:
                continue
            region.first_sample = new_start
            region.last_sample = new_end
            remaining.append(region)
        self.model.set_regions(remaining)

    def paste_samples(self, sample: int) -> None:
        if self.clipboard_samples is None:
            return
        self._insert_samples(sample, self.clipboard_samples)

    def insert_samples_dialog(self, sample: int) -> None:
        session = self.model.session
        if session is None or self.driver is None:
            return

        limits = self.driver.get_limits(session.channel_numbers)
        available = max(limits.max_total_samples - self.model.sample_count, 1)
        dialog = CreateSamplesDialog(
            session.channel_numbers,
            [channel.channel_name for channel in session.capture_channels],
            max_samples=available,
            initial_samples=min(100, available),
            insert_mode=True,
            parent=self,
        )
        if not dialog.exec() or dialog.samples is None:
            return
        self._insert_samples(sample, dialog.samples)

    def _insert_samples(self, sample: int, new_samples: list[np.ndarray]) -> None:
        session = self.model.session
        if session is None or not new_samples:
            return

        total = self.model.sample_count
        if sample > total:
            messages.warning(self, "Insert samples", "Samples cannot be inserted beyond the end of the capture.")
            return

        count = int(new_samples[0].size)
        if self.driver is not None:
            maximum = self.driver.get_limits(session.channel_numbers).max_total_samples
            if total + count > maximum:
                messages.error(
                    self,
                    "Insert samples",
                    "The capture would become too long.",
                    f"This channel mode holds at most {to_thousands(maximum)} samples.",
                )
                return

        for index, channel in enumerate(session.capture_channels):
            addition = new_samples[index] if index < len(new_samples) else np.zeros(count, np.uint8)
            if channel.samples is None:
                channel.samples = addition.copy()
                continue
            channel.samples = np.concatenate(
                (channel.samples[:sample], addition, channel.samples[sample:])
            )

        if sample <= session.pre_trigger_samples:
            session.pre_trigger_samples += count
        session.post_trigger_samples = max(
            self.model.sample_count - session.pre_trigger_samples, 0
        )
        session.loop_count = 0
        session.measure_bursts = False
        session.bursts = None

        self.model.set_regions(
            [
                region.shifted(count) if region.start >= sample else region
                for region in self.model.regions
            ]
        )
        self._after_samples_modified(sample)
        self.statusBar().showMessage(f"Inserted {to_thousands(count)} samples", 5000)

    def shift_channels(self) -> None:
        session = self.model.session
        if session is None:
            return

        dialog = ShiftChannelsDialog(
            session.capture_channels, max(self.model.sample_count - 1, 1), self
        )
        if not dialog.exec():
            return

        amount = dialog.shift_amount
        for channel in dialog.shifted_channels:
            samples = channel.samples
            if samples is None or samples.size == 0:
                continue
            amount = min(amount, samples.size)

            if dialog.direction == ShiftDirection.LEFT:
                head = samples[amount:]
                tail = self._fill_samples(dialog.mode, samples[:amount], amount)
                channel.samples = np.concatenate((head, tail))
            else:
                head = self._fill_samples(dialog.mode, samples[samples.size - amount :], amount)
                channel.samples = np.concatenate((head, samples[: samples.size - amount]))

        self._after_samples_modified(self.model.first_sample)
        self.statusBar().showMessage(f"Shifted {len(dialog.shifted_channels)} channel(s)", 5000)

    @staticmethod
    def _fill_samples(mode: ShiftMode, rotated: np.ndarray, amount: int) -> np.ndarray:
        if mode == ShiftMode.HIGH:
            return np.ones(amount, dtype=np.uint8)
        if mode == ShiftMode.LOW:
            return np.zeros(amount, dtype=np.uint8)
        return rotated.copy()

    def _after_samples_modified(self, first_sample: int) -> None:
        self.model.rebuild_transitions()
        self.model.notify_capture_changed()
        self.model.set_view(first_sample, self.model.visible_samples)
        self.sample_marker.clear_selection()
        self.decoder_manager.decode_if_automatic()

    # ------------------------------------------------------------------ misc
    def _update_actions(self) -> None:
        has_capture = self.model.session is not None and self.model.sample_count > 0
        capturing = self.driver is not None and self.driver.is_capturing
        is_real_device = self._has_real_device()

        self.capture_button.setEnabled(is_real_device and not capturing)
        self.repeat_button.setEnabled(is_real_device and has_capture and not capturing)
        self.abort_button.setEnabled(capturing)
        self.action_capture.setEnabled(self.capture_button.isEnabled())
        self.action_repeat.setEnabled(self.repeat_button.isEnabled())
        self.action_stop.setEnabled(capturing)

        self.connect_button.setEnabled(not capturing)
        self.connect_button.setText("Disconnect" if is_real_device else "Connect")
        set_variant(self.connect_button, None if is_real_device else "primary")
        set_icon(self.connect_button, "unplug" if is_real_device else "plug")
        self.action_connect.setText("&Disconnect" if is_real_device else "&Connect")
        self.action_connect.setIcon(icon("unplug" if is_real_device else "plug"))
        self.action_connect.setEnabled(not capturing)

        self.action_save.setEnabled(has_capture)
        self.action_save_as.setEnabled(has_capture)
        self.action_align.setEnabled(has_capture and not capturing)
        self.action_export_csv.setEnabled(has_capture)
        self.action_export_vcd.setEnabled(has_capture)
        self.action_device_info.setEnabled(is_real_device)
        self.action_bootloader.setEnabled(is_real_device and not capturing)
        self.action_board_test.setEnabled(is_real_device and not capturing)
        self.action_simulation.setEnabled(not capturing)
        self.action_firmware.setEnabled(not capturing)
        self.action_network_settings.setEnabled(
            is_real_device
            and self.driver.driver_type == AnalyzerDriverType.SERIAL
            and "WIFI" in (self.driver.device_version or "")
        )
        self.fit_button.setEnabled(has_capture)
        self.trigger_button.setEnabled(has_capture)

        self.empty_primary_button.setText("Start capture..." if is_real_device else "Connect device")
        set_icon(self.empty_primary_button, "record" if is_real_device else "plug")
        self.empty_primary_button.setEnabled(not capturing)
        self.capture_stack.setCurrentIndex(1 if self.model.session is not None else 0)

    def _update_info_panel(self) -> None:
        session = self.model.session
        if session is None:
            self.info_label.setText(f"<span style='color:{TEXT_MUTED}'>No capture loaded</span>")
            return

        trigger = session.trigger_type.label
        rows = [
            ("Frequency", to_large_frequency(session.frequency)),
            ("Total samples", to_thousands(self.model.sample_count)),
            ("Pre-trigger", to_thousands(session.pre_trigger_samples)),
            ("Post-trigger", to_thousands(session.post_trigger_samples)),
            ("Duration", to_small_time(self.model.sample_count / max(session.frequency, 1))),
            ("Channels", str(len(session.capture_channels))),
            (
                "Trigger",
                trigger
                if session.trigger_type == TriggerType.SIMULATION
                else f"{trigger}, channel {session.trigger_channel + 1}",
            ),
            ("Value", session.trigger_description()),
        ]
        if session.bursts:
            rows.append(("Bursts", str(len(session.bursts))))
        if self._alignment_report is not None and self._alignment_report[0] is session:
            rows.append(("Alignment", self._alignment_report[1]))

        self.info_label.setText(
            "<table width='100%' cellspacing='0' cellpadding='1'>"
            + "".join(
                f"<tr><td style='color:{TEXT_MUTED}'>{name}</td>"
                f"<td align='right'>{html.escape(str(value))}</td></tr>"
                for name, value in rows
            )
            + "</table>"
        )

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._save_window_state()
        if self.driver is not None:
            self.driver.dispose()
            self.driver = None
        super().closeEvent(event)
