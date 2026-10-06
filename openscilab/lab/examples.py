# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The example library (``examples/library/``): the projects a new one starts from.

The start page shows them as tiles by category, the *Examples* menu by category; the first category
(*Start a project*) holds the plain starting points (an empty lab, the Logic Analyzer, data
acquisition, ...), but every example is one: opening it copies it into a temporary project.

Every example is a small project – ``project.yaml`` with its devices (simulators, so each one runs
without hardware), flows, panels and own nodes – in a category folder::

    examples/library/
    ├── 01-basics/
    │   ├── category.yaml          title: Basics, description: ...
    │   ├── 01-first-capture/      project.yaml (project: name, description, open: [...]), flows/
    │   └── 02-trigger/
    └── 02-digital/

Folders sort by their number; ``project`` in ``project.yaml`` is the title of an example, ``icon``
its icon (else the ``icon`` of its category); ``realtime: true`` marks one that does not work in
virtual time (see :attr:`Example.realtime`). Opening one copies it into a temporary project: the
example itself stays as it is, saving asks where to keep the copy. Besides files, ``open`` may name
``data`` - a data view that captures with the connected device (the Logic Analyzer).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

import yaml

#: where the library is: next to the package (a checkout, the packaged application)
LIBRARY_DIRECTORIES = [
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), os.pardir, "examples", "library"),
]


@dataclass
class Example:
    #: ``<category folder>/<example folder>``
    key: str
    title: str
    description: str
    path: str
    #: the devices it uses (``sim:uno``, ...)
    devices: dict = field(default_factory=dict)
    #: it only works in real time (``realtime: true``): a capture waits for a stimulus the flow
    #: starts later, and in virtual time the clock stands still while a capture is pending
    realtime: bool = False
    #: the name of its icon (``ui/icons.py``)
    icon: str = ""


@dataclass
class Category:
    key: str
    title: str
    description: str = ""
    examples: list[Example] = field(default_factory=list)
    icon: str = ""


def library_directory() -> Optional[str]:
    for directory in LIBRARY_DIRECTORIES:
        directory = os.path.normpath(directory)
        if os.path.isdir(directory):
            return directory
    return None


def _read(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _title_of(folder: str) -> str:
    """``03-rc-low-pass`` -> ``Rc low pass`` (when a file gives no title)."""
    name = folder.split("-", 1)[1] if "-" in folder and folder.split("-", 1)[0].isdigit() else folder
    return name.replace("-", " ").replace("_", " ").capitalize()


def catalog(directory: Optional[str] = None) -> list[Category]:
    """The categories with their examples, in the order of their folders."""
    directory = directory or library_directory()
    if directory is None:
        return []
    result = []
    for folder in sorted(os.listdir(directory)):
        path = os.path.join(directory, folder)
        if not os.path.isdir(path) or folder.startswith((".", "_")):
            continue
        info = _read(os.path.join(path, "category.yaml"))
        category = Category(folder, str(info.get("title") or _title_of(folder)),
                            " ".join(str(info.get("description") or "").split()), icon=str(info.get("icon") or ""))
        for name in sorted(os.listdir(path)):
            project = os.path.join(path, name, "project.yaml")
            if not os.path.isfile(project):
                continue
            data = _read(project)
            category.examples.append(Example(f"{folder}/{name}", str(data.get("project") or _title_of(name)),
                                             " ".join(str(data.get("description") or "").split()),
                                             os.path.join(path, name), dict(data.get("devices") or {}),
                                             bool(data.get("realtime", False)),
                                             str(data.get("icon") or category.icon or "")))
        if category.examples:
            result.append(category)
    return result


def examples(directory: Optional[str] = None) -> list[Example]:
    return [example for category in catalog(directory) for example in category.examples]


def find(key: str, directory: Optional[str] = None) -> Optional[Example]:
    return next((example for example in examples(directory) if example.key == key), None)
