# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Flows in Python: the same model as the YAML file and the editor, written as code.

::

    from openscilab.lab import flow, nodes as n

    with flow("Counter") as f:
        sim = f.device("sim", "sim:free")              # a device node
        cap = n.device.capture(sim, channels=["D0", "D1"], rate="1 MHz", samples=10_000)  # wired to it
        cap.capture >> n.data.file(path="counter.lac")
    result = f.run(fast=True)

``n.<group>.<name>(...)`` adds a node: devices, pins (``uno.pin("D9")``) and ports given as
arguments are wired or set, other positional arguments fill the parameters in their order,
keyword arguments name parameters or inputs (``in_`` for ``in``). ``a.out >> b.in`` wires two
ports, ``a >> b`` the first output to the first free input. :func:`to_python` writes a flow as
such a script; reading it back gives the same flow (and the same YAML).
"""

from __future__ import annotations

import keyword
import re
from typing import Any, Optional

from ..core.signals import DEVICE
from .model import DEVICE_NODE, SUBFLOW_PREFIX, Edge, Flow, FlowError, Node, PortRef
from .nodes.registry import NodeSpec, Registry, RegistryError, default_registry

_stack: list["FlowBuilder"] = []
#: flows created by ``with flow(...)`` (``openscilab run script.py`` runs the last one)
created: list["FlowBuilder"] = []
#: set by ``openscilab run`` while it executes a script: ``FlowBuilder.run()`` does nothing then
cli_mode = False
#: registry of flows created while :func:`from_python` runs a script (the project's nodes)
_script_registry: Optional[Registry] = None


def _current() -> "FlowBuilder":
    if not _stack:
        raise FlowError("nodes are created inside 'with flow(...) as f:'")
    return _stack[-1]


class PortHandle:
    """A port of a node in the DSL; ``>>`` wires it."""

    def __init__(self, node: "NodeRef", name: str) -> None:
        self.node = node
        self.name = name

    def __rshift__(self, other):
        target = other if isinstance(other, PortHandle) else other._first_free_input()
        self.node.builder._connect(self, target)
        return other

    def __repr__(self) -> str:
        return f"<port {self.node.id}.{self.name}>"


class NodeRef:
    def __init__(self, builder: "FlowBuilder", node: Node) -> None:
        self.builder = builder
        self.node = node

    @property
    def id(self) -> str:
        return self.node.id

    def __getattr__(self, name: str) -> PortHandle:
        if name.startswith("__"):
            raise AttributeError(name)
        if name.endswith("_") and keyword.iskeyword(name[:-1]):
            name = name[:-1]
        return PortHandle(self, name)

    def __getitem__(self, name: str) -> PortHandle:
        return PortHandle(self, name)

    def __rshift__(self, other):
        return self._first_output() >> other

    def _spec(self) -> NodeSpec:
        return self.builder.flow.spec(self.node, self.builder.registry)

    def _first_output(self) -> PortHandle:
        _inputs, outputs = self._spec().resolve_ports(self.node.params)
        if not outputs:
            raise FlowError(f"{self.id} has no output")
        return PortHandle(self, outputs[0].name)

    def _first_free_input(self) -> PortHandle:
        inputs, _outputs = self._spec().resolve_ports(self.node.params)
        inputs = [port for port in inputs if port.type != DEVICE]
        for port in inputs:
            if not self.builder.flow.edges_into(self.id, port.name):
                return PortHandle(self, port.name)
        if inputs:
            return PortHandle(self, inputs[0].name)
        raise FlowError(f"{self.id} has no input")

    def __repr__(self) -> str:
        return f"<node {self.id} ({self.node.type})>"


class DeviceRef(NodeRef):
    """A device node: given to a node it wires its ``device`` output to the node's ``device``
    input; ``uno.pin("D9")`` also sets the pin."""

    @property
    def name(self) -> str:
        return self.node.id

    def pin(self, pin: str) -> "PinRef":
        return PinRef(self, pin)

    def __repr__(self) -> str:
        return f"<device {self.id}>"


class PinRef:
    def __init__(self, device: NodeRef, pin: str) -> None:
        self.device = device
        self.pin = pin


def _is_device(value: Any) -> bool:
    return isinstance(value, NodeRef) and value.node.type == DEVICE_NODE


class FlowBuilder:
    """``with flow(name) as f:`` – the flow being written."""

    def __init__(self, name: str, registry: Optional[Registry] = None, parent: Optional["FlowBuilder"] = None,
                 description: str = "", **settings) -> None:
        self.flow = Flow(name=name, description=description, settings=dict(settings))
        self.registry = registry or _script_registry or default_registry
        self.parent = parent

    def __enter__(self) -> "FlowBuilder":
        _stack.append(self)
        if self.parent is None:
            created.append(self)
        return self

    def __exit__(self, *exc_info) -> None:
        _stack.remove(self)

    # -------------------------------------------------------------- content
    def device(self, name: str, address: Optional[str] = None) -> DeviceRef:
        """A device node: ``f.device("dho", "sim:dho924s")``, or ``f.device("sim:uno")`` named ``uno``;
        ``f.device("uno", "")`` is the project's device ``uno``."""
        if address is None:
            address = name
            name = re.sub(r"[^A-Za-z0-9_]", "_", address.split(":", 1)[-1].split("/")[-1]) or "device"
        node = self.flow.add_node(DEVICE_NODE, name, address=address)
        return DeviceRef(self, node)

    def subflow(self, name: str, description: str = "") -> "FlowBuilder":
        """``with f.subflow("filter") as s:`` – a subflow (node type ``subflow.filter``)."""
        child = FlowBuilder(name, self.registry, parent=self, description=description)
        self.flow.subflows[name] = child.flow
        return child

    def input(self, name: str, port: PortHandle) -> None:
        """Expose ``port`` as the input ``name`` of this subflow."""
        self.flow.inputs[name] = PortRef(port.node.id, port.name)

    def output(self, name: str, port: PortHandle) -> None:
        self.flow.outputs[name] = PortRef(port.node.id, port.name)

    def _connect(self, source: PortHandle, target: PortHandle) -> None:
        builder = source.node.builder
        if target.node.builder is not builder:
            raise FlowError("wires connect nodes of the same flow")
        edge = Edge(PortRef(source.node.id, source.name), PortRef(target.node.id, target.name))
        if edge not in builder.flow.edges:
            builder.flow.edges.append(edge)

    def _spec_for(self, type_name: str) -> NodeSpec:
        if type_name.startswith(SUBFLOW_PREFIX):
            scope = self
            while scope is not None:
                name = type_name[len(SUBFLOW_PREFIX):]
                if name in scope.flow.subflows:
                    return scope.flow.subflows[name].as_spec(name, self.registry)
                scope = scope.parent
        return self.registry.get(type_name)

    def add(self, type_name: str, *args, **kwargs) -> NodeRef:
        spec = self._spec_for(type_name)
        node_id = kwargs.pop("id", None)
        position = kwargs.pop("at", None)
        comment = kwargs.pop("comment", "")
        params: dict[str, Any] = {}
        wires: list[tuple[PortHandle, Optional[str]]] = []
        positional = [param for param in spec.params if param.kind not in ("address", "pin")]
        for value in args:
            if _is_device(value):
                wires.append((PortHandle(value, "device"), "device"))
            elif isinstance(value, PinRef):
                wires.append((PortHandle(value.device, "device"), "device"))
                params["pin"] = value.pin
            elif isinstance(value, (PortHandle, NodeRef)):
                wires.append((value if isinstance(value, PortHandle) else value._first_output(), None))
            else:
                if not positional:
                    raise FlowError(f"{type_name}: too many positional arguments")
                params[positional.pop(0).name] = value
        inputs_static = {port.name for port in spec.inputs}
        for key, value in kwargs.items():
            name = key[:-1] if key.endswith("_") and keyword.iskeyword(key[:-1]) else key
            if _is_device(value):
                wires.append((PortHandle(value, "device"), name))
            elif isinstance(value, (PortHandle, NodeRef)):
                wires.append((value if isinstance(value, PortHandle) else value._first_output(), name))
            elif isinstance(value, PinRef):
                wires.append((PortHandle(value.device, "device"), "device"))
                params[name] = value.pin
            else:
                if name in inputs_static and spec.param(name) is None:
                    raise FlowError(f"{type_name}: {name} is an input, give a port")
                params[name] = value
        node = self.flow.add_node(type_name, node_id, **params)
        node.position = tuple(position) if position is not None else None
        node.comment = comment
        reference = DeviceRef(self, node) if type_name == DEVICE_NODE else NodeRef(self, node)
        for source, port in wires:
            target = PortHandle(reference, port) if port else reference._first_free_input()
            self._connect(source, target)
        return reference

    # ------------------------------------------------------------- results
    def to_yaml(self) -> str:
        from .yaml_io import dumps

        return dumps(self.flow)

    def to_python(self) -> str:
        return to_python(self.flow)

    def save(self, path: str) -> None:
        from .yaml_io import save

        save(self.flow, path)

    def validate(self):
        return self.flow.validate(self.registry)

    def run(self, fast: bool = False, sim: bool = False, **options):
        """Run the flow (``fast``: virtual time; ``sim``: simulators for every device)."""
        if cli_mode:
            return None
        from .engine import Engine

        timeout = options.pop("timeout", None)
        engine = Engine(self.flow, registry=self.registry, mode="virtual" if fast else "real", simulate=sim, **options)
        return engine.run(timeout)


