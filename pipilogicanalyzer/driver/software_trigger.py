# Copyright (C) 2026 Julian Decker
#
# Part of PiPiLogicAnalyzer.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Triggers evaluated by the application on a stream (``CaptureSession.software_trigger``).

:class:`SoftwareTriggerDriver` wraps the driver of any device that streams. A capture with the
software trigger streams without a trigger (endlessly into a ring buffer where the device can),
looks for the trigger in every progress event with a
:class:`~pipilogicanalyzer.core.trigger_engine.SequenceMatcher`, stops the stream once the samples
after the trigger arrived and completes with ``pre_trigger_samples`` before and
``post_trigger_samples`` after the trigger. Every other capture passes straight through.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Mapping, Optional

import numpy as np

from ..core.sample_store import DiskAllocator, MemoryAllocator, copy_array, is_on_disk
from ..core.trigger_engine import SequenceMatcher, session_trigger_sequence, validate_sequence
from .base import (
    ACQUISITION_STREAM,
    CAPABILITY_CONTINUOUS_STREAM,
    AnalyzerDriverBase,
    CaptureCompletedArgs,
    CaptureCompletedHandler,
    CaptureError,
    CaptureLimits,
    CaptureProgressArgs,
)
from .models import CaptureSession, TriggerType

log = logging.getLogger("pipilogicanalyzer.driver")

#: Seconds of samples an endless stream keeps beyond the pre- and post-trigger samples: progress
#: events come about every 0.1 s, and stopping the device takes a moment while samples go on
#: arriving (bounded by the stream limits of the device)
STOP_MARGIN_SECONDS = 0.5
NO_TRIGGER_ERROR = "The trigger did not occur before the stream ended."
STOPPED_ERROR = "The trigger did not occur before the capture was stopped."
OVERWRITTEN_ERROR = (
    "The trigger occurred, but its samples were overwritten before the stream stopped. "
    "Capture fewer channels or a lower rate."
)


def supports_software_trigger(driver: AnalyzerDriverBase) -> bool:
    """The device can stream, so the application can evaluate triggers on its samples."""
    try:
        return ACQUISITION_STREAM in driver.acquisition_modes()
    except Exception:  # noqa: BLE001 - a device that cannot answer cannot stream
        return False


