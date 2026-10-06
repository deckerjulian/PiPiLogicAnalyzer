# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The lab: flows (model, YAML, Python DSL), the engine that runs them and the node types.

::

    from openscilab.lab import flow, nodes as n
"""

from . import nodes as _node_types  # noqa: F401 - the package first; ``nodes`` below is the DSL
from .dsl import flow, from_python, nodes, to_python
from .engine import Engine, EngineEvent, NodeContext, NodeError, NodeRuntime, RunResult, ViewSink
from .model import Edge, Flow, FlowError, Node, PortRef, Problem
from .nodes.registry import In, Out, Param, Registry, default_registry, node

__all__ = [
    "Edge", "Engine", "EngineEvent", "Flow", "FlowError", "In", "Node", "NodeContext", "NodeError", "NodeRuntime",
    "Out", "Param", "PortRef", "Problem",
    "Registry", "RunResult", "ViewSink", "default_registry", "flow", "from_python", "node", "nodes", "to_python",
]
