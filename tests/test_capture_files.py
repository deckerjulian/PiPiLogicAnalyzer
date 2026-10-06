"""Capture files: samples packed (small, quick, any size), the form of the original software on
request, and exports written in blocks."""

from __future__ import annotations

import gzip
import json
import os
import time

import numpy as np
import pytest

from openscilab.core import capture_io
from openscilab.core.sample_store import DiskAllocator, is_on_disk
from openscilab.driver.models import AnalogChannel, AnalyzerChannel, CaptureSession


def session(samples: int = 1003, channels: int = 3, analog: int = 0, seed: int = 1) -> CaptureSession:
    rng = np.random.default_rng(seed)
    capture = CaptureSession(frequency=1_000_000, pre_trigger_samples=100, post_trigger_samples=samples - 100)
    capture.capture_channels = [
        AnalyzerChannel(channel_number=number, channel_name=f"CH{number + 1}",
                        samples=rng.integers(0, 2, samples, dtype=np.uint8))
        for number in range(channels)]
    capture.analog_channels = [
        AnalogChannel(channel_number=number, channel_name=f"A{number}", scale=0.001,
                      raw=rng.integers(-2000, 2000, samples).astype(np.int16))
        for number in range(analog)]
    return capture


def same(first: CaptureSession, second: CaptureSession) -> bool:
    return (all(np.array_equal(a.samples, b.samples) for a, b in zip(first.capture_channels, second.capture_channels))
            and all(np.array_equal(a.raw, b.raw) for a, b in zip(first.analog_channels, second.analog_channels))
            and len(first.capture_channels) == len(second.capture_channels)
            and len(first.analog_channels) == len(second.analog_channels))


# ------------------------------------------------------------------- packed
@pytest.mark.parametrize("ending", [".lac", ".lac.gz"])
def test_packed_captures_round_trip(tmp_path, ending):
    original = session(analog=2)
    original.state_times = np.arange(1003, dtype=np.float64) * 1.5
    original.capture_channels.append(AnalyzerChannel(channel_number=9))  # a channel without samples
    path = str(tmp_path / f"capture{ending}")
    capture_io.save_capture(path, original)
    loaded = capture_io.load_capture(path).session
    assert same(original, loaded) and loaded.capture_channels[-1].samples is None
    assert np.array_equal(loaded.state_times, original.state_times)
    assert loaded.sample_count() == 1003  # a count that is no multiple of 8

    opener = gzip.open if ending.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as handle:
        stored = json.load(handle)
    channel = stored["Settings"]["CaptureChannels"][0]
    assert channel["Samples"] is None and channel["SampleCount"] == 1003 and isinstance(channel["SamplesPacked"], str)


def test_a_packed_file_is_small_and_quick(tmp_path):
    count = 1_000_000
    capture = CaptureSession(frequency=1_000_000, post_trigger_samples=count)
    capture.capture_channels = [
        AnalyzerChannel(channel_number=number, samples=((np.arange(count) >> (number + 2)) & 1).astype(np.uint8))
        for number in range(16)]
    path = str(tmp_path / "large.lac")
    started = time.monotonic()
    capture_io.save_capture(path, capture)
    loaded = capture_io.load_capture(path).session
    assert time.monotonic() - started < 5  # (a number per sample took many seconds and 48 MB)
    assert os.path.getsize(path) < 1_000_000 and same(capture, loaded)


def test_the_form_of_the_original_software(tmp_path):
    original = session(analog=1)
    path = str(tmp_path / "original.lac")
    capture_io.save_capture(path, original, compatible=True)
    stored = json.load(open(path, encoding="utf-8"))
    channel = stored["Settings"]["CaptureChannels"][0]
    assert channel["Samples"] == original.capture_channels[0].samples.tolist() and "SamplesPacked" not in channel
    assert same(original, capture_io.load_capture(path).session)


def test_settings_files_are_as_before():
    data = capture_io.session_to_dict(session(), include_samples=False)
    assert all(set(channel) == {"TextualChannelNumber", "ChannelNumber", "ChannelName", "ChannelColor", "Hidden",
                                "Samples"} for channel in data["CaptureChannels"])
    assert "StateTimesPacked" not in data


