# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Opening a flow from either of its files: ``*.flow.yaml`` or a Python script with the DSL."""

from __future__ import annotations

from typing import Optional

from . import yaml_io
from .model import Flow
from .nodes.registry import Registry


def load_flow(path: str, registry: Optional[Registry] = None) -> Flow:
    if path.lower().endswith(".py"):
        from .dsl import from_python

        with open(path, encoding="utf-8") as handle:
            return from_python(handle.read(), registry, filename=path)
    return yaml_io.load(path)