def flow(name: str = "Flow", description: str = "", registry: Optional[Registry] = None, **settings) -> FlowBuilder:
    return FlowBuilder(name, registry, description=description, **settings)


# ------------------------------------------------------------------ namespace
class _Group:
    def __init__(self, path: str) -> None:
        self._path = path

    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)
        return _type_or_group(f"{self._path}.{name}")

    def __call__(self, *args, **kwargs) -> NodeRef:
        return _current().add(self._path, *args, **kwargs)

    def __repr__(self) -> str:
        return f"<nodes {self._path}>"


def _type_or_group(path: str):
    return _Group(path)


class _Nodes:
    """``nodes.<group>.<name>(...)``; ``nodes.capture`` is short for ``nodes.device.capture``."""

    SHORT = {"capture": "device.capture", "stream": "device.stream", "scope": "view.scope"}

    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)
        if name in self.SHORT:
            return _Group(self.SHORT[name])
        return _Group(name)

    @staticmethod
    def edge(source: str, edge: str = "rising") -> dict:
        """A trigger on an edge: ``trigger=n.edge("D0")``."""
        return {"edge": edge, "source": source}

    @staticmethod
    def pattern(source: str, pattern: str) -> dict:
        return {"pattern": pattern, "source": source}


nodes = _Nodes()


