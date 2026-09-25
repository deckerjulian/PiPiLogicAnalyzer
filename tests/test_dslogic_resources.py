"""Finding and downloading the DSLogic bitstreams."""

from __future__ import annotations

import hashlib
import io
import os

import pytest

from pipilogicanalyzer.driver.dslogic import resources


def test_the_chosen_folder_comes_first(tmp_path, monkeypatch):
    chosen, dsview = tmp_path / "chosen", tmp_path / "dsview"
    for folder, content in ((chosen, b"chosen"), (dsview, b"dsview")):
        folder.mkdir()
        (folder / "DSLogicPlus.bin").write_bytes(content)
    monkeypatch.setattr(resources, "dsview_directories", lambda: [str(dsview)])

    assert resources.load("DSLogicPlus.bin") == b"dsview"
    resources.set_chosen_directory(str(chosen))
    assert resources.chosen_directory() == str(chosen)
    assert resources.load("DSLogicPlus.bin") == b"chosen"
    with pytest.raises(resources.ResourceError):
        resources.load("DSLogicU3Pro32.bin")


def test_empty_files_are_ignored(tmp_path):
    (tmp_path / "DSLogicPlus.bin").write_bytes(b"")
    assert resources.find("DSLogicPlus.bin", [str(tmp_path)]) is None


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_download_checks_the_file(monkeypatch):
    data = b"x" * 100
    monkeypatch.setitem(resources.KNOWN_FILES, "Test.bin", (100, hashlib.sha256(data).hexdigest()))
    urls = []

    def opener(url, timeout):
        urls.append(url)
        return FakeResponse(data)

    path = resources.download("Test.bin", opener=opener)
    assert open(path, "rb").read() == data
    assert path.startswith(resources.cache_directory())
    assert resources.DSVIEW_COMMIT in urls[0] and urls[0].endswith("/DSView/res/Test.bin")
    # the cache is searched afterwards
    assert resources.find("Test.bin") == path

    with pytest.raises(resources.ResourceError, match="does not match"):
        resources.download("Test.bin", opener=lambda url, timeout: FakeResponse(b"y" * 100))
    assert open(path, "rb").read() == data  # the good copy stays
    assert not os.path.exists(path + ".part")


def test_unknown_files_are_not_downloaded():
    with pytest.raises(resources.ResourceError):
        resources.download("Other.bin")


def test_download_errors_are_reported(monkeypatch):
    def failing(url, timeout):
        raise OSError("offline")

    with pytest.raises(resources.ResourceError, match="offline"):
        resources.download("DSLogicPlus.bin", opener=failing)
