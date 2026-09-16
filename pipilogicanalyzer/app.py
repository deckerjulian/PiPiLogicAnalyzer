# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of PiPiLogicAnalyzer, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Application entry point."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Optional, Sequence

from PySide6.QtWidgets import QApplication

from . import __version__, qt_plugins
from .core import settings
from .ui.icons import app_icon
from .ui.main_window import MainWindow
from .ui.theme import STYLESHEET, apply_palette


def parse_arguments(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="pipilogicanalyzer",
        description="GUI for the LogicAnalyzer hardware (gusmanb/logicanalyzer).",
    )
    parser.add_argument("capture", nargs="?", help="capture file (.lac) to open on start-up")
    parser.add_argument(
        "--decoders",
        action="append",
        default=[],
        metavar="PATH",
        help="additional directory holding sigrok protocol decoders (repeatable)",
    )
    parser.add_argument(
        "--debug-driver",
        action="store_true",
        help="log the device communication to driver_debug.log in the settings directory",
    )
    parser.add_argument("--version", action="version", version=f"PiPiLogicAnalyzer {__version__}")
    # Used by the build workflow to check a packaged application without showing a window.
    parser.add_argument("--smoke-test", nargs="?", const="-", metavar="REPORT", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def smoke_test(window: MainWindow, report: str) -> int:
    """Check that a (packaged) application starts and finds its decoders and firmware images."""
    from .core import firmware

    registry = window.provider.registry
    registry.load()
    decoders = len(registry.decoders)
    images = len(firmware.find_images())
    text = f"PiPiLogicAnalyzer {__version__}: window created, {decoders} decoders, {images} firmware images\n"
    if report == "-":
        sys.stderr.write(text)
    else:
        with open(report, "w", encoding="utf-8") as handle:
            handle.write(text)
    window.close()
    return 0 if decoders else 1


def enable_driver_log() -> str:
    """Write the driver debug log (``DEBUG_MODE`` of the original) to a file."""
    path = settings.settings_path("driver_debug.log")
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s [%(funcName)s] %(message)s"))
    logger = logging.getLogger("pipilogicanalyzer.driver")
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    return path


def main(argv: Optional[Sequence[str]] = None) -> int:
    # Windowed builds (PyInstaller on Windows) have no console: --help/--version would fail.
    for stream in ("stdout", "stderr"):
        if getattr(sys, stream) is None:
            setattr(sys, stream, open(os.devnull, "w", encoding="utf-8"))

    arguments = parse_arguments(argv)
    if arguments.debug_driver:
        enable_driver_log()

    try:
        qt_plugins.ensure_loadable_plugins()
    except OSError as error:
        sys.stderr.write(
            "The Qt plugins are marked as hidden files (e.g. by iCloud Drive) and could not be "
            f"copied to {qt_plugins.DEFAULT_CACHE}: {error}\n"
            "Move the virtual environment out of the synced folder or run:\n\n"
            f'    chflags -R nohidden "{qt_plugins.plugin_directory()}"\n'
        )
        return 1

    application = QApplication(sys.argv[:1])
    application.setApplicationName("PiPiLogicAnalyzer")
    application.setApplicationVersion(__version__)
    application.setOrganizationName("PiPiLogicAnalyzer")
    # Fusion renders the style sheet identically on every platform.
    application.setStyle("Fusion")
    apply_palette(application)
    application.setStyleSheet(STYLESHEET)

    application.setWindowIcon(app_icon())

    window = MainWindow(decoder_paths=tuple(arguments.decoders))
    if arguments.smoke_test:
        return smoke_test(window, arguments.smoke_test)
    window.show()

    if arguments.capture and os.path.exists(arguments.capture):
        window.open_capture_file(arguments.capture)

    return application.exec()


if __name__ == "__main__":  # pragma: no cover - manual execution
    raise SystemExit(main())
