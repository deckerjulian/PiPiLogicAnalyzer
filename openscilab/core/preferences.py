# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The user's preferences (*Settings…*): appearance, startup, navigation, data, devices.

Stored in ``preferences.json`` in the settings directory. Every key has a default, so a missing
or damaged file gives the defaults; unknown keys in the file are ignored, values of the wrong
type fall back to the default. Qt-free: the shell tells its parts about changes.
"""

from __future__ import annotations

import os
from typing import Any

from . import settings

PREFERENCES_FILE = "preferences.json"

THEMES = ("system", "dark", "light")
WHEEL_MODES = ("zoom", "scroll")

#: devices on USB are read in a device process unless switched off (``driver/process``)
DEVICE_PROCESS = True

DEFAULTS: dict[str, Any] = {
    # appearance
    "appearance.theme": "dark",
    #: point size of the application font; 0: the system size
    "appearance.font_size": 10,
    # startup
    "startup.show_start_page": True,
    "startup.restore_session": True,
    #: connect the devices of the last session again (with their cards and data views)
    "startup.reconnect_devices": False,
    # navigation in the flow graph and the waveform
    #: what the mouse wheel does without modifiers: "zoom" (scroll with Ctrl/Cmd) or "scroll"
    "navigation.wheel": "zoom",
    "navigation.invert_zoom": False,
    #: flows arrange their nodes again whenever nodes or wires are added or removed
    "flow.auto_arrange": False,
    #: the node palette shows beside a flow that is edited
    "flow.show_nodes": True,
    # data
    #: where unsaved flows and new projects go; "" = ~/Documents/openSciLab
    "data.folder": "",
    "data.recent_count": 12,
    # devices
    "devices.refresh_s": 2.0,
    "devices.show_simulators": True,
    #: the simulators of the device list are shown (their group is open)
    "devices.simulators_open": False,
    #: hardware devices are read in a process of their own (docs/timing.md): nothing the application
    #: does can delay reading them
    "devices.process": DEVICE_PROCESS,
    #: the processes that read devices run with a higher priority (macOS, Linux: asks for the rights
    #: when openSciLab starts, see core/priority.py; Windows needs none)
    "devices.high_priority": False,
    # remote devices (scripts on other computers, package openscilab_device)
    #: accept remote devices (the server listens on the port only then)
    "remote.enabled": False,
    "remote.port": 24050,
    #: the token a device needs (made when remote devices are switched on)
    "remote.token": "",
    #: tell devices in the local network where openSciLab is (UDP beacon)
    "remote.beacon": True,
    # time (docs/timing.md)
    #: what this computer's clock follows: "free" (nothing), "ntp" or "ptp" - a device stamping in
    #: a shared time scale (UTC, TAI) is then converted without measuring
    "timing.host_clock": "free",
    #: how close this computer's clock is to the time scale (seconds; 0: the usual for its kind)
    "timing.host_accuracy": 0.0,
}

HOST_CLOCKS = ("free", "ntp", "ptp")

CHOICES: dict[str, tuple] = {
    "appearance.theme": THEMES,
    "navigation.wheel": WHEEL_MODES,
    "timing.host_clock": HOST_CLOCKS,
}

_cache: dict[str, Any] | None = None


def _valid(key: str, value: Any) -> bool:
    default = DEFAULTS[key]
    if key in CHOICES:
        return value in CHOICES[key]
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, (int, float)):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0
    return isinstance(value, type(default))


def _load() -> dict[str, Any]:
    global _cache
    if _cache is None:
        stored = settings.get_settings(PREFERENCES_FILE)
        stored = stored if isinstance(stored, dict) else {}
        _cache = {key: stored[key] if key in stored and _valid(key, stored[key]) else default
                  for key, default in DEFAULTS.items()}
    return _cache


def reload() -> None:
    """Read the file again (after the settings directory changed, in tests)."""
    global _cache
    _cache = None


def get(key: str) -> Any:
    return _load()[key]


def values() -> dict[str, Any]:
    return dict(_load())


def update(changes: dict[str, Any]) -> dict[str, Any]:
    """Store ``changes`` (invalid values are refused with ``ValueError``); returns what changed."""
    current = _load()
    changed = {}
    for key, value in changes.items():
        if key not in DEFAULTS:
            raise ValueError(f"unknown preference {key!r}")
        if isinstance(DEFAULTS[key], float) and isinstance(value, int) and not isinstance(value, bool):
            value = float(value)
        if not _valid(key, value):
            raise ValueError(f"{value!r} is not a valid value of {key}")
        if current[key] != value:
            changed[key] = value
    if changed:
        current.update(changed)
        settings.persist_settings(PREFERENCES_FILE, current)
    return changed


def reset() -> dict[str, Any]:
    """Back to the defaults; returns what changed."""
    return update(dict(DEFAULTS))


def default_folder(create: bool = False) -> str:
    """The folder for unsaved flows, new projects and save dialogs without a project."""
    folder = get("data.folder") or os.path.join(os.path.expanduser("~"), "Documents", "openSciLab")
    if create:
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError:
            return os.path.expanduser("~")
    return folder
