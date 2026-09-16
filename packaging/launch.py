# Copyright (C) 2026 Julian Decker
#
# Part of PiPiLogicAnalyzer 7.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Entry point of the packaged application (see pipilogicanalyzer.spec)."""

from pipilogicanalyzer.app import main

if __name__ == "__main__":
    raise SystemExit(main())
