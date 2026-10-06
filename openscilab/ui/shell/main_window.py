# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The lab shell: activity bar, sidebar, document tabs, inspector, console, command palette.

::

    ┌ header: project · Ctrl+K · Start/Stop/Pause · devices ───────────────────────────┐
    │ activity │ sidebar  │ documents (tab groups, splitters)        │ inspector       │
    │  bar     │          ├──────────────────────────────────────────┴─────────────────┤
    │          │          │ console: problems · execution · log · python               │
    └ status ──────────────────────────────────────────────────────────────────────────┘

Documents (:mod:`..documents`) bring their menus, actions and toolbar; the shell shows the menus
of the active document between *Edit* and *View* and passes *Save*, *Save as* and *Close* on.
"""

from __future__ import annotations

import logging
import html
import os
import threading
import webbrowser
from typing import Callable, Optional

from PySide6.QtCore import QByteArray, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QPushButton,
    QSizePolicy,
    QStatusBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

import openscilab

from ... import __version__
from ...core import fuzzy, preferences, recent, settings
from ...core.hub import EVENT_REMOVED, Hub
from ...core.instrument import Instrument, InstrumentStatus
from ...driver.base import DeviceConnectionError, FirmwareOutdatedError
from ...driver.simulated import follow_routes
from ...sigrok.provider import SigrokProvider
from .. import background, devices, messages
from ..devices.hub_bridge import HubBridge
from ..devices.simulated import fill_simulation_menu
from ..dialogs.device_dialogs import AboutDialog
from ..documents.dataview import DataView
from ..documents.base import Document
from ..documents.chart import view_document
from ..documents.device import DeviceDocument, status_icon
from ..documents.flow import FlowDocument, open_flow_file
from ..documents.panel import PanelDocument, open_panel_file
from ..documents.waveform import WAVE_EXTENSIONS, WaveformDocument, open_waveform_file
from ..flow.palette import NodePalette
from ..icons import app_icon, icon, logo_pixmap, set_icon
from ..theme import ACCENT, TEXT_MUTED, apply_palette, build_stylesheet, icon_px, set_role, set_variant, shell_stylesheet
from .activity_bar import ActivityBar
from .command_palette import (
    Command,
    CommandPalette,
    commands_from_actions,
    commands_from_menus,
    unique,
)
from .console import Console
from .console import Problem as ConsoleProblem
from .documents import DocumentArea
from .inspector import Inspector
from .sidebar import ProjectSection, SearchSection, Sidebar, data_kind
from .start_page import StartPage, native

log = logging.getLogger(__name__)

DOCUMENTATION_URL = "https://github.com/deckerjulian/openSciLab/wiki"
UPSTREAM_DOCUMENTATION_URL = "https://github.com/gusmanb/logicanalyzer/wiki"
SHELL_STATE_FILE = "shell-state.json"
LAYOUTS_FILE = "shell-layouts.json"
#: Version of the dock layout stored by the shell (a newer layout ignores older ones).
SHELL_LAYOUT_VERSION = 3  # 2: the node palette is a dock of its own; 3: the docks sized when shown
#: the width of the sidebar and of the inspector in a new layout (px)
DOCK_WIDTHS = (250, 260)
#: the width of the node palette when it shows beside the sidebar (px)
NODES_WIDTH = 230
CAPTURE_EXTENSIONS = (".lac", ".lac.gz", ".sr")
FLOW_EXTENSIONS = (".flow.yaml", ".flow.yml")
OPEN_FILE_FILTER = ("openSciLab files (*.lac *.lac.gz *.sr *.flow.yaml *.py *.panel.yaml *.wave.yaml *.sdl);;"
                    "Captures (*.lac *.lac.gz *.sr);;Flows (*.flow.yaml *.py);;Panels (*.panel.yaml);;"
                    "Waveforms (*.wave.yaml *.sdl);;All files (*)")
PANEL_EXTENSIONS = (".panel.yaml",)


class DevicesSection(QWidget):
    """The instruments open in the hub (with their status) and the devices that can be opened."""

    #: an open instrument was chosen (device card)
    instrument_activated = Signal(object)
    #: a device list entry was chosen (connect it)
    entry_activated = Signal(object)
    data_requested = Signal(object)
    disconnect_requested = Signal(object)
    #: disconnect every connected device (asked first)
    disconnect_all_requested = Signal()
    #: a device node for an open instrument in the active flow
    flow_node_requested = Signal(object)
    #: install the firmware on a board that cannot be used yet
    firmware_requested = Signal()
    #: the connected hardware with its firmware
    hardware_requested = Signal()

    def __init__(self, hub: Hub, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.hub = hub
        from .sections import Part, PartStack

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 4)
        layout.setSpacing(0)
        self.parts = PartStack(self)
        layout.addWidget(self.parts, 1)
        self.open_list = QListWidget(self)
        self.open_list.setFrameShape(QFrame.NoFrame)
        self.open_list.itemActivated.connect(lambda item: self.instrument_activated.emit(item.data(Qt.UserRole)))
        self.open_list.itemClicked.connect(lambda item: self.instrument_activated.emit(item.data(Qt.UserRole)))
        self.open_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.open_list.customContextMenuRequested.connect(self._open_menu)
        self.connected_part = self.parts.add(Part("connected", "Connected", self.open_list), 1)
        self.disconnect_all_button = self.connected_part.add_button(
            "unplug", "Disconnect all devices...", self.disconnect_all_requested.emit)
        self.list = QListWidget(self)
        self.list.setFrameShape(QFrame.NoFrame)
        self.list.setToolTip("Double-click to connect")
        self.list.itemActivated.connect(self._entry_activated)
        # the group of the simulators opens and closes with one click
        self.list.itemClicked.connect(lambda item: item.data(Qt.UserRole) == SIMULATOR_GROUP
                                      and self.toggle_simulators())
        self.available_part = self.parts.add(Part("available", "Available", self.list), 2)
        self.available_part.add_button("refresh", "Look for devices again", lambda: self.refresh())
        # boards that cannot be used yet (e.g. without the firmware of this application)
        self.notice = QLabel(self)
        self.notice.setWordWrap(True)
        self.notice.setContentsMargins(10, 4, 10, 4)
        set_role(self.notice, "warning")
        self.notice.setVisible(False)
        layout.addWidget(self.notice)
        from ..dialogs.common import Banner

        # why the last device could not be connected (instead of a dialog)
        self.error_banner = Banner("error", self)
        self.error_banner.setVisible(False)
        self.error_banner.button.clicked.connect(lambda: self.error_banner.setVisible(False))
        layout.addWidget(self.error_banner)
        self.firmware_button = QPushButton("Install firmware...", self)
        set_icon(self.firmware_button, "chip")
        self.firmware_button.clicked.connect(self.firmware_requested.emit)
        self.firmware_button.setVisible(False)
        layout.addWidget(self.firmware_button)
        buttons = QHBoxLayout()
        buttons.setContentsMargins(6, 4, 6, 0)
        self.hardware_button = QPushButton("Hardware...", self)
        set_icon(self.hardware_button, "chip")
        self.hardware_button.setToolTip("Every board and device with its firmware; install or update the firmware")
        self.hardware_button.clicked.connect(self.hardware_requested.emit)
        buttons.addWidget(self.hardware_button, 1)
        self.refresh_button = QPushButton("Refresh", self)
        set_icon(self.refresh_button, "refresh")
        self.refresh_button.clicked.connect(self.refresh)
        buttons.addWidget(self.refresh_button)
        layout.addLayout(buttons)
        self.refresh_open()

    def show_error(self, text: str) -> None:
        self.error_banner.set_message(html.escape(text), "Dismiss" if text else None)

    def _entry_activated(self, item: QListWidgetItem) -> None:
        entry = item.data(Qt.UserRole)
        if entry == SIMULATOR_GROUP:
            return  # (a click toggles it)
        if entry is not None:
            self.entry_activated.emit(entry)

    def toggle_simulators(self) -> None:
        """Open or close the group of the simulators."""
        preferences.update({"devices.simulators_open": not preferences.get("devices.simulators_open")})
        self.refresh()

    def _open_menu(self, position) -> None:
        item = self.open_list.itemAt(position)
        if item is None:
            return
        instrument = item.data(Qt.UserRole)
        menu = QMenu(self)
        menu.addAction(icon("devices"), "Device card", lambda: self.instrument_activated.emit(instrument))
        menu.addAction(icon("nodes"), "Add to the flow", lambda: self.flow_node_requested.emit(instrument))
        if instrument.capture is not None:
            menu.addAction(icon("channels"), "Show data", lambda: self.data_requested.emit(instrument))
        menu.addSeparator()
        menu.addAction(icon("unplug"), "Disconnect", lambda: self.disconnect_requested.emit(instrument))
        if len(self.hub) > 1:
            menu.addAction(icon("unplug"), "Disconnect all devices...", self.disconnect_all_requested.emit)
        menu.exec(self.open_list.mapToGlobal(position))

    def refresh_open(self) -> None:
        self.open_list.clear()
        for instrument in self.hub.instruments():
            item = QListWidgetItem(status_icon(instrument.status), instrument.name, self.open_list)
            item.setToolTip(f"{instrument.kind} · {instrument.status.value}\n{instrument.uri}")
            item.setData(Qt.UserRole, instrument)
        if not len(self.hub):
            item = QListWidgetItem("No device connected", self.open_list)
            item.setFlags(Qt.NoItemFlags)
        self.disconnect_all_button.setVisible(len(self.hub) > 0)
        self.connected_part.set_count(len(self.hub) or None)

    def refresh(self) -> None:
        self.refresh_open()
        self.list.clear()
        open_uris = {instrument.uri for instrument in self.hub.instruments()}
        detected, manual = [], []
        show_simulators = preferences.get("devices.show_simulators")
        for backend in devices.backends():
            try:
                detected += backend.detected()
                manual += backend.manual_entries()
            except Exception:  # noqa: BLE001 - one kind of device must not hide the others
                log.debug("The devices of %s cannot be listed", backend.id, exc_info=True)
                continue
        detected = [entry for entry in detected if devices.entry_uri(entry) not in open_uris]
        for entry in detected:
            item = QListWidgetItem(icon("chip"), entry.label, self.list)
            item.setData(Qt.UserRole, entry)
            item.setToolTip("Double-click to connect")
        if not detected:
            item = QListWidgetItem("No device detected", self.list)
            item.setFlags(Qt.NoItemFlags)
        # every simulated device is in the group of the simulators, whatever backend lists it
        simulated = [entry for entry in manual if entry.simulated] if show_simulators else []
        for entry in manual:
            if entry.simulated:
                continue
            item = QListWidgetItem(icon("plus"), entry.label, self.list)
            item.setData(Qt.UserRole, entry)
        if simulated:
            opened = bool(preferences.get("devices.simulators_open"))
            group = QListWidgetItem(icon("down" if opened else "arrow-right"), f"Simulators ({len(simulated)})",
                                    self.list)
            group.setData(Qt.UserRole, SIMULATOR_GROUP)
            group.setToolTip("Simulated devices: click to show or hide them")
            font = group.font()
            font.setBold(True)
            group.setFont(font)
            for entry in simulated if opened else []:
                item = QListWidgetItem(icon("plus"), "    " + entry.label.removeprefix("Simulation: ").removeprefix(
                    "Simulated ").replace(" (firmware protocol)", ", firmware protocol"), self.list)
                item.setData(Qt.UserRole, entry)
                item.setToolTip(entry.label)
        notice = None
        for backend in devices.backends():
            try:
                notice = backend.idle_notice()
            except Exception:  # noqa: BLE001 - enumeration problems must not disturb the list
                notice = None
            if notice:
                break
        self.notice.setText(notice or "")
        self.notice.setVisible(bool(notice))
        self.firmware_button.setVisible(bool(notice))


#: the item of the device list that opens and closes the simulators
SIMULATOR_GROUP = "simulators"


def drives_outputs(instrument: Instrument) -> bool:
    """Whether a pin of ``instrument`` is an output or PWM now."""
    from ...core.instrument import MODE_OUTPUT, MODE_PWM, InstrumentError

    gpio = instrument.gpio
    if gpio is None:
        return False
    try:
        return any(gpio.mode(pin.name) in (MODE_OUTPUT, MODE_PWM) for pin in gpio.pins())
    except InstrumentError:
        return False


def serial_ports() -> frozenset:
    """The serial ports of the computer (cheap enough to ask every few seconds)."""
    try:
        from ...driver.pico import detector

        return frozenset(port.device for port in detector.comports())
    except Exception:  # noqa: BLE001 - no pyserial, no permission: nothing to watch
        return frozenset()


def ports_of(instrument: Instrument) -> list[str]:
    """The serial ports an instrument uses (``pico:/dev/a`` or ``pico:/dev/a,/dev/b``); none over the network."""
    if not instrument.uri.startswith("pico:"):
        return []
    rest = instrument.uri.split(":", 1)[1]
    return [port for port in rest.split(",") if port.startswith(("/dev/", "COM"))]


def device_node_id(instrument: Instrument) -> str:
    """A node id for an instrument (``uno`` for *Simulation: Arduino Uno* at ``sim:uno``)."""
    import re

    base = instrument.uri.split(":", 1)[-1].split("/")[-1] if instrument.uri else instrument.name
    return re.sub(r"[^A-Za-z0-9_]", "_", base).strip("_") or "device"


class ShellWindow(QMainWindow):
    """The main window of openSciLab."""

    #: the serial ports of the computer, read in a thread (see poll_ports)
    _ports_read = Signal(object)
    #: a remote device connected or went: (event, name), from the server's thread
    _remote_event = Signal(str, str)

    def __init__(self, decoder_paths: tuple[str, ...] = (), restore_state: bool = True) -> None:
        super().__init__()
        from .. import accessibility

        accessibility.install()  # icon-only buttons get names for screen readers
        from ... import plugins

        plugins.load(ui=True)  # devices of plugins: before the device list asks for them
        self.setWindowIcon(app_icon())
        self.resize(1440, 900)
        apply_palette(self)
        self.setStyleSheet(self._window_stylesheet())
        self.setDockNestingEnabled(True)
        # Sidebar and inspector reach the bottom; the console sits below the documents only.
        self.setCorner(Qt.BottomLeftCorner, Qt.LeftDockWidgetArea)
        self.setCorner(Qt.BottomRightCorner, Qt.RightDockWidgetArea)

        #: One decoder registry for every data view.
        self.provider = SigrokProvider()
        if decoder_paths:
            self.provider.registry.search_paths = list(decoder_paths) + [
                path for path in self.provider.registry.search_paths if path not in decoder_paths
            ]
        # flows decode with the same decoders (and the same decoder folders) as the data views
        from ...lab.nodes import decode as decode_nodes

        decode_nodes.use_registry(self.provider.registry)
        #: Every instrument of the lab, open at the same time.
        self.hub = Hub()
        #: folders of projects opened from a template that were not saved yet (in the temp folder)
        self.temporary_projects: set[str] = set()
        # trigger routes between simulated instruments are wired in their circuits
        self._stop_following_routes = follow_routes(self.hub)
        self.hub_bridge = HubBridge(self.hub, self)
        self._document_menu_actions: list[QAction] = []
        self._document_file_actions: list[QAction] = []
        self._palette: Optional[CommandPalette] = None

        self.area = DocumentArea(self)
        self.area.empty_action.connect(self._empty_action)
        self.area.confirm_close = self._confirm_close
        self.area.needs_confirm = self._last_of_temporary
        self.area.global_actions = lambda: [self.action_save, self.action_save_as, self.action_close,
                                            self.action_undo, self.action_redo, self.action_palette,
                                            self.action_open, self.action_new_flow, self.action_settings]
        self.setCentralWidget(self.area)

        self._build_header()
        self._build_activity_and_sidebar()
        self._build_inspector()
        self._build_console()
        self._build_status()
        self._build_menus()
        self._fill_header_tools()
        self._remote_event.connect(self._remote_device_event)
        self.apply_remote_server()

        self.area.active_changed.connect(self._on_active_changed)
        self.area.document_updated.connect(self._sync_document_state)
        self.area.documents_changed.connect(self._on_documents_changed)
        self.hub_bridge.changed.connect(self._on_hub_event)

        self.start_page: Optional[StartPage] = None
        self._default_state = self.saveState(SHELL_LAYOUT_VERSION)
        #: the docks take their widths when the window is shown (before, the sidebar gets the room of
        #: the hidden node palette as well); not when a layout of the user was restored
        self._docks_sized = bool(restore_state and self._restore_state())
        self._on_active_changed(None)
        self._update_title()

    # ================================================================ build
    def _build_header(self) -> None:
        """The header: its widgets now, its entries once the menus exist (:meth:`_fill_header_tools`)."""
        from ..widgets.toolbar import AdaptiveToolBar

        bar = AdaptiveToolBar("header", "Header", self)
        bar.setObjectName("header-bar")
        bar.setIconSize(QSize(icon_px(14), icon_px(14)))
        self.logo = self._logo(bar)
        self.palette_button = QPushButton(f"Search commands...   {native('Ctrl+K')}", bar)
        set_icon(self.palette_button, "command")
        self.palette_button.setToolTip(f"Search the commands, files and nodes ({native('Ctrl+K')})")
        self.palette_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.palette_button.setMaximumWidth(360)
        self.palette_button.clicked.connect(self.show_command_palette)
        # Run controls of the active flow (enabled by flow documents, step 0c/0d).
        self.run_button = QPushButton("Start", bar)
        set_variant(self.run_button, "primary")
        set_icon(self.run_button, "play")
        self.pause_button = QPushButton("Pause", bar)
        set_variant(self.pause_button, "tool")
        set_icon(self.pause_button, "pause")
        self.pause_button.setToolTip("Pause")
        self.stop_button = QPushButton("Stop", bar)
        set_variant(self.stop_button, "danger")
        set_icon(self.stop_button, "stop")
        self.stop_button.setToolTip("Stop")
        for button in (self.run_button, self.pause_button, self.stop_button):
            button.setEnabled(False)
        self.run_button.clicked.connect(lambda: self._run_command("start_run"))
        self.pause_button.clicked.connect(lambda: self._run_command("pause_run"))
        self.stop_button.clicked.connect(lambda: self._run_command("stop_run"))
        self.header = bar
        self.addToolBar(Qt.TopToolBarArea, bar)

    def _logo(self, parent: QWidget) -> QWidget:
        """The mark and the name of openSciLab."""
        widget = QWidget(parent)
        widget.setObjectName("logo")
        widget.setToolTip(f"openSciLab {__version__}")
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(2, 0, 4, 0)
        layout.setSpacing(6)
        mark = QLabel(widget)
        mark.setPixmap(logo_pixmap(28, self.devicePixelRatioF() or 1.0))
        layout.addWidget(mark)
        name = QLabel(widget)
        name.setTextFormat(Qt.RichText)
        name.setText(f"<span style='color:{TEXT_MUTED}; font-weight:400'>open</span>"
                     f"<span style='color:{ACCENT}; font-weight:700'>Sci</span>"
                     f"<span style='font-weight:700'>Lab</span>")
        set_role(name, "heading")
        layout.addWidget(name)
        return widget

    def _build_activity_and_sidebar(self) -> None:
        self.activity_bar = ActivityBar(self)
        self.activity_bar.add_section("project", "Project", "folder", "Ctrl+Shift+E")
        self.activity_bar.add_section("devices", "Devices", "devices", "Ctrl+Shift+D")
        self.activity_bar.add_section("search", "Search", "search", "Ctrl+Shift+F")
        self.action_start_page = QAction(icon("home"), "Start", self)
        self.action_start_page.setToolTip("The start page: templates, recent files")
        self.action_start_page.triggered.connect(self.show_start_page)
        self.activity_bar.add_command("start", self.action_start_page)
        self.action_activity_settings = QAction(icon("gear"), "Settings", self)
        self.action_activity_settings.setToolTip("Settings")
        self.action_activity_settings.triggered.connect(lambda: self.show_preferences())
        self.activity_bar.add_command("settings", self.action_activity_settings)
        self.addToolBar(Qt.LeftToolBarArea, self.activity_bar)

        self.sidebar = Sidebar(self)
        self.project_section = ProjectSection(self.sidebar)
        self.project_section.document_activated.connect(self.area.activate)
        self.project_section.file_requested.connect(self.open_file)
        self.project_section.compare_requested.connect(self.compare_captures)
        self.project_section.export_requested.connect(self.export_capture_csv)
        self.sidebar.add_section("project", "Project", self.project_section)
        self.devices_section = DevicesSection(self.hub, self.sidebar)
        #: serial ports seen last: unplugged devices are marked, new ones listed
        self._ports: Optional[frozenset] = None
        self.port_timer = QTimer(self)
        self.port_timer.setInterval(int(preferences.get("devices.refresh_s") * 1000))
        self.port_timer.timeout.connect(self.poll_ports)
        self._ports_read.connect(self.watch_ports)
        self._ports_thread: Optional[threading.Thread] = None
        self.port_timer.start()
        self.devices_section.instrument_activated.connect(self.open_device_card)
        self.devices_section.entry_activated.connect(self.connect_entry)
        self.devices_section.data_requested.connect(self.show_data)
        self.devices_section.disconnect_requested.connect(self.disconnect_instrument)
        self.devices_section.disconnect_all_requested.connect(self.disconnect_all)
        self.sidebar.add_section("devices", "Devices", self.devices_section)
        # the node palette: beside the flow that is edited (see _update_nodes_dock), not a section
        self.nodes_section = NodePalette(parent=self)
        self.nodes_section.node_requested.connect(self._add_node_to_active_flow)
        self.devices_section.flow_node_requested.connect(self.add_instrument_to_flow)
        self.devices_section.firmware_requested.connect(self.show_hardware)
        self.devices_section.hardware_requested.connect(self.show_hardware)
        self._refresh_device_presets()
        self.search_section = SearchSection(self._search, self.sidebar)
        self.sidebar.add_section("search", "Search", self.search_section)

        self.sidebar_dock = QDockWidget("Sidebar", self)
        self.sidebar_dock.setObjectName("sidebar-dock")
        self.sidebar_dock.setWidget(self.sidebar)
        self.sidebar_dock.setTitleBarWidget(QWidget(self.sidebar_dock))
        self.sidebar_dock.setFeatures(QDockWidget.DockWidgetClosable)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.sidebar_dock)

        self.activity_bar.section_selected.connect(self._show_section)
        self.activity_bar.sidebar_toggled.connect(self.sidebar_dock.setVisible)
        self.activity_bar.select("project")

        self.nodes_dock = QDockWidget("Nodes", self)
        self.nodes_dock.setObjectName("nodes-dock")
        nodes_page = QWidget(self.nodes_dock)
        nodes_layout = QVBoxLayout(nodes_page)
        nodes_layout.setContentsMargins(0, 0, 0, 0)
        nodes_layout.setSpacing(0)
        nodes_title = QLabel("NODES", nodes_page)
        set_role(nodes_title, "sidebar-title")
        nodes_title.setToolTip("Drag a node onto the flow, or double-click it (Ctrl+Shift+N shows or hides the palette)")
        nodes_layout.addWidget(nodes_title)
        self.nodes_section.setParent(nodes_page)
        nodes_layout.addWidget(self.nodes_section, 1)
        self.nodes_dock.setWidget(nodes_page)
        self.nodes_dock.setTitleBarWidget(QWidget(self.nodes_dock))
        self.nodes_dock.setFeatures(QDockWidget.NoDockWidgetFeatures)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.nodes_dock)
        self.splitDockWidget(self.sidebar_dock, self.nodes_dock, Qt.Horizontal)
        self.nodes_dock.setVisible(False)
        self.action_nodes = QAction(icon("nodes"), "Node palette", self)
        self.action_nodes.setCheckable(True)
        self.action_nodes.setChecked(bool(preferences.get("flow.show_nodes")))
        self.action_nodes.setShortcut("Ctrl+Shift+N")
        self.action_nodes.setToolTip("The palette of nodes beside a flow that is edited (Ctrl+Shift+N)")
        self.action_nodes.toggled.connect(self._nodes_palette_toggled)
        self.addAction(self.action_nodes)

    def _nodes_palette_toggled(self, shown: bool) -> None:
        preferences.update({"flow.show_nodes": bool(shown)})
        self._update_nodes_dock(self.active_document())

    def _update_nodes_dock(self, document: Optional[QWidget]) -> None:
        """The node palette shows beside a flow that is edited: its graph in front, in this window."""
        editing = isinstance(document, FlowDocument) and document.current_view() == "Graph" \
            and not self.area.is_detached(document)
        wanted = editing and self.action_nodes.isChecked()
        if self.nodes_dock.isVisible() != wanted:
            sidebar = self.sidebar_dock.width()
            self.nodes_dock.setVisible(wanted)
            if wanted and self.sidebar_dock.isVisible():  # (beside the sidebar, which keeps its width)
                QTimer.singleShot(0, lambda: self.resizeDocks([self.sidebar_dock, self.nodes_dock],
                                                              [sidebar, NODES_WIDTH], Qt.Horizontal))

    def poll_ports(self) -> None:
        """Read the serial ports in a thread (listing them takes up to a fifth of a second on
        some systems: every few seconds that was a stutter of the window); :meth:`watch_ports`
        gets them."""
        if self._ports_thread is not None and self._ports_thread.is_alive():
            return

        def read() -> None:
            ports = serial_ports()
            try:
                self._ports_read.emit(ports)
            except RuntimeError:  # the window was closed meanwhile
                pass

        self._ports_thread = threading.Thread(target=read, name="openscilab-ports", daemon=True)
        self._ports_thread.start()

    def watch_ports(self, ports: Optional[frozenset] = None) -> None:
        """Mark open devices whose port went away as disconnected; list new boards."""
        ports = serial_ports() if ports is None else ports
        if ports == self._ports:
            return
        first = self._ports is None
        self._ports = ports
        for instrument in self.hub.instruments():
            used = ports_of(instrument)
            if used and any(port not in ports for port in used) and instrument.status != InstrumentStatus.DISCONNECTED:
                self.hub.set_status(instrument.name, InstrumentStatus.DISCONNECTED, "unplugged")
                self.statusBar().showMessage(f"{instrument.name} was unplugged", 8000)
        if not first and self.sidebar_dock.isVisible() and self.sidebar.current_key == "devices":
            self.devices_section.refresh()

    def _show_section(self, key: str) -> None:
        self.sidebar.show_section(key)
        if key == "devices":
            self.devices_section.refresh()
        elif key == "project":
            self.project_section.refresh_recent()
        elif key == "search":
            self.search_section.input.setFocus()

    def _build_inspector(self) -> None:
        self.inspector = Inspector(self)
        self.inspector_dock = QDockWidget("Inspector", self)
        self.inspector_dock.setObjectName("inspector-dock")
        self.inspector_dock.setWidget(self.inspector)
        self.inspector_dock.setTitleBarWidget(QWidget(self.inspector_dock))
        self.addDockWidget(Qt.RightDockWidgetArea, self.inspector_dock)

    def _build_console(self) -> None:
        namespace = {
            "shell": self,
            "openscilab": openscilab,
            "documents": lambda: self.area.documents(),
            "active": lambda: self.active_document(),
        }
        self.console = Console(namespace, self)
        self.console_dock = QDockWidget("Console", self)
        self.console_dock.setObjectName("console-dock")
        self.console_dock.setWidget(self.console)
        self.addDockWidget(Qt.BottomDockWidgetArea, self.console_dock)
        self.resizeDocks([self.console_dock], [170], Qt.Vertical)
        # The console starts closed; Ctrl+J opens it.
        self.console_dock.setVisible(False)
        self.console.problems.changed.connect(self._update_problem_count)
        self.console.problems.problem_activated.connect(self.show_problem)

    def _build_status(self) -> None:
        status = QStatusBar(self)
        self.mode_label = QLabel("Real time", status)
        status.addPermanentWidget(self.mode_label)
        self.devices_label = QLabel("No devices", status)
        status.addPermanentWidget(self.devices_label)
        self.saved_label = QLabel("", status)
        status.addPermanentWidget(self.saved_label)
        # errors and warnings of the open flows; a click shows them
        self.problems_button = QPushButton("", status)
        self.problems_button.setFlat(True)
        self.problems_button.setToolTip("Problems of the open flows (click to show them)")
        self.problems_button.clicked.connect(lambda: self.show_console("Problems"))
        self.problems_button.setVisible(False)
        status.insertPermanentWidget(0, self.problems_button)
        self.setStatusBar(status)
        status.messageChanged.connect(lambda text: text and self.console.message(text))

    def _action(self, text: str, slot: Callable, shortcut=None, icon_name: Optional[str] = None,
                checkable: bool = False) -> QAction:
        action = QAction(text, self)
        if shortcut is not None:
            if isinstance(shortcut, (list, tuple)):
                action.setShortcuts([QKeySequence(item) for item in shortcut])
            else:
                action.setShortcut(QKeySequence(shortcut))
        if icon_name:
            action.setIcon(icon(icon_name))
        action.setCheckable(checkable)
        action.triggered.connect(lambda _checked=False: slot())
        return action

    def _build_menus(self) -> None:
        menu_bar = self.menuBar()

        # ---------------------------------------------------------- Project
        self.project_menu = menu_bar.addMenu("&Project")
        self.new_menu = self.project_menu.addMenu(icon("file-plus"), "&New")
        self.action_new_data_view = self._action("&Data view", self.new_data_view, icon_name="channels")
        self.new_menu.addAction(self.action_new_data_view)
        self.action_new_flow = self._action("&Flow", self.new_flow, QKeySequence.New, icon_name="nodes")
        self.new_menu.addAction(self.action_new_flow)
        self.action_new_waveform = self._action("&Waveform", self.new_waveform, icon_name="wave")
        self.new_menu.addAction(self.action_new_waveform)
        self.action_new_panel = self._action("&Panel", self.new_panel, icon_name="panel")
        self.new_menu.addAction(self.action_new_panel)
        self.action_new_project = self._action("New pro&ject...", self.new_project_dialog,
                                               icon_name="file-plus")
        self.project_menu.addAction(self.action_new_project)
        self.action_open = self._action("&Open file...", self.open_file_dialog, QKeySequence.Open, "folder")
        self.project_menu.addAction(self.action_open)
        self.action_open_project = self._action("Open pro&ject...", self.open_project_dialog, "Ctrl+Shift+O",
                                                "folder")
        self.project_menu.addAction(self.action_open_project)
        self.recent_menu = self.project_menu.addMenu("Open &recent")
        self.recent_menu.aboutToShow.connect(self._rebuild_recent_menu)
        self._rebuild_recent_menu()
        self.project_menu.addSeparator()
        self.action_save = self._action("&Save", self.save_active, QKeySequence.Save, "save")
        self.project_menu.addAction(self.action_save)
        self.action_save_as = self._action("Save &as...", self.save_active_as, "Ctrl+Shift+S", "save")
        self.project_menu.addAction(self.action_save_as)
        self.action_save_project = self._action("Save &project as...", lambda: self.save_project_as(), icon_name="folder")
        self.project_menu.addAction(self.action_save_project)
        self.action_save_template = self._action("Save project as &template...", self.save_as_template,
                                                 icon_name="bookmark")
        self.project_menu.addAction(self.action_save_template)
        self.action_close = self._action("&Close document", self.close_active, QKeySequence("Ctrl+W"), "close")
        self.project_menu.addAction(self.action_close)
        self._document_section = self.project_menu.addSeparator()
        self._document_section_end = self.project_menu.addSeparator()
        self.action_settings = self._action("Se&ttings...", self.show_preferences, QKeySequence.Preferences, "gear")
        self.action_settings.setMenuRole(QAction.PreferencesRole)  # macOS: openSciLab → Settings…
        if self.action_settings.shortcut().isEmpty():
            self.action_settings.setShortcut(QKeySequence("Ctrl+,"))
        self.project_menu.addAction(self.action_settings)
        self.action_exit = self._action("E&xit", self.close, QKeySequence.Quit, "exit")
        self.project_menu.addAction(self.action_exit)

        # ------------------------------------------------------------- Edit
        self.edit_menu = menu_bar.addMenu("&Edit")
        self.action_undo = self._action("&Undo", self._undo, QKeySequence.Undo)
        self.action_redo = self._action("&Redo", self._redo, QKeySequence.Redo)
        self.edit_menu.addAction(self.action_undo)
        self.edit_menu.addAction(self.action_redo)
        self.edit_menu.addSeparator()
        self.action_palette = self._action("&Command palette...", self.show_command_palette,
                                           ["Ctrl+K", "Ctrl+Shift+P"], "command")
        self.edit_menu.addAction(self.action_palette)

        # ------------------------------------------------------------- View
        self.view_menu = menu_bar.addMenu("&View")
        self.action_sidebar = self._action("&Sidebar", self.toggle_sidebar, "Ctrl+B", checkable=True)
        self.action_sidebar.setChecked(True)
        self.view_menu.addAction(self.action_sidebar)
        self.action_inspector = self._action("&Inspector", self.toggle_inspector, "Ctrl+Alt+I", "inspector", True)
        self.action_inspector.setChecked(True)
        self.view_menu.addAction(self.action_inspector)
        self.action_console = self._action("C&onsole", self.toggle_console, "Ctrl+J", "terminal", True)
        self.view_menu.addAction(self.action_console)
        self.sidebar_dock.visibilityChanged.connect(lambda _v: self.action_sidebar.setChecked(self.sidebar_dock.isVisible()))
        self.inspector_dock.visibilityChanged.connect(lambda _v: self.action_inspector.setChecked(self.inspector_dock.isVisible()))
        self.console_dock.visibilityChanged.connect(lambda _v: self.action_console.setChecked(self.console_dock.isVisible()))
        sections = self.view_menu.addMenu("Side&bar section")
        for key in self.activity_bar.keys():
            sections.addAction(self.activity_bar.section_action(key))
        self.view_menu.addAction(self.action_nodes)
        self.view_menu.addSeparator()
        self.view_menu.addAction(self._action("Customize the &header...", lambda: self.header.customize(),
                                              icon_name="gear"))
        self.view_menu.addAction(self._action(
            "Customize the &activity bar...",
            lambda: self.activity_bar.context_menu().exec(self.activity_bar.mapToGlobal(self.activity_bar.rect().center())),
            icon_name="gear"))
        self.view_menu.addSeparator()
        self.action_next_tab = self._action("Next &tab", lambda: self._cycle_tab(1), "Ctrl+Tab")
        self.action_previous_tab = self._action("Pre&vious tab", lambda: self._cycle_tab(-1), "Ctrl+Shift+Tab")
        self.view_menu.addAction(self.action_next_tab)
        self.view_menu.addAction(self.action_previous_tab)
        self.action_split_right = self._action("Split &right", lambda: self._split(Qt.Horizontal), "Ctrl+\\", "split")
        self.action_split_down = self._action("Split &down", lambda: self._split(Qt.Vertical))
        self.action_next_group = self._action("Move to the next &group", self._move_to_next_group)
        self.action_detach = self._action("Open in a new &window", self._detach, icon_name="detach")
        for action in (self.action_split_right, self.action_split_down, self.action_next_group, self.action_detach):
            self.view_menu.addAction(action)
        self.view_menu.addSeparator()
        self.layout_menu = self.view_menu.addMenu(icon("layout"), "&Layout")
        self.layout_menu.aboutToShow.connect(self._rebuild_layout_menu)
        self._rebuild_layout_menu()
        self.view_menu.addAction(self.action_start_page)

        # ---------------------------------------------------------- Devices
        self.devices_menu = menu_bar.addMenu("De&vices")
        self.action_connect = self._action("&Connect...", self.connect_dialog, "Ctrl+Shift+K", icon_name="plug")
        self.devices_menu.addAction(self.action_connect)
        self.devices_menu.addAction(self._action("Show the &device list", lambda: self.activity_bar.select("devices"),
                                                 icon_name="devices"))
        self.devices_menu.addSeparator()
        # the open devices: their card, their data, disconnecting them (rebuilt when the menu opens)
        self.card_menu = self.devices_menu.addMenu(icon("devices"), "Device &card")
        self.data_menu_devices = self.devices_menu.addMenu(icon("channels"), "Show &data")
        self.disconnect_menu = self.devices_menu.addMenu(icon("unplug"), "D&isconnect")
        self.action_disconnect_all = self._action("Disconnect &all devices...", self.disconnect_all,
                                                  icon_name="unplug")
        self.devices_menu.addAction(self.action_disconnect_all)
        self.devices_menu.aboutToShow.connect(self._fill_device_menus)
        self._fill_device_menus()
        self.devices_menu.addSeparator()
        self.action_hardware = self._action("Connected &hardware", self.show_hardware, icon_name="chip")
        self.devices_menu.addAction(self.action_hardware)
        self.action_update_firmware = self._action("Install or &update firmware...", self.show_hardware,
                                                   icon_name="chip")
        self.devices_menu.addAction(self.action_update_firmware)
        self.devices_menu.addAction(self._action("&Forget multi device sets...", self.forget_known_devices,
                                                 icon_name="trash"))
        self.simulation_menu = self.devices_menu.addMenu(icon("chip"), "Simulate &faults")
        self.simulation_menu.aboutToShow.connect(lambda: fill_simulation_menu(self.simulation_menu, self.hub))
        fill_simulation_menu(self.simulation_menu, self.hub)

        # ------------------------------------------------------------- Help
        # --------------------------------------------------------- Templates
        # every project of the library, a sub menu per category; one opens as a copy to start from
        self.examples_menu = menu_bar.addMenu("&Templates")
        self._build_examples_menu()

        self.help_menu = menu_bar.addMenu("&Help")
        self.help_menu.addAction(self._action("&Keyboard shortcuts...", self.show_shortcuts, icon_name="keyboard"))
        self.help_menu.addAction(self._action("Online &documentation",
                                              lambda: webbrowser.open(DOCUMENTATION_URL), icon_name="book"))
        self.help_menu.addAction(self._action("Online documentation of the &original software (gusmanb)",
                                              lambda: webbrowser.open(UPSTREAM_DOCUMENTATION_URL), icon_name="book"))
        self.help_menu.addAction(self._action("Decoder search &paths...", self.show_decoder_paths, icon_name="folder"))
        self.help_menu.addAction(self._action("P&lugins...", self.show_plugins, icon_name="folder"))
        self.help_menu.addSeparator()
        self.help_menu.addAction(self._action("&About openSciLab", lambda: AboutDialog(self).exec(), icon_name="info"))

    # ============================================================ documents
    def documents(self) -> list[QWidget]:
        return self.area.documents()

    def active_document(self) -> Optional[QWidget]:
        return self.area.active_document()

    def add_document(self, document: QWidget, activate: bool = True) -> QWidget:
        if isinstance(document, FlowDocument):
            self._wire_flow_document(document)
        elif isinstance(document, DataView):
            document.play_requested.connect(self.new_waveform)
            document.device_card_requested.connect(self.open_device_card)
            document.devices_requested.connect(lambda: self.activity_bar.select("devices"))
        elif isinstance(document, PanelDocument):
            document.flow_provider = self.open_flow_document
            document.flow_requested.connect(self.show_document_or_file)
            document.execution_line.connect(self.console.execution.add)
            document.run_state_changed.connect(self._run_state_changed)
            document.fast_box.toggled.connect(lambda _checked: self._update_run_buttons())
        self.area.add_document(document, activate=activate)
        return document

    def decoders(self):
        """The decoders of a new data view: its own list, the decoder registry shared by all."""
        from ...sigrok.provider import SigrokProvider

        return SigrokProvider(self.provider.registry)

    def show_document_or_file(self, target) -> Optional[QWidget]:
        """Show ``target``: an open document, or a file (opened unless it is open already)."""
        if isinstance(target, str):
            return self.open_file(target)
        if target in self.area.documents():
            self.area.activate(target)
        else:
            self.add_document(target)
        return target

    def open_flow_document(self, path: str) -> Optional[FlowDocument]:
        """The open (top level) flow document of the file ``path``, if any."""
        absolute = os.path.realpath(path)
        for document in self.area.documents():
            if isinstance(document, FlowDocument) and document.parent_document is None and document.path \
                    and os.path.realpath(document.path) == absolute:
                return document
        return None

    def new_flow(self, flow=None, activate: bool = True) -> FlowDocument:
        """A new flow document (an empty flow, or ``flow``); the node palette shows beside it."""
        document = FlowDocument(flow, hub=self.hub)
        self.add_document(document, activate=activate)
        return document

    def _wire_flow_document(self, document: FlowDocument) -> None:
        def problems(found) -> None:
            self.console.problems.set_problems(
                str(id(document)), [ConsoleProblem(problem.severity, str(problem), document.title,
                                                   problem.node, document) for problem in found])

        document.problems_changed.connect(problems)
        document.console_requested.connect(self.show_console)
        document.message.connect(lambda text: self.statusBar().showMessage(text, 6000))
        document.execution_line.connect(self.console.execution.add)
        document.view_requested.connect(lambda node, kind, value, options, doc=document: self.show_view(
            doc, node, kind, value, options))
        document.open_requested.connect(lambda child: self.area.activate(child) if child in self.area.documents()
                                        else self.add_document(child))
        document.run_state_changed.connect(self._run_state_changed)
        document.fast_box.toggled.connect(lambda _checked: self._update_run_buttons())
        document.panel_requested.connect(lambda doc=document: self.new_panel(flow_document=doc))
        document.open_panels = lambda: [doc for doc in self.area.documents() if isinstance(doc, PanelDocument)]
        document.panel_open_requested.connect(self.show_document_or_file)
        document.data_requested.connect(lambda key, title, session, front, doc=document: self.show_run_data(
            doc, key, title, session, front))
        document.destroyed.connect(lambda *_args, key=str(id(document)): self.console.problems.set_problems(key, []))
        problems(document.problems())

    def _add_node_to_active_flow(self, text: str) -> Optional[str]:
        """Add a node (a type, or a preset of :func:`~..flow.canvas.encode_node`) to the active flow."""
        from ..flow.canvas import decode_node

        type_name, node_id, params = decode_node(text)
        document = self.active_document()
        if not isinstance(document, FlowDocument):
            document = self.new_flow()
        return document.add_node(type_name, document._center(), node_id, **params)

    def add_instrument_to_flow(self, instrument: Instrument) -> Optional[str]:
        """A device node for an instrument of the device list in the active flow."""
        from ...lab.model import DEVICE_NODE
        from ..flow.canvas import encode_node

        return self._add_node_to_active_flow(encode_node(DEVICE_NODE, device_node_id(instrument),
                                                         {"address": instrument.uri}))

    def device_presets(self) -> list:
        """The devices the node palette offers: open instruments, the project's devices, simulators."""
        from ...driver.simulated.profiles import available_profiles, load_profile
        from ..flow.palette import DevicePreset

        presets = [DevicePreset(f"{instrument.name} (open)", device_node_id(instrument), instrument.uri, "open")
                   for instrument in self.hub.instruments() if instrument.uri]
        project = getattr(self.active_document(), "project", None)
        if project is not None:
            presets += [DevicePreset(f"{name} (project: {address})", name, "", "project")
                        for name, address in project.devices.items()]
        if preferences.get("devices.show_simulators"):
            for name, title in _simulator_titles(tuple(available_profiles()), load_profile):
                presets.append(DevicePreset(title, name, f"sim:{name}", "simulator"))
        return presets

    def _refresh_device_presets(self) -> None:
        self.nodes_section.set_device_presets(self.device_presets())

    def _update_problem_count(self) -> None:
        if not hasattr(self, "problems_button"):
            return
        errors, warnings = self.console.problems.counts()
        parts = ([f"✕ {errors}"] if errors else []) + ([f"⚠ {warnings}"] if warnings else [])
        self.problems_button.setText("  ".join(parts))
        self.problems_button.setVisible(bool(parts))

    def show_problem(self, problem) -> None:
        """Where a problem is: its flow document, the node selected and in view."""
        document = getattr(problem, "owner", None)
        if document is None or document not in self.area.documents():
            return
        self.area.activate(document)
        node = getattr(problem, "node", "")
        if node and isinstance(document, FlowDocument) and node in document.scene.nodes:
            document.set_view("Graph")
            document.scene.select_nodes([node])
            document.view.centerOn(document.scene.nodes[node])

    def show_console(self, tab: str = "Problems") -> None:
        self.console_dock.setVisible(True)
        self.console.show_tab(tab)

    def _run_command(self, name: str) -> None:
        """The buttons in the header: the main action of the active document (run a flow or panel,
        capture with a device card, capture again in a data view)."""
        document = self.active_document()
        if getattr(document, "runnable", False):
            getattr(document, name)()
            if isinstance(document, (FlowDocument, PanelDocument)):
                self.show_console("Execution")
        self._update_run_buttons()

    def _update_run_buttons(self) -> None:
        document = self.active_document()
        runnable = bool(getattr(document, "runnable", False))
        state = getattr(document, "run_state", "idle") if runnable else "idle"
        running = state not in ("idle", "finished", "stopped", "error")
        can_start = getattr(document, "can_start_run", True) if runnable else False
        self.run_button.setEnabled(runnable and can_start and (not running or state == "paused"))
        self.run_button.setText("Continue" if state == "paused" else (getattr(document, "run_label", "Start")
                                                                       if runnable else "Start"))
        self.header.schedule()  # (the text changed: the header lays itself out again)
        self.run_button.setToolTip(getattr(document, "run_tooltip", "Run the active flow or panel")
                                   if runnable else "Run the active flow or panel")
        self.pause_button.setEnabled(runnable and running and state != "paused"
                                     and getattr(document, "can_pause", True))
        self.stop_button.setEnabled(runnable and running)
        # the time the active flow or panel runs in
        fast = getattr(document, "fast_box", None) if runnable else None
        self.mode_label.setVisible(fast is not None)
        if fast is not None:
            self.mode_label.setText("Virtual time" if fast.isChecked() else "Real time")

    def show_view(self, source: QWidget, node: str, kind: str, value, options: dict) -> Optional[QWidget]:
        """Show what a view node of a running flow sends: the scope in a data view, charts and
        values in documents of their own (one per node, updated while the flow runs)."""
        key = (id(source), node)
        documents = getattr(self, "_view_documents", {})
        self._view_documents = documents
        document = documents.get(key)
        if document is not None and document not in self.area.documents():
            document = None
        if kind == "scope":
            created = document is None
            if created:
                document = DataView(provider=self.decoders(), hub=self.hub)
                document.set_viewer()  # the flow sends the data: nothing to capture here
                documents[key] = document
                self.add_document(document, activate=False)
            title = options.get("title") or node
            renamed, document.display_name = document.display_name != title, title
            # more of the same signal: the part the user zoomed to stays where it is
            document.load_session(value.to_session(), reset_view=created)
            if created or renamed:
                document._update_title()
            return document
        if document is None:
            document = view_document(kind, options.get("title") or node)
            if document is None:
                return None  # checks and report sections have no view
            documents[key] = document
            self.add_document(document, activate=False)
        document.show_value(value, options)
        return document

    def show_run_data(self, source: FlowDocument, key: str, title: str, session, front: bool = True) -> DataView:
        """A data view with run data of ``source`` (one per ``key``: the whole run, a node); it is
        updated in place – the part the user zoomed to stays – while it is open."""
        documents = getattr(self, "_run_data_documents", {})
        self._run_data_documents = documents
        document = documents.get((id(source), key))
        created = document is None or document not in self.area.documents()
        if created:
            document = DataView(provider=self.decoders(), hub=self.hub)
            document.set_viewer()  # the run data of the flow: nothing to capture here
            documents[(id(source), key)] = document
            document.destroyed.connect(lambda *_args, doc=source, key=key: self._run_data_closed(doc, key))
            self.add_document(document)
        document.display_name = title
        document.load_session(session, reset_view=created)
        if created:  # the whole run at first
            document.model.set_view(0, document.model.sample_count)
            document._sync_view_controls()
        document._update_title()
        if front and not created:
            self.area.activate(document)
        return document

    def _run_data_closed(self, source: FlowDocument, key: str) -> None:
        self._run_data_documents.pop((id(source), key), None)
        try:
            source.data_view_closed(key)
        except RuntimeError:  # the flow document is gone too
            pass

    def new_data_view(self) -> DataView:
        """A new, empty data view."""
        return self.add_document(DataView(provider=self.decoders(), hub=self.hub))

    def new_panel(self, panel=None, flow_path: Optional[str] = None, flow_document=None) -> PanelDocument:
        """A new panel for the flow in ``flow_path``, of ``flow_document`` or of the active flow
        document – saved or not. A panel made for a flow starts with the widgets it suggests."""
        from ...lab import panel_model
        from ...lab.panel_model import Panel

        active = self.active_document()
        if flow_path is None and flow_document is None and isinstance(active, FlowDocument):
            flow_document = active
        while flow_document is not None and flow_document.parent_document is not None:
            flow_document = flow_document.parent_document  # a subflow: the flow it belongs to
        if flow_path is None and flow_document is not None and flow_document.path \
                and not flow_document.path.endswith(".py"):
            flow_path = flow_document.path
        if panel is None:
            name = flow_document.flow.name if flow_document is not None and flow_document.flow.name != "Flow" else "Panel"
            panel = Panel(name=name, flow=os.path.abspath(flow_path) if flow_path else "")
            if flow_document is not None:
                panel.widgets = panel_model.suggest(flow_document.flow, flow_document.registry, panel.width)
                panel.grow_to_fit()
        document = self.add_document(PanelDocument(panel, hub=self.hub, flow_document=flow_document))
        if flow_document is not None:
            self.statusBar().showMessage(
                f"A panel for {flow_document.title}" + (f" with {len(panel.widgets)} suggested widgets"
                                                        if panel.widgets else ""), 6000)
        return document

    def new_waveform(self, waveform=None) -> WaveformDocument:
        """A waveform document (a sine, or ``waveform``, e.g. a capture to play)."""
        return self.add_document(WaveformDocument(self.hub, waveform))

    def show_start_page(self) -> StartPage:
        if self.start_page is not None and self.start_page in self.area.documents():
            self.area.activate(self.start_page)
            return self.start_page
        page = StartPage()
        page.example_requested.connect(self.open_example)
        page.file_requested.connect(self.open_file)
        page.project_requested.connect(self.open_project)
        page.open_file_requested.connect(self.open_file_dialog)
        page.simulator_requested.connect(self.try_simulator)
        page.connect_requested.connect(self.connect_dialog)
        page.new_flow_requested.connect(self.new_flow)
        page.open_project_requested.connect(self.open_project_dialog)
        page.template_delete_requested.connect(self.delete_template)
        page.destroyed.connect(lambda: setattr(self, "start_page", None))
        self.start_page = page
        self.add_document(page)
        return page

    def open_data_view_of_device(self) -> "DataView":
        """A data view that captures with the connected device (asked which when there are several;
        without one the device list shows) - the ``data`` of a project's ``open``."""
        document = self.new_data_view()
        capturing = [instrument for instrument in self.hub.instruments() if instrument.capture is not None]
        if not capturing:
            self.activity_bar.select("devices")
            self.statusBar().showMessage("Connect a device in the device list; the data view captures with it", 8000)
            return document
        chosen = capturing[0] if len(capturing) == 1 else self.choose_capture_device(capturing)
        if chosen is not None:
            document.use_instrument(chosen)
        return document

    def _build_examples_menu(self) -> None:
        from ...lab import examples

        self.examples_menu.clear()
        self.example_actions: dict[str, QAction] = {}
        self.examples_menu.addAction(self.action_save_template)
        catalog = examples.catalog()
        if not catalog:
            self.examples_menu.addAction("No templates found").setEnabled(False)
            return
        self.examples_menu.addAction(icon("home"), "All templates (start page)", self.show_start_page)
        self.examples_menu.addSeparator()
        for category in catalog:
            submenu = self.examples_menu.addMenu(category.title)
            submenu.setToolTipsVisible(True)
            submenu.setToolTip(category.description)
            for example in category.examples:
                action = submenu.addAction(example.title)
                action.setToolTip(example.description)
                action.setStatusTip(example.description)
                action.triggered.connect(lambda _checked=False, key=example.key: self.open_example(key))
                self.example_actions[example.key] = action
            if category.key == examples.USER_CATEGORY:  # (the user's own: they can be deleted)
                submenu.addSeparator()
                delete_menu = submenu.addMenu(icon("trash"), "Delete")
                for example in category.examples:
                    delete_menu.addAction(example.title).triggered.connect(
                        lambda _checked=False, key=example.key: self.delete_template(key))
        self.examples_menu.setToolTipsVisible(True)

    def save_as_template(self) -> Optional[str]:
        """*Save project as template…*: the project of the active document becomes a template of the
        user (``lab/examples.save_template``), listed first under *Templates*; returns its key."""
        from ...lab import examples
        from ...lab.project import Project
        from ..dialogs.template_dialog import TemplateDialog

        title = "Save project as template"
        root = self.project_root_of(self.active_document())
        if root is None:
            messages.info(self, title, "The active document is not part of a project.",
                          "Open a project, or start one from a template, and save it as a template of your own.")
            return None
        documents = [document for document in self.area.documents() if self.project_root_of(document) == root]
        changed = [document for document in documents if document.dirty and document.path]
        if changed and not messages.confirm(
                self, title, f"{len(changed)} document(s) of the project have changes that are not saved.",
                "Save and continue", "A template is made of the files of the project: the changes are saved first."):
            return None
        if not all(document.save() for document in changed):
            return None
        try:
            project = Project.open(root)
        except (OSError, ValueError) as error:
            messages.error(self, title, f"{root} could not be read.", str(error))
            return None
        dialog = TemplateDialog(project.name, str(project.extra.get("description") or ""), self)
        if not dialog.exec():
            return None
        try:
            template = examples.save_template(root, dialog.title, dialog.description,
                                              [document.path for document in documents if document.path])
        except (OSError, ValueError) as error:
            messages.error(self, title, "The template could not be saved.", str(error))
            return None
        self._templates_changed()
        self.statusBar().showMessage(f'Template "{template.title}" saved: Templates → My templates', 8000)
        return template.key

    def delete_template(self, key: str) -> bool:
        """Delete a template of the user (after asking)."""
        from ...lab import examples

        example = examples.find(key)
        if example is None or not example.user:
            return False
        if not messages.confirm(self, "Delete template", f'Delete the template "{example.title}"?', "Delete",
                                "Projects made from it stay as they are. This cannot be undone.", destructive=True):
            return False
        try:
            examples.delete_template(key)
        except (OSError, ValueError) as error:
            messages.error(self, "Delete template", "The template could not be deleted.", str(error))
            return False
        self._templates_changed()
        return True

    def _templates_changed(self) -> None:
        self._build_examples_menu()
        if self.start_page is not None:
            self.start_page.load_library()

    def open_example(self, key: str, folder: Optional[str] = None) -> Optional[QWidget]:
        """Open the example ``key`` (``<category>/<example>``) as a copy: a temporary project
        unless ``folder`` is given; the example in the library stays as it is."""
        from ...lab import examples

        example = examples.find(key)
        if example is None:
            messages.warning(self, "Templates", f"The template '{key}' is not available.")
            return None
        return self._open_project_copy(example.path, example.name, folder, example.title)

    def _open_project_copy(self, source: str, name: str, folder: Optional[str], what: str) -> Optional[QWidget]:
        """Copy the project ``source`` (of the library) and open its files and views."""
        from ...lab import templates

        temporary = folder is None
        if temporary:
            # A temporary project at first: Save asks where to keep it (Project → Save project as).
            import tempfile

            folder = templates.unique_folder(tempfile.mkdtemp(prefix="openscilab-"), name)
        try:
            root = templates.copy_project(source, folder)
        except OSError as error:
            messages.error(self, what, "The project could not be created.", str(error))
            return None
        if temporary:
            self.temporary_projects.add(os.path.abspath(root))
            self.statusBar().showMessage("A temporary project: saving asks where to keep it", 8000)
        else:
            recent.add(root, "projects")
        opened = None
        for path in templates.files_to_open(root):
            document = self.open_file(path)
            opened = opened or document
        for view in templates.views_to_open(root):
            if view == "data":
                opened = opened or self.open_data_view_of_device()
        if opened is not None:
            self.area.activate(opened)
        return opened

    def choose_capture_device(self, instruments: list) -> Optional[Instrument]:
        """Which connected device a new data view captures with (``None``: without a device)."""
        from PySide6.QtWidgets import QInputDialog

        none = "No device (open a capture file)"
        names = [instrument.name for instrument in instruments] + [none]
        name, accepted = QInputDialog.getItem(self, "Logic Analyzer", "Capture with:", names, 0, False)
        if not accepted or name == none:
            return None
        return instruments[names.index(name)]

    def open_project(self, root: str) -> Optional[QWidget]:
        """Open the files of a project (those its ``project.yaml`` lists in ``open``, else its panels
        and flows)."""
        from ...lab import templates
        from ...lab.project import Project

        paths = templates.files_to_open(root)
        if not paths:
            try:
                project = Project.open(root)
            except (OSError, ValueError) as error:
                messages.error(self, "Open project", f"{root} could not be opened.", str(error))
                return None
            paths = project.panels() or project.flows()[:3]
        opened = None
        for path in paths:
            opened = opened or self.open_file(path)
        if opened is None:
            messages.info(self, "Open project", f"{os.path.basename(root.rstrip(os.sep))} has no flow or panel to open.",
                          "Add one with Project → New, then save it into the project's flows or panels folder.")
            return None
        recent.add(root, "projects")
        return opened

    def new_project_dialog(self) -> Optional[QWidget]:
        """*Project → New project…*: the start page, with the projects of the library to start from."""
        page = self.show_start_page()
        page.filter_edit.setFocus()
        return page

    def open_project_dialog(self) -> Optional[QWidget]:
        """*Project → Open project…*: a folder with a ``project.yaml``."""
        root = QFileDialog.getExistingDirectory(self, "Open project", preferences.default_folder())
        if not root:
            return None
        if not os.path.exists(os.path.join(root, "project.yaml")):
            messages.warning(self, "Open project", f"{os.path.basename(root)} is not an openSciLab project.",
                             "A project folder has a project.yaml; Project → New project from template creates one.")
            return None
        return self.open_project(root)

    def open_file_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open file", "", OPEN_FILE_FILTER)
        if path:
            self.open_file(path)

    def open_file(self, path: str) -> Optional[QWidget]:
        """Open ``path`` in the document that suits it; an open document is shown instead."""
        absolute = os.path.abspath(path)
        for document in self.area.documents():
            if document.path and os.path.abspath(document.path) == absolute:
                self.area.activate(document)
                return document
        lowered = path.lower()
        if lowered.endswith(FLOW_EXTENSIONS) or (lowered.endswith(".py") and _is_flow_script(path)):
            from ...lab.model import FlowError
            from ...lab.project import Project

            root = Project.find(path)
            project = Project.open(root) if root else None
            try:
                document = open_flow_file(path, registry=project.registry() if project else None, hub=self.hub,
                                          project=project)
            except (FlowError, OSError, SyntaxError) as error:
                messages.error(self, "Open flow", f"{os.path.basename(path)} could not be opened.", str(error))
                return None
            self.add_document(document)
            self._remember(path)
            return document
        if lowered.endswith(PANEL_EXTENSIONS):
            from ...lab.panel_model import PanelError

            try:
                document = open_panel_file(path, self.hub)
            except (PanelError, OSError) as error:
                messages.error(self, "Open panel", f"{os.path.basename(path)} could not be opened.", str(error))
                return None
            self.add_document(document)
            self._remember(path)
            return document
        if lowered.endswith(WAVE_EXTENSIONS):
            from ...core.waveform import WaveformError

            try:
                document = open_waveform_file(path, self.hub)
            except (WaveformError, OSError, ValueError) as error:
                messages.error(self, "Open waveform", f"{os.path.basename(path)} could not be opened.", str(error))
                return None
            self.add_document(document)
            self._remember(path)
            return document
        if lowered.endswith(CAPTURE_EXTENSIONS):
            active = self.active_document()
            if isinstance(active, DataView) and active.model.session is None and not active.dirty:
                document = active
            else:
                document = self.new_data_view()
            document.open_capture_file(path)
            if document.path is None:  # could not be opened (reported by the document)
                if document is not active and document.model.session is None:
                    self.area.close_document(document, force=True)
                return None
            self._remember(path)
            return document
        if data_kind(path) is not None:
            # tables, logs and reports open in the application the system has for them
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.abspath(path)))
            return None
        messages.warning(self, "Open file", f"{os.path.basename(path)} is not a file openSciLab can open.",
                         "Supported: captures (.lac, .lac.gz, .sr), flows (.flow.yaml, Python scripts with a flow) "
                         "panels (.panel.yaml) and waveforms (.wave.yaml, .sdl).")
        return None

    # ----------------------------------------------------------------- data
    @staticmethod
    def data_folder(document) -> Optional[str]:
        """The ``data/`` folder of the project of ``document`` (or next to its file, or the scratch
        folder of an unsaved flow)."""
        project = getattr(document, "project", None)
        if project is not None:
            return project.data_dir
        path = getattr(document, "path", None)
        if path is None and getattr(document, "document_kind", "") == "flow":
            return document.data_dir
        if path:
            candidate = os.path.join(os.path.dirname(os.path.abspath(path)), "data")
            if os.path.isdir(candidate):
                return candidate
        return None

    def compare_captures(self, capture: str, reference: str):
        """Open ``capture`` and compare it with ``reference`` (the compare dialog of the analyzer)."""
        from ..dialogs.compare_dialog import CompareDialog

        document = self.open_file(capture)
        if not isinstance(document, DataView):
            return None
        dialog = CompareDialog(document.model, document)
        dialog.set_reference_file(reference)
        dialog.compare()
        dialog.show()
        return dialog

    def export_capture_csv(self, path: str, target: Optional[str] = None) -> Optional[str]:
        from ...core import capture_io

        if target is None:
            target, _ = QFileDialog.getSaveFileName(self, "Export as CSV", os.path.splitext(path)[0] + ".csv",
                                                    "CSV files (*.csv)")
            if not target:
                return None
        def export() -> None:
            if path.lower().endswith(".sr"):
                from ...core import sigrok_session

                session = sigrok_session.load_session(path)
            else:
                session = capture_io.load_capture(path).session
            capture_io.export_csv(target, session, include_time=True)

        try:
            background.run(self, f"Exporting {os.path.basename(path)}...", export, cancellable=False)
        except Exception as error:  # noqa: BLE001 - whatever a damaged file raises is shown
            messages.error(self, "Export as CSV", f"{os.path.basename(path)} could not be exported.", str(error))
            return None
        self.project_section.refresh_data()
        return target

    # -------------------------------------------------------------- devices
    def _fill_device_menus(self) -> None:
        instruments = self.hub.instruments()
        for menu, slot, needs_capture in ((self.card_menu, self.open_device_card, False),
                                          (self.data_menu_devices, self.show_data, True),
                                          (self.disconnect_menu, self.disconnect_instrument, False)):
            menu.clear()
            usable = [instrument for instrument in instruments if not needs_capture or instrument.capture is not None]
            for instrument in usable:
                menu.addAction(instrument.name, lambda instrument=instrument, slot=slot: slot(instrument))
            menu.setEnabled(bool(usable))
        self.action_disconnect_all.setEnabled(bool(instruments))

    def connect_dialog(self) -> Optional[Instrument]:
        """*Devices → Connect…*: choose any device (detected, by hand, simulator) and connect it."""
        from ..dialogs.connect_dialog import ConnectDialog

        dialog = ConnectDialog(self, frozenset(instrument.uri for instrument in self.hub.instruments()))
        if not dialog.exec() or dialog.entry is None:
            return None
        return self.connect_entry(dialog.entry)

    def try_simulator(self, profile: str = "free") -> Optional[Instrument]:
        """Connect the simulator ``profile`` (start page: *Try a simulator*)."""
        from ..devices import DeviceEntry
        from ..devices.simulated import BACKEND_ID

        backend = next((backend for backend in devices.backends() if backend.id == BACKEND_ID), None)
        entry = next((entry for entry in (backend.manual_entries() if backend else []) if entry.value == profile),
                     None)
        if entry is None:
            entry = DeviceEntry(BACKEND_ID, "profile", profile, profile)
        existing = next((instrument for instrument in self.hub.instruments()
                         if instrument.uri == devices.entry_uri(entry)), None)
        if existing is not None:
            self.open_device_card(existing)
            return existing
        return self.connect_entry(entry)

    def connect_entry(self, entry) -> Optional[Instrument]:
        """Open the device of a device list entry in the hub and show its device card."""
        backend = next((backend for backend in devices.backends() if backend.id == entry.backend), None)
        if backend is None:
            return None
        try:
            instrument = backend.open_instrument(entry, self)
        except FirmwareOutdatedError as error:
            where = f" on {error.location}" if error.location else ""
            if messages.confirm(self, "Firmware update needed",
                                f"The device{where} needs the firmware of this version of openSciLab.",
                                "Update firmware...",
                                f"{error} The update takes a few seconds; the capture settings and profiles are kept."):
                self.show_hardware()  # lists the board with the update that fits it
            return None
        except background.Cancelled:
            self.statusBar().showMessage("Connecting was cancelled", 5000)
            return None
        except (DeviceConnectionError, OSError, ValueError) as error:
            self.activity_bar.select("devices")
            self.devices_section.show_error(f"{entry.label or entry.value} could not be connected: {error}")
            self.statusBar().showMessage("The device could not be connected", 8000)
            return None
        if instrument is None:
            return None
        self.devices_section.show_error("")
        self.hub.add(instrument)
        self.open_device_card(instrument)
        return instrument

    def device_card(self, instrument: Instrument) -> Optional[DeviceDocument]:
        for document in self.area.documents():
            if isinstance(document, DeviceDocument) and document.instrument is instrument:
                return document
        return None

    def open_device_card(self, instrument: Instrument) -> Optional[DeviceDocument]:
        if instrument is None:
            return None
        document = self.device_card(instrument)
        if document is not None:
            self.area.activate(document)
            return document
        document = DeviceDocument(instrument, self.hub, controller=self.capture_controller(instrument))
        document.data_requested.connect(self.show_data)
        document.disconnect_requested.connect(self.disconnect_instrument)
        document.reconnect_requested.connect(self.reconnect_instrument)
        document.action_performed.connect(self.mark_action)
        document.record_requested.connect(self.record_monitor)
        document.stimulus_requested.connect(self.stimulus_and_capture)
        document.firmware_requested.connect(self.update_device_firmware)
        document.example_requested.connect(self.open_example)
        document.settings_requested.connect(self.show_preferences)
        return self.add_document(document)

    def show_hardware(self):
        """The connected hardware with its firmware (one document)."""
        from ..documents.hardware import HardwareDocument

        for document in self.area.documents():
            if isinstance(document, HardwareDocument):
                self.area.activate(document)
                document.refresh()
                return document
        document = HardwareDocument(self.hub)
        document.open_requested.connect(self.connect_entry)
        document.card_requested.connect(self.open_device_card)
        document.firmware_requested.connect(self.update_firmware)
        return self.add_document(document)

    def update_device_firmware(self, instrument: Instrument):
        """*Update firmware* of the device card: the fitting image for UF2 boards (connected hardware),
        the update tool of the device otherwise."""
        from ...core import firmware

        driver = instrument.capture.driver if instrument.capture is not None else None
        method = firmware.update_method(driver) if driver is not None else None
        if method is not None and method.key == "uf2":
            return self.show_hardware().install_for(instrument)
        return self.update_firmware(instrument)

    def forget_known_devices(self) -> bool:
        """Drop the stored multi device sets so autodetect asks again."""
        from ..devices import pico as pico_devices

        known = pico_devices.known_device_sets()
        if not known:
            messages.info(self, "Multi device sets", "No multi device set has been registered yet.")
            return False
        if not messages.confirm(self, "Forget multi device sets",
                                f"Forget {len(known)} registered multi device set(s)?", "Forget",
                                "Autodetect asks again the next time several analyzers are connected.",
                                destructive=True):
            return False
        pico_devices.forget_known_device_sets()
        self.statusBar().showMessage("Registered multi device sets removed", 5000)
        return True

    def update_firmware(self, instrument: Optional[Instrument] = None) -> Optional[str]:
        """*Update firmware* of ``instrument`` (or of a board that is not open)."""
        from ..dialogs.firmware_dialog import update_firmware

        if instrument is not None:
            driver = instrument.capture.driver if instrument.capture is not None else None
            if driver is not None and driver.is_capturing:
                messages.info(self, "Update firmware", f"{instrument.name} is capturing; stop it first.")
                return None

        def release() -> None:
            if instrument is not None and instrument in self.hub:
                self.hub.remove(self.hub.name_of(instrument))

        port = update_firmware(self, instrument, release)
        self.devices_section.refresh()
        return port

    def mark_action(self, instrument: Instrument, text: str) -> int:
        """Put a marker for an action on ``instrument`` into every capture running now."""
        stamp = None
        model = getattr(instrument, "simulated_driver", None)
        if model is not None:
            stamp = model.clock()
        marked = 0
        for document in self.area.documents():
            if isinstance(document, DataView) and document.mark_action(f"{instrument.name}: {text}", stamp):
                marked += 1
        if marked:
            self.statusBar().showMessage(f"Marker: {instrument.name}: {text}", 3000)
        return marked

    def stimulus_and_capture(self, capture: Instrument, source: Instrument, pin: str, width: float) -> bool:
        """Arm a capture with ``capture`` (any instrument of the hub), then pulse ``pin`` of ``source``."""
        controller = self.capture_controller(capture)
        card = self.device_card(source)

        def pulse() -> None:
            if card is not None:
                card.pulse(pin, width)  # confirmed when it was requested; reported as a marker
            else:
                source.gpio.pulse(pin, width)

        if controller is None or not controller.arm_and_then(pulse):
            messages.info(self, "Stimulus and capture",
                          f"{capture.name} cannot start a capture now.",
                          "Capture once with it (or set the toolbar's settings) first.")
            return False
        return True

    def record_monitor(self, instrument: Instrument) -> Optional[DataView]:
        """Record the monitor of ``instrument`` into a new data view."""
        card = self.device_card(instrument)
        rate = card.monitor_rate.value() if card is not None else 20
        pins = [pin for pin in instrument.pins() if pin.usable]
        digital = [pin.name for pin in pins if "DIN" in pin.capabilities]
        analog = tuple(pin.name for pin in pins if "ADC" in pin.capabilities)
        document = self.new_data_view()
        if not document.record_monitor(instrument, rate, digital, analog):
            return None
        if card is not None:
            card.sync_monitor()
        return document

    def show_data(self, instrument: Instrument, beside: bool = False) -> DataView:
        """The data view of ``instrument``'s captures (a new one when no view shows them).
        ``beside``: a capture started on the device card, whose data view opens next to the card
        (both stay visible) and the card keeps the focus."""
        controller = self.capture_controller(instrument)
        view = controller.view if controller is not None else None
        if view is not None and view in self.area.documents():
            if beside:
                self.area.reveal(view)
            else:
                self.area.activate(view)
            return view
        active = self.active_document()
        empty = [document for document in reversed(self.area.documents())
                 if isinstance(document, DataView) and document.source is None and document.model.session is None]
        if active in empty:
            document = active
        elif empty:
            document = empty[0]  # e.g. the data view of the Logic Analyzer template
        else:
            document = self.new_data_view()
        document.use_instrument(instrument)
        card = self.device_card(instrument)
        if beside and card is not None and not self.area.is_detached(card) and \
                self.area.group_of(card) is self.area.group_of(document):
            self.area.split(document)
            self.area.activate(card)
            self.area.reveal(document)
        elif beside:
            self.area.reveal(document)
        else:
            self.area.activate(document)
        return document

    def capture_controller(self, instrument: Instrument):
        """The capture controller of ``instrument``; its captures open a data view when none shows them."""
        from ..devices.capture import capture_controller

        controller = capture_controller(instrument)
        if controller is not None and controller.view_provider is None:
            controller.changed.connect(self._update_run_buttons)
            controller.connection_lost.connect(
                lambda reason, instrument=instrument: self.connection_lost(instrument, reason))
            controller.view_provider = lambda controller: self.show_data(controller.instrument, beside=True)
            controller.reveal_view = self.area.reveal
        return controller

    def disconnect_instrument(self, instrument: Instrument, ask: bool = True) -> bool:
        """Close ``instrument``; asks first while it captures, drives outputs or a flow uses it."""
        if instrument not in self.hub:
            return False
        if ask:
            reasons = self.disconnect_reasons(instrument)
            if reasons and not messages.confirm(self, "Disconnect", f"Disconnect {instrument.name}?", "Disconnect",
                                                "While " + " and ".join(reasons) + ".", destructive=True):
                return False
        self.hub.remove(instrument.name)
        return True

    def disconnect_reasons(self, instrument: Instrument) -> list[str]:
        """Why disconnecting ``instrument`` now interrupts something."""
        reasons = []
        driver = instrument.capture.driver if instrument.capture is not None else None
        if driver is not None and driver.is_capturing:
            reasons.append("a capture is running")
        if drives_outputs(instrument):
            reasons.append("it drives outputs")
        running = [document.title for document in self.area.documents() if isinstance(document, FlowDocument)
                   and document.runner.running and instrument.uri in document.flow.devices.values()]
        if running:
            reasons.append("the flow " + ", ".join(running) + " uses it")
        return reasons

    def disconnect_all(self, ask: bool = True) -> bool:
        """Disconnect every device, after one question that names them and what that interrupts."""
        instruments = self.hub.instruments()
        if not instruments:
            return False
        if ask:
            reasons = {instrument.name: self.disconnect_reasons(instrument) for instrument in instruments}
            lines = [name + (": " + " and ".join(why) if why else "") for name, why in reasons.items()]
            count = f"all {len(instruments)} devices" if len(instruments) > 1 else instruments[0].name
            note = "Running captures and recordings stop, outputs are released." if any(reasons.values()) \
                else "Nothing is running on them now."
            if not messages.confirm(self, "Disconnect all devices", f"Disconnect {count}?", "Disconnect all",
                                    "\n".join(lines) + "\n\n" + note, destructive=True):
                return False
        for instrument in instruments:
            if instrument in self.hub:
                self.hub.remove(instrument.name)
        self.statusBar().showMessage(f"{len(instruments)} device{'s' if len(instruments) > 1 else ''} disconnected",
                                     6000)
        return True

    def connection_lost(self, instrument: Instrument, reason: str) -> None:
        """A device stopped answering: its status says so (the card offers *Reconnect*)."""
        if instrument in self.hub and instrument.status not in (InstrumentStatus.DISCONNECTED,
                                                                 InstrumentStatus.ERROR):
            self.hub.set_status(self.hub.name_of(instrument), InstrumentStatus.ERROR, reason)
            self.statusBar().showMessage(f"{instrument.name} does not answer: {reason}", 8000)

    def reconnect_instrument(self, instrument: Instrument) -> Optional[Instrument]:
        """Open the device of ``instrument`` again (after it was disconnected or unplugged); its
        device card takes the place of the old one."""
        from ...lab.engine.devices import open_instrument

        old_card = self.device_card(instrument)
        if instrument in self.hub:
            self.hub.remove(instrument.name)
        try:
            if instrument.uri.startswith("sim:"):
                from ..devices.simulated import open_at

                new = open_at(instrument.uri, self.hub)
            else:
                new = background.run(self, f"Connecting to {instrument.uri}...",
                                     lambda: open_instrument(instrument.uri))
        except background.Cancelled:
            return None
        except Exception as error:  # noqa: BLE001 - every way a device can fail to open
            if old_card is not None:
                old_card.show_banner(f"{instrument.name} could not be opened: {error}", "error")
            else:
                messages.error(self, "Reconnect", f"{instrument.name} could not be opened.", str(error))
            return None
        new.name = instrument.name if instrument.name not in self.hub.names() else new.name
        self.hub.add(new)
        card = self.open_device_card(new)
        if old_card is not None and card is not None:
            group = self.area.group_of(old_card)
            if group is not None and self.area.group_of(card) is group:
                group.tabBar().moveTab(group.indexOf(card), group.indexOf(old_card))
            self.area.close_document(old_card, force=True)
            self.area.activate(card)
        return new

    def _on_hub_event(self, _event) -> None:
        removed = getattr(_event, "instrument", None) if getattr(_event, "kind", "") == EVENT_REMOVED else None
        controller = getattr(removed, "capture_controller", None)
        if controller is not None:
            controller.close()  # no handlers, timers or settings of a device that is gone
        self.devices_section.refresh_open()
        self._refresh_device_presets()
        count = len(self.hub)
        self.devices_label.setText("No devices" if not count else f"{count} device{'s' if count > 1 else ''}")

    # ------------------------------------------------------------ projects
    def project_root_of(self, document) -> Optional[str]:
        """The project folder of ``document`` (``None`` outside a project)."""
        from ...lab.project import Project

        project = getattr(document, "project", None)
        if project is not None:
            return os.path.abspath(project.root)
        path = getattr(document, "path", None)
        root = Project.find(path) if path else None
        return os.path.abspath(root) if root else None

    def in_temporary_project(self, path: str) -> bool:
        absolute = os.path.abspath(path)
        return any(absolute == root or absolute.startswith(root + os.sep) for root in self.temporary_projects)

    def _remember(self, path: str) -> None:
        """Put ``path`` in the recent files (not files of temporary projects: they go away)."""
        if not self.in_temporary_project(path):
            recent.add(path)
        self.project_section.refresh_recent()

    def is_temporary(self, document) -> bool:
        return self.project_root_of(document) in self.temporary_projects

    def save_project_as(self, root: Optional[str] = None, parent_folder: Optional[str] = None) -> Optional[str]:
        """Copy the project ``root`` (default: the active document's) to a folder of its own; open
        documents of it move along. Returns the new folder."""
        import shutil

        from ...lab import templates
        from ...lab.project import Project

        root = root or self.project_root_of(self.active_document())
        if root is None:
            messages.info(self, "Save project", "The active document is not part of a project.")
            return None
        if parent_folder is None:
            # the user names the project folder (default: the project's name)
            try:
                name = Project.open(root).name or os.path.basename(root.rstrip(os.sep))
            except (OSError, ValueError):
                name = os.path.basename(root.rstrip(os.sep))
            folder = preferences.default_folder(create=True)
            path, _ = QFileDialog.getSaveFileName(self, "Save project as", os.path.join(folder, name),
                                                  "Project folder (*)")
            if not path:
                return None
            parent_folder, name = os.path.dirname(path), os.path.basename(path.rstrip(os.sep))
            target = templates.unique_folder(parent_folder, name)
        else:
            target = templates.unique_folder(parent_folder, os.path.basename(root.rstrip(os.sep)))
        try:
            shutil.copytree(root, target)
        except OSError as error:
            messages.error(self, "Save project", "The project could not be copied.", str(error))
            return None
        project = Project.open(target)
        for document in self.area.documents():
            path = getattr(document, "path", None)
            moved = os.path.join(target, os.path.relpath(path, root)) if path and \
                os.path.abspath(path).startswith(root + os.sep) else None
            if moved is None and self.project_root_of(document) != root:
                continue
            if moved is not None and hasattr(document, "_path"):
                document._path = moved
            if moved is not None and hasattr(document, "current_file"):
                document.current_file = moved
            if hasattr(document, "project"):
                document.project = project
            changed = getattr(document, "document_changed", None)
            if changed is not None:
                changed.emit()
        self.temporary_projects.discard(root)
        recent.add(target, "projects")
        self.statusBar().showMessage(f"Project saved in {target}", 8000)
        self._on_active_changed(self.active_document())
        return target

    def save_document(self, document, as_new: bool = False) -> bool:
        """Save ``document``; a document of a temporary project first gets the project a folder."""
        if self.is_temporary(document) and self.save_project_as(self.project_root_of(document)) is None:
            return False
        saved = document.save_as() if as_new else document.save()
        if saved and document.path:
            self._remember(document.path)
        return bool(saved)

    def save_active(self) -> bool:
        document = self.active_document()
        if document is None or not getattr(document, "can_save", lambda: False)():
            return False
        return self.save_document(document)

    def save_active_as(self) -> bool:
        document = self.active_document()
        if document is None or not getattr(document, "can_save", lambda: False)():
            return False
        return self.save_document(document, as_new=True)

    def close_active(self) -> bool:
        document = self.active_document()
        return self.area.close_document(document) if document is not None else False

    def show_preferences(self, page: str = "") -> dict:
        """*Settings…*: change the preferences; what changed applies at once (the theme on the next start)."""
        from ..dialogs.preferences_dialog import PAGES, PreferencesDialog

        dialog = PreferencesDialog(self)
        titles = [title for title, _icon in PAGES]
        if page in titles:
            dialog.pages.setCurrentRow(titles.index(page))
        if not dialog.exec():
            return {}
        self.apply_preferences(dialog.changes)
        return dialog.changes

    @staticmethod
    def _window_stylesheet() -> str:
        """The style of the window: the parts of the shell; the common style as well only when
        the application has none (a window used on its own). The application gets it at start –
        setting it here again made every widget match all rules twice."""
        application = QApplication.instance()
        if application is not None and application.styleSheet():
            return shell_stylesheet()
        return build_stylesheet() + shell_stylesheet()

    def _fill_header_tools(self) -> None:
        """The header's entries: the logo, the basic elements to make one of (flow, panel, data view,
        waveform, project), Open, Save and the examples; the search; the run controls. It adapts to
        the width of the window and is customized with a right click (``ui/widgets/toolbar.py``)."""
        from ..widgets.toolbar import HIGH, LOW, NORMAL

        bar = self.header

        def tool(text: str, icon_name: str, tip: str, menu: Optional[QMenu] = None,
                 action: Optional[QAction] = None) -> QToolButton:
            button = QToolButton(bar)
            button.setText(text)
            button.setIcon(icon(icon_name))
            button.setToolTip(tip)
            button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
            button.setAutoRaise(True)
            if menu is not None:
                button.setMenu(menu)
                button.setPopupMode(QToolButton.InstantPopup)
            elif action is not None:
                button.clicked.connect(lambda _checked=False: action.trigger())
            return button

        new = native(QKeySequence(QKeySequence.New).toString())
        #: one button for every kind of document: they are what openSciLab is made of
        self.new_tools = {
            "flow": tool("Flow", "nodes", f"A new flow ({new})", action=self.action_new_flow),
            "panel": tool("Panel", "panel", "A new panel: the front of an instrument for a flow - switches, sliders "
                          "and buttons that operate it, numbers, LEDs and charts that show its values",
                          action=self.action_new_panel),
            "data_view": tool("Data view", "channels", "A new data view: capture, or open a capture",
                              action=self.action_new_data_view),
            "waveform": tool("Waveform", "wave", "A new waveform for a generator", action=self.action_new_waveform),
            "project": tool("Project", "layers", "A new project from a template (the start page)",
                            action=self.action_new_project),
        }
        self.open_tool = tool("Open", "folder", "Open a file (Ctrl+O)", action=self.action_open)
        self.save_tool = tool("Save", "save", "Save (Ctrl+S)", action=self.action_save)
        self.examples_tool = tool("Templates", "book", "Start a project from a template (every example is one)",
                                  menu=self.examples_menu)
        bar.add_widget("logo", self.logo, "Logo", "logo", HIGH, pinned=True)
        # the search right after the logo: the first place to look for anything
        bar.add_widget("search", self.palette_button, "Search commands", "search", HIGH)
        for key, title, priority in (("flow", "New flow", NORMAL), ("panel", "New panel", NORMAL),
                                     ("data_view", "New data view", NORMAL), ("waveform", "New waveform", NORMAL),
                                     ("project", "New project from a template", LOW)):
            bar.add_widget(f"new-{key}", self.new_tools[key], title, "new", priority)
        bar.add_widget("open", self.open_tool, "Open a file", "file", HIGH, text=False)
        bar.add_widget("save", self.save_tool, "Save", "file", HIGH, text=False)
        bar.add_widget("templates", self.examples_tool, "Templates", "file", NORMAL)
        bar.add_stretch("stretch-left")
        bar.add_widget("run", self.run_button, "Start / Capture", "run", HIGH, pinned=True)
        bar.add_widget("pause", self.pause_button, "Pause", "run", HIGH, text=False)
        bar.add_widget("stop", self.stop_button, "Stop", "run", HIGH, text=False)
        bar.add_stretch("stretch-right")
        bar.finish()

    def _remote_device_event(self, event: str, name: str) -> None:
        self.statusBar().showMessage(f"Remote device {name} {event}", 6000)
        self.devices_section.refresh()

    def apply_remote_server(self) -> None:
        """Start, restart or stop the server for remote devices as the settings say."""
        from ...core import preferences
        from ...driver.remote import servers

        servers.managed = True  # flows do not start it by themselves in the application
        if not preferences.get("remote.enabled"):
            servers.stop_main()
            return
        token = str(preferences.get("remote.token") or "")
        if not token:
            token = servers.new_token()
            preferences.update({"remote.token": token})
        try:
            server = servers.start_main(int(preferences.get("remote.port")), token,
                                        beacon=bool(preferences.get("remote.beacon")))
        except OSError as error:
            self.statusBar().showMessage(f"Remote devices: port {preferences.get('remote.port')} cannot be "
                                         f"used ({error})", 10000)
            return
        server.subscribe(lambda event, connection: self._remote_event.emit(event, connection.name))

    def apply_preferences(self, changes: dict) -> None:
        if any(key.startswith("remote.") for key in changes):
            self.apply_remote_server()
        if any(key.startswith("timing.") for key in changes):
            from ...driver.remote import servers

            for connection in servers.connections():
                connection.use_timescale()  # (this computer's clock follows a time scale now, or no more)
        if "appearance.font_size" in changes:
            application = QApplication.instance()
            if application is not None and application.styleSheet():
                application.setStyleSheet(build_stylesheet())
            self.setStyleSheet(self._window_stylesheet())
        if "devices.refresh_s" in changes:
            self.port_timer.setInterval(int(preferences.get("devices.refresh_s") * 1000))
        if "devices.show_simulators" in changes:
            self.devices_section.refresh()
            self._refresh_device_presets()
        if changes.get("devices.high_priority"):
            self.raise_priority()
        if "appearance.theme" in changes:
            choice = messages.choose(self, "Theme", "The new theme applies when openSciLab starts again.",
                                     ["Restart now", "Later"])
            if choice == 0:
                self.restart()

    def raise_priority(self, ask: bool = True) -> bool:
        """The processes that read devices with a higher priority (preference ``devices.high_priority``):
        without asking where the system allows it, else - macOS, Linux - after one question with the
        password dialog of the system. Device processes started later take it over."""
        import sys

        from ...core import priority

        if priority.raise_own() or priority.is_raised():
            self.statusBar().showMessage("Devices are read with a higher priority", 6000)
            return True
        if sys.platform == "win32" or not ask:
            return False
        linux = sys.platform.startswith("linux")
        options = ["Raise the priority", "Not now"] + (["Allow it for good (from the next login)"] if linux else [])
        choice = messages.choose(
            self, "Higher priority",
            "Read devices with a higher priority? The system asks for the administrator's password.",
            options,
            "The processes that read devices (and openSciLab) run above other programs, so a busy computer does "
            "not make a stream overflow. It applies until openSciLab ends; it asks again at the next start. "
            "Settings → Devices switches it off." + (" 'For good' adds a line to /etc/security/limits.d: "
                                                     "from the next login on openSciLab raises itself." if linux
                                                     else ""))
        if choice is None or choice == 1:
            return False
        try:
            if choice == 2:
                background.run(self, "Allowing a higher priority...", priority.allow_permanently, cancellable=False)
                messages.info(self, "Higher priority", "Allowed for good: from the next login on openSciLab raises "
                              "its priority itself.")
            raised = background.run(self, "Raising the priority...", priority.raise_with_rights, cancellable=False)
        except priority.PriorityError as error:
            self.statusBar().showMessage(f"The priority was not raised: {error}", 10000)
            return False
        self.statusBar().showMessage(f"Devices are read with a higher priority ({len(raised)} processes)", 8000)
        return True

    def restart(self) -> bool:
        """Start the application again (after asking about unsaved changes)."""
        import sys

        from PySide6.QtCore import QProcess

        if not self.confirm_quit():
            return False
        self.save_state()
        # (quitting closes the window: by then the documents are closed, and saving the state
        # again would store a session without them – the new start would open nothing)
        self._restarting = True
        self.close_all_documents(force=True)
        if getattr(sys, "frozen", False):  # the packaged application is its own program
            QProcess.startDetached(sys.executable, sys.argv[1:])
        else:
            QProcess.startDetached(sys.executable, [arg for arg in sys.argv if arg])
        QApplication.instance().quit()
        return True

    def _empty_action(self, key: str) -> None:
        {"flow": self.new_flow, "connect": self.connect_dialog, "open": self.open_file_dialog,
         "start": self.show_start_page}[key]()

    def _last_of_temporary(self, document: QWidget) -> bool:
        """``document`` is the last open document of a temporary project (closing it loses the project)."""
        root = self.project_root_of(document) if self.temporary_projects else None
        if root is None or root not in self.temporary_projects:
            return False
        return not any(other is not document and self.project_root_of(other) == root for other in self.area.documents())

    def _confirm_close(self, document: QWidget) -> bool:
        """Ask what to do with the unsaved changes of ``document``; ``True`` to close it."""
        self.area.activate(document)
        if not getattr(document, "dirty", False) and self._last_of_temporary(document):
            root = self.project_root_of(document)
            choice = messages.choose(self, "Temporary project", f"The project of {document.title} is temporary "
                                     "and was not saved. Keep it?", ["Save project...", "Discard"])
            if choice is None:
                return False
            if choice == 0:
                return self.save_project_as(root) is not None
            self.temporary_projects.discard(root)
            return True
        choice = messages.choose(
            self, "Unsaved changes", f"{document.title} has changes that were not saved.",
            ["Save", "Discard"],
        )
        if choice is None:
            return False
        if choice == 0:
            return self.save_document(document)
        return True

    def _split(self, orientation) -> None:
        document = self.active_document()
        if document is None or self.area.is_detached(document):
            return
        group = self.area.group_of(document)
        if group is not None and group.count() < 2:
            self.statusBar().showMessage("Split needs a second document in this group: open one first", 5000)
            return
        self.area.split(document, orientation)

    def _cycle_tab(self, step: int) -> None:
        """Ctrl+Tab: the next document of the group (around the end)."""
        document = self.active_document()
        group = self.area.group_of(document) if document is not None else None
        if group is None or group.count() < 2:
            return
        self.area.activate(group.widget((group.indexOf(document) + step) % group.count()))

    def _move_to_next_group(self) -> None:
        document = self.active_document()
        if document is not None:
            self.area.move_to_next_group(document)

    def _detach(self) -> None:
        document = self.active_document()
        if document is not None and not self.area.is_detached(document):
            self.area.detach(document)

    # ---------------------------------------------------- active document
    def _on_active_changed(self, document: Optional[QWidget]) -> None:
        menu_bar = self.menuBar()
        for action in self._document_menu_actions:
            menu_bar.removeAction(action)
        self._document_menu_actions = []
        for action in self._document_file_actions:
            self.project_menu.removeAction(action)
        self._document_file_actions = []

        if isinstance(document, Document) and not self.area.is_detached(document):
            for menu in document.document_menus():
                action = menu.menuAction()
                menu_bar.insertAction(self.view_menu.menuAction(), action)
                self._document_menu_actions.append(action)
            for action in document.document_file_actions():
                self.project_menu.insertAction(self._document_section_end, action)
                self._document_file_actions.append(action)
        self._document_section.setVisible(bool(self._document_file_actions))

        self._sync_document_state(document)
        self.project_section.set_data_folder(self.data_folder(document))  # also lists it again
        self._refresh_device_presets()
        registry = document.registry if isinstance(document, FlowDocument) else None
        if registry is not None and registry is not self.nodes_section.registry:
            self.nodes_section.set_registry(registry)  # the project's own nodes too

    def _sync_document_state(self, document: Optional[QWidget]) -> None:
        """What follows the state of the active document: called when another document becomes
        active and whenever the active one changes (every edit, every undo step) – so only what
        is cheap. The menus of the document are built in :meth:`_on_active_changed`."""
        self._update_nodes_dock(document)
        can_save = isinstance(document, Document) and document.can_save()
        self.action_save.setEnabled(can_save)
        self.action_save_as.setEnabled(can_save)
        self.action_close.setEnabled(document is not None)
        stack = document.undo_stack() if isinstance(document, Document) else None
        self.action_undo.setEnabled(stack is not None and stack.canUndo())
        self.action_redo.setEnabled(stack is not None and stack.canRedo())
        self._update_inspector(document)
        self._update_title()
        self._update_saved_label()
        self._update_run_buttons()
        folder = self.data_folder(document)
        if folder != self.project_section.data_folder:  # e.g. the flow was saved to another place
            self.project_section.set_data_folder(folder)
        project = getattr(document, "project", None)
        if project is not self.project_section.project:
            self.project_section.set_project(project)

    def _run_state_changed(self, state: str) -> None:
        self._update_run_buttons()
        if state in ("finished", "stopped", "error"):
            self.project_section.refresh_data()  # what the run wrote

    def _update_inspector(self, document: Optional[QWidget]) -> None:
        widget = document.inspector_widget() if isinstance(document, Document) else None
        title = f"{document.title}" if document is not None and widget is not None else "Inspector"
        empty = "No document open" if document is None else f"{document.title} has no properties to edit here."
        self.inspector.show_widget(widget, owner=document, title=title, empty_text=empty)

    def _on_documents_changed(self) -> None:
        self.project_section.set_documents(self.area.documents(), self.active_document())
        self._update_saved_label()
        self._update_title()
        document = self.active_document()
        can_save = isinstance(document, Document) and document.can_save()
        self.action_save.setEnabled(can_save)
        self.action_save_as.setEnabled(can_save)

    def _update_saved_label(self) -> None:
        documents = self.area.documents()
        unsaved = [document for document in documents if getattr(document, "dirty", False)
                   or (self.temporary_projects and self.is_temporary(document))]
        savable = [document for document in documents if getattr(document, "can_save", lambda: False)()]
        if unsaved:
            text = f"{len(unsaved)} unsaved"
        else:
            text = "saved" if savable else ""
        self.saved_label.setText(text)
        self.saved_label.setToolTip("\n".join(document.title for document in unsaved))
        self.setWindowModified(bool(unsaved))

    def _update_title(self) -> None:
        document = self.active_document()
        name = f"openSciLab {__version__}"
        self.setWindowTitle(f"{document.title}[*] — {name}" if document is not None else f"{name}[*]")

    def _undo(self) -> None:
        document = self.active_document()
        stack = document.undo_stack() if isinstance(document, Document) else None
        if stack is not None:
            stack.undo()

    def _redo(self) -> None:
        document = self.active_document()
        stack = document.undo_stack() if isinstance(document, Document) else None
        if stack is not None:
            stack.redo()

    # ======================================================== view toggles
    def toggle_sidebar(self) -> None:
        self.activity_bar.set_sidebar_visible(not self.sidebar_dock.isVisible())

    def toggle_inspector(self) -> None:
        self.inspector_dock.setVisible(not self.inspector_dock.isVisible())

    def toggle_console(self) -> None:
        self.console_dock.setVisible(not self.console_dock.isVisible())

    # ===================================================== command palette
    def commands(self) -> list[Command]:
        """Every command of the shell menus and the active document."""
        document = self.active_document()
        menus = [self.project_menu, self.edit_menu]
        if isinstance(document, Document) and not self.area.is_detached(document):
            menus += document.document_menus()
        menus += [self.view_menu, self.devices_menu, self.examples_menu, self.help_menu]
        result = commands_from_menus(menus)
        if isinstance(document, Document):
            result += commands_from_actions(document.document_actions(), document.title)
        for open_document in self.area.documents():
            result.append(Command(f"Go to › {open_document.title}",
                                  lambda d=open_document: self.area.activate(d), key=f"go:{id(open_document)}"))
        for path in recent.entries("files"):
            result.append(Command(f"Open recent › {os.path.basename(path)}", lambda p=path: self.open_file(p),
                                  key=f"recent:{path}"))
        return unique(command for command in result if command.title != "Edit › Command palette")

    def show_command_palette(self) -> CommandPalette:
        if self._palette is not None:
            self._palette.close()
            self._palette.deleteLater()  # one palette at a time, not a hidden one per use
        self._palette = CommandPalette(self.commands(), self)
        self._palette.popup()
        return self._palette

    def _search(self, query: str) -> list[tuple[str, str, Callable]]:
        commands = fuzzy.rank(query, self.commands(), key=lambda command: command.title)
        return [(command.title, command.shortcut, command.callback) for command in commands if command.enabled]

    # ============================================================== layouts
    def _layouts(self) -> dict:
        data = settings.get_settings(LAYOUTS_FILE)
        return data if isinstance(data, dict) else {}

    def _rebuild_layout_menu(self) -> None:
        self.layout_menu.clear()
        self.layout_menu.addAction("Save layout as...", self.save_layout_dialog)
        names = sorted(self._layouts())
        if names:
            self.layout_menu.addSeparator()
            for name in names:
                self.layout_menu.addAction(name, lambda n=name: self.restore_layout(n))
            remove = self.layout_menu.addMenu("Delete layout")
            for name in names:
                remove.addAction(name, lambda n=name: self.delete_layout(n))
        self.layout_menu.addSeparator()
        self.layout_menu.addAction("Reset layout", self.reset_layout)

    def save_layout_dialog(self) -> None:
        name, accepted = QInputDialog.getText(self, "Save layout", "Name of the layout:")
        if accepted and name.strip():
            self.save_layout(name.strip())

    def save_layout(self, name: str) -> None:
        layouts = self._layouts()
        layouts[name] = bytes(self.saveState(SHELL_LAYOUT_VERSION).toBase64()).decode("ascii")
        settings.persist_settings(LAYOUTS_FILE, layouts)

    def restore_layout(self, name: str) -> bool:
        state = self._layouts().get(name)
        if not isinstance(state, str):
            return False
        return self.restoreState(QByteArray.fromBase64(state.encode("ascii")), SHELL_LAYOUT_VERSION)

    def delete_layout(self, name: str) -> None:
        layouts = self._layouts()
        layouts.pop(name, None)
        settings.persist_settings(LAYOUTS_FILE, layouts)

    def reset_layout(self) -> None:
        self.restoreState(self._default_state, SHELL_LAYOUT_VERSION)
        self.sidebar_dock.setVisible(True)
        self.inspector_dock.setVisible(True)
        self.console_dock.setVisible(False)
        QTimer.singleShot(0, self.size_docks)  # (once the restored layout is laid out)

    def size_docks(self) -> None:
        """The sidebar and the inspector at their widths of a new layout (:data:`DOCK_WIDTHS`)."""
        self.resizeDocks([self.sidebar_dock, self.inspector_dock], list(DOCK_WIDTHS), Qt.Horizontal)

    def showEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        if not self._docks_sized:
            self._docks_sized = True
            QTimer.singleShot(0, self.size_docks)  # (once the window has its size)

    def _restore_state(self) -> bool:
        """The window as it was; whether its layout was restored."""
        state = settings.get_settings(SHELL_STATE_FILE)
        if not isinstance(state, dict):
            return False
        restored = False
        try:
            width, height = int(state.get("width", 0)), int(state.get("height", 0))
            if width > 400 and height > 300:
                self.resize(width, height)
            if "x" in state and "y" in state and _on_a_screen(int(state["x"]), int(state["y"]),
                                                              self.width(), self.height()):
                self.move(int(state["x"]), int(state["y"]))  # not where no screen is any more
            layout = state.get("layout")
            if isinstance(layout, str):
                restored = self.restoreState(QByteArray.fromBase64(layout.encode("ascii")), SHELL_LAYOUT_VERSION)
            section = state.get("section")
            if section in self.activity_bar.keys():
                self.activity_bar.select(section)
            if state.get("sidebar") is False:
                self.activity_bar.set_sidebar_visible(False)
            if state.get("maximized"):
                QTimer.singleShot(0, self.showMaximized)
        except (TypeError, ValueError):
            return restored
        return restored

    def restore_session(self) -> bool:
        """Open the documents (and connect the devices) of the last session; whether any opened."""
        from . import session

        state = settings.get_settings(SHELL_STATE_FILE)
        if not isinstance(state, dict):
            return False
        return session.restore(self, state.get("session"), preferences.get("startup.reconnect_devices"))

    def save_state(self) -> None:
        from . import session

        geometry = self.normalGeometry()
        settings.persist_settings(SHELL_STATE_FILE, {
            "session": session.session_state(self),
            "x": geometry.x(), "y": geometry.y(), "width": geometry.width(), "height": geometry.height(),
            "maximized": self.isMaximized(),
            "layout": bytes(self.saveState(SHELL_LAYOUT_VERSION).toBase64()).decode("ascii"),
            "section": self.activity_bar.current,
            "sidebar": self.sidebar_dock.isVisible(),
        })

    # ================================================================= misc
    def _rebuild_recent_menu(self) -> None:
        self.recent_menu.clear()
        projects = recent.entries("projects")
        paths = recent.entries("files")
        if projects:
            self.recent_menu.addSection("Projects")
            for root in projects:
                self.recent_menu.addAction(icon("folder"), os.path.basename(root.rstrip(os.sep)),
                                           lambda r=root: self.open_project(r)).setToolTip(root)
        if paths:
            self.recent_menu.addSection("Files")
        for path in paths:
            self.recent_menu.addAction(os.path.basename(path), lambda p=path: self.open_file(p)).setToolTip(path)
        if not paths and not projects:
            self.recent_menu.addAction("Nothing opened yet").setEnabled(False)
        else:
            self.recent_menu.addSeparator()
            self.recent_menu.addAction("Clear the list", lambda: (recent.clear("files"), recent.clear("projects")))

    def show_shortcuts(self):
        """*Help → Keyboard shortcuts*: every shortcut of the menus and the documents, and the keys
        and gestures of the flow graph, the waveform, charts and panels; searchable."""
        from ..dialogs.shortcuts_dialog import ShortcutsDialog

        rows = sorted({(command.title, command.shortcut) for command in self.commands() if command.shortcut})
        dialog = ShortcutsDialog(rows, self)
        dialog.show()
        return dialog

    def show_decoder_paths(self) -> None:
        registry = self.provider.registry
        registry.load()
        paths = "\n".join(registry.search_paths) or "(none)"
        messages.info(
            self, "Decoder search paths", f"{len(registry.decoders)} protocol decoders are loaded.",
            f"Search paths:\n{paths}\n\nBy default the 'decoders' folder next to the application is used. "
            "Pass --decoders, set the OPENSCILAB_DECODERS environment variable or copy decoders into "
            "the 'decoders' folder of the settings directory to add more.",
        )

    def show_plugins(self) -> None:
        from ... import plugins
        from ...driver import kinds

        found = plugins.load(ui=True)
        lines = [f"{plugin.name}: {plugin.error or 'loaded'} ({plugin.source})" for plugin in found]
        added = [f"{kind.kind}: {kind.title}{' (device process)' if kind.process else ''}"
                 for kind in kinds.registered()]
        folders = "\n".join(plugins.directories())
        failed = plugins.problems()
        details = (
            "\n".join(lines or ["(no plugins)"]) + "\n\nKinds of devices they add:\n" + "\n".join(added or ["(none)"])
            + f"\n\nPlugins are loaded from:\n{folders}\nand from installed packages (entry points "
            f"'{plugins.GROUP}') when openSciLab starts: restart it for a new plugin. See docs/drivers.md."
            + "".join(f"\n\n{plugin.details}" for plugin in failed)
        )
        messages.info(
            self, "Plugins",
            f"{len(found) - len(failed)} plugins are loaded" + (f", {len(failed)} could not be." if failed else "."),
            details,
        )

    def close_all_documents(self, force: bool = False) -> bool:
        return self.area.close_all(force=force)

    def confirm_quit(self) -> bool:
        """Before the window closes: one question for everything unsaved (documents with changes,
        temporary projects); nothing is closed when the user cancels."""
        documents = self.area.documents()
        dirty = [document for document in documents if getattr(document, "dirty", False)]
        temporary = sorted({root for root in (self.project_root_of(document) for document in documents)
                            if root in self.temporary_projects})
        if not dirty and not temporary:
            return True
        names = [document.title for document in dirty] + [
            f"the temporary project {os.path.basename(root)}" for root in temporary
            if not any(self.project_root_of(document) == root for document in dirty)]
        if len(names) > 1:
            text, details = "Save before closing?", "\n".join(names)
        else:
            text = f"{names[0][:1].upper()}{names[0][1:]} was not saved. Save it before closing?"
            details = "Discard closes without saving." if not temporary else \
                "It is in a temporary folder: without saving, it is gone."
        choice = messages.choose(self, "Unsaved changes", text, ["Save all" if len(names) > 1 else "Save", "Discard"],
                                 details)
        if choice is None:
            return False
        if choice == 1:
            return True
        for document in dirty:
            if getattr(document, "dirty", False) and not self.save_document(document):
                return False
        for root in temporary:
            if root in self.temporary_projects and self.save_project_as(root) is None:
                return False
        return True

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if getattr(self, "_restarting", False):  # restart() saved the state and asked already
            self.hub.close_all()
            self.console.detach_logging()
            super().closeEvent(event)
            return
        self.save_state()
        if not self.confirm_quit() or not self.close_all_documents(force=True):
            event.ignore()
            return
        self.hub.close_all()
        self.console.detach_logging()
        from ...driver.remote import servers

        servers.stop_main()
        super().closeEvent(event)

    def force_close(self) -> None:
        """Close without asking about unsaved changes (tests, smoke test)."""
        from ...driver.remote import servers

        servers.stop_main()
        self.save_state()
        self.close_all_documents(force=True)
        self.hub.close_all()
        self.hub_bridge.close()
        self.console.detach_logging()
        self.hide()
        self.deleteLater()



def _is_flow_script(path: str) -> bool:
    """A Python file that defines a flow with the DSL."""
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read(20000)
    except OSError:
        return False
    return "openscilab.lab" in text and "flow(" in text


_TITLES: dict[tuple, list] = {}


def _simulator_titles(names: tuple, load_profile) -> list[tuple[str, str]]:
    """``(name, title)`` of the simulator profiles; read once per set of profiles."""
    if names not in _TITLES:
        titles = []
        for name in names:
            try:
                titles.append((name, load_profile(name)["title"]))
            except (OSError, ValueError, KeyError):
                continue
        _TITLES[names] = titles
    return _TITLES[names]


def _on_a_screen(x: int, y: int, width: int, height: int) -> bool:
    """A window at ``x``, ``y`` would have its title bar on one of the screens."""
    from PySide6.QtCore import QRect

    title = QRect(x, y, max(width, 200), 40)
    screens = QApplication.screens()
    return not screens or any(screen.availableGeometry().intersects(title) for screen in screens)
