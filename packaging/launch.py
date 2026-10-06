# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Entry point of the packaged application (see openscilab.spec)."""

from openscilab.app import main

if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()  # (the decoder process of a packaged application starts here)
    raise SystemExit(main())
