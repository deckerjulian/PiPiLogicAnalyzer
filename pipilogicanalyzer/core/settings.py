# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of PiPiLogicAnalyzer, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Persistence of application settings (port of ``Classes/AppSettingsManager.cs``).

All settings live in a per-user configuration directory.  The original code
persisted the capture settings twice -- once in the application data folder and
once, through ``File.WriteAllText(settingsFile, ...)``, in the process' working
directory; only the first one was ever read back.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from typing import Any, Optional

APP_NAME = "PiPiLogicAnalyzer"
#: Settings directory of the releases before the project was renamed.
PREVIOUS_APP_NAME = "LogicAnalyzer"


def _base_directory(app_name: str) -> str:
    if sys.platform.startswith("win"):
        return os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), app_name)
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~/Library/Application Support"), app_name)
    config_home = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    return os.path.join(config_home, app_name)


def settings_directory() -> str:
    """Return (and create) the directory holding the application settings.

    The settings of the releases before the rename (``LogicAnalyzer``) are taken over once, so
    profiles and capture settings survive the new name.
    """
    override = (os.environ.get("PIPILOGICANALYZER_SETTINGS_DIR")
                or os.environ.get("LOGICANALYZER_SETTINGS_DIR"))
    base = override or _base_directory(APP_NAME)

    if not override and not os.path.isdir(base):
        previous = _base_directory(PREVIOUS_APP_NAME)
        if os.path.isdir(previous):
            try:
                shutil.copytree(previous, base)
            except OSError:  # pragma: no cover - permissions, disk full
                pass

    os.makedirs(base, exist_ok=True)
    return base


def settings_path(file_name: str) -> str:
    return os.path.join(settings_directory(), file_name)


def get_settings(file_name: str) -> Optional[Any]:
    """Load a JSON settings file, returning ``None`` when unavailable."""
    try:
        path = settings_path(file_name)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def persist_settings(file_name: str, settings: Any) -> bool:
    """Store ``settings`` as JSON, atomically replacing the previous file."""
    path = settings_path(file_name)
    temporary = f"{path}.tmp"
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(settings, handle, indent=2)
        os.replace(temporary, path)
        return True
    except (OSError, TypeError, ValueError):
        try:
            os.remove(temporary)
        except OSError:
            pass
        return False
