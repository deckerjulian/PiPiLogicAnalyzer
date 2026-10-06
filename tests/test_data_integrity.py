"""Nothing is lost or silently changed: files are replaced only when written completely, a
damaged settings file is kept, markers and pinned channels are in the capture file, and edits
act on the data they were meant for."""

from __future__ import annotations

import json
import os

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from openscilab.core import capture_io, files, settings
from openscilab.core.profiles import Profile, ProfileStore
from openscilab.driver.models import AnalyzerChannel, CaptureSession
from openscilab.ui.dialogs import shift_dialog
from openscilab.ui.widgets.sample_marker import Selection


def session(samples: int = 1000, channels: int = 3) -> CaptureSession:
    capture = CaptureSession(frequency=1_000_000, pre_trigger_samples=100, post_trigger_samples=samples - 100)
    capture.capture_channels = [
        AnalyzerChannel(channel_number=number, channel_name=f"CH{number + 1}",
                        samples=((np.arange(samples) // (number + 2)) % 2).astype(np.uint8))
        for number in range(channels)]
    return capture


def loaded(make_dataview):
    view = make_dataview()
    view.load_session(session())
    QApplication.processEvents()
    return view


# ----------------------------------------------------------------- files
def test_a_failed_write_keeps_the_file(tmp_path):
    path = tmp_path / "data.txt"
    files.atomic_write(str(path), "first")
    with pytest.raises(RuntimeError):
        with files.atomic_open(str(path)) as handle:
            handle.write("half of the sec")
            raise RuntimeError("disk full")
    assert path.read_text() == "first"
    assert os.listdir(tmp_path) == ["data.txt"]  # no temporary file left
    files.atomic_write(str(path), b"\x00\x01")
    assert path.read_bytes() == b"\x00\x01"


def test_a_capture_that_cannot_be_written_keeps_the_old_one(tmp_path, monkeypatch):
    path = str(tmp_path / "capture.lac")
    capture_io.save_capture(path, session())
    before = open(path, "rb").read()

    def broken(*_args, **_kwargs):
        raise OSError("No space left on device")

    monkeypatch.setattr(json, "dump", broken)
    with pytest.raises(OSError):
        capture_io.save_capture(path, session(samples=2000))
    assert open(path, "rb").read() == before
    assert os.listdir(tmp_path) == ["capture.lac"]


def test_a_compressed_capture_round_trips(tmp_path):
    path = str(tmp_path / "capture.lac.gz")
    original = session()
    capture_io.save_capture(path, original)
    again = capture_io.load_capture(path).session
    assert [channel.samples.tobytes() for channel in again.capture_channels] == \
        [channel.samples.tobytes() for channel in original.capture_channels]


def test_a_damaged_settings_file_is_kept(tmp_path):
    path = settings.settings_path("profiles.json")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write('{"Profiles": [{"Name": "mine"')  # cut off by a crash
    store = ProfileStore()
    assert store.user_profiles() == []
    store.add(Profile("new"))
    assert store.save()
    assert os.path.exists(path + ".bad")  # what was there is not overwritten by the next save
    assert "mine" in open(path + ".bad", encoding="utf-8").read()
    assert [profile.name for profile in ProfileStore().user_profiles()] == ["new"]


def test_one_unreadable_profile_does_not_lose_the_others(tmp_path):
    path = settings.settings_path("profiles.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"Profiles": [{"Name": "good"}, {"Name": ""}, {"Name": "also good"}]}, handle)
    store = ProfileStore()
    assert [profile.name for profile in store.user_profiles()] == ["good", "also good"]
    assert os.path.exists(path + ".bad")


# ------------------------------------------------- markers and pinned channels
def test_markers_and_pins_are_saved_with_the_capture(make_dataview, tmp_path):
    view = loaded(make_dataview)
    view.model.add_bookmark(40, "start")
    view.model.add_bookmark(700, "end")
    view.model.set_pinned(view.model.channels[1], True)
    assert view.dirty
    path = str(tmp_path / "marked.lac")
    view._write_capture(path)
    assert not view.dirty

    other = make_dataview()
    other.open_capture_file(path)
    QApplication.processEvents()
    assert [(bookmark.sample, bookmark.name) for bookmark in other.model.bookmarks] == [(40, "start"), (700, "end")]
    assert other.model.pinned_numbers() == [1]
    assert not other.dirty and not other.undo.canUndo()  # what the file holds is no edit
    # the first edit after opening does not take the markers away when it is undone
    other.model.add_bookmark(500, "more")
    other.undo.undo()
    assert len(other.model.bookmarks) == 2 and other.model.pinned_numbers() == [1]


def test_files_without_markers_stay_as_they_were(tmp_path):
    data = capture_io.capture_to_dict(session())
    assert set(data) == {"Settings", "Samples", "SelectedRegions"}
    exported = capture_io.capture_from_dict(data)
    assert exported.bookmarks == [] and exported.pinned == []


# ------------------------------------------------------------------ edits
def test_a_new_capture_clears_the_selection(make_dataview):
    view = loaded(make_dataview)
    view.sample_marker.selection = Selection(10, 500)
    view.load_session(session(samples=600))
    assert view.sample_marker.selection is None  # cut or delete cannot act on the old positions


def test_an_edit_keeps_the_selection_until_it_is_done(make_dataview):
    view = loaded(make_dataview)
    view.sample_marker.selection = Selection(10, 50)
    view.model.channels[0].channel_name = "CLK"
    view.model.notify_channels_changed()
    view.model.notify_capture_changed()
    assert view.sample_marker.selection is not None  # the same data


def test_a_short_channel_limits_only_its_own_shift(make_dataview, monkeypatch):
    view = loaded(make_dataview)
    channels = view.model.session.capture_channels
    channels[0].samples = channels[0].samples[:5].copy()
    expected = np.concatenate((np.zeros(20, np.uint8), channels[1].samples[:-20]))

    class Dialog:
        def __init__(self, shown, _maximum, _parent):
            self.shifted_channels = list(shown)
            self.shift_amount = 20
            self.direction = shift_dialog.ShiftDirection.RIGHT
            self.mode = shift_dialog.ShiftMode.LOW

        def exec(self):
            return True

    monkeypatch.setattr("openscilab.ui.documents.dataview.ShiftChannelsDialog", Dialog)
    view.shift_channels()
    assert np.array_equal(channels[1].samples, expected)  # shifted by 20, not by the 5 of channel 1
    assert channels[0].samples.size == 5 and not channels[0].samples.any()


def test_inserting_keeps_all_channels_equally_long(make_dataview):
    view = loaded(make_dataview)
    channels = view.model.session.capture_channels
    channels[2].samples = None
    view._insert_samples(100, [np.ones(50, np.uint8)] * 3)
    assert [channel.samples.size for channel in channels] == [1050, 1050, 1050]
    assert channels[2].samples[100:150].all() and not channels[2].samples[:100].any()


def test_search_hits_do_not_survive_an_edit(make_dataview):
    view = loaded(make_dataview)
    view.model.set_search_hits(np.array([10, 200, 400]))
    view.delete_samples(0, 100)
    assert len(view.model.search_hits) == 0


# ------------------------------------------------- found by the second check
def test_writing_through_a_link_keeps_the_link(tmp_path):
    real = tmp_path / "real.json"
    real.write_text("old")
    link = tmp_path / "link.json"
    try:
        os.symlink(real, link)
    except (OSError, NotImplementedError):
        pytest.skip("no symbolic links here")
    files.atomic_write(str(link), "new")
    assert os.path.islink(link) and real.read_text() == "new"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_permissions_are_as_with_a_plain_open(tmp_path):
    script = tmp_path / "run.sh"
    script.write_text("old")
    os.chmod(script, 0o755)
    files.atomic_write(str(script), "new")
    assert os.stat(script).st_mode & 0o777 == 0o755  # the mode of the file stays

    fresh = tmp_path / "fresh.txt"
    files.atomic_write(str(fresh), "x")
    plain = tmp_path / "plain.txt"
    plain.write_text("x")
    assert os.stat(fresh).st_mode & 0o777 == os.stat(plain).st_mode & 0o777  # a new file: as any new file

    locked = tmp_path / "locked.txt"
    locked.write_text("keep")
    os.chmod(locked, 0o444)
    if os.access(locked, os.W_OK):
        pytest.skip("running as a user who may write everything")
    with pytest.raises(PermissionError):
        files.atomic_write(str(locked), "overwritten")
    assert locked.read_text() == "keep"  # a write-protected file is not replaced behind its back


def test_a_long_file_name_can_still_be_written(tmp_path):
    limit = os.pathconf(tmp_path, "PC_NAME_MAX") if hasattr(os, "pathconf") else 255
    path = tmp_path / ("n" * (limit - 5) + ".json")
    files.atomic_write(str(path), "{}")  # (the temporary name must not be longer than the name)
    assert path.read_text() == "{}"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_a_file_in_a_folder_that_takes_no_new_files(tmp_path):
    folder = tmp_path / "fixed"
    folder.mkdir()
    target = folder / "data.txt"
    target.write_text("old")
    os.chmod(folder, 0o555)
    try:
        if os.access(folder, os.W_OK):
            pytest.skip("running as a user who may write everything")
        files.atomic_write(str(target), "new")
        assert target.read_text() == "new"
    finally:
        os.chmod(folder, 0o755)
