"""Arrays two processes share (core/shared_arrays): the samples a device process writes are read by
the application without being copied through the pipe; their memory lives as long as an array of
it, in either process."""

from __future__ import annotations

import multiprocessing
import os
import sys

import numpy as np
import pytest

from openscilab.core import sample_store, shared_arrays

CONTEXT = multiprocessing.get_context("spawn")


def _child(connection, on_disk: bool, folder: str) -> None:  # pragma: no cover - another process
    """A device process: a ring store in shared arrays, filled while the other side reads it."""
    from openscilab.core import sample_store as store_module
    from openscilab.core import shared_arrays as shared

    store_module.share_arrays()
    allocator = store_module.DiskAllocator(folder) if on_disk else store_module.MemoryAllocator()
    store = store_module.RingStore([0, 3], 1000, allocator)
    store.append({0: np.full(600, 1, np.uint8), 3: np.full(600, 3, np.uint8)})
    views, first = store.window()
    data, keep = shared.dumps({"views": views, "arrays": store.arrays, "first": first,
                               "big": np.arange(300_000, dtype=np.int32), "small": np.arange(5)})
    connection.send_bytes(data)
    assert connection.recv() == "mapped"
    del keep
    store.append({0: np.full(300, 7, np.uint8), 3: np.full(300, 9, np.uint8)})  # written after: seen there
    connection.send("written")
    connection.recv()


@pytest.mark.parametrize("on_disk", [False, True])
def test_the_application_reads_what_the_device_process_writes(tmp_path, on_disk):
    folder = tmp_path / "segments"  # (a folder of its own: the settings directory of the test is in tmp_path)
    folder.mkdir()
    parent, child = CONTEXT.Pipe()
    process = CONTEXT.Process(target=_child, args=(child, on_disk, str(folder)))
    process.start()
    try:
        message = shared_arrays.loads(parent.recv_bytes())
        parent.send("mapped")
        views = message["views"]
        assert list(views[0][:600]) == [1] * 600 and list(views[3][:600]) == [3] * 600
        assert shared_arrays.describe(views[0]) is not None  # a view of the segment, not a copy
        assert np.array_equal(message["big"], np.arange(300_000, dtype=np.int32))  # copied into a segment
        assert list(message["small"]) == [0, 1, 2, 3, 4]
        assert parent.recv() == "written"
        arrays = message["arrays"]
        assert list(arrays[0][600:900]) == [7] * 300  # the same memory
        if on_disk and sys.platform != "win32":
            assert os.listdir(folder) == []  # mapped by both: the file name is gone already
    finally:
        parent.send("bye")
        process.join(10)
    # the device process ended: the samples stay as long as the application holds them
    assert list(arrays[3][600:900]) == [9] * 300
    assert sample_store.is_on_disk(views[0]) == on_disk


def test_arrays_of_this_process_and_the_allocators():
    array = shared_arrays.zeros(10, np.uint16)
    array[:] = np.arange(10)
    kind, name, size, offset, shape, strides, dtype = shared_arrays.describe(array[2:6])
    assert kind == "memory" and size == 20 and offset == 4 and shape == (4,) and dtype == np.dtype(np.uint16).str
    assert shared_arrays.describe(np.zeros(4)) is None
    sample_store.share_arrays()
    try:
        assert shared_arrays.describe(sample_store.MemoryAllocator().zeros(8)) is not None
    finally:
        sample_store.share_arrays(False)
    assert shared_arrays.describe(sample_store.MemoryAllocator().zeros(8)) is None


def test_held_segments_until_confirmed():
    sent = shared_arrays.Sent()
    _data, keep = shared_arrays.dumps([shared_arrays.zeros(4)])
    number = sent.hold(keep)
    assert number is not None and len(sent) == 1 and sent.hold([]) is None
    sent.release(number)
    assert len(sent) == 0
