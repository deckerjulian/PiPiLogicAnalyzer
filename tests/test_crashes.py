"""What is left of an error nobody caught (core/crashes.py): crash.log with the traceback, a notice
for the user, repeats counted; faults.log of the interpreter; the application installs it."""

from __future__ import annotations

import faulthandler
import os
import sys
import threading

import pytest

from openscilab.core import crashes

# (pytest-qt takes every call of sys.excepthook for an error in the Qt event loop)
pytestmark = pytest.mark.qt_no_exception_capture


@pytest.fixture
def hooked(tmp_path, monkeypatch):
    notices: list[str] = []
    # the hooks of pytest are not the ones to chain to here: what the tests raise on purpose would be
    # reported as an error of a later test
    monkeypatch.setattr(threading, "excepthook", lambda args: None)
    path = crashes.install(str(tmp_path / "crash.log"), notify=notices.append, faults=False)
    yield path, notices
    crashes.uninstall()


def raise_through_hook(error: BaseException) -> None:
    try:
        raise error
    except BaseException:
        sys.excepthook(*sys.exc_info())


def test_an_uncaught_error_lands_in_the_log_and_is_noticed(hooked, capsys):
    path, notices = hooked
    assert crashes.installed()
    raise_through_hook(RuntimeError("the slot failed\nmore"))
    text = crashes.read(path)
    assert "RuntimeError: the slot failed" in text and "Traceback" in text and "raise_through_hook" in text
    assert notices == ["RuntimeError: the slot failed"]
    assert "RuntimeError: the slot failed" in capsys.readouterr().err  # (the default hook still runs)


def test_the_same_error_again_and_again_is_counted(hooked):
    path, notices = hooked
    for _ in range(4):
        raise_through_hook(ValueError("every tick"))
    raise_through_hook(KeyError("another"))
    text = crashes.read(path)
    assert text.count("ValueError: every tick") == 1 and "repeated 3 times" in text
    assert notices == ["ValueError: every tick", "KeyError: 'another'"]


def test_errors_of_threads_are_caught_too(hooked):
    path, notices = hooked
    thread = threading.Thread(name="openscilab-ports")
    try:
        raise OSError("no port")
    except OSError as error:
        # (pytest wraps threading.excepthook around each test: the hook is called as a thread would)
        args = threading.ExceptHookArgs([OSError, error, error.__traceback__, thread])
    crashes._installed["hooks"][1](args)
    assert "in thread openscilab-ports" in crashes.read(path)
    assert notices == ["OSError: no port in thread openscilab-ports"]
    crashes._installed["hooks"][1](threading.ExceptHookArgs([SystemExit, SystemExit(0), None, thread]))
    assert len(notices) == 1  # (a thread that exits is no error)


def test_the_log_is_cut_when_it_grows(hooked, monkeypatch):
    path, _notices = hooked
    monkeypatch.setattr(crashes, "MAX_SIZE", 2000)
    for number in range(40):
        raise_through_hook(RuntimeError(f"error {number} " + "x" * 100))
    assert os.path.getsize(path) < 3000 and crashes.read(path).startswith("(older entries cut)")
    assert "error 39" in crashes.read(path)


def test_uninstall_restores_the_hooks(tmp_path):
    before = (sys.excepthook, threading.excepthook)
    crashes.install(str(tmp_path / "crash.log"), faults=False)
    assert (sys.excepthook, threading.excepthook) != before
    crashes.uninstall()
    assert (sys.excepthook, threading.excepthook) == before and not crashes.installed()
    assert crashes.read(str(tmp_path / "nothing.log")) == ""


def test_faulthandler_writes_to_the_settings_directory(tmp_path, monkeypatch):
    was_enabled = faulthandler.is_enabled()
    path = crashes.enable_faulthandler(str(tmp_path / "faults.log"))
    try:
        assert path == str(tmp_path / "faults.log") and faulthandler.is_enabled()
        faulthandler.dump_traceback(crashes._faults_file, all_threads=False)
        crashes._faults_file.flush()
        assert "test_faulthandler_writes" in open(path, encoding="utf-8").read()
    finally:
        # back to where it was: off, or on stderr (PYTHONFAULTHANDLER in the CI) - not the file of this test
        faulthandler.disable()
        if was_enabled:
            faulthandler.enable(sys.stderr, all_threads=True)
    assert crashes.enable_faulthandler(str(tmp_path / "no-such-folder" / "faults.log")) is None
    faulthandler.disable()
    if was_enabled:
        faulthandler.enable(sys.stderr, all_threads=True)


def test_the_application_installs_the_hook(shell, qtbot):
    from openscilab.app import CrashNotifier

    notifier = CrashNotifier()
    notifier.failed.connect(shell.show_crash_notice)
    path = crashes.install(notify=notifier.failed.emit, faults=False)
    try:
        assert path == crashes.crash_log_path()
        raise_through_hook(RuntimeError("a slot failed"))
        qtbot.waitUntil(lambda: "a slot failed" in shell.statusBar().currentMessage())
        assert "crash.log" in shell.statusBar().currentMessage() and "a slot failed" in crashes.read()
    finally:
        crashes.uninstall()
    assert any(action.text() == "Open the &log folder" for action in shell.help_menu.actions())
