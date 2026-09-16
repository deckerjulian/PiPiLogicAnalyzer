"""Start-up handling of Qt plugins hidden by macOS (e.g. iCloud Drive)."""

from __future__ import annotations

import os
import stat

import pytest

from pipilogicanalyzer import app, qt_plugins

requires_file_flags = pytest.mark.skipif(
    not hasattr(stat, "UF_HIDDEN") or not hasattr(os, "chflags"),
    reason="file flags are only available on macOS/BSD",
)


def make_plugins(root, hidden: bool):
    for group, name in (("platforms", "libqcocoa.dylib"), ("styles", "libqmacstyle.dylib")):
        directory = root / group
        directory.mkdir(parents=True)
        path = directory / name
        path.write_bytes(b"plugin")
        path.chmod(0o755)
        if hidden:
            os.chflags(path, stat.UF_HIDDEN)
    return root


@requires_file_flags
def test_hidden_files_are_detected(tmp_path):
    make_plugins(tmp_path, hidden=True)
    assert qt_plugins.hidden_files(str(tmp_path / "platforms")) == [
        str(tmp_path / "platforms" / "libqcocoa.dylib")
    ]


def test_missing_directory_has_no_hidden_files(tmp_path):
    assert qt_plugins.hidden_files(str(tmp_path / "missing")) == []


@requires_file_flags
def test_hidden_plugins_are_copied_without_the_flag(tmp_path):
    source = make_plugins(tmp_path / "source", hidden=True)
    environ: dict[str, str] = {}

    target = qt_plugins.ensure_loadable_plugins(str(source), str(tmp_path / "cache"), environ)

    copied = tmp_path / "cache" / "platforms" / "libqcocoa.dylib"
    assert target == str(tmp_path / "cache")
    assert copied.read_bytes() == b"plugin"
    assert not os.lstat(copied).st_flags & stat.UF_HIDDEN
    assert os.access(copied, os.X_OK)
    assert (tmp_path / "cache" / "styles" / "libqmacstyle.dylib").exists()
    assert environ["QT_QPA_PLATFORM_PLUGIN_PATH"] == str(tmp_path / "cache" / "platforms")
    assert environ["QT_PLUGIN_PATH"] == target


@requires_file_flags
def test_visible_plugins_are_used_in_place(tmp_path):
    source = make_plugins(tmp_path / "source", hidden=False)
    environ: dict[str, str] = {}
    assert qt_plugins.ensure_loadable_plugins(str(source), str(tmp_path / "cache"), environ) is None
    assert environ == {}
    assert not (tmp_path / "cache").exists()


def test_start_is_refused_with_a_hint_when_copying_fails(monkeypatch, capsys):
    def fail():
        raise OSError("read-only")

    monkeypatch.setattr(qt_plugins, "ensure_loadable_plugins", fail)
    monkeypatch.setattr(qt_plugins, "plugin_directory", lambda: "/x/plugins")

    assert app.main([]) == 1
    assert "chflags -R nohidden" in capsys.readouterr().err
