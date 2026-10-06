"""Streams recorded to disk: memory-mapped sample arrays, limits by the free disk space."""

from __future__ import annotations

import os
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from openscilab.core import analysis, sample_store
from openscilab.core.analysis import ChannelTransitions
from openscilab.core.sample_store import DiskAllocator, RingStore, SampleStore, is_on_disk
from openscilab.driver import base
from openscilab.driver.base import ACQUISITION_STREAM, CaptureError
from openscilab.driver.models import AnalyzerChannel, CaptureSession, TriggerType

from test_dslogic_driver import FakeDSLogic, header, info, open_driver, session, stream_data

DISK = 100 << 30


@pytest.fixture(autouse=True)
def disk(tmp_path, monkeypatch):
    """Stream files in a temporary folder, and a disk with 100 GiB free."""
    monkeypatch.setattr(sample_store, "disk_directory", lambda: str(tmp_path / "streams"))
    monkeypatch.setattr(base, "disk_sample_bytes", lambda: DISK)
    return tmp_path / "streams"


@pytest.fixture(scope="module")
def application():
    return QApplication.instance() or QApplication([])


def test_disk_arrays_are_mapped_files_without_a_name(disk):
    array = DiskAllocator().zeros(1000)
    array[:] = 7
    assert is_on_disk(array) and is_on_disk(array[10:20]) and int(array.sum()) == 7000
    if os.name == "posix":
        assert os.listdir(disk) == []  # unlinked, released with the array


def test_stores_on_disk_keep_their_samples_on_disk():
    linear = SampleStore([0, 1], 500, DiskAllocator())
    linear.append({0: np.ones(300, np.uint8), 1: np.zeros(300, np.uint8)})
    views, first = linear.result()
    assert first == 0 and is_on_disk(views[0]) and int(views[0].sum()) == 300

    ring = RingStore([0], 100, DiskAllocator())
    ring.append({0: (np.arange(250) % 2).astype(np.uint8)})
    views, first = ring.result()
    assert first == 150 and is_on_disk(views[0])
    assert np.array_equal(views[0], (np.arange(150, 250) % 2).astype(np.uint8))


def test_the_index_of_a_capture_on_disk_goes_to_disk(monkeypatch):
    monkeypatch.setattr(analysis, "DISK_INDEX_RUNS", 100)
    monkeypatch.setattr(analysis, "INDEX_BLOCK", 1000)
    samples = DiskAllocator().zeros(20_000)
    samples[::2] = 1  # an edge on every sample
    transitions = ChannelTransitions(samples, 1)
    assert len(transitions) == 20_000 and is_on_disk(transitions.absolute_starts)
    in_memory = ChannelTransitions(np.array(samples), 1)
    assert not is_on_disk(in_memory.absolute_starts)
    assert np.array_equal(transitions.starts, in_memory.starts)


def test_stream_limits_follow_the_disk():
    driver = open_driver(FakeDSLogic(info(0x002D)))
    memory = driver.get_limits(range(4), ACQUISITION_STREAM).max_total_samples
    on_disk = driver.get_limits(range(4), ACQUISITION_STREAM, to_disk=True).max_total_samples
    endless = driver.get_limits(range(4), ACQUISITION_STREAM, to_disk=True, continuous=True).max_total_samples
    assert memory == (1 << 30) // 4
    assert on_disk == DISK // 4 and endless == DISK // 8
    buffer = driver.get_limits(range(4), "buffer").max_total_samples
    assert driver.get_limits(range(4), "buffer", to_disk=True).max_total_samples == buffer


def test_a_dslogic_stream_to_disk(monkeypatch):
    from openscilab.driver.dslogic import driver as module

    monkeypatch.setattr(module, "PROGRESS_INTERVAL", 0)
    device = FakeDSLogic(info())
    driver = open_driver(device)
    signals, chunks = stream_data(64 * 256)
    device.bulk_in = [header(real_pos=0)] + chunks
    capture = session([0, 1], pre=0, post=64 * 256, acquisition_mode=ACQUISITION_STREAM,
                      trigger_type=TriggerType.IMMEDIATE, to_disk=True)
    too_long = session([0, 1], pre=0, post=(1 << 30), acquisition_mode=ACQUISITION_STREAM,
                       trigger_type=TriggerType.IMMEDIATE)
    assert driver.capture_setup(too_long) is None  # more than the memory holds
    too_long.to_disk = True
    assert driver.capture_setup(too_long) is not None

    done = threading.Event()
    results = []
    assert driver.start_capture(capture, lambda args: (results.append(args), done.set())) is CaptureError.NONE
    assert done.wait(5) and results[0].success
    samples = results[0].session.capture_channels[0].samples
    assert is_on_disk(samples) and np.array_equal(samples, signals[0])


