"""The session: saved documents, groups, zoom and the console come back at the next start - and the
devices with their cards when asked for; nothing saved means a fresh start."""

from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from openscilab.core import preferences, settings
from openscilab.lab import Flow, yaml_io
from openscilab.ui.documents.dataview import DataView
from openscilab.ui.documents.device import DeviceDocument
from openscilab.ui.documents.flow import FlowDocument


def pump(times: int = 5) -> None:
    for _ in range(times):
        QApplication.processEvents()
        time.sleep(0.01)


def new_shell():
    from openscilab.ui.shell.main_window import ShellWindow

    QApplication.instance() or QApplication([])
    window = ShellWindow()
    window.resize(1600, 1000)
    window.show()
    return window


@pytest.fixture
def flow_path(tmp_path) -> str:
    flow = Flow("kept")
    flow.add_node("control.timer", "timer")
    flow.nodes["timer"].position = (0.0, 0.0)
    flow.add_node("data.table", "table")
    flow.nodes["table"].position = (600.0, 300.0)
    path = str(tmp_path / "kept.flow.yaml")
    yaml_io.save(flow, path)
    return path


def test_the_session_comes_back(flow_path):
    preferences.update({"startup.reconnect_devices": True})  # (off by default: see the fresh start below)
    first = new_shell()
    try:
        flow = first.open_file(flow_path)
        pump()
        flow.view.zoom_at(1.5, flow.view.viewport().rect().center())
        flow.view.centerOn(300, 150)
        first.try_simulator("uno")
        card = first.active_document()
        first.show_data(card.instrument)  # beside the card: a second group
        first.show_console("Execution")
        first.area.activate(flow)
        groups = len(first.area.groups())
        zoom = flow.view.zoom_level()
        first.save_state()
    finally:
        first.force_close()
    second = new_shell()
    try:
        assert second.restore_session()
        pump()
        kinds = sorted(document.document_kind for document in second.area.documents())
        assert kinds == ["data", "device", "flow"]
        assert len(second.area.groups()) == groups
        assert [instrument.uri for instrument in second.hub.instruments()] == ["sim:uno"]
        restored = next(document for document in second.area.documents() if isinstance(document, FlowDocument))
        assert second.active_document() is restored
        assert restored.view.zoom_level() == pytest.approx(zoom, abs=0.01)
        assert second.console_dock.isVisible()
        data = next(document for document in second.area.documents() if isinstance(document, DataView))
        card = next(document for document in second.area.documents() if isinstance(document, DeviceDocument))
        assert data.source is card.controller
    finally:
        second.force_close()


def test_nothing_saved_is_a_fresh_start(flow_path):
    """Device cards, their data views, the connected hardware and unsaved documents do not come back:
    without a saved file the next start is empty; with one, only the file is there."""
    assert preferences.get("startup.reconnect_devices") is False
    first = new_shell()
    try:
        first.show_start_page()
        first.new_flow()
        first.try_simulator("uno")
        first.show_data(first.active_document().instrument)
        first.show_hardware()
        first.save_state()
    finally:
        first.force_close()
    second = new_shell()
    try:
        assert not second.restore_session()
        assert second.area.documents() == [] and len(second.hub) == 0  # (app.py makes the start page)
        second.open_file(flow_path)
        second.try_simulator("uno")
        second.save_state()
    finally:
        second.force_close()
    third = new_shell()
    try:
        assert third.restore_session()
        assert [document.document_kind for document in third.area.documents()] == ["flow"]
        assert len(third.hub) == 0
    finally:
        third.force_close()


def test_gone_files_and_devices_are_left_out(flow_path, tmp_path):
    preferences.update({"startup.reconnect_devices": True})
    state = {"session": {"groups": [[{"kind": "file", "path": str(tmp_path / "gone.flow.yaml")},
                                     {"kind": "file", "path": flow_path, "current": True, "active": True},
                                     {"kind": "device", "uri": "pico:/dev/cu.nothing"}]],
                         "devices": ["pico:/dev/cu.nothing"], "detached": []}}
    settings.persist_settings("shell-state.json", state)
    shell = new_shell()
    try:
        assert shell.restore_session()
        assert [document.path for document in shell.area.documents()] == [flow_path]
        assert "cu.nothing" in shell.devices_section.error_banner.label.text()
    finally:
        shell.force_close()


def test_the_start_follows_the_settings(monkeypatch, flow_path):
    from openscilab import app
    from openscilab.ui.shell.main_window import ShellWindow

    instance = QApplication.instance() or QApplication([])
    monkeypatch.setattr(app, "QApplication", lambda *args: instance)
    monkeypatch.setattr(instance, "setStyle", lambda *args: None)
    monkeypatch.setattr(instance, "setStyleSheet", lambda *args: None)
    monkeypatch.setattr(instance, "exec", lambda: 0)
    windows = []
    original = ShellWindow.show
    monkeypatch.setattr(ShellWindow, "show", lambda self: (windows.append(self), original(self)))
    settings.persist_settings("shell-state.json", {"session": {"groups": [[{"kind": "file", "path": flow_path}]]}})
    assert app.main([]) == 0
    try:
        assert [document.path for document in windows[0].area.documents()] == [flow_path]  # restored
    finally:
        windows[0].force_close()
    preferences.update({"startup.restore_session": False, "startup.show_start_page": False})
    settings.persist_settings("shell-state.json", {"session": {"groups": [[{"kind": "file", "path": flow_path}]]}})
    assert app.main([]) == 0
    try:
        assert [document.document_kind for document in windows[1].area.documents()] == ["flow"]
        assert windows[1].area.documents()[0].path is None  # a new flow, no start page
    finally:
        windows[1].force_close()


def test_a_window_where_no_screen_is_comes_back_on_one():
    settings.persist_settings("shell-state.json", {"x": 40000, "y": 40000, "width": 900, "height": 700})
    shell = new_shell()
    try:
        assert shell.x() < 10000
    finally:
        shell.force_close()
