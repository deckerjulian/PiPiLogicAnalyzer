# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Arranging the nodes of a flow: left to right along the wires, without overlaps.

A layered layout (Sugiyama):

1. Feedback wires (a sweep waiting for a measurement before its next step) are found with the
   greedy order of Eades, Lin and Smyth – nodes that mostly send come first – and do not count.
2. Every node goes into the column after the latest of its sources (longest path); a node without
   inputs moves right before the first node it feeds.
3. A wire that spans columns gets a waypoint in every column it passes (so the nodes of those
   columns make room for it).
4. Within a column the nodes are ordered by where the ports they are wired to are – sweeps of the
   barycenter heuristic down and up; the order with the fewest crossings wins.
5. Vertical places: every node is pulled to the height where its wires run straight (the mean of
   its wired ports), as close as the real heights of the nodes of its column allow (isotonic
   regression, pool adjacent violators); sweeps left to right and back.

Comments and groups are not part of the wiring; they go below.
"""

from __future__ import annotations

from typing import Callable, Iterable, Optional

from .model import Flow

#: node types that are no part of the wiring
UNWIRED_TYPES = ("structure.comment", "structure.group")
#: horizontal distance of two columns (left edge to left edge) beyond the node width
COLUMN_GAP = 80.0
#: vertical gap between two nodes of a column
ROW_GAP = 30.0


def _order(nodes: list[str], edges: list[tuple[str, str]]) -> list[str]:
    """Eades–Lin–Smyth: sources to the front, sinks to the back, else the node that sends most."""
    outgoing = {node: set() for node in nodes}
    incoming = {node: set() for node in nodes}
    for source, target in edges:
        if source != target:
            outgoing[source].add(target)
            incoming[target].add(source)
    rest = list(nodes)  # file order breaks ties
    front: list[str] = []
    back: list[str] = []

    def remove(node: str) -> None:
        rest.remove(node)
        for other in outgoing.pop(node):
            incoming[other].discard(node)
        for other in incoming.pop(node):
            outgoing[other].discard(node)

    while rest:
        changed = True
        while changed:
            changed = False
            for node in list(rest):
                if not outgoing[node]:
                    back.insert(0, node)
                    remove(node)
                    changed = True
            for node in list(rest):
                if not incoming[node]:
                    front.append(node)
                    remove(node)
                    changed = True
        if rest:
            node = max(rest, key=lambda item: len(outgoing[item]) - len(incoming[item]))
            front.append(node)
            remove(node)
    return front + back


#: height a waypoint of a long wire takes in its column, and the gap around it
WAYPOINT = 12.0
WAYPOINT_GAP = 10.0
#: sweeps of the ordering and of the vertical placement
ORDER_SWEEPS = 12
PLACE_SWEEPS = 12
#: where a wire meets a node when no port is known (the middle of its title)
DEFAULT_PORT = 13.0

PortOffset = Callable[[str, Optional[str], bool], float]


def _isotonic(values: list[float], weights: list[float]) -> list[float]:
    """The non-decreasing sequence closest to ``values`` (weighted least squares; pool adjacent violators)."""
    blocks: list[list[float]] = []  # [mean, weight, count]
    for value, weight in zip(values, weights):
        blocks.append([value, weight, 1])
        while len(blocks) > 1 and blocks[-2][0] > blocks[-1][0]:
            mean2, weight2, count2 = blocks.pop()
            mean1, weight1, count1 = blocks[-1]
            total = weight1 + weight2
            blocks[-1] = [(mean1 * weight1 + mean2 * weight2) / total, total, count1 + count2]
    result: list[float] = []
    for mean, _weight, count in blocks:
        result.extend([mean] * count)
    return result


def _crossings(columns: dict[int, list[str]], segments: list[tuple[str, str, float, float]],
               column: dict[str, int], fraction: Callable[[str, float], float]) -> int:
    """Wires that cross between neighbouring columns."""
    place = {node: index for members in columns.values() for index, node in enumerate(members)}
    total = 0
    by_column: dict[int, list[tuple[float, float]]] = {}
    for source, target, source_offset, target_offset in segments:
        by_column.setdefault(column[source], []).append(
            (place[source] + fraction(source, source_offset), place[target] + fraction(target, target_offset)))
    for wires in by_column.values():
        for index, (a1, b1) in enumerate(wires):
            for a2, b2 in wires[index + 1:]:
                if (a1 - a2) * (b1 - b2) < 0:
                    total += 1
    return total


def layered(nodes: list[str], edges: Iterable[tuple], height: Callable[[str], float], width: float,
            origin: tuple[float, float] = (0.0, 0.0),
            port: Optional[PortOffset] = None) -> dict[str, tuple[float, float]]:
    """Positions (top left) of ``nodes`` wired by ``edges``: ``(source, target)`` or ``(source,
    target, source port, target port)``; ``port(node, port name, is_input)`` is where a port sits
    below the top of its node (default: the middle of the title)."""
    port = port or (lambda _node, _name, _is_input: DEFAULT_PORT)
    known = set(nodes)
    links: list[tuple[str, str, Optional[str], Optional[str]]] = []
    for edge in edges:
        source, target = edge[0], edge[1]
        if source in known and target in known and source != target:
            links.append((source, target, edge[2] if len(edge) > 2 else None, edge[3] if len(edge) > 3 else None))
    pairs = sorted({(source, target) for source, target, _sp, _tp in links})
    rank = {node: index for index, node in enumerate(_order(nodes, pairs))}
    forward = [link for link in links if rank[link[0]] < rank[link[1]]]
    forward_pairs = sorted({(source, target) for source, target, _sp, _tp in forward})

    # ------------------------------------------------------------- columns
    column: dict[str, int] = {}
    for node in sorted(nodes, key=rank.__getitem__):
        column[node] = 1 + max((column[source] for source, target in forward_pairs if target == node), default=-1)
    # a node without inputs sits right before the first node it feeds (a device next to its users)
    for node in sorted(nodes, key=rank.__getitem__, reverse=True):
        if not any(target == node for _source, target in forward_pairs):
            targets = [column[target] for source, target in forward_pairs if source == node]
            if targets:
                column[node] = max(column[node], min(targets) - 1)
    # a node without wires (a report written when the flow ends) goes to the end
    last = max(column.values(), default=0)
    for node in nodes:
        if not any(node in edge for edge in forward_pairs):
            column[node] = last

    # --------------------------------------------- waypoints of long wires
    sizes = {node: float(height(node)) for node in nodes}
    segments: list[tuple[str, str, float, float]] = []  # (left, right, offset in left, offset in right)
    waypoint_of: dict[str, tuple[str, int]] = {}
    for index, (source, target, source_port, target_port) in enumerate(forward):
        start = port(source, source_port, False)
        end = port(target, target_port, True)
        previous, previous_offset = source, start
        for step in range(column[source] + 1, column[target]):
            point = f"\0wire{index}.{step}"
            column[point] = step
            sizes[point] = WAYPOINT
            waypoint_of[point] = (source, index)
            segments.append((previous, point, previous_offset, WAYPOINT / 2))
            previous, previous_offset = point, WAYPOINT / 2
        segments.append((previous, target, previous_offset, end))

    def fraction(node: str, offset: float) -> float:
        return 0.5 * offset / max(sizes[node], 1.0)

    # ------------------------------------------------------------ ordering
    columns: dict[int, list[str]] = {}
    order_key = {**rank, **{point: rank[source] + 0.5 for point, (source, _index) in waypoint_of.items()}}
    for node in sorted(column, key=lambda item: (order_key[item], item)):
        columns.setdefault(column[node], []).append(node)
    left_of: dict[str, list[tuple[str, float, float]]] = {node: [] for node in column}
    right_of: dict[str, list[tuple[str, float, float]]] = {node: [] for node in column}
    for source, target, source_offset, target_offset in segments:
        right_of[source].append((target, source_offset, target_offset))
        left_of[target].append((source, target_offset, source_offset))
    best = {key: list(members) for key, members in columns.items()}
    best_crossings = _crossings(columns, segments, column, fraction)
    keys = sorted(columns)
    for sweep in range(ORDER_SWEEPS):
        downward = sweep % 2 == 0
        for key in (keys[1:] if downward else list(reversed(keys))[1:]):
            place = {node: index for members in columns.values() for index, node in enumerate(members)}
            members = columns[key]

            def center(node: str, place=place, downward=downward) -> float:
                found = [place[other] + fraction(other, other_offset)
                         for other, _own, other_offset in (left_of[node] if downward else right_of[node])]
                return sum(found) / len(found) if found else float(place[node])

            members.sort(key=center)
        crossings = _crossings(columns, segments, column, fraction)
        if crossings < best_crossings:
            best_crossings = crossings
            best = {key: list(members) for key, members in columns.items()}
    columns = best

    # ------------------------------------------------------ vertical places
    def gap(first: str, second: str) -> float:
        return WAYPOINT_GAP if first in waypoint_of or second in waypoint_of else ROW_GAP

    y: dict[str, float] = {}
    for members in columns.values():
        top = 0.0
        for index, node in enumerate(members):
            y[node] = top
            if index + 1 < len(members):
                top += sizes[node] + gap(node, members[index + 1])

    def place_column(members: list[str], sides: tuple[str, ...]) -> None:
        wanted, weights, offsets = [], [], []
        top = 0.0
        for index, node in enumerate(members):
            links_of = [(other, own, other_offset) for side in sides
                        for other, own, other_offset in (left_of[node] if side == "left" else right_of[node])]
            if links_of:
                target = sum(y[other] + other_offset - own for other, own, other_offset in links_of) / len(links_of)
                weight = float(len(links_of))
            else:
                target, weight = y[node], 0.25  # (nothing wired here: stays, unless pushed)
            offsets.append(top)
            wanted.append(target - top)
            weights.append(weight)
            if index + 1 < len(members):
                top += sizes[node] + gap(node, members[index + 1])
        for node, value, offset in zip(members, _isotonic(wanted, weights), offsets):
            y[node] = value + offset

    for sweep in range(PLACE_SWEEPS):
        if sweep == PLACE_SWEEPS - 1:
            for key in keys:
                place_column(columns[key], ("left", "right"))
        elif sweep % 2 == 0:
            for key in keys[1:]:
                place_column(columns[key], ("left",))
        else:
            for key in list(reversed(keys))[1:]:
                place_column(columns[key], ("right",))

    real = [node for node in nodes]
    top = min((y[node] for node in real), default=0.0)
    return {node: (origin[0] + column[node] * (width + COLUMN_GAP), origin[1] + y[node] - top) for node in real}


def flow_edges(flow: Flow) -> list[tuple[str, str, str, str]]:
    """The wires of ``flow`` as ``(source, target, source port, target port)``."""
    return [(edge.source.node, edge.target.node, edge.source.port, edge.target.port) for edge in flow.edges]


def arrange(flow: Flow, height: Callable[[str], float], width: float,
            port: Optional[PortOffset] = None) -> dict[str, tuple[float, float]]:
    """Positions for every node of ``flow``; comments and groups go below the wired nodes."""
    wired = [node_id for node_id, node in flow.nodes.items() if node.type not in UNWIRED_TYPES]
    positions = layered(wired, flow_edges(flow), height, width, port=port)
    loose = [node_id for node_id, node in flow.nodes.items() if node.type in UNWIRED_TYPES]
    bottom = max((y + height(node_id) for node_id, (_x, y) in positions.items()), default=-2 * ROW_GAP)
    for index, node_id in enumerate(loose):
        positions[node_id] = (index * (width + COLUMN_GAP), bottom + 2 * ROW_GAP)
    return {node_id: (round(x / 8) * 8.0, round(y / 8) * 8.0) for node_id, (x, y) in positions.items()}
