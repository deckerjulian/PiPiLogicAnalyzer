# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Recently opened files and projects (start page, *Project → Recent*)."""

from __future__ import annotations

import logging
import os

from . import settings

log = logging.getLogger(__name__)

RECENT_FILE = "recent.json"
#: Entries kept per list.
MAX_ENTRIES = 12


def limit() -> int:
    """Entries kept per list (*Settings → Data*)."""
    from . import preferences

    try:
        return max(int(preferences.get("data.recent_count")), 1)
    except Exception:  # noqa: BLE001
        log.debug("return max(int(preferences.get('data.recent_count')), 1) failed (ignored)", exc_info=True)
        return MAX_ENTRIES


def _load() -> dict:
    data = settings.get_settings(RECENT_FILE)
    return data if isinstance(data, dict) else {}


def entries(kind: str = "files", existing_only: bool = True) -> list[str]:
    """Recent paths of ``kind`` (``"files"`` or ``"projects"``), newest first."""
    paths = [path for path in _load().get(kind, []) if isinstance(path, str)]
    if existing_only:
        paths = [path for path in paths if os.path.exists(path)]
    return paths


def add(path: str, kind: str = "files") -> None:
    """Put ``path`` at the top of the list ``kind``."""
    data = _load()
    path = os.path.abspath(path)
    paths = [entry for entry in data.get(kind, []) if entry != path]
    data[kind] = ([path] + paths)[:limit()]
    settings.persist_settings(RECENT_FILE, data)


def clear(kind: str = "files") -> None:
    data = _load()
    data[kind] = []
    settings.persist_settings(RECENT_FILE, data)
