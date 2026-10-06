# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The node registry: what kinds of nodes a flow can use.

A node type is registered with the decorator :func:`node` on

* a subclass of :class:`NodeRuntime` (the built-in nodes): ``run()`` is its active part (a
  coroutine, e.g. a timer), ``on_input()`` reacts to values on its inputs;
* an ``async def`` function ``(ctx)``: the run part of a node, for own nodes;
* a plain function: computed whenever an input changes, from the latest value of every input;
  its result is the value of the only output, or a dict of output values.

::

    @node("my.double", inputs=[In("x", "Scalar")], outputs=[Out("y", "Scalar")])
    def double(x):
        return 2 * x
"""

from __future__ import annotations

import importlib
import inspect
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

from ...core import signals

#: Groups of the palette, in their order.
GROUPS = {
    "device": "Devices",
    "remote": "Remote devices",
    "timing": "Time",
    "gpio": "GPIO",
    "gen": "Generator",
    "decode": "Decoders",
    "measure": "Measurement",
    "dsp": "Signal processing",
    "control": "Control",
    "data": "Data",
    "view": "Views",
    "panel": "Panel",
    "report": "Report",
    "convert": "Conversions",
    "structure": "Structure",
}

#: Modules whose nodes are made on demand by a function ``nodes()``, by group (the decoders: loading
#: the sigrok decoders takes a moment, so flows without decoders do not wait for it).
LAZY_MODULES = {"decode": "openscilab.lab.nodes.decode"}

#: Modules with the built-in nodes (loaded on first use of the registry).
BUILTIN_MODULES = [
    "openscilab.lab.nodes.device",
    "openscilab.lab.nodes.remote",
    "openscilab.lab.nodes.timing",
    "openscilab.lab.nodes.control",
    "openscilab.lab.nodes.data",
    "openscilab.lab.nodes.view",
    "openscilab.lab.nodes.structure",
    "openscilab.lab.nodes.convert",
    "openscilab.lab.nodes.measure",
    "openscilab.lab.nodes.dsp",
    "openscilab.lab.nodes.gpio",
    "openscilab.lab.nodes.gen",
    "openscilab.lab.nodes.report",
]


class RegistryError(ValueError):
    pass


@dataclass(frozen=True)
class PortSpec:
    name: str
    type: str = signals.ANY
    description: str = ""
    #: an input that does not have to be wired
    optional: bool = True
    #: an input that takes several wires (values arrive in turn)
    multiple: bool = False


def In(name: str, type: str = signals.ANY, description: str = "", optional: bool = True,  # noqa: N802
       multiple: bool = False) -> PortSpec:
    return PortSpec(name, type, description, optional, multiple)


def Out(name: str, type: str = signals.ANY, description: str = "") -> PortSpec:  # noqa: N802
    return PortSpec(name, type, description)


#: Kinds of parameters (the inspector builds its editor from them).
#: ``address``: where an instrument is (``sim:uno``, ``pico:/dev/cu.usbmodem1``)
PARAM_KINDS = ("float", "int", "str", "bool", "choice", "quantity", "list", "dict", "address", "pin",
               "path", "code", "table", "any")


@dataclass(frozen=True)
class ParamSpec:
    name: str
    kind: str = "any"
    default: Any = None
    #: unit of a ``quantity`` (``"s"``, ``"Hz"``, ``"V"``)
    unit: str = ""
    choices: tuple = ()
    description: str = ""
    required: bool = False
    #: where the editor finds values to offer (:data:`SUGGESTIONS`); the check warns of others
    suggest: str = ""
    #: the keys of a ``dict`` parameter that maps fixed names to values (a decoder's channels)
    keys: tuple = ()

    def __post_init__(self) -> None:
        if self.kind not in PARAM_KINDS:
            raise RegistryError(f"unknown parameter kind {self.kind!r}")
        if self.suggest and self.suggest.split(":", 1)[0] not in SUGGESTIONS:
            raise RegistryError(f"unknown suggestion {self.suggest!r}")


#: Sources of suggested values (``ParamSpec.suggest``), answered by :mod:`openscilab.lab.hints`:
#: ``channels``: capture channels of the device the node is wired to; ``analog``: its analog inputs;
#: ``pins`` (``pins:PWM``: those that can do it, see ``PIN_*`` of ``core/instrument.py``): its
#: pins; ``outputs``: its generator outputs; ``upstream_channels``: the channels of the capture
#: wired to the node; ``upstream_columns``: the columns of the table wired to it; ``remote_inputs``,
#: ``remote_outputs``, ``remote_commands``, ``remote_syncs``: what a remote device describes.
SUGGESTIONS = ("channels", "analog", "pins", "outputs", "upstream_channels", "upstream_columns", "remote_inputs",
               "remote_outputs", "remote_commands", "remote_syncs")


def Param(name: str, kind: str = "any", default: Any = None, unit: str = "", choices: Sequence = (),  # noqa: N802
          description: str = "", required: bool = False, suggest: str = "", keys: Sequence = ()) -> ParamSpec:
    return ParamSpec(name, kind, default, unit, tuple(choices), description, required, suggest, tuple(keys))


PortsFunction = Callable[[dict], tuple[list[PortSpec], list[PortSpec]]]


@dataclass
class NodeSpec:
    """A node type."""

    type: str
    implementation: Any
    title: str = ""
    description: str = ""
    inputs: list[PortSpec] = field(default_factory=list)
    outputs: list[PortSpec] = field(default_factory=list)
    params: list[ParamSpec] = field(default_factory=list)
    #: ``ports(params) -> (inputs, outputs)`` for nodes whose ports depend on their parameters
    #: (a capture has an output per channel); the static ports above are added to them.
    ports: Optional[PortsFunction] = None
    icon: str = "nodes"
    #: where the node was defined (module or file), for errors and the palette
    source: str = ""

    @property
    def group(self) -> str:
        return self.type.split(".", 1)[0] if "." in self.type else "structure"

    @property
    def name(self) -> str:
        return self.type.rsplit(".", 1)[-1]

    def param(self, name: str) -> Optional[ParamSpec]:
        return next((param for param in self.params if param.name == name), None)

    def resolve_ports(self, params: dict) -> tuple[list[PortSpec], list[PortSpec]]:
        inputs, outputs = list(self.inputs), list(self.outputs)
        if self.ports is not None:
            try:
                extra_in, extra_out = self.ports(params)
            except Exception:  # noqa: BLE001 - invalid parameters: the static ports only
                extra_in, extra_out = [], []
            names = {port.name for port in inputs}
            inputs += [port for port in extra_in if port.name not in names]
            names = {port.name for port in outputs}
            outputs += [port for port in extra_out if port.name not in names]
        return inputs, outputs

    def input(self, name: str, params: Optional[dict] = None) -> Optional[PortSpec]:
        inputs, _ = self.resolve_ports(params or {})
        return next((port for port in inputs if port.name == name), None)

    def output(self, name: str, params: Optional[dict] = None) -> Optional[PortSpec]:
        _, outputs = self.resolve_ports(params or {})
        return next((port for port in outputs if port.name == name), None)

    def defaults(self) -> dict:
        return {param.name: param.default for param in self.params if param.default is not None}


class Registry:
    """Node types by name."""

    def __init__(self, load_builtin: bool = True) -> None:
        self._specs: dict[str, NodeSpec] = {}
        self._load_builtin = load_builtin
        self._loaded = False
        self._lazy_loaded: set[str] = set()

    def _ensure_loaded(self) -> None:
        if self._loaded or not self._load_builtin:
            return
        self._loaded = True
        for module in BUILTIN_MODULES:
            imported = importlib.import_module(module)
            for spec in getattr(imported, "NODES", []):
                self.add(spec, replace=True)

    def _ensure_group(self, group: str) -> None:
        if not self._load_builtin or group in self._lazy_loaded or group not in LAZY_MODULES:
            return
        self._lazy_loaded.add(group)
        module = importlib.import_module(LAZY_MODULES[group])
        for spec in module.nodes():
            if spec.type not in self._specs:
                self._specs[spec.type] = spec

    def _ensure_all(self) -> None:
        self._ensure_loaded()
        for group in LAZY_MODULES:
            self._ensure_group(group)

    def add(self, spec: NodeSpec, replace: bool = False) -> NodeSpec:
        if spec.type in self._specs and not replace:
            raise RegistryError(f"the node type {spec.type!r} exists already ({self._specs[spec.type].source})")
        self._specs[spec.type] = spec
        return spec

    def remove(self, type_name: str) -> None:
        self._specs.pop(type_name, None)

    def get(self, type_name: str) -> NodeSpec:
        self._ensure_loaded()
        if type_name not in self._specs:
            self._ensure_group(type_name.split(".", 1)[0])
        try:
            return self._specs[type_name]
        except KeyError:
            raise RegistryError(f"unknown node type {type_name!r}") from None

    def find(self, type_name: str) -> Optional[NodeSpec]:
        self._ensure_loaded()
        if type_name not in self._specs:
            self._ensure_group(type_name.split(".", 1)[0])
        return self._specs.get(type_name)

    def __contains__(self, type_name: str) -> bool:
        return self.find(type_name) is not None

    def types(self) -> list[str]:
        self._ensure_all()
        return sorted(self._specs)

    def specs(self, lazy: bool = True) -> list[NodeSpec]:
        """Every node type. ``lazy=False``: without the groups that are only loaded when they
        are needed (the decoders: importing all of them takes a moment)."""
        if lazy:
            self._ensure_all()
        else:
            self._ensure_loaded()
        return [self._specs[name] for name in sorted(self._specs)]

    def pending_groups(self) -> list[str]:
        """The groups :meth:`specs` leaves out with ``lazy=False`` (not loaded yet)."""
        if not self._load_builtin:
            return []
        return [group for group in LAZY_MODULES if group not in self._lazy_loaded]

    def groups(self) -> dict[str, list[NodeSpec]]:
        result: dict[str, list[NodeSpec]] = {}
        for spec in self.specs():
            result.setdefault(spec.group, []).append(spec)
        return result

    def copy(self) -> "Registry":
        self._ensure_loaded()
        other = Registry(load_builtin=self._load_builtin)
        other._loaded = True
        other._specs = dict(self._specs)
        other._lazy_loaded = set(self._lazy_loaded)
        return other

    def load_module(self, path: str) -> list[NodeSpec]:
        """Register the nodes of a Python file (``nodes/`` of a project); returns them."""
        import importlib.util
        import os
        import sys

        name = f"openscilab_project_nodes_{abs(hash(os.path.abspath(path)))}"
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise RegistryError(f"{path} is not a Python module")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        before = len(_COLLECTED)
        _COLLECTING.append(self)
        try:
            spec.loader.exec_module(module)
        finally:
            _COLLECTING.pop()
        added = _COLLECTED[before:]
        for item in added:
            item.source = path
        return added


#: The registry used when none is given; built-in nodes plus those of ``@node`` in modules.
default_registry = Registry()
_COLLECTED: list[NodeSpec] = []
#: Registries that receive nodes defined while a project module is loaded
_COLLECTING: list[Registry] = []


def node(type_name: str, *, title: str = "", description: str = "", inputs: Sequence[PortSpec] = (),
         outputs: Sequence[PortSpec] = (), params: Sequence[ParamSpec] = (), ports: Optional[PortsFunction] = None,
         icon: str = "nodes", register: bool = True):
    """Decorator registering a node type (see the module documentation)."""

    def decorate(target):
        spec = NodeSpec(
            type=type_name,
            implementation=target,
            title=title or type_name.rsplit(".", 1)[-1].replace("_", " ").capitalize(),
            description=description or inspect.getdoc(target) or "",
            inputs=list(inputs),
            outputs=list(outputs),
            params=list(params),
            ports=ports,
            icon=icon,
            source=getattr(target, "__module__", ""),
        )
        if inspect.isfunction(target) and not inputs and not outputs:
            _infer_function_ports(spec, target)
        _COLLECTED.append(spec)
        if register:
            if _COLLECTING:
                _COLLECTING[-1].add(spec, replace=True)
            elif not spec.source.startswith("openscilab.lab.nodes."):
                default_registry.add(spec, replace=True)
        try:
            target.node_spec = spec
        except (AttributeError, TypeError):
            pass
        return target

    return decorate


def _infer_function_ports(spec: NodeSpec, function) -> None:
    """Inputs of a plain function node from its arguments; one output ``out``."""
    if inspect.iscoroutinefunction(function):
        return
    for parameter in inspect.signature(function).parameters.values():
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            continue
        if parameter.name == "params":
            continue  # the parameters of the node, given by the engine: not an input to wire
        annotation = parameter.annotation if isinstance(parameter.annotation, str) else signals.ANY
        if annotation not in signals.SIGNAL_TYPES:
            annotation = signals.ANY
        spec.inputs.append(PortSpec(parameter.name, annotation, optional=parameter.default is not parameter.empty))
    spec.outputs.append(PortSpec("out", signals.ANY))


def collect(module_globals: dict) -> list[NodeSpec]:
    """The node specs defined in a built-in module (its ``NODES`` list)."""
    return [value.node_spec for value in module_globals.values() if hasattr(value, "node_spec")]
