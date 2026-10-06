# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of openSciLab, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Allows ``python -m openscilab``."""

from .app import main

if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()  # (the decoder process of a packaged application starts here)
    raise SystemExit(main())