# --------------------------------------------------------------------- export
def _identifier(name: str, taken: set[str]) -> str:
    base = re.sub(r"[^A-Za-z0-9_]", "_", name) or "x"
    if base[0].isdigit():
        base = f"_{base}"
    if keyword.iskeyword(base) or base in ("f", "n", "flow", "nodes"):
        base += "_"
    candidate = base
    number = 2
    while candidate in taken:
        candidate = f"{base}{number}"
        number += 1
    taken.add(candidate)
    return candidate


def _port(name: str) -> str:
    if keyword.iskeyword(name):
        return f"{name}_"
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
        return name
    return f"[{name!r}]"


def _access(variable: str, port: str) -> str:
    text = _port(port)
    return f"{variable}{text}" if text.startswith("[") else f"{variable}.{text}"


def _flow_body(flow: Flow, builder: str, indent: str, taken: set[str], registry: Registry) -> list[str]:
    lines: list[str] = []
    for name, subflow in flow.subflows.items():
        variable = _identifier(f"sub_{name}", taken)
        description = f", description={subflow.description!r}" if subflow.description else ""
        lines.append(f"{indent}with {builder}.subflow({name!r}{description}) as {variable}:")
        inner = _flow_body(subflow, variable, indent + "    ", taken, registry)
        lines += inner or [f"{indent}    pass"]
    variables: dict[str, str] = {}
    for node in flow.nodes.values():
        variable = _identifier(node.id, taken)
        variables[node.id] = variable
        params = dict(node.params)
        if node.type == DEVICE_NODE and list(params) == ["address"] and params["address"] \
                and node.position is None and not node.comment:
            lines.append(f"{indent}{variable} = {builder}.device({node.id!r}, {params['address']!r})")
            continue
        arguments = [f"id={node.id!r}"]
        for key, value in params.items():
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key) and not keyword.iskeyword(key):
                arguments.append(f"{key}={value!r}")
            else:
                arguments.append(f"**{{{key!r}: {value!r}}}")
        if node.position is not None:
            arguments.append(f"at={tuple(node.position)!r}")
        if node.comment:
            arguments.append(f"comment={node.comment!r}")
        lines.append(f"{indent}{variable} = n.{node.type}({', '.join(arguments)})")
    for edge in flow.edges:
        lines.append(f"{indent}{_access(variables[edge.source.node], edge.source.port)} >> "
                     f"{_access(variables[edge.target.node], edge.target.port)}")
    for name, ref in flow.inputs.items():
        lines.append(f"{indent}{builder}.input({name!r}, {_access(variables[ref.node], ref.port)})")
    for name, ref in flow.outputs.items():
        lines.append(f"{indent}{builder}.output({name!r}, {_access(variables[ref.node], ref.port)})")
    return lines


def to_python(flow: Flow, registry: Optional[Registry] = None) -> str:
    """``flow`` as a Python script using the DSL."""
    registry = registry or default_registry
    options = [repr(flow.name)]
    if flow.description:
        options.append(f"description={flow.description!r}")
    for key, value in flow.settings.items():
        options.append(f"{key}={value!r}")
    taken: set[str] = set()
    lines = ["from openscilab.lab import flow, nodes as n", "", f"with flow({', '.join(options)}) as f:"]
    body = _flow_body(flow, "f", "    ", taken, registry)
    lines += body or ["    pass"]
    lines += ["", 'if __name__ == "__main__":', "    print(f.run())"]
    return "\n".join(lines) + "\n"


def from_python(source: str, registry: Optional[Registry] = None, filename: str = "<flow>") -> Flow:
    """The (last) flow a DSL script defines, without running it."""
    global cli_mode, _script_registry
    previous, cli_mode = cli_mode, True
    previous_registry, _script_registry = _script_registry, registry
    before = len(created)
    try:
        namespace: dict[str, Any] = {"__name__": "__openscilab_flow__", "__file__": filename}
        exec(compile(source, filename, "exec"), namespace)  # noqa: S102 - the user's own flow script
    finally:
        cli_mode = previous
        _script_registry = previous_registry
    if len(created) == before:
        raise FlowError(f"{filename} defines no flow (use 'with flow(...) as f:')")
    return created[-1].flow


__all__ = ["DeviceRef", "FlowBuilder", "NodeRef", "PortHandle", "flow", "from_python", "nodes", "to_python",
           "RegistryError"]
