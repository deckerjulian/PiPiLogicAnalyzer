# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The session of the shell: which saved documents were open (and where), the project, how far a
flow or a capture was zoomed. Stored with the window state when the window closes and opened again
at the next start (*Settings → Startup*).

What was saved comes back: files (flows, panels, captures, waveforms) and the project. Nothing saved
means a fresh start - unsaved documents, the connected hardware and, unless *Connect the devices of
the last session again* is on, the devices with their cards and data views are not kept.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any, Optional

from PySide6.QtCore import QPointF, Qt, QTimer

from .. import background

if TYPE_CHECKING:  # pragma: no cover
    from .main_window import ShellWindow

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ saving
def document_state(shell: "ShellWindow", document) -> Optional[dict[str, Any]]:
    """What reopens ``document`` (``None``: it cannot be reopened, e.g. an unsaved flow)."""
    from ..documents.dataview import DataView
    from ..documents.device import DeviceDocument
    from ..documents.flow import FlowDocument

    kind = getattr(document, "document_kind", "")
    path = getattr(document, "path", None)
    if kind == "start":
        return {"kind": "start"}
    if isinstance(document, DeviceDocument):
        return {"kind": "device", "uri": document.instrument.uri} if document.instrument.uri else None
    if isinstance(document, FlowDocument):
        if document.parent_document is not None or not path or shell.in_temporary_project(path):
            return None
        view = document.view
        center = view.mapToScene(view.viewport().rect().center())
        return {"kind": "file", "path": os.path.abspath(path), "zoom": round(view.zoom_level(), 4),
                "center": [round(center.x(), 1), round(center.y(), 1)]}
    if isinstance(document, DataView):
        state: dict[str, Any] = {"first": document.model.first_sample, "visible": document.model.visible_samples}
        if path and not shell.in_temporary_project(path):
            return {"kind": "file", "path": os.path.abspath(path), **state}
        if document.source is not None and document.source.instrument.uri:
            return {"kind": "data", "uri": document.source.instrument.uri}
        return None
    if path and not shell.in_temporary_project(path):
        return {"kind": "file", "path": os.path.abspath(path)}
    return None


def session_state(shell: "ShellWindow") -> dict[str, Any]:
    area = shell.area
    groups = []
    for group in area.groups():
        entries = []
        for index in range(group.count()):
            state = document_state(shell, group.widget(index))
            if state is not None:
                state["current"] = group.currentWidget() is group.widget(index)
                state["active"] = shell.active_document() is group.widget(index)
                entries.append(state)
        if entries:
            groups.append(entries)
    detached = [state for state in (document_state(shell, document) for document in area.documents()
                                    if area.is_detached(document)) if state is not None]
    project = None
    active = shell.active_document()
    root = shell.project_root_of(active) if active is not None else None
    if root and root not in shell.temporary_projects:
        project = root
    return {
        "groups": groups,
        "orientation": "vertical" if area.root.orientation() == Qt.Vertical else "horizontal",
        "detached": detached,
        "devices": [instrument.uri for instrument in shell.hub.instruments() if instrument.uri],
        "project": project,
        "console": shell.console_dock.isVisible(),
        "console_tab": shell.console.tabText(shell.console.currentIndex()),
    }


# --------------------------------------------------------------- restoring
def reconnect(shell: "ShellWindow", uris: list[str]) -> list[str]:
    """Connect the devices of ``uris`` again; returns those that could not be opened."""
    from ...lab.engine.devices import open_instrument

    failed = []
    for uri in uris:
        if any(instrument.uri == uri for instrument in shell.hub.instruments()):
            continue
        try:
            if uri.startswith("sim:"):
                from ..devices.simulated import open_at

                instrument = open_at(uri, shell.hub)  # on the hub's clock, simulating what it did
            else:
                # not in the thread of the window: a device that is gone would freeze the start
                instrument = background.run(shell, f"Connecting to {uri}...", lambda uri=uri: open_instrument(uri))
        except background.Cancelled:
            failed.append(uri)
            continue
        except Exception as error:  # noqa: BLE001 - every way a device can be missing
            log.info("Device %s of the last session is not there: %s", uri, error)
            failed.append(uri)
            continue
        shell.hub.add(instrument)
    if failed:
        shell.devices_section.show_error("Not connected again (not found): " + ", ".join(failed)
                                         + ". Plug them in and connect them from the list.")
    return failed