def test_samples_that_are_not_bits_are_kept(tmp_path):
    capture = session(channels=1)
    capture.capture_channels[0].samples[5] = 3  # no level a logic analyzer captures, but not lost
    path = str(tmp_path / "odd.lac")
    capture_io.save_capture(path, capture)
    assert capture_io.load_capture(path).session.capture_channels[0].samples[5] == 3


def test_a_capture_on_disk_is_packed_in_blocks_and_comes_back_on_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(capture_io, "PACK_BLOCK", 256)
    monkeypatch.setattr(capture_io, "DISK_LOAD_BYTES", 1000)
    data = DiskAllocator().zeros(5000)
    data[::7] = 1
    capture = CaptureSession(frequency=1000, post_trigger_samples=5000)
    capture.capture_channels = [AnalyzerChannel(channel_number=0, samples=data)]
    path = str(tmp_path / "disk.lac")
    capture_io.save_capture(path, capture)
    loaded = capture_io.load_capture(path).session.capture_channels[0].samples
    assert is_on_disk(loaded) and np.array_equal(loaded, data)


@pytest.mark.parametrize("damage", ["cut", "text", "short", "gzip", "list"])
def test_damaged_files_are_reported_as_such(tmp_path, damage):
    path = str(tmp_path / ("capture.lac.gz" if damage == "gzip" else "capture.lac"))
    capture_io.save_capture(path, session())
    if damage == "gzip":
        content = open(path, "rb").read()
        open(path, "wb").write(content[: len(content) // 2])
    elif damage == "list":
        open(path, "w").write("[1, 2, 3]")
    else:
        stored = json.load(open(path, encoding="utf-8"))
        channel = stored["Settings"]["CaptureChannels"][0]
        if damage == "cut":
            channel["SamplesPacked"] = channel["SamplesPacked"][:20]
        elif damage == "text":
            channel["SamplesPacked"] = "not base64 !!"
        else:
            channel["SampleCount"] = 100_000
        json.dump(stored, open(path, "w", encoding="utf-8"))
    with pytest.raises(ValueError):  # what the window shows as "could not be opened", nothing else
        capture_io.load_capture(path)


def test_the_oldest_form_is_still_read():
    values = [0b01, 0b10, 0b11, (1 << 70) | 1]
    data = {"Settings": {"Frequency": 1000, "PostTriggerSamples": 4,
                         "CaptureChannels": [{"ChannelNumber": number} for number in range(72)]},
            "Samples": values}
    loaded = capture_io.capture_from_dict(data).session
    assert loaded.capture_channels[0].samples.tolist() == [1, 0, 1, 1]
    assert loaded.capture_channels[1].samples.tolist() == [0, 1, 1, 0]
    assert loaded.capture_channels[70].samples.tolist() == [0, 0, 0, 1]
    assert loaded.capture_channels[71].samples.tolist() == [0, 0, 0, 0]


# ------------------------------------------------------------------- exports
def test_exports_are_written_in_blocks(tmp_path, monkeypatch):
    capture = session(samples=5000, channels=3, analog=1)
    whole_csv, whole_vcd = str(tmp_path / "whole.csv"), str(tmp_path / "whole.vcd")
    capture_io.export_csv(whole_csv, capture, include_time=True)
    capture_io.export_vcd(whole_vcd, capture)
    monkeypatch.setattr(capture_io, "EXPORT_BLOCK", 700)  # several blocks: the same files
    blocks_csv, blocks_vcd = str(tmp_path / "blocks.csv"), str(tmp_path / "blocks.vcd")
    capture_io.export_csv(blocks_csv, capture, include_time=True)
    capture_io.export_vcd(blocks_vcd, capture)
    assert open(whole_csv).read() == open(blocks_csv).read()
    assert open(whole_vcd).read() == open(blocks_vcd).read()
    rows = open(whole_csv).read().splitlines()
    assert len(rows) == 5001 and rows[1].count(",") == 4


def test_the_csv_of_digital_channels(tmp_path):
    capture = session(samples=10, channels=2)
    capture.capture_channels[0].channel_name = "clock, inverted"
    capture.capture_channels[0].samples[:] = [0, 1] * 5
    capture.capture_channels[1].samples[:] = 1
    path = str(tmp_path / "digital.csv")
    capture_io.export_csv(path, capture)
    rows = open(path).read().splitlines()
    assert rows[0] == '"clock, inverted",CH2'  # a comma in a name does not add a column
    assert rows[1:3] == ["0,1", "1,1"] and len(rows) == 11


def test_a_vcd_has_no_values_that_are_no_numbers(tmp_path):
    capture = session(samples=100, channels=1)
    capture.analog_channels = [AnalogChannel(channel_number=0, channel_name="A0", scale=0.001,
                                             raw=np.arange(40, dtype=np.int16))]  # shorter than the capture
    path = str(tmp_path / "short.vcd")
    capture_io.export_vcd(path, capture)
    assert "nan" not in open(path).read().lower()

    empty = CaptureSession(frequency=1000)
    empty.capture_channels = [AnalyzerChannel(channel_number=0, samples=np.zeros(0, np.uint8))]
    with pytest.raises(ValueError):
        capture_io.export_vcd(str(tmp_path / "empty.vcd"), empty)


# ------------------------------------------------------------------ the window
def test_save_as_for_the_original_software(make_dataview, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog

    from openscilab.ui.documents import dataview

    view = make_dataview()
    view.load_session(session())
    path = str(tmp_path / "for-the-original.lac")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args, **kwargs: (path, dataview.COMPATIBLE_FILTER))
    view.save_capture_as()
    stored = json.load(open(path, encoding="utf-8"))
    assert isinstance(stored["Settings"]["CaptureChannels"][0]["Samples"], list)
    view.delete_samples(0, 10)
    view.save_capture()  # the same file again: still in the form that was chosen
    stored = json.load(open(path, encoding="utf-8"))
    assert len(stored["Settings"]["CaptureChannels"][0]["Samples"]) == 993

    packed = str(tmp_path / "packed.lac")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args, **kwargs: (packed, "Captures (*.lac)"))
    view.save_capture_as()
    assert "SamplesPacked" in json.load(open(packed, encoding="utf-8"))["Settings"]["CaptureChannels"][0]


def test_a_damaged_file_is_a_message(make_dataview, tmp_path, monkeypatch):
    from openscilab.ui import messages

    path = tmp_path / "broken.lac.gz"
    path.write_bytes(b"\x1f\x8b\x08\x00 not a gzip stream")
    shown = []
    monkeypatch.setattr(messages, "error", lambda *args, **kwargs: shown.append(args[2]))
    view = make_dataview()
    view.open_capture_file(str(path))
    assert shown == ["broken.lac.gz could not be opened."] and view.model.session is None


# ------------------------------------------------- found by the second check
def test_saving_keeps_the_form_of_the_file(make_dataview, tmp_path):
    """A capture of the original software that is opened and saved stays readable for it."""
    original = str(tmp_path / "original.lac")
    capture_io.save_capture(original, session(), compatible=True)
    assert capture_io.load_capture(original).compatible
    view = make_dataview()
    view.open_capture_file(original)
    view.delete_samples(0, 3)
    view.save_capture()
    stored = json.load(open(original, encoding="utf-8"))["Settings"]["CaptureChannels"][0]
    assert isinstance(stored["Samples"], list) and len(stored["Samples"]) == 1000 and "SamplesPacked" not in stored

    packed = str(tmp_path / "packed.lac")
    capture_io.save_capture(packed, session())
    assert not capture_io.load_capture(packed).compatible
    view.open_capture_file(packed)
    view.delete_samples(0, 3)
    view.save_capture()
    assert "SamplesPacked" in json.load(open(packed, encoding="utf-8"))["Settings"]["CaptureChannels"][0]
    # the examples of the project are of the original form
    assert capture_io.load_capture("examples/demo.lac").compatible


def test_data_that_changed_while_saving_is_not_marked_saved(make_dataview, tmp_path, monkeypatch):
    from openscilab.ui import background

    view = make_dataview()
    view.load_session(session())
    view.delete_samples(0, 3)
    run = background.run

    def edit_meanwhile(parent, text, call, cancellable=True):
        result = run(parent, text, call, cancellable)
        view.delete_samples(0, 5)  # the user went on editing while the file was written
        return result

    monkeypatch.setattr(background, "run", edit_meanwhile)
    path = str(tmp_path / "busy.lac")
    view._write_capture(path)
    assert view.dirty and view.current_file == path  # the file is there, the later edit is not in it
    assert capture_io.load_capture(path).session.sample_count() == 1000
    monkeypatch.setattr(background, "run", run)
    view.save_capture()
    assert not view.dirty and capture_io.load_capture(path).session.sample_count() == 995
