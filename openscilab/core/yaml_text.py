# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Writing YAML whose text reads back as it was written.

PyYAML writes some characters as they are where YAML does not read them back the same: control
characters in a block of text (``|``) make the file unreadable, the line breaks of Unicode (NEL,
LS, PS) in quotes come back as spaces. Every file openSciLab writes in YAML (flows, panels,
projects, waveforms) goes through :class:`SafeDumper`, which writes such text in double quotes
with escapes (``"a\\bb"``, ``"\\N"``); all other text, Unicode included, stays as it is. Qt free.
"""

from __future__ import annotations

import re
from typing import Any

import yaml

#: characters YAML does not read back as they were written outside double quotes: the controls
#: (also a carriage return), the line breaks of Unicode and the byte order mark
UNSAFE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f  ﻿]")


def represent_str(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    if UNSAFE.search(data):
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style='"')
    return yaml.SafeDumper.represent_str(dumper, data)


class SafeDumper(yaml.SafeDumper):
    """``yaml.SafeDumper`` whose text reads back unchanged (see :data:`UNSAFE`)."""


SafeDumper.add_representer(str, represent_str)


def dump(data: Any, stream: Any = None, **options: Any) -> Any:
    """``yaml.safe_dump`` with :class:`SafeDumper`; Unicode as it is, mappings in their order."""
    options.setdefault("allow_unicode", True)
    options.setdefault("sort_keys", False)
    return yaml.dump(data, stream, Dumper=SafeDumper, **options)
