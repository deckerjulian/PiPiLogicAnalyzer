# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Completions for the text editors: the YAML of a flow and Python code (of a Python node, the
console).

Each function takes the text before the cursor and returns where the word being typed starts
and the completions that fit it. YAML: the sections of a flow file, node types after ``type:``,
the parameters of a node's type, the values a parameter can have (its choices, the pins and
channels of the device – :mod:`.hints`) and ``node.port`` in the edges (outputs before ``->``,
inputs after it). Python: the attributes of the names of a namespace (``ctx.``, ``np.``,
``signals.``), found by looking at the objects (nothing is called or evaluated), and port names
in the strings of ``ctx.emit("…")`` and ``ctx.receive("…")``.
"""

from __future__ import annotations

import inspect
import keyword
import re
from dataclasses import dataclass
from typing import Any, Optional

from . import hints as hint_values
from .model import Flow
from .nodes.registry import NodeSpec, Registry, RegistryError, default_registry
from .yaml_io import SECTION_ORDER

#: characters of a word being completed (``sim:uno``, ``node.port``, ``D0``, ``1 kHz`` is two)
_WORD = re.compile(r"[\w.:/*#+\-]*$")
#: extra keys every node has
NODE_KEYS = (("type", "the node type"), ("at", "position in the graph [x, y]"), ("comment", "a note at the node"))
SECTION_HELP = {"flow": "the name of the flow", "description": "what the flow does", "settings": "seed, duration",
                "inputs": "exposed inputs of a subflow", "outputs": "exposed outputs of a subflow",
                "nodes": "the nodes: name: {type: ..., parameters}", "edges": "the wires: - node.port -> node.port",
                "subflows": "flows used as nodes"}


@dataclass(frozen=True)
class Completion:
    text: str
    detail: str = ""
    #: what is inserted (``text`` when empty): ``"rate: "`` for the key ``rate``
    insert: str = ""

    @property
    def inserted(self) -> str:
        return self.insert or self.text


def _matching(items: list[Completion], prefix: str) -> list[Completion]:
    lowered = prefix.lower()
    seen, result = set(), []
    for item in items:
        if item.text not in seen and item.text.lower().startswith(lowered) and item.text != prefix:
            seen.add(item.text)
            result.append(item)
    # then the ones that contain the prefix (``pwm`` finds ``gpio.pwm``)
    if lowered:
        for item in items:
            if item.text not in seen and lowered in item.text.lower():
                seen.add(item.text)
                result.append(item)
    return result


# ====================================================================== YAML
def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _section(lines: list[str]) -> str:
    for line in reversed(lines[:-1]):
        if line.strip() and not line.startswith((" ", "#", "-")) and ":" in line:
            return line.split(":", 1)[0].strip()
    return ""


def _flow_segment(text: str) -> Optional[tuple[str, Optional[str], bool]]:
    """In a flow-style mapping ``{a: 1, b: [x, y`` (the text after the node name): the current
    ``(key, value or None, inside a list)``; ``None`` outside the braces."""
    depth = 0
    start = None
    for index, char in enumerate(text):
        if char == "{":
            depth += 1
            if depth == 1:
                start = index + 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                start = None
        elif char == "," and depth == 1 and text[:index].count("[") <= text[:index].count("]"):
            start = index + 1
    if start is None or depth < 1:
        return None
    segment = text[start:]
    if ":" not in segment:
        return segment.strip(), None, False
    key, _, value = segment.partition(":")
    in_list = value.count("[") > value.count("]")
    if in_list:
        value = value[value.rfind("[") + 1:].split(",")[-1]
    return key.strip(), value.lstrip(), in_list


def _node_type(lines: list[str], node_id: str, flow: Optional[Flow]) -> str:
    """The type of ``node_id``: from the text (``type: x`` on its line or in its block), else the model."""
    for index, line in enumerate(lines):
        if _indent(line) == 2 and line.strip().startswith(f"{node_id}:"):
            match = re.search(r"type:\s*([\w.\-]+)", line)
            if match:
                return match.group(1)
            for below in lines[index + 1:]:
                if below.strip() and _indent(below) <= 2:
                    break
                match = re.match(r"\s+type:\s*([\w.\-]+)", below)
                if match:
                    return match.group(1)
    node = flow.nodes.get(node_id) if flow is not None else None
    return node.type if node is not None else ""


def _spec(type_name: str, flow: Optional[Flow], registry: Registry) -> Optional[NodeSpec]:
    if not type_name:
        return None
    try:
        if flow is not None and type_name.startswith("subflow."):
            from .model import Node

            return flow.spec(Node("x", type_name), registry)
        return registry.get(type_name)
    except RegistryError:
        return None


def _value_completions(node_id: str, spec: Optional[NodeSpec], key: str, flow: Optional[Flow], registry: Registry,
                       devices: dict, addresses: list[tuple[str, str]]) -> list[Completion]:
    if key == "type":
        return [Completion(item.type, item.title) for item in registry.specs()]
    if spec is None:
        return []
    param = spec.param(key)
    if param is None:
        return []
    if param.kind == "address":
        return [Completion(address, label) for label, address in addresses]
    if param.kind == "bool":
        return [Completion("true"), Completion("false")]
    found: list[Completion] = []
    if param.choices:
        found += [Completion(str(choice), "choice") for choice in param.choices]
    if param.suggest and flow is not None:
        values = hint_values.suggestions(flow, node_id, param, devices) or []
        found += [Completion(str(value), _source_label(param.suggest)) for value in values]
    if param.kind == "quantity" and param.default not in (None, ""):
        found.append(Completion(str(param.default), f"default, in {param.unit}" if param.unit else "default"))
    return found


def _source_label(suggest: str) -> str:
    source, _, capability = suggest.partition(":")
    label = {"channels": "channel", "analog": "analog input", "pins": "pin", "outputs": "output",
             "upstream_channels": "channel of the input", "upstream_columns": "column of the input"}.get(source, source)
    return f"{label} ({capability})" if capability else label


def yaml_completions(text: str, flow: Optional[Flow] = None, registry: Optional[Registry] = None,
                     devices: Optional[dict] = None, addresses: Optional[list[tuple[str, str]]] = None
                     ) -> tuple[int, list[Completion]]:
    """Completions at the end of ``text`` (the YAML before the cursor): ``(start of the word,
    completions)``. ``flow``: the flow as the editor last read it (its nodes and wires give the
    ports and the devices' values)."""
    registry = registry or default_registry
    devices = devices or {}
    lines = text.split("\n")
    line = lines[-1]
    word = _WORD.search(line).group(0)
    start = len(text) - len(word)
    stripped = line.strip()
    if stripped.startswith("#"):
        return start, []
    if _indent(line) == 0 and ":" not in line and not line.startswith("-"):
        present = {existing.split(":", 1)[0] for existing in lines if existing and not existing.startswith(" ")}
        items = [Completion(name, SECTION_HELP.get(name, ""), f"{name}: ") for name in SECTION_ORDER if name not in present]
        return start, _matching(items, word)
    section = _section(lines)

    if section == "edges" and stripped.startswith("-"):
        if flow is None:
            return start, []
        after = "->" in line
        items = []
        for node_id, node in flow.nodes.items():
            try:
                inputs, outputs = flow.spec(node, registry).resolve_ports(node.params)
            except RegistryError:
                continue
            for port in inputs if after else outputs:
                items.append(Completion(f"{node_id}.{port.name}", port.type, f"{node_id}.{port.name}" if after
                                        else f"{node_id}.{port.name} -> "))
        return start, _matching(items, word)

    if section not in ("nodes", "subflows"):
        return start, []
    node_indent = 2
    if _indent(line) == node_indent and ":" in line:
        node_id = line.split(":", 1)[0].strip()
        segment = _flow_segment(line.split(":", 1)[1])
        if segment is None:
            return start, []
        key, value, _in_list = segment
        spec = _spec(_node_type(lines, node_id, flow), flow, registry)
        if value is None:
            present = set(re.findall(r"[{,]\s*([\w]+)\s*:", line))
            return start, _matching(_key_completions(spec, present), word)
        return start, _matching(_value_completions(node_id, spec, key, flow, registry, devices, addresses or []), word)
    if _indent(line) > node_indent:
        node_id = next((above.split(":", 1)[0].strip() for above in reversed(lines[:-1])
                        if above.strip() and _indent(above) == node_indent), "")
        if not node_id:
            return start, []
        spec = _spec(_node_type(lines, node_id, flow), flow, registry)
        if ":" not in stripped:
            block = []
            for above in reversed(lines[:-1]):
                if _indent(above) <= node_indent:
                    break
                block.append(above)
            present = {above.split(":", 1)[0].strip() for above in block if ":" in above}
            return start, _matching(_key_completions(spec, present), word)
        key = stripped.split(":", 1)[0]  # (in a list: an item gets the same values as the key)
        return start, _matching(_value_completions(node_id, spec, key.strip(), flow, registry, devices,
                                                   addresses or []), word)
    return start, []


def _key_completions(spec: Optional[NodeSpec], present: set) -> list[Completion]:
    items = [Completion(name, detail, f"{name}: ") for name, detail in NODE_KEYS if name not in present]
    if spec is not None:
        for param in spec.params:
            if param.name in present:
                continue
            detail = param.kind + (f" [{param.unit}]" if param.unit else "") + (" – required" if param.required else "")
            if param.description:
                detail += f" – {param.description}"
            items.insert(0 if param.required else len(items), Completion(param.name, detail, f"{param.name}: "))
    return items


# ==================================================================== Python
#: the names a Python node's code has (control.python)
def node_namespace() -> dict[str, Any]:
    import numpy as np

    from ..core import signals, units
    from .engine.runtime import NodeContext

    return {"np": np, "signals": signals, "units": units, "ctx": NodeContext}


#: methods of ``ctx`` whose first argument is an output or an input of the node
_OUTPUT_CALLS = ("emit", "send")
_INPUT_CALLS = ("receive", "latest", "wired")


def _describe(value: Any) -> str:
    try:
        if inspect.isclass(value):
            return "class"
        if callable(value):
            signature = str(inspect.signature(value))
            return signature.replace("(self, ", "(").replace("(self)", "()")
    except (TypeError, ValueError):
        return "function"
    if inspect.ismodule(value):
        return "module"
    return type(value).__name__


def _attributes(value: Any) -> list[Completion]:
    names = [name for name in dir(value) if not name.startswith("_")]
    items = []
    for name in names:
        try:
            attribute = inspect.getattr_static(value, name)
            if isinstance(attribute, (staticmethod, classmethod)):
                attribute = getattr(value, name)
        except AttributeError:
            continue
        items.append(Completion(name, _describe(attribute.fget if isinstance(attribute, property) else attribute)))
    return items


def python_completions(text: str, namespace: Optional[dict] = None, inputs: Optional[list[str]] = None,
                       outputs: Optional[list[str]] = None, devices: Optional[list[str]] = None
                       ) -> tuple[int, list[Completion]]:
    """Completions at the end of ``text`` (the code before the cursor)."""
    namespace = node_namespace() if namespace is None else namespace
    line = text.split("\n")[-1]
    # a port name in a string: ctx.emit("ou|  /  port == "i|
    match = re.search(r"""(?:ctx\.(\w+)\(\s*|port\s*[=!]=\s*)(["'])([\w\-.]*)$""", line)
    if match:
        call, word = match.group(1), match.group(3)
        if call in _OUTPUT_CALLS:
            names = outputs or []
        elif call == "device":
            names = devices or []
        else:
            names = inputs or []
        what = "output" if call in _OUTPUT_CALLS else ("device" if call == "device" else "input")
        return len(text) - len(word), _matching([Completion(name, what) for name in names], word)
    match = re.search(r"([A-Za-z_][\w.]*)\.(\w*)$", line)
    if match:
        path, word = match.group(1), match.group(2)
        parts = path.split(".")
        value = namespace.get(parts[0], _MISSING)
        for part in parts[1:]:
            if value is _MISSING:
                break
            try:
                value = inspect.getattr_static(value, part)
            except AttributeError:
                value = _MISSING
        if value is _MISSING:
            return len(text) - len(word), []
        return len(text) - len(word), _matching(_attributes(value), word)
    match = re.search(r"[A-Za-z_]\w*$", line)
    word = match.group(0) if match else ""
    if not word or line[: len(line) - len(word)].rstrip().endswith(("def", "class")):
        return len(text) - len(word), []
    names = {name: _describe(value) for name, value in namespace.items() if not name.startswith("_")}
    for name in re.findall(r"\b([A-Za-z_]\w*)\b", text[: len(text) - len(word)]):
        names.setdefault(name, "")
    items = [Completion(name, detail) for name, detail in sorted(names.items())]
    items += [Completion(name, "keyword") for name in keyword.kwlist]
    items += [Completion(name, "built-in") for name in ("print", "len", "range", "float", "int", "str", "list",
                                                         "dict", "abs", "min", "max", "sum", "round", "enumerate")]
    return len(text) - len(word), _matching(items, word)


_MISSING = object()
