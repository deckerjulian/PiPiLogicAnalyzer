# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Decoding in a process of its own, so it never stalls recording and synchronisation.

The sigrok decoders are Python; a long decode in a thread of the application holds the GIL for
milliseconds at a time - long enough to make the thread that receives a stream or a remote
device wait (``tools/benchmark.py stall``: up to about 90 ms). :class:`DecodeService` runs them in
a separate process instead:

* the process starts once (``spawn``) and keeps the decoders loaded (a registry per set of
  decoder folders);
* the samples go through shared memory (one copy, no pickling), the annotations come back;
* *cancel* sets a shared value the decoders look at between steps;
* a process that died is started again; when no process can be started the decoders run in the
  calling thread, as before.

:func:`run` is what the data views and the flow nodes call (from a worker thread: it blocks).
The Python API decodes in its own process (scripts without a ``__main__`` guard must not start
processes). Qt free.
"""

from __future__ import annotations

import itertools
import logging
import multiprocessing
import threading
from multiprocessing import shared_memory
from typing import Any, Callable, Optional

import numpy as np

from ..driver.models import AnalyzerChannel, CaptureSession

log = logging.getLogger(__name__)

#: copies into shared memory in pieces of this many bytes (other threads run in between)
COPY_CHUNK = 16 << 20
#: seconds between looks at ``cancelled`` while waiting for the process
POLL = 0.05
#: annotations come back in parts of this many segments: unpacking one holds the GIL a few ms only
PART = 4000


# ------------------------------------------------------------- the process
def _serve(connection, cancel) -> None:  # pragma: no cover - runs in the decoder process
    """The decoder process: one job after the other until ``None`` comes."""
    from ..core import priority

    priority.lower_own()  # (it took over a raised priority of the application: decoding must not compete)
    from .engine import DecoderRegistry
    from .provider import SigrokProvider

    registries: dict[tuple, DecoderRegistry] = {}
    while True:
        try:
            message = connection.recv()
        except (EOFError, OSError):
            return
        if message is None:
            return
        job, paths, instances, frequency, layout, memory_name = message
        memory = None
        try:
            registry = registries.get(paths)
            if registry is None:
                registry = registries[paths] = DecoderRegistry(list(paths))
                registry.load()
            memory = shared_memory.SharedMemory(name=memory_name)
            session = CaptureSession(frequency=int(frequency))
            for number, offset, length in layout:
                channel = AnalyzerChannel(channel_number=int(number))
                channel.samples = np.ndarray((length,), dtype=np.uint8, buffer=memory.buf, offset=offset)
                session.capture_channels.append(channel)
            provider = SigrokProvider(registry)
            provider.from_list(instances)
            groups = provider.run(session, cancelled=lambda job=job: cancel.value == job)
            positions = {id(instance): index for index, instance in enumerate(provider.instances)}
            del session, channel, provider
            for group in groups:
                connection.send(("group", job, (positions[id(group.instance)], group.decoder_name, group.color_index,
                                                group.error, [row.name for row in group.annotations])))
                for row, annotation in enumerate(group.annotations):
                    segments = annotation.segments
                    for first in range(0, len(segments), PART):
                        connection.send(("segments", job, (row, [
                            (item.type_id, item.first_sample, item.last_sample, item.values, item.sample_point)
                            for item in segments[first:first + PART]])))
            connection.send(("ok", job, None))
        except BaseException as error:  # noqa: BLE001 - reported to the application
            try:
                connection.send(("error", job, f"{type(error).__name__}: {error}"))
            except (OSError, ValueError):
                return
        finally:
            if memory is not None:
                try:
                    memory.close()
                except BufferError:  # a view still lives: closed with the process
                    pass


class _Worker:
    def __init__(self, context) -> None:
        self.connection, child = context.Pipe()
        self.cancel = context.Value("q", 0, lock=False)
        self.process = context.Process(target=_serve, args=(child, self.cancel), name="openscilab-decoder",
                                       daemon=True)
        self.process.start()
        child.close()
        self.busy = False

    @property
    def alive(self) -> bool:
        return self.process.is_alive()

    def close(self) -> None:
        try:
            self.connection.send(None)
        except (OSError, ValueError):
            pass
        self.process.join(1.0)
        if self.process.is_alive():
            self.process.terminate()
        self.connection.close()


class DecodeService:
    """Decoder processes (up to ``workers`` at a time); thread safe."""

    def __init__(self, workers: int = 2) -> None:
        self.max_workers = max(int(workers), 1)
        self._context = multiprocessing.get_context("spawn")
        self._workers: list[_Worker] = []
        self._lock = threading.Condition()
        self._jobs = itertools.count(1)
        #: no process could be started: the decoders run in the calling thread
        self.unavailable = False

    def warm(self) -> None:
        """Start a process now (it loads in the background), so the first decode does not wait."""
        with self._lock:
            if not self._workers and not self.unavailable:
                self._start()

    def _start(self) -> Optional[_Worker]:
        try:
            worker = _Worker(self._context)
        except (OSError, RuntimeError, ValueError) as error:
            log.warning("no decoder process (%s): decoding in the application", error)
            self.unavailable = True
            return None
        self._workers.append(worker)
        return worker

    def _take(self) -> Optional[_Worker]:
        with self._lock:
            while True:
                self._workers = [worker for worker in self._workers if worker.alive or worker.busy]
                for worker in self._workers:
                    if not worker.busy and worker.alive:
                        worker.busy = True
                        return worker
                if self.unavailable:
                    return None
                if len(self._workers) < self.max_workers:
                    worker = self._start()
                    if worker is None:
                        return None
                    worker.busy = True
                    return worker
                self._lock.wait(POLL)

    def _give_back(self, worker: _Worker) -> None:
        with self._lock:
            worker.busy = False
            self._lock.notify_all()

    def run(self, provider, session: CaptureSession, cancelled: Optional[Callable[[], bool]] = None) -> list:
        """The annotation groups of ``provider`` over ``session`` (blocks; call it from a worker
        thread). Raises ``RuntimeError`` when the decoder process ended in the middle."""
        from .engine import Annotation, AnnotationSegment
        from .provider import AnnotationGroup

        channels = [channel for channel in session.capture_channels if channel.samples is not None]
        if not channels or not provider.instances or session.sample_count() == 0:
            return provider.run(session, cancelled)
        worker = self._take()
        if worker is None:
            return provider.run(session, cancelled)
        memory = None
        try:
            layout, offset = [], 0
            for channel in channels:
                length = int(channel.samples.shape[0])
                layout.append((int(channel.channel_number), offset, length))
                offset += length
            memory = shared_memory.SharedMemory(create=True, size=max(offset, 1))
            buffer = np.ndarray((offset,), dtype=np.uint8, buffer=memory.buf)
            for (_number, start, length), channel in zip(layout, channels):
                source = np.asarray(channel.samples, dtype=np.uint8).reshape(-1)
                for first in range(0, length, COPY_CHUNK):
                    buffer[start + first:start + min(first + COPY_CHUNK, length)] = source[first:first + COPY_CHUNK]
            del buffer
            job = next(self._jobs)
            paths = tuple(provider.registry.search_paths)
            worker.connection.send((job, paths, provider.to_list(), float(session.frequency), layout, memory.name))
            parts: list = []
            while True:
                while not worker.connection.poll(POLL):
                    if not worker.alive:
                        raise RuntimeError("the decoder process ended")
                    if cancelled is not None and cancelled():
                        worker.cancel.value = job  # the decoders end at their next step; the answer still comes
                kind, _job, result = worker.connection.recv()
                if kind in ("ok", "error"):
                    break
                parts.append((kind, result))
        except (EOFError, OSError, BrokenPipeError) as error:
            worker.process.terminate()
            raise RuntimeError(f"the decoder process ended ({error})") from None
        finally:
            if memory is not None:
                memory.close()
                memory.unlink()
            self._give_back(worker)
        if kind == "error":
            raise RuntimeError(result)
        groups: list = []
        originals = getattr(provider, "_originals", {})
        for kind, part in parts:
            if kind == "group":
                index, name, color, error, rows = part
                instance = provider.instances[index]
                groups.append(AnnotationGroup(instance=originals.get(id(instance), instance), decoder_name=name,
                                              color_index=color, annotations=[Annotation(row) for row in rows],
                                              error=error, info=provider.info_for(instance)))
            else:
                row, segments = part
                groups[-1].annotations[row].segments.extend(
                    AnnotationSegment(type_id, first, last, values, point) for type_id, first, last, values, point
                    in segments)
        return groups

    def close(self) -> None:
        with self._lock:
            workers, self._workers = self._workers, []
        for worker in workers:
            worker.close()


_service: Optional[DecodeService] = None
_service_lock = threading.Lock()


def service() -> DecodeService:
    """The decoder processes of the application (made on first use)."""
    global _service
    with _service_lock:
        if _service is None:
            _service = DecodeService()
        return _service


def run(provider, session: CaptureSession, cancelled: Optional[Callable[[], bool]] = None) -> Any:
    """Decode in the decoder process (in this thread when there is none)."""
    return service().run(provider, session, cancelled)
