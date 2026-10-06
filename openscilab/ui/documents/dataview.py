# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of openSciLab, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The data view: captures shown, analysed, decoded and edited (port of ``MainWindow.axaml(.cs)``).

It was the main window up to 7.x and is a ``QMainWindow`` still, so its toolbar, docks (analysis
panels, listing) and status bar stay, but it lives in a document tab of the shell: its menu bar is
hidden, the shell shows its *Data* and *Analyze* menus and its view menu as *Waveform*, and *Open,
Save, Exit* and the window geometry are the shell's.

A data view does not connect devices or start captures: the device card of an instrument does
(its :class:`~..devices.capture.CaptureController`), and the captures of that instrument arrive
here – live, progressively or when they complete. Files and test signals are shown the same way.
"""

from __future__ import annotations

import copy
import html
import math
import os
import time
from typing import Optional

import numpy as np
from PySide6.QtCore import QByteArray, QEvent, QEventLoop, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QKeySequence, QShortcut, QUndoStack
from PySide6.QtWidgets import (
    QComboBox,
    QDockWidget,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QScrollBar,
    QSizePolicy,
    QSlider,
    QStackedWidget,
    QStatusBar,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from ... import __version__
from ...core import alignment, capture_io, settings
from ...core import simulation as simulation_signals
from ...core.formatting import to_large_frequency, to_small_time, to_thousands
from ...core.hub import EVENT_REMOVED, Hub
from ...core.instrument import Instrument
from ...core.regions import SampleRegion
from ...driver.base import (
    MAX_SAMPLE_BYTES,
    AnalyzerDriverBase,
    CaptureCompletedArgs,
    CaptureError,
    CaptureProgressArgs,
)
from ...driver.emulated import EmulatedAnalyzerDriver
from ...driver.models import CaptureSession, TriggerType
from ...sigrok.provider import SigrokProvider
from .. import background, messages
from ..devices.capture import CaptureBridge
from ..devices.hub_bridge import HubBridge
from ..dialogs.align_dialog import AlignDialog
from ..dialogs.annotation_list import AnnotationListWindow
from ..dialogs.bus_dialog import BusDialog
from ..dialogs.capture_dialog import CaptureDialog
from ..dialogs.chart_dialog import ChartDialog
from ..dialogs.common import Banner, hint
from ..dialogs.compare_dialog import CompareDialog
from ..dialogs.create_samples_dialog import CreateSamplesDialog
from ..dialogs.measure_dialog import MeasureDialog
from ..dialogs.region_dialog import RegionDialog
from ..dialogs.shift_dialog import ShiftChannelsDialog, ShiftDirection, ShiftMode
from ..dialogs.simulation_dialog import SimulationDialog
from ..icons import icon, set_icon
from ..panels.listing_panel import ListingPanel
from ..panels.markers_panel import MarkersPanel
from ..panels.measure_panel import MeasurePanel
from ..panels.search_panel import SearchPanel
from ..shell.start_page import native
from ..theme import ACCENT, TEXT_MUTED, icon_px, set_role, set_variant
from ..view_model import (
    CHANNEL_HEIGHT_STEP,
    DEFAULT_CHANNEL_HEIGHT,
    LARGEST_CHANNEL_HEIGHT,
    SMALLEST_CHANNEL_HEIGHT,
    CaptureViewModel,
)
from ..widgets.analog_viewer import AnalogViewer, derive_digital
from ..widgets.annotation_viewer import AnnotationViewer
from ..widgets.bus_viewer import BusViewer
from ..widgets.channel_viewer import CHANNEL_COLUMN_WIDTH, ChannelViewer
from ..widgets.decoder_manager import DecoderManager
from ..widgets.sample_marker import SampleMarker
from ..widgets.sample_previewer import SamplePreviewer
from ..widgets.sample_viewer import SampleViewer
from .base import Document

WINDOW_STATE_FILE = "analyzer-state.json"
#: Pixels of the name of the source device in the toolbar (longer names are shortened)
DEVICE_LABEL_WIDTH = 230
#: Version of the dock layout stored in the window state (a newer layout ignores older ones)
WINDOW_LAYOUT_VERSION = 4  # 4: one tool bar (capture, device, settings, panels), it adapts to the width
ZOOM_SLIDER_STEPS = 1000
#: A streaming capture shows its last 1/10 s until the user zooms
LIVE_WINDOW_DIVISOR = 10
CAPTURE_FILE_FILTER = "Captures (*.lac *.lac.gz *.sr);;All files (*)"
#: *Save as*: a number per sample, as the original software writes (and reads) its captures
COMPATIBLE_FILTER = "Captures for the original LogicAnalyzer (*.lac)"
#: milliseconds between two looks at a transfer that an operation waits for
TRANSFER_CHECK_MS = 50


def long_duration(seconds: float) -> str:
    """``to_small_time`` below a minute, else minutes and hours (a long stream)."""
    if seconds < 60:
        return to_small_time(seconds)
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} min" if hours else f"{minutes} min {secs:02d} s"


#: samples a capture without a device may grow to by inserting
INSERT_LIMIT = 100_000_000

class DataView(QMainWindow, Document):
    document_kind = "data"
    #: Title, file or unsaved state changed (see :class:`~.base.Document`).
    document_changed = Signal()
    #: a waveform made of the capture, to play on a generator output (``Play as signal``)
    play_requested = Signal(object)
    #: the device card of the instrument the data come from
    device_card_requested = Signal(object)
    #: the device list (to open a device and capture with it)
    devices_requested = Signal()

    def __init__(
        self,
        decoder_paths: tuple[str, ...] = (),
        provider: Optional[SigrokProvider] = None,
        hub: Optional[Hub] = None,
    ) -> None:
        super().__init__()
        #: The device hub of the lab
        self.hub = hub if hub is not None else Hub()
        #: The capture controller of the instrument whose captures this view shows (``None`` for
        #: files and test signals); the device card starts the captures.
        self.source = None
        #: Title of a capture that is not a file (e.g. shown by a scope node of a flow)
        self.display_name: Optional[str] = None
        #: the data come from elsewhere (the run data of a flow, a scope node): the view shows them
        #: and does not capture (see set_viewer)
        self.viewer = False
        # A widget in a document tab, not a window of its own.
        self.setWindowFlags(Qt.Widget)
        self._dirty = False
        #: changed by the user since it was loaded, captured or saved (a new capture then opens
        #: in another data view instead of replacing the changes)
        self._edited = False
        #: loading data: the model's change signals are not edits then
        self._loading = 0
        #: a file is being read or written in the background (no second one meanwhile)
        self._file_busy = False
        #: the open file is written as the original software does (chosen in *Save as*)
        self._save_compatible = False
        #: Number of successful saves (tells save()/save_as() whether the user saved).
        self._save_count = 0

        self.model = CaptureViewModel(self)
        for signal in (self.model.channels_changed, self.model.regions_changed, self.model.bookmarks_changed,
                       self.model.buses_changed):
            signal.connect(self._model_edited)
        # other data in the model (from anywhere): what it announces right then is no edit
        self._data_session = None
        self.model.capture_changed.connect(self._data_replaced)
        self.model.channels_changed.connect(lambda: hasattr(self, "show_all_button") and self._update_hidden_button())
        #: edits of the data, undone with Ctrl+Z (see data_edits.py)
        self.undo = QUndoStack(self)
        self._state = None
        self.undo.indexChanged.connect(self._undo_moved)
        #: Open annotation list windows by (decoder instance, row name).
        self._annotation_lists: dict[tuple[int, str], AnnotationListWindow] = {}
        self.provider = provider if provider is not None else SigrokProvider()
        if decoder_paths and provider is None:
            self.provider.registry.search_paths = list(decoder_paths) + [
                path for path in self.provider.registry.search_paths if path not in decoder_paths
            ]
        #: Settings of the simulated capture in progress (decoders to add).
        self._pending_simulation: Optional[dict] = None
        self.clipboard_samples: Optional[list[np.ndarray]] = None
        self.current_file: Optional[str] = None
        self._updating_view_controls = False
        #: (session, samples before any alignment by channel id) to align again with another method.
        self._unaligned: Optional[tuple[CaptureSession, dict[int, np.ndarray]]] = None
        #: (session, how its boards were aligned) for the capture information.
        self._alignment_report: Optional[tuple[CaptureSession, str]] = None
        #: (session, regions, file) the shown state analysis was made of
        self._timing_capture: Optional[tuple] = None

        self.hub_bridge = HubBridge(self.hub, self)
        self.hub_bridge.changed.connect(self._on_hub_event)
        # test signals computed on this computer complete through it (captures of instruments
        # arrive through their capture controller)
        self.bridge = CaptureBridge(self)
        self.bridge.completed.connect(self._on_capture_completed)
        #: the display asks a progressive transfer for the visible range (after scrolling settles)
        self._priority_timer = QTimer(self)
        self._priority_timer.setSingleShot(True)
        self._priority_timer.setInterval(80)
        self._priority_timer.timeout.connect(self._prioritize_visible)
        #: The newest progress event not shown yet: a slow display skips the older ones
        self._pending_progress: Optional[CaptureProgressArgs] = None
        #: The capture started from this window until its completion was handled.
        self._running_capture: Optional[CaptureSession] = None
        #: The capture streaming in (live display): shown session and the one the driver fills.
        self._live_session: Optional[CaptureSession] = None
        self._live_source: Optional[CaptureSession] = None
        #: Samples the live display shows, and what it last set (to notice the user zooming).
        self._live_window = 0
        self._live_visible = 0
        #: stream position of the first sample shown (an endless stream drops the oldest ones)
        self._live_first = 0
        #: actions on devices while a capture runs: (stream sample or None, monotonic time, text,
        #: the marker shown live or None)
        self._action_marks: list[tuple] = []
        self._capture_started = 0.0
        self._live_arrival = 0.0
        #: a monitor recorded into this document (see :meth:`record_monitor`)
        self.recording = None
        self._recording_session = None
        self._recording_marks: list[tuple[int, str]] = []
        self._recording_stop = None
        self._recording_timer = QTimer(self)
        self._recording_timer.setInterval(200)
        self._recording_timer.timeout.connect(self._show_recording)

        self._build_ui()
        self._build_menu()
        self._apply_menu_icons()

        self.model.view_changed.connect(self._sync_view_controls)
        self.model.view_changed.connect(lambda: self.model.progressive is not None and self._priority_timer.start())
        self.model.capture_changed.connect(self._on_capture_changed)
        self.model.samples_appended.connect(self._sync_view_controls)

        self._update_title()
        self._update_actions()
        self._update_info_panel()
        self._sync_view_controls()
        self._restore_window_state()
        self._embed()

    # ---------------------------------------------------------- dock layout
    def _restore_window_state(self) -> None:
        """Restore the dock layout, overview and channel height of the last document."""
        state = settings.get_settings(WINDOW_STATE_FILE)
        if not isinstance(state, dict):
            return
        try:
            layout = state.get("layout")
            if isinstance(layout, str):
                self.restoreState(QByteArray.fromBase64(layout.encode("ascii")), WINDOW_LAYOUT_VERSION)
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
        settings.persist_settings(
            WINDOW_STATE_FILE,
            {
                "layout": bytes(self.saveState(WINDOW_LAYOUT_VERSION).toBase64()).decode("ascii"),
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

        banner_holder = QWidget(central)
        banner_layout = QVBoxLayout(banner_holder)
        banner_layout.setContentsMargins(8, 8, 8, 0)
        # A progressive transfer that stopped before the capture was complete.
        self.transfer_banner = Banner("warning", banner_holder)
        self.transfer_banner.button.clicked.connect(self.resume_transfer)
        banner_layout.addWidget(self.transfer_banner)
        self.transfer_banner.setVisible(False)
        # a capture that failed or ended early: shown here, not in a dialog
        self.capture_banner = Banner("error", banner_holder)
        self.capture_banner.button.clicked.connect(self._capture_banner_action)
        banner_layout.addWidget(self.capture_banner)
        self.capture_banner.setVisible(False)
        self._banner_holder = banner_holder
        banner_holder.setVisible(False)
        layout.addWidget(banner_holder)

        layout.addWidget(self._build_capture_area(), 1)
        self.setCentralWidget(central)
        self.setDockNestingEnabled(True)

        self.panels_dock = QDockWidget("Analysis", self)
        self.panels_dock.setObjectName("analysis-dock")
        self.panels_dock.setWidget(self._build_side_panel())
        self.panels_dock.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetClosable)
        # The tabs are the title; the dock is moved by its tab bar's empty area
        self.panels_dock.setTitleBarWidget(QWidget(self.panels_dock))
        self.addDockWidget(Qt.RightDockWidgetArea, self.panels_dock)
        self.resizeDocks([self.panels_dock], [400], Qt.Horizontal)

        self.listing_dock = QDockWidget("Listing", self)
        self.listing_dock.setObjectName("listing-dock")
        self.listing_panel = ListingPanel(self.model, self.listing_dock)
        self.listing_dock.setWidget(self.listing_panel)
        self.listing_dock.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetClosable)
        self.addDockWidget(Qt.BottomDockWidgetArea, self.listing_dock)
        self.listing_dock.setVisible(False)

        for button, dock in ((self.listing_tool_button, self.listing_dock), (self.panels_tool_button, self.panels_dock)):
            button.setChecked(not dock.isHidden())
            button.toggled.connect(dock.setVisible)
            dock.visibilityChanged.connect(
                lambda _visible, button=button, dock=dock: button.setChecked(not dock.isHidden())
            )
        status = QStatusBar(self)
        self.setStatusBar(status)
        self.statusBar().showMessage("Ready")

    def _tool_button(
        self, parent: QWidget, icon_name: str, tip: str, slot=None, checkable: bool = False, text: str = ""
    ) -> QPushButton:
        """A flat icon button of the toolbar or the view bar (``text``: shown when the toolbar shows text)."""
        button = QPushButton(text, parent)
        set_variant(button, "tool")
        set_icon(button, icon_name)
        button.setToolTip(tip)
        button.setCheckable(checkable)
        button.setFocusPolicy(Qt.NoFocus)
        if slot is not None:
            button.clicked.connect(slot)
        return button

    @staticmethod
    def _toolbar_spacer(bar: QToolBar) -> QWidget:
        spacer = QWidget(bar)
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        return spacer

    def _build_toolbar(self) -> QToolBar:
        """One tool bar: capture, the device and its settings, then the panels and the file. It adapts
        to the width of the view (``ui/widgets/toolbar.py``); a right click customizes it."""
        from ..devices.capture_controls import CaptureControls
        from ..widgets.toolbar import HIGH, LOW, NORMAL, AdaptiveToolBar

        bar = AdaptiveToolBar("dataview", "Data view", self)
        self.main_toolbar = bar
        self.capture_controls = controls = CaptureControls(bar, repeat=self.repeat_capture, stop=self.abort_capture)
        self.device_combo = QComboBox(bar)
        self.device_combo.setMinimumWidth(150)
        self.device_combo.setToolTip("The device this view captures with")
        self.device_combo.activated.connect(self._device_chosen)
        # ---------------------------------------------------------- capture
        bar.add_widget("capture", controls.capture_button, "Capture", "capture", HIGH, pinned=True)
        bar.add_widget("again", controls.repeat_button, "Capture again", "capture", NORMAL)
        bar.add_widget("stop", controls.stop_button, "Stop", "capture", HIGH, text=False)
        # ---------------------------------------------------------- device
        bar.add_widget("device", self.device_combo, "Device", "device", HIGH)
        bar.add_widget("quick", controls.quick_capture, "Rate, length and trigger", "device", NORMAL,
                       overflow=lambda menu: menu.addAction(icon("gear"), "Rate, length and trigger...",
                                                            controls.capture_settings))
        bar.add_widget("settings", controls.settings_button, "Capture settings", "device", NORMAL)
        bar.add_widget("profiles", controls.profiles_button, "Profiles", "device", NORMAL)
        bar.add_widget("state", controls.state_label, "Device state", "device", LOW)
        bar.add_stretch()
        # ---------------------------------------------------------- panels
        self.search_tool_button = self._tool_button(
            bar, "search", "Search the capture (Ctrl+F)", lambda: self.show_panel(self.search_panel), text="Search")
        self.measure_tool_button = self._tool_button(
            bar, "ruler", "Cursors and measurements (Ctrl+Shift+M)", lambda: self.show_panel(self.measure_panel),
            text="Measure")
        self.listing_tool_button = self._tool_button(bar, "list", "Listing below the waveform (Ctrl+2)",
                                                     checkable=True, text="Listing")
        self.panels_tool_button = self._tool_button(bar, "panel", "Analysis panels (Ctrl+1)", checkable=True,
                                                    text="Panels")
        bar.add_widget("search", self.search_tool_button, "Search", "panels", NORMAL, text=False)
        bar.add_widget("measure", self.measure_tool_button, "Measure", "panels", NORMAL, text=False)
        bar.add_widget("listing", self.listing_tool_button, "Listing", "panels", LOW, text=False)
        bar.add_widget("panels", self.panels_tool_button, "Analysis panels", "panels", NORMAL, text=False)
        # ------------------------------------------------------------ file
        self.open_button = self._tool_button(bar, "folder", "Open a capture (Ctrl+O)", self.open_capture, text="Open")
        self.save_button = self._tool_button(bar, "save", "Save the capture (Ctrl+S)", self.save_capture, text="Save")
        bar.add_widget("open", self.open_button, "Open a capture", "file", LOW, text=False)
        bar.add_widget("save", self.save_button, "Save the capture", "file", LOW, text=False)
        # (the names of the buttons the view always had)
        self.capture_button = controls.capture_button
        self.repeat_button = controls.repeat_button
        self.abort_button = controls.stop_button
        QShortcut(QKeySequence(Qt.Key_F5), self, activated=self.capture, context=Qt.WidgetWithChildrenShortcut)
        return bar.finish()

    def _fill_device_combo(self) -> None:
        """No device, the connected instruments that capture, and connecting another one."""
        combo = self.device_combo
        combo.blockSignals(True)
        combo.clear()
        combo.addItem(icon("folder"), "No device" if self.model.session is None or self.source is not None
                      else ("File" if self.current_file else "No device (test signals)"), None)
        for instrument in self.hub.instruments():
            if instrument.capture is not None:
                combo.addItem(icon("chip"), instrument.name, instrument)
        combo.addItem(icon("plus"), "Connect a device...", "connect")
        index = combo.findData(self.source.instrument) if self.source is not None else 0
        combo.setCurrentIndex(max(index, 0))
        combo.blockSignals(False)

    def _device_chosen(self, index: int) -> None:
        data = self.device_combo.itemData(index)
        if data == "connect":
            self._fill_device_combo()
            self.devices_requested.emit()
            return
        if data is None:
            if self.source is not None:
                self.detach_source(self.source)
            return
        self.use_instrument(data)

    #: entries of the tool bar for capturing: not in a view of data that come from elsewhere
    CAPTURE_ENTRIES = ("capture", "again", "stop", "device", "quick", "settings", "profiles", "state", "open")

    def set_viewer(self, viewer: bool = True) -> None:
        """A view of data that come from elsewhere - the run data of a flow, a scope node: it shows
        them (search, measure, decode, save) and cannot capture, so the capture tool bar, the device
        and *Open* are not there (the flow would replace what they loaded)."""
        self.viewer = bool(viewer)
        self.main_toolbar.set_available(self.CAPTURE_ENTRIES, not self.viewer)
        for action in (self.action_new, self.action_open, self.action_repeat, self.action_stop,
                       self.action_device_card, self.action_simulation):
            action.setVisible(not self.viewer)
        self._update_actions()
        self.document_changed.emit()  # the header's Capture follows

    def capture(self) -> bool:
        """Capture with the device and the settings of the capture tool bar (F5)."""
        if self.viewer:
            return False
        if self.source is None:
            self.devices_requested.emit()
            self.statusBar().showMessage("Choose a device to capture with (or connect one)", 6000)
            return False
        return self.capture_controls.start_capture()

    def _build_capture_area(self) -> QWidget:
        self.capture_stack = QStackedWidget(self)
        self.capture_stack.addWidget(self._build_empty_page())

        container = QWidget(self.capture_stack)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        #: room on the right of the rows outside the scroll area, as wide as its vertical scroll
        #: bar while it shows: every row maps the samples onto the same width (ticks and lines meet)
        self._gutters: list[QWidget] = []

        def with_gutter(widget: QWidget) -> QWidget:
            row = QWidget(container)
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(0)
            row_layout.addWidget(widget, 1)
            gutter = QWidget(row)
            gutter.setFixedWidth(0)
            row_layout.addWidget(gutter)
            self._gutters.append(gutter)
            return row

        self.annotation_viewer = AnnotationViewer(self.model, container)
        self.annotation_viewer.row_clicked.connect(self.show_annotation_list)
        layout.addWidget(with_gutter(self.annotation_viewer))

        ruler_row = QWidget(container)
        ruler_layout = QHBoxLayout(ruler_row)
        ruler_layout.setContentsMargins(0, 0, 0, 0)
        ruler_layout.setSpacing(0)

        show_all_button = QPushButton("Show all", ruler_row)
        set_icon(show_all_button, "eye")
        show_all_button.setFixedWidth(CHANNEL_COLUMN_WIDTH)
        show_all_button.setToolTip("Hidden channels: show them again (all or one by one)")
        # the hidden channels, to show again one by one or all at once
        self.hidden_menu = QMenu(show_all_button)
        self.hidden_menu.aboutToShow.connect(self._fill_hidden_menu)
        show_all_button.setMenu(self.hidden_menu)
        self.show_all_button = show_all_button
        ruler_layout.addWidget(show_all_button)

        self.sample_marker = SampleMarker(self.model, ruler_row)
        self.sample_marker.setToolTip(
            "Drag to select samples, right-click for editing, measuring and regions"
        )
        ruler_layout.addWidget(self.sample_marker, 1)
        ruler_gutter = QWidget(ruler_row)
        ruler_gutter.setFixedWidth(0)
        ruler_layout.addWidget(ruler_gutter)
        self._gutters.append(ruler_gutter)
        layout.addWidget(ruler_row)

        # Buses and groups: with the channels, below the time axis
        self.bus_viewer = BusViewer(self.model, container)
        self.bus_viewer.edit_requested.connect(self.edit_bus)
        layout.addWidget(with_gutter(self.bus_viewer))

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
        pinned_gutter = QWidget(self.pinned_area)
        pinned_gutter.setFixedWidth(0)
        pinned_layout.addWidget(pinned_gutter)
        self._gutters.append(pinned_gutter)
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
        container_layout = QVBoxLayout(waveform_container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.setSpacing(0)
        digital_rows = QWidget(waveform_container)
        waveform_layout = QHBoxLayout(digital_rows)
        waveform_layout.setContentsMargins(0, 0, 0, 0)
        waveform_layout.setSpacing(0)

        self.channel_viewer = ChannelViewer(self.model, digital_rows, section="scrolling")
        waveform_layout.addWidget(self.channel_viewer)

        self.sample_viewer = SampleViewer(self.model, digital_rows, section="scrolling")
        waveform_layout.addWidget(self.sample_viewer, 1)
        # the rows start at the top with their height (Taller/Shorter channels); room below stays free
        self.digital_rows = digital_rows
        container_layout.addWidget(digital_rows)

        # Analog channels: tracks below the digital ones, on the same time axis.
        self.analog_viewer = AnalogViewer(self.model, waveform_container)
        self.analog_viewer.derive_requested.connect(self.derive_digital_channel)
        container_layout.addWidget(self.analog_viewer)
        container_layout.addStretch(1)
        for signal in (self.model.capture_changed, self.model.channels_changed, self.model.channel_height_changed):
            signal.connect(self._fit_digital_rows)
        self.model.capture_changed.connect(
            lambda: digital_rows.setVisible(bool(self.model.channels) or not self.model.analog_channels))

        self.scroll_area.setWidget(waveform_container)
        layout.addWidget(self.scroll_area, 1)
        self.scroll_area.verticalScrollBar().installEventFilter(self)

        layout.addWidget(self._build_view_bar(container))

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

        title = QLabel("No data", card)
        set_role(title, "title")
        card_layout.addWidget(title)
        card_layout.addWidget(
            hint(
                "A data view shows captures: capture with a device (its device card in the device "
                "list, or a flow), open a saved capture, or generate test signals.",
                card,
            )
        )
        card_layout.addSpacing(6)

        # two lines of buttons: the empty page fits beside a device card
        buttons = QGridLayout()
        buttons.setSpacing(8)
        self.empty_primary_button = QPushButton("Devices...", card)
        set_variant(self.empty_primary_button, "primary")
        set_icon(self.empty_primary_button, "devices")
        self.empty_primary_button.clicked.connect(self._empty_page_primary_action)
        buttons.addWidget(self.empty_primary_button, 0, 0, 1, 2)
        open_button = QPushButton("Open capture...", card)
        set_icon(open_button, "folder")
        open_button.clicked.connect(self.open_capture)
        buttons.addWidget(open_button, 1, 0)
        simulate_button = QPushButton("Test signals...", card)
        set_icon(simulate_button, "wave")
        simulate_button.clicked.connect(self.simulated_capture)
        buttons.addWidget(simulate_button, 1, 1)
        card_layout.addLayout(buttons)

        card_layout.addSpacing(6)
        card_layout.addWidget(
            hint("  ·  ".join(f"{native(keys)} {what}" for keys, what in (
                ("Ctrl+O", "open"), ("Ctrl+0", "zoom to fit"), ("Ctrl+T", "go to trigger"), ("Ctrl+F", "find"))), card)
        )

        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(card, 3)
        row.addStretch(1)
        outer.addLayout(row)
        outer.addStretch(3)
        return page

    def _build_view_bar(self, parent: QWidget) -> QToolBar:
        """Zoom and channel height, below the waveform; it adapts to the width like the tool bar."""
        from ..widgets.toolbar import HIGH, LOW, NORMAL, AdaptiveToolBar

        bar = AdaptiveToolBar("dataview-view", "View", parent)
        self.view_toolbar = bar
        bar.setIconSize(QSize(icon_px(14), icon_px(14)))
        self.fit_button = self._tool_button(bar, "fit", "Show the whole capture (Ctrl+0)", self.model.zoom_to_fit,
                                            text="Fit")
        bar.add_widget("fit", self.fit_button, "Show the whole capture", "fit", HIGH, text=False)
        self.fit_selection_button = QPushButton("Fit selection", bar)
        set_variant(self.fit_selection_button, "tool")
        set_icon(self.fit_selection_button, "fit")
        self.fit_selection_button.setToolTip("Show the samples selected on the time axis (Ctrl+E)")
        self.fit_selection_button.clicked.connect(self.model.zoom_to_selection)
        self.fit_selection_button.setEnabled(False)
        self.model.marker_changed.connect(
            lambda: self.fit_selection_button.setEnabled(self.model.selection is not None))
        bar.add_widget("fit-selection", self.fit_selection_button, "Fit selection", "fit", NORMAL)
        self.trigger_button = self._tool_button(bar, "target", "Center the view on the trigger (Ctrl+T)",
                                                self.go_to_trigger, text="Trigger")
        bar.add_widget("trigger", self.trigger_button, "Center on the trigger", "fit", NORMAL, text=False)
        # Roll mode of a live capture: the view runs with the newest samples, or holds still.
        self.follow_button = QPushButton("Roll", bar)
        set_variant(self.follow_button, "tool")
        self.follow_button.setCheckable(True)
        self.follow_button.setChecked(True)
        set_icon(self.follow_button, "play")
        self.follow_button.setToolTip("Live: the view follows the newest samples; off holds the view still "
                                      "while the capture goes on")
        self.follow_button.toggled.connect(self._follow_toggled)
        bar.add_widget("roll", self.follow_button, "Roll (live)", "fit", HIGH)
        # State captures with times: one column per state, or the states on their real time.
        self.state_time_button = QPushButton("Real time", bar)
        set_variant(self.state_time_button, "tool")
        self.state_time_button.setCheckable(True)
        set_icon(self.state_time_button, "clock")
        self.state_time_button.setToolTip("State capture: show the states on their real time instead of "
                                          "one per state")
        self.state_time_button.toggled.connect(self.set_state_real_time)
        bar.add_widget("real-time", self.state_time_button, "States on their real time", "fit", HIGH)
        for key in ("roll", "real-time"):  # (only while a capture streams, for a state capture with times)
            bar.item(key).available = False

        zoom_out = self._tool_button(bar, "zoom-out", "Zoom out (-)", lambda: self.model.zoom(2.0), text="Zoom out")
        bar.add_widget("zoom-out", zoom_out, "Zoom out", "zoom", HIGH, text=False)
        self.zoom_slider = QSlider(Qt.Horizontal, bar)
        self.zoom_slider.setRange(0, ZOOM_SLIDER_STEPS)
        self.zoom_slider.setMinimumWidth(80)
        self.zoom_slider.setMaximumWidth(180)
        self.zoom_slider.setToolTip("Samples shown on screen")
        self.zoom_slider.valueChanged.connect(self._on_zoom_slider)
        bar.add_widget("zoom-slider", self.zoom_slider, "Zoom slider", "zoom", NORMAL,
                       overflow=lambda menu: None)
        zoom_in = self._tool_button(bar, "zoom-in", "Zoom in (+)", lambda: self.model.zoom(0.5), text="Zoom in")
        bar.add_widget("zoom-in", zoom_in, "Zoom in", "zoom", HIGH, text=False)
        self.visible_samples_label = QLabel("-", bar)
        set_role(self.visible_samples_label, "hint")
        bar.add_widget("samples", self.visible_samples_label, "Samples on screen", "zoom", LOW)
        bar.add_stretch()

        rows = QLabel(bar)
        rows.setPixmap(icon("rows").pixmap(14, 14))
        rows.setToolTip("Height of the channels")
        self.channel_height_slider = QSlider(Qt.Horizontal, bar)
        self.channel_height_slider.setRange(SMALLEST_CHANNEL_HEIGHT, LARGEST_CHANNEL_HEIGHT)
        self.channel_height_slider.setValue(self.model.channel_height)
        self.channel_height_slider.setMinimumWidth(60)
        self.channel_height_slider.setMaximumWidth(120)
        self.channel_height_slider.setToolTip(
            "Height of the channels: lower channels fit more of them on the screen "
            "(Alt + mouse wheel, Ctrl+Shift+Up/Down)"
        )
        self.channel_height_slider.valueChanged.connect(self.model.set_channel_height)
        self.channel_height_label = QLabel(f"{self.model.channel_height} px", bar)
        self.channel_height_label.setMinimumWidth(self.channel_height_label.fontMetrics().horizontalAdvance("000 px"))
        set_role(self.channel_height_label, "hint")
        height_box = QWidget(bar)
        height_layout = QHBoxLayout(height_box)
        height_layout.setContentsMargins(0, 0, 0, 0)
        height_layout.setSpacing(4)
        for widget in (rows, self.channel_height_slider, self.channel_height_label):
            height_layout.addWidget(widget)
        bar.add_widget("height", height_box, "Height of the channels", "height", LOW, overflow=self._height_menu)
        self.model.channel_height_changed.connect(self._sync_channel_height)
        return bar.finish()

    def _height_menu(self, menu: QMenu) -> None:
        """The heights of the channels, in the *more* menu of the view bar."""
        sub = menu.addMenu(icon("rows"), "Height of the channels")
        for height in sorted({SMALLEST_CHANNEL_HEIGHT, 16, 24, 32, 48, 64, 96, LARGEST_CHANNEL_HEIGHT}):
            if not SMALLEST_CHANNEL_HEIGHT <= height <= LARGEST_CHANNEL_HEIGHT:
                continue
            action = sub.addAction(f"{height} px")
            action.setCheckable(True)
            action.setChecked(height == self.model.channel_height)
            action.triggered.connect(lambda _checked=False, height=height: self.model.set_channel_height(height))

    def set_state_real_time(self, checked: bool) -> None:
        """A state capture with times: show the states on their real time (a timing view of them)
        or one per state."""
        from ...core.state_mode import states_to_timing

        if checked:
            session = self.model.session
            if session is None or session.state_times is None or len(session.state_times) < 2:
                self.state_time_button.setChecked(False)
                return
            # Another view of the same capture, not other data: its regions and markers (at
            # state numbers, which are no positions in time) wait for the way back, and the file
            # still holds the states.
            self._state_session = (session, list(self.model.regions),
                                   [(bookmark.sample, bookmark.name) for bookmark in self.model.bookmarks])
            self._loading += 1
            try:
                self.model.set_regions([])
                self.model.set_session(states_to_timing(session))
            finally:
                self._loading -= 1
            self.model.zoom_to_fit()
            self.statusBar().showMessage("States on their real time", 5000)
        else:
            stored = getattr(self, "_state_session", None)
            self._state_session = None
            if stored is not None:
                self._loading += 1
                try:
                    self.model.set_session(stored[0])
                    self.model.set_regions(stored[1])
                    self.model.set_bookmarks(stored[2])
                finally:
                    self._loading -= 1
                self._reset_undo()
                self.model.zoom_to_fit()
        self._sync_view_controls()
        self._update_info_panel()

    def _follow_toggled(self, checked: bool) -> None:
        set_icon(self.follow_button, "play" if checked else "pause")
        if checked and self.model.is_live:
            count = self.model.sample_count
            self.model.set_view(count - self._live_window, self._live_window)

    def _fit_digital_rows(self) -> None:
        """The scrolling rows as tall as their channels (they do not stretch to fill the view)."""
        count = len(self.model.section_channels("scrolling"))
        self.digital_rows.setFixedHeight(max(count, 1) * self.model.channel_height)

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

    def _build_side_panel(self) -> QWidget:
        self.side_tabs = QTabWidget(self)
        self.side_tabs.setObjectName("side-tabs")
        self.side_tabs.setDocumentMode(True)
        self.side_tabs.setUsesScrollButtons(False)
        self.side_tabs.setElideMode(Qt.ElideNone)
        self.side_tabs.setMinimumWidth(260)

        def page(widget: QWidget) -> QScrollArea:
            holder = QScrollArea(self.side_tabs)
            holder.setWidgetResizable(True)
            holder.setFrameShape(QFrame.NoFrame)
            inner = QWidget(holder)
            inner_layout = QVBoxLayout(inner)
            inner_layout.setContentsMargins(8, 8, 8, 8)
            widget.setParent(inner)
            inner_layout.addWidget(widget, 1)
            holder.setWidget(inner)
            return holder

        self.decoder_manager = DecoderManager(self.model, self.provider, self.side_tabs)
        self.side_tabs.addTab(page(self.decoder_manager), "Decoders")
        self.measure_panel = MeasurePanel(self.model, self.side_tabs)
        self.side_tabs.addTab(page(self.measure_panel), "Measure")
        self.search_panel = SearchPanel(self.model, self.side_tabs)
        self.side_tabs.addTab(page(self.search_panel), "Search")
        self.markers_panel = MarkersPanel(self.model, self.side_tabs)
        self.side_tabs.addTab(page(self.markers_panel), "Markers")

        panel = QWidget(self.side_tabs)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

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
        next_box = QFrame(panel)
        set_role(next_box, "card")
        next_layout = QVBoxLayout(next_box)
        next_layout.setContentsMargins(10, 8, 10, 8)
        next_title = QLabel("Next capture", next_box)
        set_role(next_title, "heading")
        next_layout.addWidget(next_title)
        next_layout.addWidget(self.capture_controls.summary_label)
        next_layout.addWidget(self.capture_controls.channel_table, 1)
        layout.addWidget(next_box, 1)

        self.side_tabs.addTab(page(panel), "Capture")
        return self.side_tabs

    def _build_menu(self) -> None:
        menu = self.menuBar()

        # ------------------------------------------------------------ File
        self.file_menu = file_menu = menu.addMenu("&File")
        self.action_new = QAction("&New capture...", self)  # Ctrl+N is the shell's New flow
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

        self.export_menu = export_menu = file_menu.addMenu("&Export")
        self.action_export_csv = QAction("Comma separated values (.csv)...", self)
        self.action_export_csv.triggered.connect(lambda: self.export_capture("csv"))
        export_menu.addAction(self.action_export_csv)
        self.action_export_vcd = QAction("Value change dump (.vcd)...", self)
        self.action_export_vcd.triggered.connect(lambda: self.export_capture("vcd"))
        export_menu.addAction(self.action_export_vcd)
        self.action_export_sr = QAction("sigrok session for PulseView (.sr)...", self)
        self.action_export_sr.triggered.connect(lambda: self.export_capture("sr"))
        export_menu.addAction(self.action_export_sr)
        export_menu.addSeparator()
        self.action_export_annotations = QAction("Decoder output (.csv, .json)...", self)
        self.action_export_annotations.triggered.connect(self.export_annotations)
        export_menu.addAction(self.action_export_annotations)

        file_menu.addSeparator()
        self.action_exit = action_exit = QAction("E&xit", self)
        action_exit.setShortcut(QKeySequence.Quit)
        action_exit.triggered.connect(self.close)
        file_menu.addAction(action_exit)

        # ------------------------------------------------------------ Data
        self.data_menu = data_menu = menu.addMenu("&Data")
        self.action_repeat = QAction("Capture &again", self)
        self.action_repeat.setShortcut(QKeySequence("Ctrl+R"))
        self.action_repeat.setStatusTip("Capture again with the device and the settings of the shown capture")
        self.action_repeat.triggered.connect(self.repeat_capture)
        data_menu.addAction(self.action_repeat)
        self.action_stop = QAction("S&top capture", self)
        self.action_stop.setShortcut(QKeySequence("Shift+F5"))
        self.action_stop.triggered.connect(self.abort_capture)
        data_menu.addAction(self.action_stop)
        self.action_device_card = QAction("&Device card", self)
        self.action_device_card.setStatusTip("The device the data come from: capture settings, profiles, device functions")
        self.action_device_card.triggered.connect(lambda: self.source is not None
                                                  and self.device_card_requested.emit(self.source.instrument))
        data_menu.addAction(self.action_device_card)
        data_menu.addSeparator()
        self.action_simulation = QAction("&Test signals...", self)
        self.action_simulation.setStatusTip("Signals of common protocols computed on this computer")
        self.action_simulation.triggered.connect(self.simulated_capture)
        data_menu.addAction(self.action_simulation)
        self.action_align = QAction("&Align boards", self)
        self.action_align.setStatusTip(
            "Align the boards of a multi device capture using a reference line or the clock"
        )
        self.action_align.triggered.connect(self.align_boards)
        data_menu.addAction(self.action_align)

        self.action_use_names = QAction("Use these channel &names for the next capture", self)
        self.action_use_names.setStatusTip("The device names its channels as here (renamed in this data view)")
        self.action_use_names.triggered.connect(self.use_names_for_device)
        data_menu.addAction(self.action_use_names)

        # editing the samples of the selection in the ruler (also in its right-click menu)
        data_menu.addSeparator()
        self.sample_actions = []
        for text, shortcut, slot in (
            ("Cu&t samples", QKeySequence.Cut, lambda: self._on_selection(self.cut_samples)),
            ("&Copy samples", QKeySequence.Copy, lambda: self._on_selection(self.copy_samples)),
            ("&Paste samples", QKeySequence.Paste, self._paste_at_marker),
            ("De&lete samples", QKeySequence.Delete, lambda: self._on_selection(self.delete_samples)),
            ("&Insert samples...", None, lambda: self.insert_samples_dialog(self._edit_position())),
            ("S&hift channels...", None, self.shift_channels),
        ):
            action = QAction(text, self)
            if shortcut is not None:
                action.setShortcut(QKeySequence(shortcut))
                action.setShortcutContext(Qt.WidgetWithChildrenShortcut)
                self.addAction(action)
            action.triggered.connect(lambda _checked=False, slot=slot: slot())
            data_menu.addAction(action)
            self.sample_actions.append(action)

        for action, name in (
            (self.action_new, "file-plus"),
            (self.action_open, "folder"),
            (self.action_save, "save"),
            (self.action_save_as, "save"),
            (self.action_export_csv, "export"),
            (self.action_export_vcd, "export"),
            (self.action_repeat, "repeat"),
            (self.action_stop, "stop"),
            (self.action_device_card, "devices"),
            (self.action_simulation, "wave"),
        ):
            action.setIcon(icon(name))

        # --------------------------------------------------------- Analyze
        self.analyze_menu = analyze_menu = menu.addMenu("&Analyze")
        self.action_buses = QAction("&New bus or group...", self)
        self.action_buses.setStatusTip("Show several channels as one value, with a symbol table")
        self.action_buses.triggered.connect(lambda: self.edit_bus(None))
        analyze_menu.addAction(self.action_buses)
        analyze_menu.addSeparator()
        self.action_find = QAction("&Find...", self)
        self.action_find.setShortcut(QKeySequence.Find)
        self.action_find.triggered.connect(lambda: self.show_panel(self.search_panel))
        analyze_menu.addAction(self.action_find)
        self.action_find_next = QAction("Find &next", self)
        self.action_find_next.setShortcut(QKeySequence("F3"))
        self.action_find_next.triggered.connect(lambda: self.search_panel.step(1))
        analyze_menu.addAction(self.action_find_next)
        self.action_find_previous = QAction("Find &previous", self)
        self.action_find_previous.setShortcut(QKeySequence("Shift+F3"))
        self.action_find_previous.triggered.connect(lambda: self.search_panel.step(-1))
        analyze_menu.addAction(self.action_find_previous)
        analyze_menu.addSeparator()
        self.action_measure = QAction("&Measurements", self)
        self.action_measure.setShortcut(QKeySequence("Ctrl+Shift+M"))  # Cmd+M minimises on macOS
        self.action_measure.triggered.connect(lambda: self.show_panel(self.measure_panel))
        analyze_menu.addAction(self.action_measure)
        self.action_charts = QAction("&Charts and histograms...", self)
        self.action_charts.triggered.connect(lambda: ChartDialog(self.model, self).exec())
        analyze_menu.addAction(self.action_charts)
        self.action_compare = QAction("C&ompare with a reference...", self)
        self.action_compare.triggered.connect(lambda: CompareDialog(self.model, self).exec())
        analyze_menu.addAction(self.action_compare)
        self.action_state = QAction("&State analysis (clocked)...", self)
        self.action_state.setStatusTip("Resample the capture on the edges of a clock channel")
        self.action_state.triggered.connect(self.state_analysis)
        analyze_menu.addAction(self.action_state)
        self.action_back_to_timing = QAction("&Back to the timing capture", self)
        self.action_back_to_timing.setStatusTip("Leave the state analysis for the capture it was made of")
        self.action_back_to_timing.triggered.connect(self.back_to_timing)
        analyze_menu.addAction(self.action_back_to_timing)
        analyze_menu.addSeparator()
        self.action_play = QAction("&Play as signal...", self)
        self.action_play.setStatusTip("Play channels of the capture on a generator output (between the cursors if set)")
        self.action_play.triggered.connect(self.play_as_signal)
        analyze_menu.addAction(self.action_play)
        self._capture_actions = [
            self.action_buses, self.action_find, self.action_measure, self.action_charts,
            self.action_compare, self.action_state, self.action_export_sr, self.action_export_annotations,
            self.action_play,
        ]
        for action, name in (
            (self.action_buses, "bus"), (self.action_find, "search"), (self.action_measure, "ruler"),
            (self.action_charts, "chart"), (self.action_compare, "compare"), (self.action_state, "clock"),
            (self.action_back_to_timing, "reset"),
            (self.action_export_sr, "export"), (self.action_export_annotations, "export"),
        ):
            action.setIcon(icon(name))

        # ------------------------------------------------------------ View
        self.view_menu = view_menu = menu.addMenu("&View")
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
        self.action_fit_selection = QAction("Zoom to the &selection", self)
        self.action_fit_selection.setShortcut("Ctrl+E")
        self.action_fit_selection.setStatusTip("Show the samples selected on the time axis")
        self.action_fit_selection.triggered.connect(self.model.zoom_to_selection)
        view_menu.addAction(self.action_fit_selection)

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
        view_menu.addSeparator()
        panels_action = self.panels_dock.toggleViewAction()
        panels_action.setText("&Analysis panels")
        panels_action.setShortcut(QKeySequence("Ctrl+1"))
        view_menu.addAction(panels_action)
        listing_action = self.listing_dock.toggleViewAction()
        listing_action.setText("&Listing")
        listing_action.setShortcut(QKeySequence("Ctrl+2"))
        view_menu.addAction(listing_action)

    #: Icons of the menu entries that do not set their own (by their text without "&" and "...")
    MENU_ICONS = {
        "Exit": "exit",
        "Align boards": "align",
        "Find next": "down",
        "Find previous": "up",
        "Zoom in": "zoom-in",
        "Zoom out": "zoom-out",
        "Zoom to fit": "fit",
        "Go to trigger": "target",
        "Taller channels": "rows",
        "Shorter channels": "rows",
        "Default channel height": "reset",
        "Navigate": "arrow-right",
        "Show all channels": "eye",
        "Unpin all channels": "pin",
        "Keyboard shortcuts": "keyboard",
        "Online documentation": "book",
        "Online documentation of the original software (gusmanb)": "book",
        "Decoder search paths": "folder",
        "About openSciLab": "info",
        "Export": "export",
    }

    def _apply_menu_icons(self) -> None:
        def visit(menu) -> None:
            for action in menu.actions():
                if action.isSeparator():
                    continue
                name = action.text().replace("&", "").rstrip(".").strip()
                if action.icon().isNull() and not action.isCheckable() and name in self.MENU_ICONS:
                    action.setIcon(icon(self.MENU_ICONS[name]))
                if action.menu() is not None:
                    visit(action.menu())

        for action in self.menuBar().actions():
            if action.menu() is not None:
                visit(action.menu())

    def _update_title(self) -> None:
        name = f"openSciLab {__version__}"
        if self.current_file:
            self.setWindowTitle(f"{os.path.basename(self.current_file)} — {name}")
        else:
            self.setWindowTitle(name)
        self.document_changed.emit()

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt naming
        if watched is self.scroll_area.verticalScrollBar() and event.type() in (
                QEvent.Show, QEvent.Hide, QEvent.Resize):
            self._sync_gutters()
        return super().eventFilter(watched, event)

    def _sync_gutters(self) -> None:
        """The rows above the tracks leave room for the tracks' scroll bar (same sample width)."""
        bar = self.scroll_area.verticalScrollBar()
        width = bar.width() if bar.isVisible() else 0
        for gutter in self._gutters:
            if gutter.width() != width:
                gutter.setFixedWidth(width)

    # ------------------------------------------------------------- document
    def _embed(self) -> None:
        """Become a document tab: the shell shows the menus and owns Open, Save and Exit."""
        # not the native menu bar of macOS: it would replace the shell's menus whenever the data
        # view has the focus (Examples, Devices and the others were gone)
        self.menuBar().setNativeMenuBar(False)
        self.menuBar().setVisible(False)
        # The shell has these shortcuts and passes them on to the active document.
        for action in (self.action_open, self.action_save, self.action_save_as, self.action_exit):
            action.setShortcuts([])
        self.view_menu.setTitle("Di&splay")  # "Waveform" is a kind of document of its own
        # keys without Ctrl/Cmd (arrows, +, -) only act while the data view has the focus: they
        # must not scroll the capture while typing in the sidebar or a tree
        for action in self.findChildren(QAction):
            if any(sequence.count() and not (sequence[0].keyboardModifiers() & (
                    Qt.ControlModifier | Qt.MetaModifier | Qt.AltModifier))
                   and not sequence.toString().startswith(("F", "Shift+F", "Del", "Esc"))
                   for sequence in action.shortcuts()):
                action.setShortcutContext(Qt.WidgetWithChildrenShortcut)
                self.addAction(action)

    def _set_dirty(self, dirty: bool) -> None:
        self._edited = dirty
        if dirty != self._dirty:
            self._dirty = dirty
            self.document_changed.emit()

    def _data_replaced(self) -> None:
        if self.model.session is self._data_session:
            return  # the same data changed (an edit), not other data
        self._data_session = self.model.session
        # a selection in the ruler belongs to the data before: cutting or deleting must not act
        # on those positions in the new data
        self.sample_marker.clear_selection()
        self._loading += 1
        QTimer.singleShot(0, self._data_settled)
        self._edited = False
        self._reset_undo()

    def _reset_undo(self) -> None:
        """New data: nothing to undo, the state to compare edits with is this one."""
        from .data_edits import take, undo_limit

        self.undo.clear()
        self.undo.setUndoLimit(undo_limit(self.model.sample_count, len(self.model.channels)))
        self._state = take(self)

    def record_edit(self, text: str) -> None:
        """An edit happened: one undo step from the state before it to the state now."""
        from .data_edits import DataEdit, take

        after = take(self)
        if after is None:
            return
        before = self._state if self._state is not None else after
        self._state = after
        self.undo.push(DataEdit(self, text, before, after))

    def _data_settled(self) -> None:
        self._loading = max(self._loading - 1, 0)

    def _model_edited(self) -> None:
        """Names, colours, regions, markers or buses changed: saved in the file, so unsaved now."""
        if not self._loading and self.model.session is not None:
            self._set_dirty(True)
            self.record_edit("Change channels, markers or regions")

    def undo_stack(self):
        return self.undo

    @property
    def has_edits(self) -> bool:
        """The user changed the data since it was loaded, captured or saved."""
        return self._edited and self.model.session is not None

    def confirm_replace(self, title: str) -> bool:
        """Before other data replace unsaved data: save, discard or cancel."""
        if not self._dirty or self.model.session is None:
            return True
        choice = messages.choose(self, title, f"{self.title} has unsaved data. Save it first?",
                                 ["Save", "Discard"])
        if choice == 0:
            saves = self._save_count
            self.save_capture()
            return self._save_count > saves
        return choice == 1

    @property
    def title(self) -> str:
        if self.current_file:
            return os.path.basename(self.current_file)
        if self.display_name:
            return self.display_name
        if self.source is not None:
            return f"Data · {self.source.instrument.name}"  # the device these data come from
        return "Capture" if self.model.session is not None else "Data view"

    @property
    def path(self) -> Optional[str]:
        return self.current_file

    @property
    def dirty(self) -> bool:
        return self._dirty

    def document_menus(self) -> list:
        return [self.data_menu, self.analyze_menu, self.view_menu]

    def document_file_actions(self) -> list:
        return [self.action_new, self.export_menu.menuAction()]

    def toolbar(self) -> Optional[QToolBar]:
        return self.main_toolbar

    def views(self) -> list[str]:
        return ["Analyzer"]

    def inspector_widget(self, selection=None) -> Optional[QWidget]:
        """The capture: file, device and the figures of the *Capture* tab."""
        if getattr(self, "_inspector", None) is None:
            self._inspector = QLabel(self)
            self._inspector.setTextFormat(Qt.RichText)
            self._inspector.setWordWrap(True)
            self._inspector.setAlignment(Qt.AlignTop | Qt.AlignLeft)
            self._inspector.setContentsMargins(10, 6, 10, 6)
            self._inspector.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self._inspector.hide()
            self._update_inspector()
        return self._inspector

    def _update_inspector(self) -> None:
        if getattr(self, "_inspector", None) is None:
            return
        device = self.driver.device_version if self._has_real_device() else "none"
        rows = [("File", html.escape(self.current_file or "not saved")), ("Device", html.escape(str(device)))]
        head = "".join(
            f"<tr><td style='color:{TEXT_MUTED}'>{name}</td><td>{value}</td></tr>" for name, value in rows
        )
        self._inspector.setText(f"<table cellspacing='0' cellpadding='2'>{head}</table><br>{self.info_label.text()}")

    def can_save(self) -> bool:
        return self.model.session is not None and self.model.sample_count > 0

    def save(self) -> bool:
        count = self._save_count
        self.save_capture()
        return self._save_count > count

    def save_as(self) -> bool:
        count = self._save_count
        self.save_capture_as()
        return self._save_count > count

    def shutdown(self) -> None:
        self.decoder_manager.shutdown()
        self.stop_recording()
        self._save_window_state()
        if self.source is not None:
            self.detach_source(self.source)  # the device stays open in the hub
        self.hub_bridge.close()

    # ------------------------------------------------------------ view state
    def _on_capture_changed(self) -> None:
        self._sync_view_controls()
        self._update_info_panel()
        self._update_actions()

    def _sync_view_controls(self) -> None:
        if self._updating_view_controls:
            return
        from .. import accessibility

        model = self.model
        last = model.first_sample + model.visible_samples
        accessibility.describe(
            self.sample_viewer, "Waveform",
            f"{len(model.visible_channels)} channels; samples {model.first_sample:,} to {last:,} of "
            f"{model.sample_count:,}" if model.session is not None else "No data")
        self._updating_view_controls = True
        try:
            total = self.model.sample_count
            visible = self.model.visible_samples

            self.position_scrollbar.setRange(0, max(total - visible, 0))
            self.position_scrollbar.setPageStep(max(visible, 1))
            self.position_scrollbar.setSingleStep(max(visible // 10, 1))
            self.position_scrollbar.setValue(self.model.first_sample)

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

    # ---------------------------------------------------------------- source
    @property
    def driver(self) -> Optional[AnalyzerDriverBase]:
        """The driver of the instrument the data come from (``None`` for files and test signals)."""
        return self.source.driver if self.source is not None else None

    @property
    def instrument(self) -> Optional[Instrument]:
        return self.source.instrument if self.source is not None else None

    def use_instrument(self, instrument: Instrument) -> None:
        """Show the captures of ``instrument`` of the hub (its device card starts them)."""
        from ..devices.capture import capture_controller

        controller = capture_controller(instrument)
        if controller is None:
            messages.warning(self, "Data", f"{instrument.name} cannot capture.")
            return
        self.attach_source(controller)

    def attach_source(self, controller) -> None:
        """Captures of ``controller`` go to this view; one view per instrument at a time."""
        if controller is self.source:
            return
        if self.viewer:
            self.set_viewer(False)  # it captures from now on
        if self.source is not None:
            self.detach_source(self.source)
        previous = controller.view
        if previous is not None and previous is not self:
            previous.detach_source(controller)
        controller.view = self
        self.source = controller
        controller.instrument.owner = self
        controller.changed.connect(self._update_actions)
        self.capture_controls.bind(controller, self.hub)
        self._update_actions()
        self._fill_device_combo()
        self.document_changed.emit()  # the header's Capture follows

    def detach_source(self, controller) -> None:
        if controller is not self.source:
            return
        try:
            controller.changed.disconnect(self._update_actions)
        except (RuntimeError, TypeError):
            pass
        if controller.view is self:
            controller.view = None
        if controller.instrument.owner is self:
            controller.instrument.owner = None
        self.source = None
        self._running_capture = None
        self.capture_controls.bind(None)
        self._update_actions()
        self._fill_device_combo()
        self.document_changed.emit()

    def _update_source_label(self) -> None:
        """The device chooser follows the source (and the instruments of the hub)."""
        self._fill_device_combo()

    def _on_hub_event(self, event) -> None:
        self._fill_device_combo()
        if event.kind == EVENT_REMOVED and self.source is not None and event.name == self.source.name \
                and self.source.instrument not in self.hub:
            source = self.source
            self.detach_source(source)
            source.close()

    # ------------------------------------------------------------ progressive
    def _on_tile(self, args) -> None:
        """Parts of a progressively transferred capture arrived."""
        if args.session is not self.model.session:
            return
        progressive = args.session.progressive
        if args.error:
            self.statusBar().showMessage(f"Transfer interrupted: {args.error}")
            self._set_transfer_notice(True)
            return
        self.model.tiles_loaded()
        if progressive is not None and progressive.complete:
            self.statusBar().showMessage("Transfer complete", 5000)
            self._set_transfer_notice(False)
            self._update_info_panel()
            self._update_actions()
        elif progressive is not None:
            self.statusBar().showMessage(f"Transferring the capture: {progressive.fraction * 100:.0f} %")

    def _prioritize_visible(self) -> None:
        session = self.model.session
        if session is None or self.model.progressive is None or self.driver is None:
            return
        self.driver.prioritize(session, self.model.first_sample, self.model.first_sample + self.model.visible_samples)

    def _set_transfer_notice(self, interrupted: bool) -> None:
        self.transfer_banner.set_message(
            "The transfer of the capture stopped before all of it arrived." if interrupted else "",
            "Resume transfer" if interrupted else None)
        self._update_banners()

    def _update_banners(self) -> None:
        self._banner_holder.setVisible(not self.transfer_banner.isHidden() or not self.capture_banner.isHidden())

    def show_capture_problem(self, text: str, details: str = "", kind: str = "error") -> None:
        """A problem of the last capture above the waveform (``""`` hides it)."""
        from ..theme import set_role

        set_role(self.capture_banner, f"banner-{kind}")
        message = html.escape(text) + (f"<br><span style='opacity:0.8'>{html.escape(details)}</span>" if details else "")
        self.capture_banner.set_message(message if text else "", "Device card" if self.source is not None else
                                        ("Dismiss" if text else None))
        self._update_banners()

    def _capture_banner_action(self) -> None:
        if self.source is not None:
            self.device_card_requested.emit(self.source.instrument)
        else:
            self.show_capture_problem("")

    def resume_transfer(self) -> bool:
        session = self.model.session
        if session is None or self.driver is None or not self.driver.resume_transfer(session):
            messages.warning(self, "Transfer", "The transfer cannot be resumed.",
                             "Capture again to get the samples.")
            return False
        self._set_transfer_notice(False)
        self.statusBar().showMessage("Transfer resumed")
        return True

    def wait_for_transfer(self, operation: str) -> bool:
        """``operation`` needs the whole capture: wait (with progress) until it arrived."""
        progressive = self.model.progressive
        if progressive is None:
            return True
        dialog = QProgressDialog(f"{operation}: waiting for the rest of the capture...", "Cancel", 0, 100, self)
        dialog.setWindowTitle(operation)
        dialog.setWindowModality(Qt.WindowModal)
        dialog.setMinimumDuration(0)
        # an event loop that ends when the capture is there (or the transfer stopped, or the user
        # cancels), looked at a few times a second – not a loop that spins the application
        loop = QEventLoop()
        timer = QTimer()
        timer.setInterval(TRANSFER_CHECK_MS)

        def check() -> None:
            if self.model.progressive is None or progressive.interrupted:
                loop.quit()
            else:
                dialog.setValue(int(progressive.fraction * 100))

        timer.timeout.connect(check)
        dialog.canceled.connect(loop.quit)
        dialog.setValue(int(progressive.fraction * 100))
        if self.model.progressive is not None and not progressive.interrupted:
            timer.start()
            loop.exec()
            timer.stop()
        cancelled = dialog.wasCanceled()
        waiting = self.model.progressive is not None
        dialog.close()
        dialog.deleteLater()
        if cancelled:
            return False
        if waiting and progressive.interrupted:
            messages.warning(self, operation, "The transfer stopped before the capture was complete.",
                             "Resume the transfer first.")
            return False
        return True

    def derive_digital_channel(self, channel) -> Optional[object]:
        """A digital channel from an analog one: above a threshold, with hysteresis."""
        low, high = self.model.analog_range(channel)
        threshold, accepted = QInputDialog.getDouble(
            self, "Digital channel", f"Threshold of {channel.display_name} ({channel.unit}):",
            (low + high) / 2, low - abs(high - low), high + abs(high - low), 3)
        if not accepted:
            return None
        hysteresis, accepted = QInputDialog.getDouble(
            self, "Digital channel", f"Hysteresis ({channel.unit}):", abs(high - low) * 0.05, 0.0,
            abs(high - low), 3)
        if not accepted:
            return None
        return self.add_derived_channel(channel, threshold, hysteresis)

    def add_derived_channel(self, channel, threshold: float, hysteresis: float):
        session = self.model.session
        if session is None or not self.wait_for_transfer("Digital channel"):
            return None
        number = max([c.channel_number for c in session.capture_channels] + [-1]) + 1
        derived = derive_digital(channel, threshold, hysteresis, number)
        count = session.sample_count() if session.capture_channels else len(derived.samples)
        derived.samples = derived.samples[:count] if len(derived.samples) >= count else np.concatenate(
            [derived.samples, np.zeros(count - len(derived.samples), np.uint8)])
        session.capture_channels.append(derived)
        self._loading += 1
        try:
            self.model.rebuild_transitions()
            self.model.notify_channels_changed()
            self.model.notify_capture_changed()
        finally:
            self._loading -= 1
        self._set_dirty(True)
        self.record_edit(f"Add {derived.channel_name}")
        self.statusBar().showMessage(f"Added {derived.channel_name}", 5000)
        return derived

    def _has_real_device(self) -> bool:
        return self.driver is not None and self.driver.is_hardware

    # ------------------------------------------------------------ test signals
    def expect_simulation(self, dialog) -> None:
        """The next capture holds test signals of ``dialog`` (its decoders are added with it)."""
        self._pending_simulation = {
            "pattern": dialog.pattern,
            "channels": dialog.channel_count,
            "frequency": dialog.frequency,
            "add_decoders": dialog.add_decoders,
        }

    def simulated_capture(self) -> None:
        """Test signals computed on this computer (the device card has those of a board)."""
        if self.is_capturing or not self.confirm_replace("Test signals"):
            return
        driver = EmulatedAnalyzerDriver(1)
        dialog = SimulationDialog(driver, on_board=False, parent=self)
        if not dialog.exec() or dialog.session is None:
            return
        if self.source is not None:
            self.detach_source(self.source)
        self.expect_simulation(dialog)
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
    def show_panel(self, panel: QWidget) -> None:
        """Brings a tab of the analysis panels to the front."""
        self.panels_dock.setVisible(True)
        for index in range(self.side_tabs.count()):
            holder = self.side_tabs.widget(index)
            if holder.isAncestorOf(panel):
                self.side_tabs.setCurrentIndex(index)
                break

    def edit_bus(self, bus) -> None:
        """Defines a new bus (``bus=None``) or changes one."""
        if self.model.session is None:
            return
        dialog = BusDialog(self.model.channels, bus, self)
        if not dialog.exec():
            return
        buses = self.model.buses
        if bus is None:
            buses.append(dialog.bus)
        else:
            buses[next(index for index, item in enumerate(buses) if item is bus)] = dialog.bus
        self.model.set_buses(buses)

    def export_annotations(self) -> None:
        from ...core.annotation_export import export_annotations

        groups = [group for group in self.model.annotation_groups if not group.error]
        if self.model.session is None or not groups:
            messages.info(self, "Export decoder output", "There is no decoder output to export.", "Add a decoder first.")
            return
        base = os.path.splitext(os.path.basename(self.current_file or "capture"))[0]
        path, chosen = QFileDialog.getSaveFileName(
            self, "Export decoder output", f"{base}-decoded.csv", "CSV (*.csv);;JSON (*.json)"
        )
        if not path:
            return
        if not path.lower().endswith((".csv", ".json")):
            path += ".json" if "JSON" in chosen else ".csv"
        try:
            export_annotations(path, groups, self.model.session)
        except (OSError, ValueError) as error:
            messages.error(self, "Export decoder output", f"{os.path.basename(path)} could not be written.", str(error))
            return
        self.statusBar().showMessage(f"Exported {path}", 5000)

    def back_to_timing(self) -> None:
        """Returns from a state analysis to the capture it was made of."""
        if self._timing_capture is None:
            return
        session, regions, path = self._timing_capture
        self.current_file = path
        self.model.set_regions(regions)
        self.load_session(session)
        self._update_title()
        self.statusBar().showMessage("Back to the timing capture", 5000)

    def state_analysis(self) -> None:
        """Resamples the loaded capture on the edges of a clock channel (a new capture of states)."""
        from ..dialogs.state_dialog import StateDialog

        session = self.model.session
        if session is None:
            return
        dialog = StateDialog(session, self)
        if not dialog.exec() or dialog.result_capture is None:
            return
        state = dialog.result_capture
        # The states are a capture of their own; a repeated capture uses the device's timing again
        state.session.clock_channel = None
        # Kept until another capture is loaded, for Back to the timing capture
        timing = (session, self.model.regions, self.current_file)
        self.current_file = None
        self._loading += 1
        try:
            self.model.set_regions([])  # they mark samples of the timing capture, not states
        finally:
            self._loading -= 1
        self.load_session(state.session)
        self._timing_capture = timing
        self._update_title()
        self._update_actions()
        self.statusBar().showMessage(
            f"{to_thousands(state.state_count)} states clocked by channel {dialog.clock_channel + 1}", 8000
        )

    def repeat_capture(self) -> bool:
        """Capture again with the device the shown data came from."""
        if self.source is None:
            messages.warning(self, "Capture again", "These data do not come from a device.",
                             "Choose a device in the capture tool bar.")
            return False
        return self.source.repeat_capture()

    def capture_as_waveform(self, channels: Optional[list[int]] = None, analog: Optional[int] = None):
        """The shown capture as a waveform: digital ``channels`` (indexes, default all) as a pattern,
        or the analog channel ``analog`` as arbitrary points; between the cursors when both are set."""
        from ...core import waveform as waves

        session = self.model.session
        if session is None:
            return None
        first, last = 0, self.model.sample_count
        a, b = self.model.cursor("A"), self.model.cursor("B")
        if a is not None and b is not None and a != b:
            first, last = min(a, b), max(a, b) + 1
        if analog is not None:
            channel = session.analog_channels[analog]
            values = channel.volts(first, last)
            rate = channel.rate or session.frequency
            waveform = waves.from_points(values, rate=rate, repeat=1)
            waveform.name = channel.display_name
            return waveform
        chosen = [session.capture_channels[index] for index in (channels if channels is not None
                                                                else range(len(session.capture_channels)))]
        tracks = {channel.display_name: np.asarray(channel.samples[first:last], dtype=np.uint8)
                  for channel in chosen if channel.samples is not None}
        return waves.pattern(tracks, session.frequency, repeat=1, name=os.path.splitext(self.title)[0])

    def play_as_signal(self) -> None:
        """Ask what to play, then hand the waveform to a waveform document."""
        session = self.model.session
        if session is None:
            return
        options = ["Digital channels (pattern)"] + [f"Analog: {channel.display_name}"
                                                    for channel in session.analog_channels]
        choice = 0
        if len(options) > 1:
            choice = messages.choose(self, "Play as signal", "What should be played?", options)
            if choice is None:
                return
        waveform = self.capture_as_waveform(analog=None if choice == 0 else choice - 1)
        if waveform is not None:
            self.play_requested.emit(waveform)

    def _begin_capture(self, session: CaptureSession) -> bool:
        """Capture ``session`` with the source instrument (the device card does this as well)."""
        return self.source is not None and self.source.capture(session)

    def capture_started(self, session: CaptureSession) -> None:
        """The source started ``session``: its progress and result come here."""
        self._running_capture = session
        self._action_marks = []
        self._capture_started = time.monotonic()
        self._update_actions()
        self.statusBar().showMessage("Capturing, waiting for the trigger...")

    def capture_aborted(self) -> None:
        """The source stopped a capture that delivers nothing (a buffer capture): not capturing
        any more; what the view shows stays."""
        if self._running_capture is None:
            return
        self._running_capture = None
        self._live_session = self._live_source = None
        self._action_marks = []
        self._update_actions()
        self.statusBar().showMessage("Capture aborted", 5000)
        self.document_changed.emit()

    def abort_capture(self) -> None:
        if self.recording is not None:
            self.stop_recording()
            return
        if self.source is not None:
            self.source.abort()
        self._update_actions()

    def _queue_capture_progress(self, args: CaptureProgressArgs) -> None:
        if self._pending_progress is None:
            QTimer.singleShot(0, self._show_pending_progress)
        self._pending_progress = args

    def _show_pending_progress(self) -> None:
        args, self._pending_progress = self._pending_progress, None
        if args is not None:
            self._on_capture_progress(args)

    def _on_capture_progress(self, args: CaptureProgressArgs) -> None:
        """Shows a streaming capture while it runs, following its end unless scrolled back."""
        if args.session is not self._running_capture or args.sample_count == 0:
            return  # queued behind the completion
        model = self.model
        live = self._live_session
        if live is None or self._live_source is not args.session:
            live = args.session.clone_settings()
            for channel in live.capture_channels:
                channel.samples = args.samples.get(channel.channel_number)
            self._apply_live_extras(live, args)
            self._live_session, self._live_source = live, args.session
            self._live_window = max(live.frequency // LIVE_WINDOW_DIVISOR, 1_000)
            self._live_first = args.first_sample
            model.set_session(live, live=True, first_sample=args.first_sample)
            follow = True
        else:
            if model.visible_samples != self._live_visible:
                self._live_window = model.visible_samples  # zoomed while streaming
            follow = model.first_sample + model.visible_samples >= model.sample_count - 1
            for channel in live.capture_channels:
                channel.samples = args.samples.get(channel.channel_number, channel.samples)
            self._apply_live_extras(live, args)
            model.extend_live(args.first_sample)
            if live.analog_channels:
                model.notify_analog_changed()
            if not self.follow_button.isChecked():
                follow = False
        dropped = args.first_sample - self._live_first
        self._live_first = args.first_sample
        self._live_arrival = time.monotonic()

        count = model.sample_count
        if follow:
            model.set_view(count - self._live_window, self._live_window)
        elif dropped:
            # Scrolled back: keep the samples on screen until they drop out of the window.
            model.set_view(model.first_sample - dropped, model.visible_samples)
        self._live_visible = model.visible_samples
        rate = max(live.frequency, 1)
        lost = f", {to_thousands(args.lost)} samples lost" if args.lost else ""
        if args.session.continuous:
            streamed = args.first_sample + count
            text = (f"Streaming until stopped: {long_duration(streamed / rate)}, "
                    f"keeping the last {to_thousands(count)} samples ({to_small_time(count / rate)})")
        else:
            text = (f"Streaming: {to_thousands(count)} of {to_thousands(args.session.total_samples)} "
                    f"samples ({to_small_time(count / rate)})")
        if live.state_times is not None:
            text = f"Live state: {to_thousands(count)} states"
        self.statusBar().showMessage(text + lost)
        self.view_toolbar.set_available("roll", True)

    # ------------------------------------------- the header's Start/Stop
    can_pause = False
    run_label = "Capture"
    run_tooltip = "Capture with the device and the settings of the capture tool bar (F5)"

    @property
    def runnable(self) -> bool:
        return self.source is not None and not self.viewer

    @property
    def can_start_run(self) -> bool:
        return self.source is not None

    @property
    def run_state(self) -> str:
        return "running" if self.is_capturing else "idle"

    def start_run(self) -> bool:
        return self.capture()

    def pause_run(self) -> None:
        pass

    def stop_run(self) -> None:
        self.abort_capture()

    # ------------------------------------------------------- action markers
    @property
    def is_capturing(self) -> bool:
        return self._running_capture is not None or self.recording is not None

    def mark_action(self, text: str, device_time: Optional[float] = None) -> bool:
        """A marker for something set on a device while this document captures.

        In a live display it appears at once at the newest sample; otherwise it is placed when the
        capture completes, at the time since the start (marked with ``≈``: the start of a buffered
        capture is only known to the computer's clock). ``device_time``: the time of the action on
        the recorded instrument (exact for a monitor recording).
        """
        if self.recording is not None:
            sample = self.recording.sample_at(device_time) if device_time is not None else self.recording.count - 1
            self._recording_marks.append((max(sample, 0), text))
            if self._recording_session is not None:
                self.model.add_bookmark(max(sample, 0), text)
            return True
        if self._running_capture is None:
            return False
        stream_sample = shown = None
        if self._live_session is not None and self.model.is_live:
            # the newest block arrived a moment ago: the stream went on since
            count = self.model.sample_count
            ahead = int((time.monotonic() - self._live_arrival) * self._live_session.frequency)
            stream_sample = self._live_first + max(count - 1, 0) + max(ahead, 0)
            shown = self.model.add_bookmark(max(count - 1, 0), text)
        self._action_marks.append((stream_sample, time.monotonic(), text, shown))
        return True

    def _place_action_marks(self, session: CaptureSession, first_sample: int) -> None:
        marks, self._action_marks = self._action_marks, []
        count = self.model.sample_count
        if not marks or not count:
            return
        for stream_sample, stamp, text, shown in marks:
            if stream_sample is not None:
                sample, name = stream_sample - first_sample, text
            else:
                sample = int((stamp - self._capture_started) * session.frequency)
                name = f"≈ {text}"
            sample = min(max(sample, 0), count - 1)
            if shown is not None:
                self.model.remove_bookmark(shown)
            self.model.add_bookmark(sample, name)

    # ------------------------------------------------------ monitor record
    def record_monitor(self, instrument: Instrument, rate: float, pins: list[str],
                       analog: tuple[str, ...] = ()) -> bool:
        """Record the monitor of ``instrument`` as a slow capture into this document."""
        from ...core.monitor_recording import MonitorRecording

        monitor = instrument.monitor
        if monitor is None or self.is_capturing:
            return False
        self.recording = MonitorRecording(rate, pins, analog)
        self._recording_session = None
        self._recording_marks: list[tuple[int, str]] = []
        self._recording_monitor = monitor
        self._recording_stop = monitor.on_state(self.recording.add)
        try:
            monitor.start(rate, pins, analog)
        except Exception as error:  # noqa: BLE001 - shown to the user
            self._recording_stop()
            self.recording = self._recording_stop = None
            messages.error(self, "Monitor", "The monitor could not be started.", str(error))
            return False
        self._recording_timer.start()
        self.statusBar().showMessage(f"Recording the monitor of {instrument.name}...")
        self._update_actions()
        return True

    def _show_recording(self) -> None:
        recording = self.recording
        if recording is None or recording.count == 0:
            return
        model = self.model
        first = self._recording_session is None
        session = recording.session(self._recording_session)
        if first:
            self._recording_session = session
            model.set_session(session, live=True)
            for sample, name in self._recording_marks:
                model.add_bookmark(sample, name)
        else:
            model.extend_live(0)
            if session.analog_channels:
                model.notify_analog_changed()
        window = max(session.frequency * 10, 50)
        model.set_view(model.sample_count - window, window)
        self.statusBar().showMessage(
            f"Recording the monitor: {to_thousands(recording.count)} samples "
            f"({to_small_time(recording.count / session.frequency)})")

    def stop_recording(self) -> Optional[CaptureSession]:
        """End a monitor recording; the result stays in the document."""
        if self.recording is None:
            return None
        self._recording_timer.stop()
        self._recording_stop()
        try:
            self._recording_monitor.stop()
        except Exception:  # noqa: BLE001 - the device may be gone
            pass
        self._show_recording()
        recording, self.recording, self._recording_stop = self.recording, None, None
        session = recording.session(self._recording_session)
        bookmarks = list(self._recording_marks)
        if self._recording_session is None or not self.model.finish_live(session):
            self.load_session(session)
            for sample, name in bookmarks:
                self.model.add_bookmark(sample, name)
        self._recording_session = None
        self._dirty = True
        self._update_title()
        self._update_actions()
        self.statusBar().showMessage(f"Recorded {to_thousands(recording.count)} monitor samples", 5000)
        return session

    def _apply_live_extras(self, live: CaptureSession, args: CaptureProgressArgs) -> None:
        """Analog channels and state times of a stream while it runs."""
        for channel in live.analog_channels:
            raw = args.analog.get(channel.channel_number)
            if raw is not None:
                channel.raw = raw
                channel.length = None
        if args.state_times is not None:
            live.state_times = args.state_times
        if args.analog and not live.capture_channels:
            live.post_trigger_samples = live.sample_count()

    def _on_capture_completed(self, args: CaptureCompletedArgs) -> None:
        self._update_actions()
        was_live = self._live_session is not None
        live_first = self._live_first
        self._live_session = self._live_source = self._running_capture = None

        if not args.success:
            if was_live:
                self.model.set_session(None)
            self._pending_simulation = None
            self.show_capture_problem("The capture failed: no samples were received from the device.",
                                      (f"{args.error} " if args.error else "")
                                      + "Try again; if it fails again, reconnect the device.")
            self.statusBar().showMessage("Capture failed")
            return
        self.show_capture_problem("")

        self._loading += 1
        try:
            self.model.clear_regions()
        finally:
            self._loading -= 1
        self.current_file = None
        self._dirty = True
        self._update_title()
        aligned = ""
        if self.driver is not None and self.driver.board_count > 1:
            # Before loading, so the display and the decoders see the corrected samples.
            per_device = self.driver.channels_per_device
            self._remember_unaligned(args.session)
            options = alignment.alignment_options(args.session, per_device)
            if options:
                report = self._apply_alignment_choices(
                    args.session, per_device, {device: candidates[0] for device, candidates in options.items()}
                )
                if any(len(candidates) > 1 for candidates in options.values()):
                    report += " (Data > Align boards to choose another method)"
                aligned = "; " + report
        # Before loading: the capture change decodes with the new decoders once.
        self._apply_simulation_decoders(args.session)
        # After a live display the view stays where the user watched the samples arrive, and the
        # edge index of the live display is kept (a capture on disk is not read again).
        dropped = args.first_sample - live_first
        if was_live and self.model.finish_live(args.session, args.first_sample):
            if dropped:
                self.model.set_view(self.model.first_sample - dropped, self.model.visible_samples)
            self._sync_view_controls()
            self._update_info_panel()
            self._update_actions()
        else:
            self.load_session(args.session, reset_view=not was_live)
        if self.model.on_disk and self.provider.instances:
            self.decoder_manager.status_label.setText("Recorded to disk: press Decode now to decode it")
        self.statusBar().showMessage(
            f"Captured {to_thousands(args.session.total_samples)} samples "
            f"at {to_large_frequency(args.session.frequency)}{aligned}"
        )
        self._place_action_marks(args.session, args.first_sample)
        if args.error:
            # Samples arrived, but the capture ended early (a stream the link could not keep up with)
            self.show_capture_problem(
                f"The capture ended early after {to_thousands(args.session.total_samples)} samples.", args.error,
                "warning")

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
        if self.driver is not None and self.driver.board_count > 1:
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
        # Another capture: a state analysis shown before is no longer the way back
        self._timing_capture = None
        self._state_session = None
        if self.state_time_button.isChecked():
            self.state_time_button.blockSignals(True)
            self.state_time_button.setChecked(False)
            self.state_time_button.blockSignals(False)
        self._loading += 1
        try:
            self.model.set_session(session)
        finally:
            self._loading -= 1
        self._edited = False
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
        dialog = CaptureDialog(driver, self, decoder_configuration=self.provider.to_list(), accept_text="Next")
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

        self.model.clear_regions()
        self.current_file = None
        self._update_title()
        self.load_session(session)
        self._set_dirty(True)
        self.statusBar().showMessage("Created a new capture", 5000)

    def open_capture(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open capture", "", CAPTURE_FILE_FILTER)
        if path:
            self.open_capture_file(path)

    def open_capture_file(self, path: str) -> None:
        """Load a capture file, as the *Open* menu entry and the CLI do."""
        if self._file_busy or not self.confirm_replace("Open capture"):
            return

        def load() -> capture_io.ExportedCapture:
            if path.lower().endswith(".sr"):
                from ...core import sigrok_session

                return capture_io.ExportedCapture(sigrok_session.load_session(path), [])
            return capture_io.load_capture(path)

        name = os.path.basename(path)
        self._file_busy = True
        try:
            # in the background: a large file does not freeze the window while it is read
            exported = background.run(self, f"Opening {name}...", load)
        except background.Cancelled:
            return
        except Exception as error:  # noqa: BLE001 - whatever a damaged file raises is shown, not lost
            messages.error(self, "Open capture", f"{name} could not be opened.", str(error))
            return
        finally:
            self._file_busy = False

        if self.source is not None:
            self.detach_source(self.source)  # a file now, not the device's captures

        self.current_file = path
        # Save keeps the form the file has: one of the original software stays readable for it
        # (Save as chooses the form)
        self._save_compatible = bool(getattr(exported, "compatible", False))
        self._loading += 1
        try:
            self.model.set_regions(exported.regions)
        finally:
            self._loading -= 1
        self.load_session(exported.session)
        self._loading += 1  # what the file holds is not an edit
        try:
            self.model.set_bookmarks(exported.bookmarks)
            self.model.set_pinned_numbers(exported.pinned)
        finally:
            self._loading -= 1
        self._reset_undo()  # the state edits are compared with has the markers and pins
        self._dirty = False
        self._update_title()
        self.statusBar().showMessage(f"Opened {path}", 5000)

    def save_capture(self) -> None:
        """Save to the file the capture came from, or ask for a name."""
        if self.model.session is None:
            return
        if self.current_file and self.current_file.endswith((".lac", ".lac.gz")):
            self._write_capture(self.current_file, self._save_compatible)
        else:
            self.save_capture_as()

    def _too_large_to_copy(self, operation: str) -> bool:
        """A capture recorded to disk that does not fit into memory: exporting it as text (or
        saving it for the original software, a number per sample) would make a file of many
        gigabytes. Shows why and returns ``True`` then. (Saving as .lac packs the samples and
        reads them block by block: that works for any size.)"""
        session = self.model.session
        if session is None or not self.model.on_disk:
            return False
        size = self.model.sample_count * len(session.capture_channels)
        if size <= MAX_SAMPLE_BYTES:
            return False
        messages.warning(
            self,
            operation,
            f"This capture was recorded to disk and holds {to_thousands(size)} samples, too many "
            "to write as text.",
            f"Captures of up to {to_thousands(MAX_SAMPLE_BYTES)} samples over all channels can be "
            "exported; save it as a capture (.lac) or export a sigrok session instead.",
        )
        return True

    def save_capture_as(self) -> None:
        if self.model.session is None:
            return
        path, chosen = QFileDialog.getSaveFileName(
            self,
            "Save capture",
            self.current_file or "capture.lac",
            f"Captures (*.lac);;Compressed captures (*.lac.gz);;{COMPATIBLE_FILTER}",
        )
        if not path:
            return
        if not path.endswith((".lac", ".lac.gz")):
            # the ending of the type chosen in the dialog
            path += ".lac.gz" if chosen.startswith("Compressed") else ".lac"
        self._write_capture(path, compatible=chosen == COMPATIBLE_FILTER)

    def _write_capture(self, path: str, compatible: bool = False) -> None:
        """``compatible``: as the original LogicAnalyzer software writes its captures (a number
        per sample: large, but that software can open it)."""
        if self._file_busy or (compatible and self._too_large_to_copy("Save capture")) or \
                not self.wait_for_transfer("Save capture"):
            return
        session = self.model.session
        regions = list(self.model.regions)
        bookmarks = [(bookmark.sample, bookmark.name) for bookmark in self.model.bookmarks]
        shown_states = getattr(self, "_state_session", None)
        if shown_states is not None:
            # the states shown on their real time: the file holds the states (the capture), not
            # the picture of them
            session, regions, bookmarks = shown_states
        # What is written is the capture as it is now: the user may go on editing while it is
        # saved (edits replace the sample arrays, they never change them).
        snapshot = copy.copy(session)
        snapshot.capture_channels = [copy.copy(channel) for channel in session.capture_channels]
        snapshot.analog_channels = [copy.copy(channel) for channel in session.analog_channels]
        pinned = self.model.pinned_numbers()
        name = os.path.basename(path)
        saved_session, saved_step = self.model.session, self.undo.index()
        self._file_busy = True
        try:
            # (not to be cancelled: the file would be replaced later all the same)
            background.run(self, f"Saving {name}...",
                           lambda: capture_io.save_capture(path, snapshot, regions, bookmarks=bookmarks,
                                                           pinned=pinned, compatible=compatible),
                           cancellable=False)
        except (OSError, ValueError) as error:
            messages.error(self, "Save capture", f"{name} could not be saved.", str(error))
            return
        except background.Cancelled:
            return  # (cannot happen for a wait that cannot be cancelled; never as an error of a slot)
        finally:
            self._file_busy = False

        self.current_file = path
        self._save_compatible = compatible
        if self.model.session is not saved_session or self.undo.index() != saved_step:
            # While the file was written (the window stays alive), a capture arrived or the data
            # was edited: what is shown now is not what the file holds.
            self._update_title()
            self.statusBar().showMessage(f"Saved {path} (as it was when saving started)", 8000)
            return
        self._dirty = False
        self._edited = False
        self._save_count += 1
        self.undo.setClean()
        self._update_title()
        self.statusBar().showMessage(f"Saved {path}", 5000)

    def export_capture(self, kind: str) -> None:
        session = self.model.session
        # A sigrok session is written block by block, also from a capture on disk
        if session is None or (kind != "sr" and self._too_large_to_copy("Export")) or \
                not self.wait_for_transfer("Export"):
            return

        base = os.path.splitext(os.path.basename(self.current_file or "capture"))[0]
        if kind == "sr":
            from ...core import sigrok_session

            path, _ = QFileDialog.getSaveFileName(
                self, "Export as sigrok session", f"{base}.sr", "sigrok session (*.sr)"
            )
            exporter = sigrok_session.save_session
            extension = ".sr"
        elif kind == "csv":
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

        if self._file_busy:
            return
        self._file_busy = True
        try:
            background.run(self, f"Exporting {os.path.basename(path)}...", lambda: exporter(path, session),
                           cancellable=False)
        except (OSError, ValueError) as error:
            messages.error(self, "Export", f"{os.path.basename(path)} could not be written.", str(error))
            return
        except background.Cancelled:
            return
        finally:
            self._file_busy = False

        self.statusBar().showMessage(f"Exported {path}", 5000)

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

    def _on_selection(self, action) -> bool:
        """Run ``action(first, count)`` on the selection in the ruler (none: says how to make one)."""
        selection = self.sample_marker.selection
        if selection is None or selection.sample_count <= 0:
            self.statusBar().showMessage("Select samples first: drag in the ruler above the waveform", 5000)
            return False
        result = action(selection.start, selection.sample_count)
        return result is not False

    def _edit_position(self) -> int:
        selection = self.sample_marker.selection
        if selection is not None:
            return selection.start
        marker = self.model.user_marker if hasattr(self.model, "user_marker") else None
        return marker if marker is not None else self.model.first_sample

    def _paste_at_marker(self) -> None:
        if self.clipboard_samples is None:
            self.statusBar().showMessage("Nothing to paste: copy samples first", 5000)
            return
        self.paste_samples(self._edit_position())

    def copy_samples(self, first_sample: int, sample_count: int) -> bool:
        session = self.model.session
        if session is None:
            return False
        if first_sample < 0 or first_sample + sample_count > self.model.sample_count:
            self._selection_out_of_range()
            return False
        self.clipboard_samples = [
            channel.samples[first_sample : first_sample + sample_count].copy()
            if channel.samples is not None
            else np.zeros(sample_count, dtype=np.uint8)
            for channel in session.capture_channels
        ]
        self.sample_marker.has_clipboard = True
        self.statusBar().showMessage(f"Copied {to_thousands(sample_count)} samples", 5000)
        return True

    def cut_samples(self, first_sample: int, sample_count: int) -> None:
        if self.copy_samples(first_sample, sample_count):
            self.delete_samples(first_sample, sample_count)

    def delete_samples(self, first_sample: int, sample_count: int) -> None:
        session = self.model.session
        if session is None or sample_count <= 0:
            return
        total = self.model.sample_count
        if not 0 <= first_sample < total:
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

        self._loading += 1
        try:
            self._remap_regions_after_delete(first_sample, last_sample, sample_count)
        finally:
            self._loading -= 1
        self._after_samples_modified(first_sample, f"Delete {to_thousands(sample_count)} samples")
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
        if session is None:
            return

        # a file or test signals have no device that limits the length
        maximum = self.driver.get_limits(session.channel_numbers).max_total_samples if self.driver is not None \
            else INSERT_LIMIT
        available = max(maximum - self.model.sample_count, 1)
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
            # a channel without samples is as long as the others (low), so all stay equally long
            samples = channel.samples if channel.samples is not None else np.zeros(total, np.uint8)
            channel.samples = np.concatenate((samples[:sample], addition, samples[sample:]))

        if sample <= session.pre_trigger_samples:
            session.pre_trigger_samples += count
        session.post_trigger_samples = max(
            self.model.sample_count - session.pre_trigger_samples, 0
        )
        session.loop_count = 0
        session.measure_bursts = False
        session.bursts = None

        self._loading += 1
        try:
            self.model.set_regions(
                [
                    region.shifted(count) if region.start >= sample else region
                    for region in self.model.regions
                ]
            )
        finally:
            self._loading -= 1
        self._after_samples_modified(sample, f"Insert {to_thousands(count)} samples")
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

        for channel in dialog.shifted_channels:
            samples = channel.samples
            if samples is None or samples.size == 0:
                continue
            amount = min(dialog.shift_amount, samples.size)  # per channel: a short one limits only itself

            if dialog.direction == ShiftDirection.LEFT:
                head = samples[amount:]
                tail = self._fill_samples(dialog.mode, samples[:amount], amount)
                channel.samples = np.concatenate((head, tail))
            else:
                head = self._fill_samples(dialog.mode, samples[samples.size - amount :], amount)
                channel.samples = np.concatenate((head, samples[: samples.size - amount]))

        self._after_samples_modified(self.model.first_sample, "Shift channels")
        self.statusBar().showMessage(f"Shifted {len(dialog.shifted_channels)} channel(s)", 5000)

    @staticmethod
    def _fill_samples(mode: ShiftMode, rotated: np.ndarray, amount: int) -> np.ndarray:
        if mode == ShiftMode.HIGH:
            return np.ones(amount, dtype=np.uint8)
        if mode == ShiftMode.LOW:
            return np.zeros(amount, dtype=np.uint8)
        return rotated.copy()

    def use_names_for_device(self) -> bool:
        """The channel names of this data view for the next capture of its device."""
        if self.source is None or self.model.session is None:
            return False
        names = {channel.channel_number: channel.channel_name for channel in self.model.session.capture_channels}
        if self.source.use_channel_names(names):
            self.statusBar().showMessage(f"{self.source.name} names its channels like this from the next capture",
                                         6000)
            return True
        return False

    def _fill_hidden_menu(self) -> None:
        self.hidden_menu.clear()
        hidden = [channel for channel in self.model.channels if channel.hidden]
        self.hidden_menu.addAction("Show all channels", lambda: self.channel_viewer.show_all_channels())
        if hidden:
            self.hidden_menu.addSeparator()
        for channel in hidden:
            self.hidden_menu.addAction(f"Show {channel.display_name}", lambda channel=channel: self._show_channel(channel))

    def _show_channel(self, channel) -> None:
        channel.hidden = False
        self.model.notify_channels_changed()

    def _update_hidden_button(self) -> None:
        hidden = sum(1 for channel in self.model.channels if channel.hidden)
        self.show_all_button.setText(f"{hidden} hidden" if hidden else "Show all")
        self.show_all_button.setEnabled(bool(hidden))

    def _undo_moved(self, _index: int) -> None:
        if self.undo.isClean() and self._save_count and self.current_file:
            self._dirty = self._edited = False  # back to what was saved
        elif self.undo.index() or self.undo.count():
            self._dirty = True
        self.document_changed.emit()

    def _after_samples_modified(self, first_sample: int, text: str = "Edit samples") -> None:
        self._set_dirty(True)
        self.record_edit(text)
        self.model.rebuild_transitions()
        self.model.notify_capture_changed()
        self.model.set_view(first_sample, self.model.visible_samples)
        self.sample_marker.clear_selection()
        # (the decoders run again through capture_changed)

    # ------------------------------------------------------------------ misc
    def _update_actions(self) -> None:
        has_capture = self.model.session is not None and self.model.sample_count > 0
        source_capturing = self.source is not None and self.source.is_capturing
        capturing = source_capturing or self.recording is not None or self._running_capture is not None
        for action in getattr(self, "sample_actions", []):
            action.setEnabled(has_capture and not capturing)
        if hasattr(self, "action_use_names"):
            self.action_use_names.setEnabled(has_capture and self.source is not None)

        self.capture_controls.update()  # (Again needs a capture done before)
        self.abort_button.setEnabled(capturing)  # (a capture, or a recording of the monitor)
        self.action_repeat.setEnabled(self.source is not None and not capturing)
        self.action_stop.setEnabled(capturing)
        self.action_device_card.setEnabled(self.source is not None)
        self.save_button.setEnabled(has_capture)
        self.open_button.setEnabled(not capturing)
        for button in (self.search_tool_button, self.measure_tool_button):
            button.setEnabled(has_capture)

        self.action_save.setEnabled(has_capture)
        self.action_save_as.setEnabled(has_capture)
        for action in self._capture_actions:
            action.setEnabled(has_capture and not capturing)
        self.action_back_to_timing.setEnabled(self._timing_capture is not None and not capturing)
        self.action_align.setEnabled(has_capture and not capturing)
        self.action_export_csv.setEnabled(has_capture)
        self.action_export_vcd.setEnabled(has_capture)
        self.action_simulation.setEnabled(not capturing)
        self.fit_button.setEnabled(has_capture)
        self.trigger_button.setEnabled(has_capture)

        state_capture = self.model.session is not None and self.model.session.state_times is not None
        self.view_toolbar.set_available("real-time", state_capture or getattr(self, "_state_session", None) is not None)
        if not self.model.is_live:
            self.view_toolbar.set_available("roll", False)
        self.empty_primary_button.setEnabled(not capturing)
        # with a device: its card (where captures start), else the device list
        self.empty_primary_button.setText("Device card..." if self.source is not None else "Devices...")
        self.capture_stack.setCurrentIndex(1 if self.model.session is not None else 0)
        self._update_source_label()

    def _empty_page_primary_action(self) -> None:
        if self.source is not None:
            self.device_card_requested.emit(self.source.instrument)
        else:
            self.devices_requested.emit()

    def _update_info_panel(self) -> None:
        self._fill_info_panel()
        self._update_inspector()

    def _fill_info_panel(self) -> None:
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
        self.shutdown()
        super().closeEvent(event)
