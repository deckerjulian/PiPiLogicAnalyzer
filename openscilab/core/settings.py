# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of openSciLab, a port and extension of his software;
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
import logging
import os
import shutil
import sys
from typing import Any, Optional

from .files import atomic_open

log = logging.getLogger(__name__)

APP_NAME = "openSciLab"


def _base_directory(app_name: str) -> str:
    if sys.platform.startswith("win"):
        return os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), app_name)
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~/Library/Application Support"), app_name)
    config_home = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    return os.path.join(config_home, app_name)


def settings_directory() -> str:
    """Return (and create) the directory holding the application settings."""
    base = os.environ.get("OPENSCILAB_SETTINGS_DIR") or _base_directory(APP_NAME)

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
    except OSError:
        return None
    except ValueError as error:
        # A damaged file: keep it beside the new one instead of overwriting it with defaults on
        # the next save (the profiles of a user are in such a file).
        kept = f"{path}.bad"
        try:
            os.replace(path, kept)
            log.warning("%s is damaged (%s); kept as %s", path, error, kept)
        except OSError:
            log.warning("%s is damaged (%s)", path, error)
        return None


def keep_copy(file_name: str) -> Optional[str]:
    """Copy a settings file to ``<name>.bad`` (parts of it cannot be read and the next save
    would drop them); the path of the copy, or ``None``."""
    path = settings_path(file_name)
    kept = f"{path}.bad"
    try:
        shutil.copy2(path, kept)
    except OSError:
        return None
    log.warning("%s is kept as %s", path, kept)
    return kept


def persist_settings(file_name: str, settings: Any) -> bool:
    """Store ``settings`` as JSON, atomically replacing the previous file."""
    try:
        with atomic_open(settings_path(file_name)) as handle:
            json.dump(settings, handle, indent=2)
        return True
    except (OSError, TypeError, ValueError) as error:
        log.warning("%s could not be saved: %s", file_name, error)
        return False
