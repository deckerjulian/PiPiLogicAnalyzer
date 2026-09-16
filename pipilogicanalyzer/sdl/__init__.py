# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of PiPiLogicAnalyzer 7, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Signal Description Language used to compose sample patterns by hand."""

from .parser import (
    DuplicatedGroupError,
    InvalidGroupError,
    InvalidNumberError,
    InvalidTokenError,
    MissingGroupError,
    SDLError,
    TokenizedSDL,
    get_tokens,
    samples_from_source,
)

__all__ = [
    "DuplicatedGroupError",
    "InvalidGroupError",
    "InvalidNumberError",
    "InvalidTokenError",
    "MissingGroupError",
    "SDLError",
    "TokenizedSDL",
    "get_tokens",
    "samples_from_source",
]