def test_a_pico_stream_to_disk(monkeypatch):
    import test_pico_stream as pico_tests
    from openscilab.driver.pico import analyzer
    from openscilab.driver.pico.analyzer import PicoDriver

    transport = pico_tests.StreamingTransport()
    transport.queue_response("CAPS:SELFTEST,DEVICEINFO,STREAM=800000")
    monkeypatch.setattr(analyzer, "SerialTransport", lambda *args, **kwargs: transport)
    driver = PicoDriver("/dev/fake")
    driver.capabilities()
    words = np.arange(2000) & 0xFF
    transport.queue_response("STREAM_STARTED:0,1,2")
    transport.queue_data(pico_tests.chunks(words))
    capture = pico_tests.stream_session(samples=1500)
    capture.to_disk = True
    result, _progress = pico_tests.run(driver, capture)
    samples = result.session.capture_channels[1].samples
    assert result.success and is_on_disk(samples) and np.array_equal(samples, (words[:1500] >> 1) & 1)


def test_the_dialog_records_a_stream_to_disk(application):
    from openscilab.ui.dialogs.capture_dialog import CaptureDialog

    driver = open_driver(FakeDSLogic(info(0x002D)))
    dialog = CaptureDialog(driver)
    try:
        for selector in dialog.channel_selectors:
            selector.enabled = selector.channel_number < 4
        assert dialog.disk_box.isHidden()  # buffer mode
        dialog.acquisition_box.setCurrentIndex(dialog.acquisition_box.findData(ACQUISITION_STREAM))
        dialog.immediate_radio.setChecked(True)
        assert not dialog.disk_box.isHidden()
        dialog.max_samples_button.click()
        assert dialog.post_samples_box.value() == (1 << 30) // 4
        dialog.disk_box.setChecked(True)  # at the maximum: grows to what the disk holds
        assert dialog.post_samples_box.value() == DISK // 4
        assert "on disk" in dialog.summary_label.text()
        dialog._accept()
        settings = dialog.selected_settings
        assert settings.to_disk and driver.capture_setup(settings) is not None

        again = CaptureDialog(driver)
        try:
            assert again.disk_box.isChecked() and again.post_samples_box.value() == DISK // 4
        finally:
            again.close()
    finally:
        dialog.close()


def test_clone_settings_does_not_copy_the_samples():
    capture = CaptureSession(frequency=1000, post_trigger_samples=10)
    samples = DiskAllocator().zeros(10)
    capture.capture_channels = [AnalyzerChannel(channel_number=0, samples=samples)]
    clone = capture.clone_settings()
    assert clone.capture_channels[0].samples is None
    assert capture.capture_channels[0].samples is samples


def test_the_main_window_keeps_the_live_index_and_saves_huge_captures(application, monkeypatch, make_dataview,
                                                                    tmp_path):
    from openscilab.ui.documents import dataview as module

    warnings = []
    monkeypatch.setattr(module.messages, "warning", lambda *args, **kwargs: warnings.append(args))
    window = make_dataview()
    try:
        model = window.model
        live = CaptureSession(frequency=1000, post_trigger_samples=1000)
        data = DiskAllocator().zeros(1000)
        data[::3] = 1
        live.capture_channels = [AnalyzerChannel(channel_number=0, samples=data[:600])]
        model.set_session(live, live=True)
        index = model.transitions[0]
        final = live.clone_settings()
        final.capture_channels[0].samples = data
        assert model.finish_live(final)
        assert model.transitions[0] is index and index.sample_count == 1000 and not model.is_live
        assert model.on_disk

        # A capture too large for memory is saved all the same (packed, block by block) and
        # comes back from the file on disk again; only writing it as text is refused.
        monkeypatch.setattr(module, "MAX_SAMPLE_BYTES", 500)
        monkeypatch.setattr(module.capture_io, "DISK_LOAD_BYTES", 500)
        monkeypatch.setattr(module.capture_io, "PACK_BLOCK", 64)  # several blocks
        path = str(tmp_path / "huge.lac")
        window._write_capture(path)
        assert warnings == [] and os.path.getsize(path) < 3000
        loaded = module.capture_io.load_capture(path).session.capture_channels[0].samples
        assert is_on_disk(loaded) and np.array_equal(loaded, data)

        window.export_capture("csv")
        window._write_capture(str(tmp_path / "original.lac"), compatible=True)
        assert len(warnings) == 2 and "too many" in warnings[0][2]
        assert not os.path.exists(tmp_path / "original.lac")
    finally:
        window.close()


def test_a_stream_to_disk_stops_when_the_disk_fills(monkeypatch):
    from openscilab.driver.dslogic import driver as module

    monkeypatch.setattr(module, "PROGRESS_INTERVAL", 0)
    monkeypatch.setattr(base.DiskWatch.__init__, "__defaults__", (0.01, None))
    device = FakeDSLogic(info())
    driver = open_driver(device)
    _signals, chunks = stream_data(64 * 1024)
    device.bulk_in = [header(real_pos=0)] + chunks[: len(chunks) // 2]  # then it waits for more
    free = iter([DISK] * 5 + [0] * 100_000)
    monkeypatch.setattr(base, "disk_sample_bytes", lambda: next(free))
    capture = session([0, 1], pre=0, post=64 * 1024, acquisition_mode=ACQUISITION_STREAM,
                      trigger_type=TriggerType.IMMEDIATE, to_disk=True)
    done = threading.Event()
    results = []
    assert driver.start_capture(capture, lambda args: (results.append(args), done.set())) is CaptureError.NONE
    assert done.wait(5)
    assert results[0].success and "disk is almost full" in results[0].error
    assert 0 < len(results[0].session.capture_channels[0].samples) < 64 * 1024