def software_trigger_limits(limits: CaptureLimits) -> CaptureLimits:
    """Limits of a capture whose trigger the application finds in a stream (``limits``: those of
    the stream). The samples before the trigger are kept from the stream as well, up to half of it."""
    total = limits.max_total_samples
    return CaptureLimits(
        min_pre_samples=0,
        max_pre_samples=max(total // 2, 0),
        min_post_samples=max(limits.min_post_samples, 1),
        max_post_samples=max(total, 1),
    )


class _Run:
    """A capture with the software trigger in progress."""

    def __init__(
        self,
        session: CaptureSession,
        stream: CaptureSession,
        matcher: SequenceMatcher,
        handler: Optional[CaptureCompletedHandler],
    ) -> None:
        self.session = session
        self.stream = stream
        self.matcher = matcher
        self.handler = handler
        self.pre = max(int(session.pre_trigger_samples), 0)
        self.post = max(int(session.post_trigger_samples), 1)
        #: Stream position after the last sample received
        self.received = 0
        self.stop_requested = False
        self.user_stopped = False
        self.lock = threading.Lock()

    @property
    def trigger(self) -> Optional[int]:
        return self.matcher.trigger

    def feed(self, samples: Mapping[int, np.ndarray], first: int) -> None:
        count = min((len(values) for values in samples.values()), default=0)
        with self.lock:
            self.received = max(self.received, first + count)
            if self.matcher.trigger is None and count:
                self.matcher.feed(samples, first)

    @property
    def complete(self) -> bool:
        """The samples after the trigger have arrived."""
        trigger = self.matcher.trigger
        return trigger is not None and self.received >= trigger + self.post


class SoftwareTriggerDriver(AnalyzerDriverBase):
    """A driver evaluating the trigger of ``software_trigger`` captures on a stream of ``inner``.

    Everything else is delegated to ``inner``: the properties and methods of the driver base
    explicitly (the class defines them, so ``__getattr__`` would not see them), any other
    attribute through ``__getattr__``. :attr:`last_error` explains a rejected capture.
    """

    def __init__(self, inner: AnalyzerDriverBase) -> None:
        self._inner = inner
        super().__init__()
        self._run: Optional[_Run] = None
        #: Why the last software triggered capture was rejected
        self.last_error: Optional[str] = None
        inner.add_capture_progress_handler(self._on_inner_progress)

    @property
    def inner(self) -> AnalyzerDriverBase:
        return self._inner

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__") or name == "_inner":
            raise AttributeError(name)
        return getattr(self._inner, name)

    # --------------------------------------------------------------- capture
    def start_capture(
        self,
        session: CaptureSession,
        completed_handler: Optional[CaptureCompletedHandler] = None,
        **kwargs: Any,
    ) -> CaptureError:
        sequence = session_trigger_sequence(session) if session.software_trigger else None
        if sequence is None:
            self._run = None

            def passed(args: CaptureCompletedArgs) -> None:
                self._raise_capture_completed(args, completed_handler)

            return self._inner.start_capture(session, passed, **kwargs)

        self.last_error = None
        if self._inner.is_capturing:
            return CaptureError.BUSY
        if not supports_software_trigger(self._inner):
            self.last_error = "The device cannot stream, so the application cannot evaluate the trigger."
            return CaptureError.UNSUPPORTED
        error = validate_sequence(sequence, session.channel_numbers, session.frequency)
        if error is None and (session.pre_trigger_samples < 0 or session.post_trigger_samples < 1):
            error = "The capture needs samples after the trigger."
        stream = None if error else self.stream_session(session)
        if error is None and stream is None:
            error = "The device cannot stream that many samples around the trigger."
        if error is not None or stream is None:
            self.last_error = error
            log.debug("Software trigger rejected: %s", error)
            return CaptureError.BAD_PARAMS

        run = _Run(session, stream, SequenceMatcher(sequence, session.frequency), completed_handler)
        self._run = run  # before starting: progress may arrive at once

        def completed(args: CaptureCompletedArgs) -> None:
            self._on_run_completed(run, args)

        result = self._inner.start_capture(stream, completed, **kwargs)
        if result != CaptureError.NONE and self._run is run:
            self._run = None
        return result

    def stream_session(self, session: CaptureSession) -> Optional[CaptureSession]:
        """The stream without trigger the inner driver captures for ``session``; ``None`` when
        the device cannot stream enough samples."""
        stream = session.clone_settings()
        continuous = CAPABILITY_CONTINUOUS_STREAM in self._inner.capabilities()
        stream.acquisition_mode = ACQUISITION_STREAM
        stream.trigger_type = TriggerType.IMMEDIATE
        stream.trigger_sequence = None
        stream.software_trigger = False
        stream.loop_count = 0
        stream.measure_bursts = False
        stream.continuous = continuous
        limits = self._inner.get_limits(
            session.channel_numbers, ACQUISITION_STREAM, to_disk=session.to_disk, continuous=continuous
        )
        budget = limits.max_post_samples
        wanted = max(session.pre_trigger_samples, 0) + session.post_trigger_samples
        if wanted > budget:
            return None
        if continuous:
            samples = min(budget, wanted + int(session.frequency * STOP_MARGIN_SECONDS))
        else:
            samples = budget
        stream.pre_trigger_samples = 0
        stream.post_trigger_samples = samples
        return stream

    def stop_capture(self) -> bool:
        run = self._run
        if run is not None:
            run.user_stopped = True
        return self._inner.stop_capture()

    def _stop_inner(self) -> None:
        try:
            self._inner.stop_capture()
        except Exception as error:  # noqa: BLE001 - the device may be gone
            log.debug("Stopping the stream failed: %s", error)

    def _on_inner_progress(self, args: CaptureProgressArgs) -> None:
        run = self._run
        if run is None:
            self._raise_capture_progress(args)
            return
        if args.session is not run.stream:
            return
        run.feed(args.samples, args.first_sample)
        if run.complete and not run.stop_requested:
            run.stop_requested = True
            log.debug("Software trigger at %d, stopping the stream", run.trigger)
            # Not from this thread: drivers stop by joining the thread that raises progress.
            threading.Thread(
                target=self._stop_inner, name="pipilogicanalyzer-software-trigger-stop", daemon=True
            ).start()
        self._raise_capture_progress(CaptureProgressArgs(run.session, args.samples, args.first_sample))

    def _on_run_completed(self, run: _Run, args: CaptureCompletedArgs) -> None:
        if self._run is run:
            self._run = None
        try:
            result = self._finish(run, args)
        except Exception as error:  # noqa: BLE001 - reported to the UI
            log.debug("Error completing the software triggered capture: %s", error)
            result = CaptureCompletedArgs(success=False, session=run.session, error=str(error))
        self._raise_capture_completed(result, run.handler)

    def _finish(self, run: _Run, args: CaptureCompletedArgs) -> CaptureCompletedArgs:
        session = run.session
        samples = {
            channel.channel_number: channel.samples
            for channel in args.session.capture_channels
            if channel.samples is not None
        }
        count = min((len(values) for values in samples.values()), default=0)
        first = int(args.first_sample)
        if count:
            run.feed(samples, first)  # what arrived after the last progress event
        trigger = run.trigger
        if trigger is None:
            if not args.success and args.error:
                error = args.error
            else:
                error = STOPPED_ERROR if run.user_stopped else NO_TRIGGER_ERROR
                if args.error:
                    error += " " + args.error
            return CaptureCompletedArgs(success=False, session=session, error=error)
        if not count or trigger < first or trigger >= first + count:
            return CaptureCompletedArgs(
                success=False, session=session, error=args.error or OVERWRITTEN_ERROR
            )

        low = max(trigger - run.pre, first)
        high = min(trigger + run.post, first + count)
        for channel in session.capture_channels:
            values = samples.get(channel.channel_number)
            if values is None:
                channel.samples = np.zeros(high - low, dtype=np.uint8)
                continue
            part = values[low - first : high - first]
            if len(part) < len(values):  # release the rest of the stream
                part = copy_array(part, DiskAllocator() if is_on_disk(values) else MemoryAllocator())
            channel.samples = part
        session.pre_trigger_samples = trigger - low
        session.post_trigger_samples = high - trigger
        session.loop_count = 0
        session.bursts = None
        log.debug("Software triggered capture: trigger at %d, samples %d to %d", trigger, low, high)
        return CaptureCompletedArgs(success=True, session=session, error=args.error, first_sample=low)

    # --------------------------------------------------------------- cleanup
    def dispose(self) -> None:
        self._inner.remove_capture_progress_handler(self._on_inner_progress)
        self._inner.dispose()
        super().dispose()


#: Members of the driver base the wrapper implements itself; every other public one is delegated.
_OWN_MEMBERS = frozenset({
    "add_capture_completed_handler",
    "remove_capture_completed_handler",
    "add_capture_progress_handler",
    "remove_capture_progress_handler",
    "start_capture",
    "stop_capture",
    "dispose",
})


def _delegate(name: str, member: Any) -> Any:
    if isinstance(member, property):
        return property(lambda self: getattr(self._inner, name), doc=member.__doc__)

    def method(self: SoftwareTriggerDriver, *args: Any, **kwargs: Any) -> Any:
        return getattr(self._inner, name)(*args, **kwargs)

    method.__name__ = name
    method.__doc__ = getattr(member, "__doc__", None)
    return method


for _name, _member in list(vars(AnalyzerDriverBase).items()):
    if _name.startswith("_") or _name in _OWN_MEMBERS or _name in vars(SoftwareTriggerDriver):
        continue
    if isinstance(_member, property) or callable(_member):
        setattr(SoftwareTriggerDriver, _name, _delegate(_name, _member))
del _name, _member