def _instrument(shell: "ShellWindow", uri: str):
    return next((instrument for instrument in shell.hub.instruments() if instrument.uri == uri), None)


def open_state(shell: "ShellWindow", state: dict[str, Any]):
    """Open one document of a saved session (``None`` when it is gone)."""
    kind = state.get("kind")
    if kind == "start":
        return shell.show_start_page()
    if kind == "device":
        instrument = _instrument(shell, state.get("uri", ""))
        return shell.open_device_card(instrument) if instrument is not None else None
    if kind == "data":
        instrument = _instrument(shell, state.get("uri", ""))
        return shell.show_data(instrument) if instrument is not None and instrument.capture is not None else None
    if kind == "file":
        path = state.get("path", "")
        if not path or not os.path.exists(path):
            log.info("A file of the last session is gone: %s", path)
            return None
        document = shell.open_file(path)
        if document is not None:
            QTimer.singleShot(0, lambda document=document, state=state: _restore_view(document, state))
        return document
    return None


def _restore_view(document, state: dict[str, Any]) -> None:
    """Zoom and position as they were (after the document is laid out)."""
    from ..documents.dataview import DataView
    from ..documents.flow import FlowDocument

    try:
        if isinstance(document, FlowDocument) and "zoom" in state:
            document._fitted = True  # the saved view, not "fit"
            view = document.view
            view.resetTransform()
            view.zoom_at(float(state["zoom"]), QPointF(view.viewport().rect().center()))
            x, y = state.get("center", (0.0, 0.0))
            view.centerOn(float(x), float(y))
        elif isinstance(document, DataView) and "visible" in state and document.model.session is not None:
            document.model.set_view(int(state["first"]), int(state["visible"]))
    except (RuntimeError, TypeError, ValueError):  # closed meanwhile, or a damaged entry
        pass


def restore(shell: "ShellWindow", state: Any, reconnect_devices: bool = True) -> bool:
    """Open the session ``state`` again; returns whether any document was opened."""
    if not isinstance(state, dict):
        return False
    if reconnect_devices:
        reconnect(shell, [uri for uri in state.get("devices", []) if isinstance(uri, str)])
    opened_any = False
    opened: list = []
    active = None
    area = shell.area
    orientation = Qt.Vertical if state.get("orientation") == "vertical" else Qt.Horizontal
    for number, entries in enumerate(state.get("groups", [])):
        current = None
        first_of_group = None
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            if not reconnect_devices and entry.get("kind") in ("device", "data"):
                continue  # (a device connected by hand meanwhile does not bring its card back)
            document = open_state(shell, entry)
            if document is None:
                continue
            # the start page alone is no session: nothing saved was open, the start is a fresh one
            opened_any = opened_any or entry.get("kind") != "start"
            opened.append(document)
            if first_of_group is None:
                first_of_group = document
                group = area.group_of(document)
                if number > 0 and group is not None and group.count() > 1:
                    area.split(document, orientation)  # the next group beside the last one
            elif area.group_of(document) is not area.group_of(first_of_group):
                area.move_to_group(document, area.group_of(first_of_group))
            if entry.get("current"):
                current = document
            if entry.get("active"):
                active = document
        if current is not None:
            area.activate(current)
    for entry in state.get("detached", []):
        if isinstance(entry, dict) and (reconnect_devices or entry.get("kind") not in ("device", "data")):
            document = open_state(shell, entry)
            if document is not None:
                opened_any = True
                area.detach(document)
    if active is not None and active in area.documents():
        area.activate(active)
    project = state.get("project")
    if not opened_any and isinstance(project, str) and os.path.isdir(project):
        opened_any = shell.open_project(project) is not None
    if not opened_any:
        # only the start page came back: closed again, the start makes its own (app.py)
        for document in opened:
            if document in area.documents():
                area.close_document(document, force=True)
        return False
    if state.get("console"):
        shell.show_console(state.get("console_tab") or "Problems")
    return True
