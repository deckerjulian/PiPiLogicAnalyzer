# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Copying a project of the library (``lab/examples.py``) into a new folder, and what it opens.

A project's ``project.yaml`` lists in ``open`` the files to open after copying it; besides files it
may name ``data`` (a data view that captures with the connected device), see :data:`VIEWS`.
"""

from __future__ import annotations

import os
import shutil

import yaml

#: entries of ``open`` that are no file of the project: a data view with the device that captures
VIEWS = ("data",)


def copy_project(source: str, target: str) -> str:
    """Copy the project folder ``source`` (of the library) to the new folder ``target``."""
    if os.path.exists(target) and os.listdir(target):
        raise FileExistsError(f"{target} is not empty")
    shutil.copytree(source, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
    for folder in ("flows", "panels", "nodes", "waveforms", "data", "tests"):
        os.makedirs(os.path.join(target, folder), exist_ok=True)
    keep = os.path.join(target, "data", ".gitkeep")
    if os.path.exists(keep):
        os.remove(keep)
    return target


def _open_entries(root: str) -> list[str]:
    try:
        with open(os.path.join(root, "project.yaml"), encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except (OSError, yaml.YAMLError):
        return []
    entries = data.get("open", []) if isinstance(data, dict) else []
    return [str(entry) for entry in entries] if isinstance(entries, list) else []


def files_to_open(root: str) -> list[str]:
    """The files the project wants opened (``open`` in ``project.yaml``)."""
    return [os.path.join(root, path) for path in _open_entries(root)
            if path not in VIEWS and os.path.exists(os.path.join(root, path))]


def views_to_open(root: str) -> list[str]:
    """The views the project wants opened that are no files (``data``)."""
    return [entry for entry in _open_entries(root) if entry in VIEWS]


def unique_folder(parent: str, name: str) -> str:
    path = os.path.join(parent, name)
    number = 2
    while os.path.exists(path):
        path = os.path.join(parent, f"{name}-{number}")
        number += 1
    return path
