# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of openSciLab, a port and extension of his software;
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

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication

from . import __version__, qt_plugins
from .core import crashes, settings
from .core.sample_store import clean_disk_directory


class CrashNotifier(QObject):
    """Carries the notice of an uncaught error from the thread that failed to the window."""

    #: one line about the error (see ``core.crashes.install``)
    failed = Signal(str)


def parse_arguments(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="openscilab",
        description="openSciLab: open measurement and control lab with a logic analyzer.",
    )
    parser.add_argument("files", nargs="*", help="files to open on start-up (captures: .lac, .sr)")
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
    parser.add_argument("--version", action="version", version=f"openSciLab {__version__}")
    # Used by the build workflow to check a packaged application without showing a window.
    parser.add_argument("--smoke-test", nargs="?", const="-", metavar="REPORT", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def smoke_test(window, report: str) -> int:
    """Check that a (packaged) application starts and finds its decoders, firmware images and libusb."""
    from .core import firmware
    from .driver.dslogic import usb as dslogic_usb

    window.open_example("00-start/02-logic-analyzer")
    registry = window.provider.registry
    registry.load()
    decoders = len(registry.decoders)
    images = len(firmware.find_images())
    libusb = dslogic_usb.backend_available()
    text = (
        f"openSciLab {__version__}: shell created, {decoders} decoders, {images} firmware images, "
        f"libusb {'found' if libusb else 'MISSING'}\n"
    )
    if report == "-":
        sys.stderr.write(text)
    else:
        with open(report, "w", encoding="utf-8") as handle:
            handle.write(text)
    window.force_close()
    return 0 if decoders and libusb else 1


def enable_driver_log() -> str:
    """Write the driver debug log (``DEBUG_MODE`` of the original) to a file."""
    path = settings.settings_path("driver_debug.log")
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s [%(funcName)s] %(message)s"))
    logger = logging.getLogger("openscilab.driver")
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    return path


#: First arguments that make ``openscilab`` a command line program (``openscilab run flow.yaml``)
CLI_COMMANDS = ("run", "sim", "capture", "devices", "info", "decode", "convert", "decoders", "plugins")


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments_list = list(sys.argv[1:] if argv is None else argv)
    if arguments_list and arguments_list[0] in CLI_COMMANDS:
        from . import cli

        return cli.main(arguments_list)
    return _gui(arguments_list)


def _gui(argv: Sequence[str]) -> int:
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
    # an error nobody caught (a slot of Qt that failed, a thread): crash.log, a notice in the window
    notifier = CrashNotifier()
    crashes.install(notify=notifier.failed.emit)
    # The user interface is imported with the application in place: its theme is chosen when
    # the module is loaded, and "follow the system" asks the application for the colour scheme
    # (without one it fell back to "dark" on Linux and to a helper program on macOS).
    from .ui.icons import app_icon
    from .ui.shell.main_window import ShellWindow
    from .ui.theme import apply_palette, build_stylesheet

    application.setApplicationName("openSciLab")
    application.setApplicationVersion(__version__)
    application.setOrganizationName("openSciLab")
    # Fusion renders the style sheet identically on every platform.
    application.setStyle("Fusion")
    apply_palette(application)
    application.setStyleSheet(build_stylesheet())

    application.setWindowIcon(app_icon())

    # Stream files an earlier run could not delete while they were mapped (Windows), shared memory
    # of device processes that crashed (Linux)
    clean_disk_directory()
    from .core.shared_arrays import clean_segments

    clean_segments()

    window = ShellWindow(decoder_paths=tuple(arguments.decoders))
    notifier.failed.connect(window.show_crash_notice)
    if arguments.smoke_test:
        return smoke_test(window, arguments.smoke_test)
    window.show()
    from . import plugins
    from .core import preferences as preferences_module

    if preferences_module.get("devices.high_priority"):
        QTimer.singleShot(500, window.raise_priority)  # (asks once the window is there)
    failed = plugins.problems()
    if failed:
        window.statusBar().showMessage(
            f"{len(failed)} plugin{'s' if len(failed) > 1 else ''} could not be loaded ({failed[0].name}: "
            f"{failed[0].error}): Help → Plugins...", 20000)

    files = [path for path in arguments.files if os.path.exists(path)]
    for path in files:
        window.open_file(path)
    if not files:
        from .core import preferences

        restored = preferences.get("startup.restore_session") and window.restore_session()
        if not restored:
            # The start page in front (what to do first), a flow to start with beside it
            # (temporary until it is saved).
            show_start = preferences.get("startup.show_start_page")
            if show_start:
                window.show_start_page()
            window.new_flow(activate=not show_start)

    # What the application made while starting stays: the garbage collector stops walking it, so a
    # full collection holds every thread for well under a millisecond instead of about 10 ms
    QTimer.singleShot(2000, settle_memory)
    return application.exec()


def settle_memory() -> None:
    """Collect once, then freeze what is left (``gc.freeze``): later full collections only walk
    what was made since. Called once the application started and again after big loads."""
    import gc

    gc.collect()
    gc.freeze()


if __name__ == "__main__":  # pragma: no cover - manual execution
    raise SystemExit(main())
