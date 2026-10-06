# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The flow model: nodes, ports and edges (``*.flow.yaml``).

A :class:`Flow` is the canonical form of a measurement program; the YAML file
(:mod:`.yaml_io`), the visual editor and the Python DSL (:mod:`.dsl`) are views of it::

    flow: Counter
    nodes:
      sim:   {type: device.instrument, address: "sim:free"}
      timer: {type: control.timer, interval: 10 ms, count: 3}
      cap:   {type: device.capture, channels: [D0, D1], rate: 1 MHz, samples: 1000}
    edges:
      - sim.device -> cap.device
      - timer.tick -> cap.arm

Devices are nodes (``device.instrument``) too: their ``device`` output is wired to the ``device``
input of every node that uses the instrument.

Node parameters stay as written (``"10 ms"``); the node types (:mod:`.nodes.registry`) say what
they mean. :meth:`Flow.validate` checks node types, ports, parameters and the types
of the wires, and proposes conversion nodes for wires whose types do not fit.

*Subflows* are flows used as nodes: defined in ``subflows:`` of the file (node type
``subflow.<name>``) with ``inputs:``/``outputs:`` naming the inner ports they expose.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from ..core import signals
from .nodes.registry import NodeSpec, PortSpec, Registry, RegistryError, default_registry

_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SUBFLOW_PREFIX = "subflow."
#: the node type of an instrument in a flow
DEVICE_NODE = "device.instrument"


class FlowError(ValueError):
    pass


@dataclass(frozen=True)
class PortRef:
    node: str
    port: str

    def __str__(self) -> str:
        return f"{self.node}.{self.port}"

    @staticmethod
    def parse(text: str) -> "PortRef":
        # node names have no dots; ports may (channels of a multi device: capture.B2.GP5)
        node, sep, port = text.strip().partition(".")
        if not sep or not node or not port:
            raise FlowError(f"{text!r} is not a port (write node.port)")
        return PortRef(node, port)


@dataclass(frozen=True)
class Edge:
    source: PortRef
    target: PortRef

    def __str__(self) -> str:
        return f"{self.source} -> {self.target}"

    @staticmethod
    def parse(text: str) -> "Edge":
        if "->" not in text:
            raise FlowError(f"{text!r} is not an edge (write a.out -> b.in)")
        left, right = text.split("->", 1)
        return Edge(PortRef.parse(left), PortRef.parse(right))


@dataclass
class Node:
    id: str
    type: str
    #: parameters as written in the file, in their order
    params: dict[str, Any] = field(default_factory=dict)
    #: position in the graph view (``at: [x, y]``), ``None`` before it was placed
    position: Optional[tuple[float, float]] = None
    #: a comment shown at the node
    comment: str = ""

    def param(self, name: str, default: Any = None) -> Any:
        return self.params.get(name, default)


@dataclass
class Problem:
    """A finding of :meth:`Flow.validate`."""

    severity: str  # "error" or "warning"
    text: str
    node: str = ""
    edge: Optional[Edge] = None
    #: a node type that would turn the source type into the target type
    suggestion: str = ""

    def __str__(self) -> str:
        where = f"{self.node}: " if self.node and not self.edge else (f"{self.edge}: " if self.edge else "")
        hint = f" (insert {self.suggestion})" if self.suggestion else ""
        return f"{where}{self.text}{hint}"


