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

The user's own templates - projects saved with :func:`save_template` - are kept in the folder
``templates`` of the settings directory and come first, as the category *My templates*
(:data:`USER_CATEGORY`).
"""

from __future__ import annotations

import os
import re
import shutil
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
    #: a template of the user (:func:`save_template`), which can be deleted
    user: bool = False

    @property
    def name(self) -> str:
        """The name of a new project made from it: its folder without the number (``first-capture``)."""
        return re.sub(r"^\d+-", "", self.key.split("/")[-1]) or "project"


@dataclass
class Category:
    key: str
    title: str
    description: str = ""
    examples: list[Example] = field(default_factory=list)
    icon: str = ""


#: the folder of the user's templates in the settings directory
USER_FOLDER = "templates"
#: the key of their category (its examples are ``my-templates/<folder>``)
USER_CATEGORY = "my-templates"
#: what a template leaves out of a project: the files of the runs (captures, logs, reports in
#: ``data/``) and what Python and the system make
_SKIP = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store", ".git")


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


def user_directory() -> str:
    """The folder of the user's templates (made when needed)."""
    from ..core import settings

    return os.path.join(settings.settings_directory(), USER_FOLDER)


def _example(key: str, path: str, category_icon: str = "", user: bool = False) -> Example:
    data = _read(os.path.join(path, "project.yaml"))
    return Example(key, str(data.get("project") or _title_of(os.path.basename(path))),
                   " ".join(str(data.get("description") or "").split()), path, dict(data.get("devices") or {}),
                   bool(data.get("realtime", False)), str(data.get("icon") or category_icon or ""), user)


def user_templates() -> Optional[Category]:
    """The category *My templates* (``None`` while there are none), by title."""
    directory = user_directory()
    try:
        names = os.listdir(directory)
    except OSError:
        return None
    found = [_example(f"{USER_CATEGORY}/{name}", os.path.join(directory, name), "bookmark", user=True)
             for name in names if os.path.isfile(os.path.join(directory, name, "project.yaml"))]
    if not found:
        return None
    return Category(USER_CATEGORY, "My templates",
                    "Projects you saved as templates (Project → Save project as template…); right-click one to "
                    "delete it.", sorted(found, key=lambda example: example.title.lower()), "bookmark")


def catalog(directory: Optional[str] = None) -> list[Category]:
    """The categories with their examples, in the order of their folders; of the library openSciLab
    brings (no ``directory``) with the user's templates first."""
    own = user_templates() if directory is None else None
    directory = directory or library_directory()
    if directory is None:
        return [own] if own is not None else []
    result = [own] if own is not None else []
    for folder in sorted(os.listdir(directory)):
        path = os.path.join(directory, folder)
        if not os.path.isdir(path) or folder.startswith((".", "_")):
            continue
        info = _read(os.path.join(path, "category.yaml"))
        category = Category(folder, str(info.get("title") or _title_of(folder)),
                            " ".join(str(info.get("description") or "").split()), icon=str(info.get("icon") or ""))
        for name in sorted(os.listdir(path)):
            if os.path.isfile(os.path.join(path, name, "project.yaml")):
                category.examples.append(_example(f"{folder}/{name}", os.path.join(path, name), category.icon))
        if category.examples:
            result.append(category)
    return result


def examples(directory: Optional[str] = None) -> list[Example]:
    return [example for category in catalog(directory) for example in category.examples]


def find(key: str, directory: Optional[str] = None) -> Optional[Example]:
    return next((example for example in examples(directory) if example.key == key), None)


def save_template(root: str, title: str, description: str = "", open_files: Optional[list[str]] = None) -> Example:
    """Keep the project ``root`` as a template of the user: a copy without the files of its runs
    (``data/``), named ``title``; ``open_files`` (paths in the project) open when it is used."""
    from .project import Project
    from .templates import unique_folder

    title = " ".join(title.split())
    if not title:
        raise ValueError("a template needs a name")
    root = os.path.abspath(root)
    data_folder = os.path.join(root, "data")

    def skip(folder: str, names: list[str]) -> set[str]:
        if os.path.abspath(folder) == data_folder:
            return set(names)
        return set(_SKIP(folder, names))

    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "template"
    target = unique_folder(user_directory(), slug)
    shutil.copytree(root, target, ignore=skip)
    project = Project.open(target)
    project.name = title
    if description.strip():
        project.extra["description"] = " ".join(description.split())
    else:
        project.extra.pop("description", None)
    entries = [os.path.relpath(path, root).replace(os.sep, "/") for path in open_files or []
               if os.path.abspath(path).startswith(root + os.sep)]
    if entries:
        views = [entry for entry in project.extra.get("open") or [] if entry == "data"]
        project.extra["open"] = views + entries
    project.save()
    return _example(f"{USER_CATEGORY}/{os.path.basename(target)}", target, "bookmark", user=True)


def delete_template(key: str) -> None:
    """Delete the user's template ``key`` (``my-templates/<folder>``)."""
    category, _, name = key.partition("/")
    path = os.path.join(user_directory(), name)
    if category != USER_CATEGORY or not name or os.sep in name or name.startswith(".") or not os.path.isdir(path):
        raise ValueError(f"{key} is no template of yours")
    shutil.rmtree(path)
