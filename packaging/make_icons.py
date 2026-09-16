# Copyright (C) 2026 Julian Decker
#
# Part of PiPiLogicAnalyzer.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Renders the application icon as PNG, ICO (Windows) and ICNS (macOS) for packaging.

    python packaging/make_icons.py build/icons

The drawing is ``APP_ICON_SVG`` in ``pipilogicanalyzer/ui/icons.py``, the same icon the
running application uses for its windows. Needs PySide6 and Pillow.
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, ROOT)

from pipilogicanalyzer import qt_plugins  # noqa: E402

qt_plugins.ensure_loadable_plugins()

from PIL import Image  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402

from pipilogicanalyzer.ui.icons import render_app_icon  # noqa: E402

PNG_SIZES = (16, 24, 32, 48, 64, 128, 256, 512, 1024)
ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def main(output: str) -> None:
    os.makedirs(output, exist_ok=True)
    application = QGuiApplication.instance() or QGuiApplication([])  # noqa: F841 - needed for painting

    for size in PNG_SIZES:
        path = os.path.join(output, f"pipilogicanalyzer-{size}.png")
        if not render_app_icon(size).save(path, "PNG"):
            raise SystemExit(f"cannot write {path}")

    largest = Image.open(os.path.join(output, "pipilogicanalyzer-1024.png"))
    largest.save(os.path.join(output, "pipilogicanalyzer.ico"), sizes=ICO_SIZES)
    largest.save(os.path.join(output, "pipilogicanalyzer.icns"))
    print(f"Icons written to {output}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "build", "icons"))
