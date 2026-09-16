"""Shared test fixtures."""

from __future__ import annotations

import os
import sys

import pytest

from pipilogicanalyzer import qt_plugins

# Before any test creates a QApplication: iCloud Drive hides the plugin files of
# a virtual environment inside ~/Documents, which Qt would not load.
qt_plugins.ensure_loadable_plugins()

FIXTURE_DECODERS = os.path.join(os.path.dirname(__file__), "fixtures", "decoders")


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    """Keep the tests away from the real settings directory."""
    monkeypatch.setenv("PIPILOGICANALYZER_SETTINGS_DIR", str(tmp_path / "settings"))


@pytest.fixture(autouse=True)
def no_unanswered_message_boxes(monkeypatch):
    """A message box nobody answers would block the test run forever; fail instead.

    Tests that expect a question or a message patch ``pipilogicanalyzer.ui.messages``.
    """
    from pipilogicanalyzer.ui import messages

    def unexpected(box):
        raise AssertionError(f"Unexpected message box '{box.windowTitle()}': {box.text()}")

    monkeypatch.setattr(messages, "_exec", unexpected)


@pytest.fixture
def decoder_registry():
    from pipilogicanalyzer.sigrok.engine import DecoderRegistry

    registry = DecoderRegistry([FIXTURE_DECODERS])
    registry.load(force=True)
    yield registry
    sys.modules.pop("testdec", None)
    sys.modules.pop("testdec.pd", None)
