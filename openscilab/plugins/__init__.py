# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Plugins: Python code that adds devices (``docs/drivers.md``).

A plugin is

* one of the modules of this package: the devices that come with openSciLab (:data:`BUILT_IN`),
* a ``.py`` file or a package folder in the ``plugins`` folder of the settings directory, or in a
  folder of the environment variable ``OPENSCILAB_PLUGINS`` (separated by ``:`` or ``;``), or
* an installed Python package with an entry point in the group ``openscilab.plugins`` (naming a
  module, or a function that is called once).

Importing it registers what it adds - kinds of devices with :func:`openscilab.driver.kinds.register`.
A plugin that brings user interface of its own (a device backend that asks for an address, see
:func:`openscilab.ui.devices.register_backend`) does that in a function ``setup_ui()``, which only
the application calls: the command line and the device processes stay without Qt.

Those of openSciLab load first, when a kind of device is looked up; the others once, when the
application starts or when an address of a kind not known yet is opened (command line, scripts,
device processes). A plugin that fails is reported (:func:`problems`), the others load. Qt free.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import os
import sys
import traceback
from dataclasses import dataclass
from types import ModuleType
from typing import Any, Optional

log = logging.getLogger(__name__)

GROUP = "openscilab.plugins"
FOLDER = "plugins"
ENVIRONMENT = "OPENSCILAB_PLUGINS"
#: the modules of plugin files are named so (pickled objects of a plugin find their class by it)
PREFIX = "openscilab_plugin_"
#: the plugins that come with openSciLab (modules of this package), in the order of the device list
BUILT_IN = ("pico", "arduino", "dslogic", "rigol", "simulation", "remote")


@dataclass
class Plugin:
    #: the file's or entry point's name
    name: str
    #: where it came from: a path, or ``entry point <value>``
    source: str
    module: Optional[ModuleType] = None
    #: why it could not be loaded (the last line of the error; ``details`` has the traceback)
    error: str = ""
    details: str = ""
    ui_ready: bool = False
    #: one of openSciLab's own (:data:`BUILT_IN`)
    builtin: bool = False


_plugins: list[Plugin] = []
_loaded = False
_loading = False
_built_in = False


def directories() -> list[str]:
    """The folders plugins are loaded from, the settings directory's last."""
    from ..core import settings

    folders = [path for path in os.environ.get(ENVIRONMENT, "").split(os.pathsep) if path.strip()]
    folders.append(os.path.join(settings.settings_directory(), FOLDER))
    seen: list[str] = []
    for folder in folders:
        folder = os.path.abspath(os.path.expanduser(folder))
        if folder not in seen:
            seen.append(folder)
    return seen


def load_built_in() -> None:
    """Load the plugins of openSciLab (once): its own kinds of devices."""
    global _built_in
    if _built_in:
        return
    _built_in = True  # (before: what they register asks for them)
    for name in BUILT_IN:
        plugin = Plugin(name, "built in", builtin=True)
        _plugins.append(plugin)
        try:
            plugin.module = importlib.import_module(f"{__name__}.{name}")
        except Exception as error:  # noqa: BLE001 - reported, the other plugins load
            _failed(plugin, error)


def load(ui: bool = False) -> list[Plugin]:
    """Load the plugins (once), openSciLab's own first; ``ui``: also call their ``setup_ui()`` (the
    application, with Qt)."""
    global _loaded, _loading
    load_built_in()
    if not _loaded and not _loading:
        _loading = True  # (a plugin that opens a device while it loads finds the kinds so far)
        try:
            for folder in directories():
                _load_folder(folder)
            _load_entry_points()
        finally:
            _loading = False
            _loaded = True
    if ui:
        for plugin in _plugins:
            if plugin.module is None or plugin.ui_ready:
                continue
            plugin.ui_ready = True
            setup = getattr(plugin.module, "setup_ui", None)
            if callable(setup):
                try:
                    setup()
                except Exception as error:  # noqa: BLE001 - one plugin must not stop the application
                    _failed(plugin, error)
    return list(_plugins)


def loaded() -> list[Plugin]:
    return [plugin for plugin in _plugins if not plugin.error]


def problems() -> list[Plugin]:
    return [plugin for plugin in _plugins if plugin.error]


def reset() -> None:
    """Forget the plugins of the user and the kinds they registered (tests); openSciLab's own stay."""
    global _loaded
    from ..driver import kinds

    for plugin in _plugins:
        if plugin.module is not None and not plugin.builtin:
            for entry in list(kinds._kinds.values()):
                if entry.module == plugin.module.__name__:
                    kinds.unregister(entry.kind)
            sys.modules.pop(plugin.module.__name__, None)
    _plugins[:] = [plugin for plugin in _plugins if plugin.builtin]
    _loaded = False


def _failed(plugin: Plugin, error: BaseException) -> None:
    plugin.error = f"{type(error).__name__}: {error}"
    plugin.details = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    log.warning("The plugin %s (%s) could not be loaded: %s", plugin.name, plugin.source, plugin.error)


def _load_folder(folder: str) -> None:
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return
    for name in names:
        path = os.path.join(folder, name)
        if name.startswith((".", "_")):
            continue
        if name.endswith(".py") and os.path.isfile(path):
            _load_file(name[:-3], path, None)
        elif os.path.isfile(os.path.join(path, "__init__.py")):
            _load_file(name, os.path.join(path, "__init__.py"), [path])


def _load_file(name: str, path: str, package: Optional[list[str]]) -> None:
    plugin = Plugin(name, path)
    _plugins.append(plugin)
    module_name = PREFIX + "".join(char if char.isalnum() else "_" for char in name)
    if module_name in sys.modules:  # (a plugin of the same name in an earlier folder)
        plugin.error = f"another plugin is named {name}"
        return
    try:
        spec = importlib.util.spec_from_file_location(module_name, path, submodule_search_locations=package)
        if spec is None or spec.loader is None:
            raise ImportError(f"{path} cannot be imported")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(module_name, None)
            raise
        plugin.module = module
    except Exception as error:  # noqa: BLE001 - reported, the other plugins load
        _failed(plugin, error)


def _load_entry_points() -> None:
    try:
        from importlib.metadata import entry_points

        found = list(entry_points(group=GROUP))
    except Exception:  # (broken package metadata must not stop the application)
        log.debug("The entry points of %s cannot be read", GROUP, exc_info=True)
        return
    for point in found:
        plugin = Plugin(point.name, f"entry point {point.value}")
        _plugins.append(plugin)
        try:
            target: Any = point.load()
            if isinstance(target, ModuleType):
                plugin.module = target
            elif callable(target):
                target()
                plugin.module = sys.modules.get(getattr(target, "__module__", ""))
            else:
                raise TypeError(f"{point.value} is neither a module nor a function")
        except Exception as error:  # noqa: BLE001 - reported, the other plugins load
            _failed(plugin, error)


def import_module(name: str) -> None:
    """Import the module that registered a kind (in a device process): a plugin file by loading the
    plugins, any other module by its name."""
    if not name or name in sys.modules:
        return
    if name.startswith(PREFIX):
        load()
        return
    importlib.import_module(name)
