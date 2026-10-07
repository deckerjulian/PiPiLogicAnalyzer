# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""A project: a folder with everything of one measurement setup.

::

    my-lab/
    ├── project.yaml     name, devices (also simulator profiles), settings, layout
    ├── flows/           *.flow.yaml, subflows
    ├── panels/          *.panel.yaml
    ├── nodes/           own Python nodes (@node)
    ├── waveforms/       waveforms, SDL patterns
    ├── data/            captures, logs, reports
    └── tests/           flow tests against the simulator (pytest)
"""

from __future__ import annotations

import os
from typing import Any, Optional

from ..core import yaml_text
from ..core.files import atomic_open
from .model import Flow, FlowError
from .nodes.registry import Registry, default_registry
from .yaml_io import safe_load as yaml_safe_load

PROJECT_FILE = "project.yaml"
FOLDERS = ("flows", "panels", "nodes", "waveforms", "data", "tests")


class Project:
    def __init__(self, root: str, name: str = "", devices: Optional[dict[str, str]] = None,
                 settings: Optional[dict[str, Any]] = None, extra: Optional[dict[str, Any]] = None) -> None:
        self.root = os.path.abspath(root)
        self.name = name or os.path.basename(self.root)
        #: device name -> address, for every flow of the project
        self.devices = dict(devices or {})
        self.settings = dict(settings or {})
        #: other sections of project.yaml (layout, simulation, ...), kept as they are
        self.extra = dict(extra or {})
        self._registry: Optional[Registry] = None

    # ---------------------------------------------------------------- files
    @property
    def file(self) -> str:
        return os.path.join(self.root, PROJECT_FILE)

    def folder(self, name: str) -> str:
        return os.path.join(self.root, name)

    @property
    def data_dir(self) -> str:
        return self.folder("data")

    @property
    def flows_dir(self) -> str:
        return self.folder("flows")

    def files(self, folder: str, suffixes: tuple[str, ...]) -> list[str]:
        directory = self.folder(folder)
        if not os.path.isdir(directory):
            return []
        found = []
        for current, _dirs, names in os.walk(directory):
            for name in sorted(names):
                if name.lower().endswith(suffixes):
                    found.append(os.path.join(current, name))
        return sorted(found)

    def flows(self) -> list[str]:
        return self.files("flows", (".flow.yaml", ".py"))

    def panels(self) -> list[str]:
        return self.files("panels", (".panel.yaml",))

    def waveforms(self) -> list[str]:
        return self.files("waveforms", (".wave.yaml", ".sdl"))

    def data_files(self) -> list[str]:
        return self.files("data", (".lac", ".lac.gz", ".sr", ".csv", ".vcd", ".html", ".log", ".json", ".txt"))

    # ------------------------------------------------------------ load/save
    @staticmethod
    def find(path: str) -> Optional[str]:
        """The project folder containing ``path`` (a file or folder), ``None`` outside a project."""
        current = os.path.abspath(path)
        if os.path.isfile(current):
            current = os.path.dirname(current)
        while True:
            if os.path.isfile(os.path.join(current, PROJECT_FILE)):
                return current
            parent = os.path.dirname(current)
            if parent == current:
                return None
            current = parent

    @staticmethod
    def open(path: str) -> "Project":
        root = path if os.path.isdir(path) else os.path.dirname(os.path.abspath(path))
        file = os.path.join(root, PROJECT_FILE)
        if not os.path.isfile(file):
            raise FlowError(f"{root} is not a project (no {PROJECT_FILE})")
        with open(file, encoding="utf-8") as handle:
            data = yaml_safe_load(handle) or {}
        if not isinstance(data, dict):
            raise FlowError(f"{file} is not a mapping")
        data = dict(data)
        return Project(root, str(data.pop("project", "") or ""), data.pop("devices", None) or {},
                       data.pop("settings", None) or {}, data)

    @staticmethod
    def create(root: str, name: str = "", devices: Optional[dict[str, str]] = None) -> "Project":
        project = Project(root, name, devices)
        os.makedirs(project.root, exist_ok=True)
        for folder in FOLDERS:
            os.makedirs(project.folder(folder), exist_ok=True)
        project.save()
        return project

    def to_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {"project": self.name}
        if self.devices:
            data["devices"] = dict(self.devices)
        if self.settings:
            data["settings"] = dict(self.settings)
        data.update(self.extra)
        return data

    def save(self) -> None:
        with atomic_open(self.file, newline="\n") as handle:
            yaml_text.dump(self.to_data(), handle)

    # --------------------------------------------------------------- flows
    def registry(self) -> Registry:
        """The built-in nodes and the project's own nodes (``nodes/*.py``)."""
        if self._registry is None:
            registry = default_registry.copy()
            for path in self.files("nodes", (".py",)):
                registry.load_module(path)
            self._registry = registry
        return self._registry

    def resolve(self, flow_path: str) -> str:
        if os.path.exists(flow_path):
            return os.path.abspath(flow_path)
        for candidate in (os.path.join(self.root, flow_path), os.path.join(self.flows_dir, flow_path),
                          os.path.join(self.flows_dir, f"{flow_path}.flow.yaml")):
            if os.path.exists(candidate):
                return candidate
        raise FlowError(f"no flow {flow_path!r} in {self.root}")

    def load_flow(self, flow_path: str) -> Flow:
        from .flow_files import load_flow

        flow = load_flow(self.resolve(flow_path), self.registry())
        return self.complete(flow)

    def complete(self, flow: Flow) -> Flow:
        """Device nodes without an address get the address of the project's device of their name."""
        for name, address in flow.devices.items():
            if not address and name in self.devices:
                flow.nodes[name].params["address"] = self.devices[name]
        return flow
