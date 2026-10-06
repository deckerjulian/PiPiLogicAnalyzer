# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Reading and writing ``*.flow.yaml``.

The writer produces one canonical form: the sections in a fixed order, nodes and parameters in
the order of the model, a node on one line when it fits (``{type: ..., ...}``), otherwise as a
block. ``dumps(loads(text)) == text`` holds for every file the writer produced, so the files diff
well and the YAML view of the editor shows exactly the saved file. Comments in hand-written
files are not kept.
"""

from __future__ import annotations

import os
import re
from typing import Any, Optional

import yaml

from ..core.files import atomic_write
from .model import Edge, Flow, FlowError, Node, PortRef

LINE_WIDTH = 100
INDENT = "  "
SECTION_ORDER = ("flow", "description", "settings", "inputs", "outputs", "nodes", "edges", "subflows")


# ---------------------------------------------------------------------- read
class Loader(yaml.SafeLoader):
    """YAML as version 1.2 reads it: only ``true`` and ``false`` are truth values. YAML 1.1 (the
    default of PyYAML) also took ``on``, ``off``, ``yes`` and ``no`` - a state machine's ``on:``
    and a state named ``off`` became ``True`` and ``False``."""


Loader.yaml_implicit_resolvers = {
    first: [(tag, pattern) for tag, pattern in resolvers if tag != "tag:yaml.org,2002:bool"]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
Loader.add_implicit_resolver("tag:yaml.org,2002:bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
                             list("tTfF"))


class Dumper(yaml.SafeDumper):
    """Writes for :class:`Loader`: ``on`` and ``off`` need no quotes (text fields of the editor; the
    files keep the quotes, other tools read YAML 1.1)."""


Dumper.yaml_implicit_resolvers = Loader.yaml_implicit_resolvers


def safe_load(text: Any) -> Any:
    """``yaml.safe_load`` with the truth values of YAML 1.2 (see :class:`Loader`)."""
    return yaml.load(text, Loader=Loader)  # noqa: S506 - a SafeLoader


def loads(text: str) -> Flow:
    try:
        data = safe_load(text)
    except yaml.YAMLError as error:
        raise FlowError(f"not valid YAML: {error}") from None
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise FlowError("a flow file is a mapping (flow:, nodes:, edges:)")
    return from_data(data)


def load(path: str) -> Flow:
    with open(path, encoding="utf-8") as handle:
        flow = loads(handle.read())
    if flow.name == "Flow" and path:
        flow.name = os.path.basename(path).split(".")[0] or flow.name
    return flow


def from_data(data: dict) -> Flow:
    if "devices" in data:
        raise FlowError("devices are nodes: write them as nodes of type device.instrument (address: sim:uno) "
                        "and wire their 'device' output to the nodes that use them")
    unknown = set(data) - set(SECTION_ORDER)
    if unknown:
        raise FlowError(f"unknown section(s): {', '.join(sorted(map(str, unknown)))}")
    flow = Flow(name=str(data.get("flow") or "Flow"), description=str(data.get("description") or ""))
    flow.settings = dict(data.get("settings") or {})

    nodes = data.get("nodes") or {}
    if not isinstance(nodes, dict):
        raise FlowError("nodes: is a mapping of name: {type: ..., parameters}")
    for node_id, body in nodes.items():
        if isinstance(body, str):
            body = {"type": body}
        if not isinstance(body, dict) or "type" not in body:
            raise FlowError(f"the node {node_id!r} has no type")
        params = dict(body)
        type_name = str(params.pop("type"))
        position = params.pop("at", None)
        comment = str(params.pop("comment", "") or "")
        if position is not None:
            if not (isinstance(position, (list, tuple)) and len(position) == 2):
                raise FlowError(f"at: of {node_id!r} is [x, y]")
            try:
                position = (float(position[0]), float(position[1]))
            except (TypeError, ValueError):
                raise FlowError(f"at: of {node_id!r} is [x, y] in numbers") from None
        # (not as keyword arguments: a parameter may be called "position" or "node_id")
        node = flow.add_node(type_name, str(node_id))
        node.params = params
        node.position = position
        node.comment = comment

    edges = data.get("edges") or []
    if not isinstance(edges, list):
        raise FlowError("edges: is a list of 'a.out -> b.in'")
    for text in edges:
        flow.edges.append(Edge.parse(str(text)))

    for section, target in (("inputs", flow.inputs), ("outputs", flow.outputs)):
        for name, ref in (data.get(section) or {}).items():
            target[str(name)] = PortRef.parse(str(ref))

    for name, body in (data.get("subflows") or {}).items():
        if not isinstance(body, dict):
            raise FlowError(f"the subflow {name!r} is a mapping")
        body = dict(body)
        body.setdefault("flow", name)
        flow.subflows[str(name)] = from_data(body)
    return flow


# --------------------------------------------------------------------- write
_PLAIN_RE = re.compile(r"^[A-Za-z0-9_./~-][^#,\[\]{}&*!|>'\"%@`]*$")


def _inline(value: Any) -> str:
    """``value`` as YAML in flow style on one line."""
    if isinstance(value, str) and _PLAIN_RE.match(value) and ": " not in value and not value.endswith((":", " ")):
        # Addresses like sim:uno need no quotes as long as YAML reads them back unchanged.
        try:
            if yaml.safe_load(f"[{value}]") == [value]:
                return value
        except yaml.YAMLError:
            pass
    text = yaml.safe_dump([value], default_flow_style=True, allow_unicode=True, sort_keys=False,
                          width=10**9).strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    return text


def _is_multiline(value: Any) -> bool:
    return isinstance(value, str) and "\n" in value


def _block_text(value: str) -> bool:
    """Whether ``value`` can be written as a block (``|``) and read back unchanged: the first
    line tells YAML the indentation, so it must not be empty or start with a space."""
    first = value.split("\n", 1)[0]
    return bool(first) and not first[0].isspace() and "\r" not in value and "\t" not in value


def _scalar_lines(key: str, value: Any, indent: str) -> list[str]:
    if _is_multiline(value) and _block_text(value):
        text = value.rstrip("\n")
        trailing = len(value) - len(text)
        # "|" ends with one line break; "|-" with none, "|+" keeps all of them
        chomp = "-" if trailing == 0 else ("" if trailing == 1 else "+")
        lines = text.split("\n") + [""] * max(trailing - 1, 0)
        return [f"{indent}{key}: |{chomp}"] + [f"{indent}{INDENT}{line}" if line.strip() else "" for line in lines]
    return [f"{indent}{key}: {_inline(value)}"]


def _node_lines(node: Node, indent: str) -> list[str]:
    items: list[tuple[str, Any]] = [("type", node.type)] + list(node.params.items())
    if node.comment:
        items.append(("comment", node.comment))
    if node.position is not None:
        items.append(("at", [_number(node.position[0]), _number(node.position[1])]))
    if not any(_is_multiline(value) for _key, value in items):
        line = f"{indent}{_key(node.id)}: {{" + ", ".join(f"{_key(key)}: {_inline(value)}" for key, value in items) + "}"
        if len(line) <= LINE_WIDTH:
            return [line]
    lines = [f"{indent}{_key(node.id)}:"]
    for key, value in items:
        lines += _scalar_lines(_key(key), value, indent + INDENT)
    return lines


def _number(value: float):
    return int(value) if float(value).is_integer() else round(float(value), 2)


def _key(key: str) -> str:
    return _inline(str(key))


def _flow_lines(flow: Flow, indent: str = "", subflow: bool = False) -> list[str]:
    lines: list[str] = []
    if not subflow:
        lines.append(f"{indent}flow: {_inline(flow.name)}")
    elif flow.name:
        lines.append(f"{indent}flow: {_inline(flow.name)}")
    if flow.description:
        lines += _scalar_lines("description", flow.description, indent)
    if flow.settings:
        lines.append(f"{indent}settings:")
        for key, value in flow.settings.items():
            lines += _scalar_lines(_key(key), value, indent + INDENT)
    for section, mapping in (("inputs", flow.inputs), ("outputs", flow.outputs)):
        if mapping:
            lines.append(f"{indent}{section}:")
            lines += [f"{indent}{INDENT}{_key(name)}: {ref}" for name, ref in mapping.items()]
    lines.append(f"{indent}nodes:" + ("" if flow.nodes else " {}"))
    for node in flow.nodes.values():
        lines += _node_lines(node, indent + INDENT)
    lines.append(f"{indent}edges:" + ("" if flow.edges else " []"))
    lines += [f"{indent}{INDENT}- {edge}" for edge in flow.edges]
    if flow.subflows:
        lines.append(f"{indent}subflows:")
        for name, inner in flow.subflows.items():
            lines.append(f"{indent}{INDENT}{_key(name)}:")
            lines += _flow_lines(inner, indent + INDENT * 2, subflow=True)
    return lines


def dumps(flow: Flow) -> str:
    return "\n".join(_flow_lines(flow)) + "\n"


def save(flow: Flow, path: str) -> None:
    atomic_write(path, dumps(flow), newline="\n")


def roundtrip_problem(text: str) -> Optional[str]:
    """``None`` when ``text`` is in the canonical form, else the canonical form."""
    canonical = dumps(loads(text))
    return None if canonical == text else canonical