@dataclass
class Flow:
    name: str = "Flow"
    description: str = ""
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    #: subflows by name (node type ``subflow.<name>``)
    subflows: dict[str, "Flow"] = field(default_factory=dict)
    #: exposed ports of a subflow: name -> inner port
    inputs: dict[str, PortRef] = field(default_factory=dict)
    outputs: dict[str, PortRef] = field(default_factory=dict)
    #: settings of the run: ``seed``, ``duration``
    settings: dict[str, Any] = field(default_factory=dict)

    @property
    def devices(self) -> dict[str, str]:
        """The device nodes: name -> address (``sim:uno``, ``pico:/dev/cu.usbmodem1``; ``""``: the
        project's device of that name)."""
        return {node_id: str(node.params.get("address") or "") for node_id, node in self.nodes.items()
                if node.type == DEVICE_NODE}

    def device_of(self, node_id: str, port: str = "device") -> Optional[str]:
        """The device node wired to ``node_id.port``, ``None`` without a wire."""
        for edge in self.edges_into(node_id, port):
            if edge.source.node in self.nodes and self.nodes[edge.source.node].type == DEVICE_NODE:
                return edge.source.node
        return None

    # ------------------------------------------------------------ editing
    def add_node(self, type_name: str, node_id: Optional[str] = None, **params) -> Node:
        position = params.pop("position", None)
        node_id = node_id or self.free_id(type_name.rsplit(".", 1)[-1])
        if not _ID_RE.match(node_id):
            raise FlowError(f"{node_id!r} is not a valid node name (letters, digits, _)")
        if node_id in self.nodes:
            raise FlowError(f"there is a node {node_id!r} already")
        node = Node(node_id, type_name, dict(params), position)
        self.nodes[node_id] = node
        return node

    def free_id(self, base: str) -> str:
        base = re.sub(r"[^A-Za-z0-9_]", "_", base) or "node"
        if base[0].isdigit():
            base = f"n_{base}"
        if base not in self.nodes:
            return base
        number = 2
        while f"{base}{number}" in self.nodes:
            number += 1
        return f"{base}{number}"

    def remove_node(self, node_id: str) -> None:
        self.nodes.pop(node_id, None)
        self.edges = [edge for edge in self.edges if node_id not in (edge.source.node, edge.target.node)]

    def rename_node(self, old: str, new: str) -> None:
        if new == old:
            return
        if not _ID_RE.match(new) or new in self.nodes:
            raise FlowError(f"{new!r} is not a free node name")
        self.nodes = {(new if key == old else key): value for key, value in self.nodes.items()}
        self.nodes[new].id = new

        def rename(ref: PortRef) -> PortRef:
            return PortRef(new, ref.port) if ref.node == old else ref

        self.edges = [Edge(rename(edge.source), rename(edge.target)) for edge in self.edges]
        # the ports this flow offers as a subflow point to the node by name as well
        self.inputs = {name: rename(ref) for name, ref in self.inputs.items()}
        self.outputs = {name: rename(ref) for name, ref in self.outputs.items()}

    def connect(self, source: str, target: str) -> Edge:
        """Add the wire ``source -> target`` (``"node.port"`` each)."""
        edge = Edge(PortRef.parse(source), PortRef.parse(target))
        if edge in self.edges:
            return edge
        self.edges.append(edge)
        return edge

    def disconnect(self, edge: Edge) -> None:
        if edge in self.edges:
            self.edges.remove(edge)

    def edges_into(self, node_id: str, port: Optional[str] = None) -> list[Edge]:
        return [edge for edge in self.edges if edge.target.node == node_id and (port is None or edge.target.port == port)]

    def edges_from(self, node_id: str, port: Optional[str] = None) -> list[Edge]:
        return [edge for edge in self.edges if edge.source.node == node_id and (port is None or edge.source.port == port)]

    def copy(self) -> "Flow":
        return copy.deepcopy(self)

    # -------------------------------------------------------------- types
    def spec(self, node: Node, registry: Optional[Registry] = None) -> NodeSpec:
        """The node type of ``node`` (subflows become a spec with their exposed ports)."""
        registry = registry or default_registry
        if node.type.startswith(SUBFLOW_PREFIX):
            name = node.type[len(SUBFLOW_PREFIX):]
            if name not in self.subflows:
                raise RegistryError(f"unknown subflow {name!r}")
            return self.subflows[name].as_spec(name, registry, parent=self)
        return registry.get(node.type)

    def as_spec(self, name: str, registry: Optional[Registry] = None, parent: Optional["Flow"] = None) -> NodeSpec:
        """This flow as a node type (for subflows)."""
        registry = registry or default_registry
        scope = self._with_parent_subflows(parent)

        def port_type(ref: PortRef, inputs: bool) -> str:
            inner = scope.nodes.get(ref.node)
            if inner is None:
                return signals.ANY
            try:
                spec = scope.spec(inner, registry)
            except RegistryError:
                return signals.ANY
            port = spec.input(ref.port, inner.params) if inputs else spec.output(ref.port, inner.params)
            return port.type if port is not None else signals.ANY

        return NodeSpec(
            type=f"{SUBFLOW_PREFIX}{name}",
            implementation=None,
            title=self.name or name,
            description=self.description,
            inputs=[PortSpec(port, port_type(ref, True)) for port, ref in self.inputs.items()],
            outputs=[PortSpec(port, port_type(ref, False)) for port, ref in self.outputs.items()],
            icon="nodes",
        )

    def _with_parent_subflows(self, parent: Optional["Flow"]) -> "Flow":
        if parent is None or not parent.subflows:
            return self
        merged = Flow(self.name, nodes=self.nodes, edges=self.edges, subflows={**parent.subflows, **self.subflows},
                      inputs=self.inputs, outputs=self.outputs)
        return merged

    def port_types(self, edge: Edge, registry: Optional[Registry] = None) -> tuple[Optional[str], Optional[str]]:
        """Types of the source and the target port of ``edge`` (``None`` when unknown)."""
        registry = registry or default_registry
        result = []
        for ref, inputs in ((edge.source, False), (edge.target, True)):
            node = self.nodes.get(ref.node)
            if node is None:
                result.append(None)
                continue
            try:
                spec = self.spec(node, registry)
            except RegistryError:
                result.append(None)
                continue
            port = spec.input(ref.port, node.params) if inputs else spec.output(ref.port, node.params)
            result.append(port.type if port is not None else None)
        return result[0], result[1]

    def can_connect(self, source: str, target: str, registry: Optional[Registry] = None) -> tuple[bool, str]:
        """Whether ``source -> target`` fits; ``(False, node type of a conversion or "")`` if not."""
        edge = Edge(PortRef.parse(source), PortRef.parse(target))
        source_type, target_type = self.port_types(edge, registry)
        if source_type is None or target_type is None:
            return False, ""
        if signals.compatible(source_type, target_type):
            return True, ""
        found = signals.conversion(source_type, target_type)
        return False, found.node if found else ""

    # ---------------------------------------------------------- validation
    def validate(self, registry: Optional[Registry] = None, external: Iterable[str] = (),
                 _own: Optional[Iterable[str]] = None) -> list[Problem]:
        """Every problem of the flow; an empty list for a flow that can run. ``external``: inputs
        (``node.port``) that get their values from outside (a panel, or the flow around a
        subflow)."""
        registry = registry or default_registry
        external = set(external)
        problems: list[Problem] = []
        specs: dict[str, NodeSpec] = {}
        for node_id, node in self.nodes.items():
            try:
                spec = self.spec(node, registry)
            except RegistryError as error:
                problems.append(Problem("error", str(error), node_id))
                continue
            specs[node_id] = spec
            for param in spec.params:
                if param.required and node.params.get(param.name) in (None, "", []):
                    problems.append(Problem("error", f"the parameter {param.name!r} is missing", node_id))
            known = {param.name for param in spec.params}
            for name in node.params:
                if spec.params and name not in known:
                    import difflib

                    close = difflib.get_close_matches(str(name), sorted(known), n=1, cutoff=0.5)
                    hint = f" - did you mean {close[0]!r}?" if close else f" (it has {', '.join(sorted(known))})"
                    problems.append(Problem("warning", f"{spec.type} has no parameter {name!r}{hint}", node_id))
            inputs, _ = spec.resolve_ports(node.params)
            for port in inputs:
                if not port.optional and not self.edges_into(node_id, port.name) \
                        and f"{node_id}.{port.name}" not in external:
                    problems.append(Problem("error", f"the input {port.name!r} is not wired", node_id))

        seen_targets: dict[PortRef, Edge] = {}
        for edge in self.edges:
            source_spec, target_spec = specs.get(edge.source.node), specs.get(edge.target.node)
            if edge.source.node not in self.nodes or edge.target.node not in self.nodes:
                missing = edge.source.node if edge.source.node not in self.nodes else edge.target.node
                problems.append(Problem("error", f"no node {missing!r}", edge=edge))
                continue
            if source_spec is None or target_spec is None:
                continue
            source_port = source_spec.output(edge.source.port, self.nodes[edge.source.node].params)
            target_port = target_spec.input(edge.target.port, self.nodes[edge.target.node].params)
            if source_port is None:
                problems.append(Problem("error", f"{edge.source.node} has no output {edge.source.port!r}", edge=edge))
                continue
            if target_port is None:
                problems.append(Problem("error", f"{edge.target.node} has no input {edge.target.port!r}", edge=edge))
                continue
            if not signals.compatible(source_port.type, target_port.type):
                found = signals.conversion(source_port.type, target_port.type)
                problems.append(Problem("error", f"{source_port.type} does not fit {target_port.type}", edge=edge,
                                        suggestion=found.node if found else ""))
            if edge.target in seen_targets and not target_port.multiple:
                problems.append(Problem("error", f"the input {edge.target} has several wires", edge=edge))
            seen_targets[edge.target] = edge

        # A subflow sees the subflows of the flow around it, and its exposed inputs are wired
        # from there. (``_own``: of a subflow in that scope, only its own subflows are checked
        # again – the others belong to the flow around it.)
        for name, subflow in self.subflows.items():
            if _own is not None and name not in _own:
                continue
            exposed = {str(ref) for ref in subflow.inputs.values()}
            scoped = subflow._with_parent_subflows(self)
            for problem in scoped.validate(registry, external=exposed, _own=set(subflow.subflows)):
                problem.node = f"{name}/{problem.node}" if problem.node else name
                problems.append(problem)
        return problems

    def errors(self, registry: Optional[Registry] = None, external: Iterable[str] = ()) -> list[Problem]:
        return [problem for problem in self.validate(registry, external=external) if problem.severity == "error"]

    # ---------------------------------------------------------- expansion
    def expanded(self, registry: Optional[Registry] = None, prefix: str = "") -> "Flow":
        """The flow with every subflow node replaced by its nodes (``sub/inner``), for running.

        Parameters of a subflow node named ``inner.param`` set the parameter of an inner node.
        """
        registry = registry or default_registry
        result = Flow(self.name, self.description, settings=dict(self.settings))
        # (subflow node, exposed port) -> the inner port it leads to
        exposed_in: dict[tuple[str, str], PortRef] = {}
        exposed_out: dict[tuple[str, str], PortRef] = {}
        for node_id, node in self.nodes.items():
            if not node.type.startswith(SUBFLOW_PREFIX):
                result.nodes[f"{prefix}{node_id}"] = Node(f"{prefix}{node_id}", node.type, dict(node.params),
                                                          node.position, node.comment)
                continue
            name = node.type[len(SUBFLOW_PREFIX):]
            if name not in self.subflows:
                raise FlowError(f"unknown subflow {name!r}")
            inner_prefix = f"{prefix}{node_id}/"
            inner = self.subflows[name]._with_parent_subflows(self).expanded(registry, prefix=inner_prefix)
            for key, value in node.params.items():
                inner_id, _sep, param = key.rpartition(".")
                target = inner.nodes.get(f"{inner_prefix}{inner_id}")
                if target is not None and param:
                    target.params[param] = value
            result.nodes.update(inner.nodes)
            result.edges += inner.edges
            for port, ref in inner.inputs.items():
                exposed_in[(node_id, port)] = ref
            for port, ref in inner.outputs.items():
                exposed_out[(node_id, port)] = ref
        for edge in self.edges:
            source = exposed_out.get((edge.source.node, edge.source.port),
                                     PortRef(f"{prefix}{edge.source.node}", edge.source.port))
            target = exposed_in.get((edge.target.node, edge.target.port),
                                    PortRef(f"{prefix}{edge.target.node}", edge.target.port))
            result.edges.append(Edge(source, target))
        result.inputs = {port: exposed_in.get((ref.node, ref.port), PortRef(f"{prefix}{ref.node}", ref.port))
                         for port, ref in self.inputs.items()}
        result.outputs = {port: exposed_out.get((ref.node, ref.port), PortRef(f"{prefix}{ref.node}", ref.port))
                          for port, ref in self.outputs.items()}
        return result
