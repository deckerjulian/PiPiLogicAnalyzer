# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Fuzzy matching for the command palette and the node search.

The characters of the query have to appear in the text in order; a match scores higher when the
characters are adjacent, start words, or the text starts with the query.
"""

from __future__ import annotations

from typing import Iterable, Optional, Sequence, TypeVar

T = TypeVar("T")

_SEPARATORS = " _-./›>:()"


def score(query: str, text: str) -> Optional[int]:
    """Score of ``text`` for ``query`` (higher is better), ``None`` when it does not match.

    An empty query matches everything with score 0.
    """
    query = query.strip().lower()
    if not query:
        return 0
    lowered = text.lower()
    if query in lowered:
        index = lowered.index(query)
        start_bonus = 40 if index == 0 else (25 if lowered[index - 1] in _SEPARATORS else 0)
        return 200 + start_bonus - min(index, 50) + 2 * len(query)

    total = 0
    position = 0
    previous = -2
    for char in query:
        if char == " ":
            continue
        index = lowered.find(char, position)
        if index < 0:
            return None
        if index == previous + 1:
            total += 8  # adjacent to the previous match
        if index == 0 or lowered[index - 1] in _SEPARATORS:
            total += 6  # start of a word
        total -= min(index - position, 10)  # gap
        previous = index
        position = index + 1
    return total


def rank(query: str, items: Iterable[T], key, boost: Optional[Sequence[str]] = None) -> list[T]:
    """``items`` that match ``query``, best first.

    ``key(item)`` is the text matched; ``boost`` lists texts used recently (most recent first),
    which come first with an empty query and get a bonus otherwise.
    """
    recent = {text: len(boost) - index for index, text in enumerate(boost or [])}
    scored = []
    for order, item in enumerate(items):
        text = key(item)
        value = score(query, text)
        if value is None:
            continue
        bonus = recent.get(text, 0)
        if query.strip():
            value += 15 * min(bonus, 5) // 5 if bonus else 0
            scored.append((-value, order, item))
        else:
            # Recently used first (most recent on top), the rest in their order.
            scored.append((-(1000 + bonus) if bonus else 0, order, item))
    scored.sort(key=lambda entry: (entry[0], entry[1]))
    return [item for _value, _order, item in scored]
