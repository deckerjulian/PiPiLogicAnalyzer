# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of openSciLab, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""A ``sigrokdecode`` compatible runtime.

The original application embedded CPython through Python.NET and reimplemented
the decoder API on the C# side, evaluating the wait conditions one sample at a
time through dictionaries of boxed values.  This port runs the decoders
natively and evaluates the conditions with ``numpy`` over blocks of samples,
which is both simpler and dramatically faster.

Installing this module under the name ``sigrokdecode`` (see
:func:`install`) makes the unmodified ``pd.py`` files of libsigrokdecode work
as they do inside PulseView.
"""

from __future__ import annotations

import sys
import types
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

import numpy as np

from ..core.analysis import ChannelTransitions

SRD_CONF_SAMPLERATE = 0

OUTPUT_ANN = 0
OUTPUT_PYTHON = 1
OUTPUT_BINARY = 2
OUTPUT_LOGIC = 3
OUTPUT_META = 4

#: Samples evaluated at once while searching for a wait condition.
CHUNK_SIZE = 1 << 16

#: Samples of the first window searched for a condition on several channels (it doubles up to
#: CHUNK_SIZE while nothing is found).
FIRST_WINDOW = 64

#: The terms of a wait condition: low, high, rising, falling, either edge, stable.
_TERMS = ("l", "h", "r", "f", "e", "s")
#: Marks a condition that is searched in windows (see ConditionMatcher.find).
_WINDOWED = object()

#: Value reported for channels the user did not assign.
UNASSIGNED_PIN = 0xFF


class DecoderStop(Exception):
    """Raised by :meth:`Decoder.wait` when the capture is exhausted.

    libsigrokdecode terminates the decoder thread instead; raising lets the
    decoder unwind naturally and the engine catches it.
    """


class DecoderError(Exception):
    """A decoder failed while running."""


@dataclass
class OutputValue:
    """One ``put()`` call performed by a decoder."""

    start_sample: int
    end_sample: int
    value: Any
    #: Last sample inside the range at which the decoder read the pins, i.e. where it took the
    #: value (``None`` when it did not wait inside the range, e.g. stacked decoders).
    sample_point: Optional[int] = None


#: Wait positions remembered to find the sample point of a ``put()``.
SAMPLE_POINT_HISTORY = 8


@dataclass
class RegisteredOutput:
    output_type: int
    proto_id: Optional[str] = None
    meta: Any = None
    output_id: int = 0
    values: list[OutputValue] = field(default_factory=list)


class ConditionMatcher:
    """Finds the next sample matching a set of libsigrokdecode conditions."""

    def __init__(self, channel_samples: Mapping[int, np.ndarray], sample_count: int) -> None:
        self.channel_samples = dict(channel_samples)
        self.sample_count = int(sample_count)
        #: channel -> (first sample, level) of its runs
        self._runs: dict[int, tuple[np.ndarray, np.ndarray]] = {}
        self._indexes: list[ChannelTransitions] = []

    def pins(self, sample: int, channel_count: int) -> tuple[int, ...]:
        if sample < 0 or sample >= self.sample_count:
            sample = max(min(sample, self.sample_count - 1), 0)
        return tuple(
            int(self.channel_samples[index][sample]) if index in self.channel_samples else UNASSIGNED_PIN
            for index in range(channel_count)
        )

    def find(
        self, current_sample: int, conditions: Sequence[Mapping[Any, Any]]
    ) -> Optional[tuple[int, tuple[bool, ...]]]:
        """Return the first sample after ``current_sample`` matching any condition."""
        start = max(current_sample + 1, 0)
        if start >= self.sample_count:
            return None

        handled, fast = self._pure_skip_match(current_sample, conditions)
        if handled:
            return fast

        # The first match of every condition. Most waits are for one edge or level of one
        # channel, or for that until a number of samples passed: those are looked up in the
        # index of the channel's edges, however far away the edge is. Conditions on several
        # channels are searched in windows that start small and grow.
        firsts = [self._first_match(condition, start, current_sample) for condition in conditions]
        indexed = [first for first in firsts if first is not None and first is not _WINDOWED]
        nearest = min(indexed) if indexed else None
        windowed = [index for index, first in enumerate(firsts) if first is _WINDOWED]
        if windowed:
            # nothing behind the nearest indexed match matters (it is included: a windowed
            # condition may match at the same sample)
            limit = self.sample_count if nearest is None else nearest + 1
            position, size = start, min(FIRST_WINDOW, CHUNK_SIZE)
            while position < limit:
                end = min(position + size, limit)
                masks = {index: self._condition_mask(conditions[index], position, end, current_sample)
                         for index in windowed}
                best_index: Optional[int] = None
                for mask in masks.values():
                    if mask is None:
                        continue
                    candidate = int(mask.argmax())
                    if mask[candidate] and (best_index is None or candidate < best_index):
                        best_index = candidate
                if best_index is not None:
                    sample = position + best_index
                    return sample, tuple(
                        (masks[index] is not None and bool(masks[index][best_index])) if index in masks
                        else first == sample
                        for index, first in enumerate(firsts)
                    )
                position = end
                size = min(size * 2, CHUNK_SIZE)

        if nearest is None:
            return None
        return nearest, tuple(first is not _WINDOWED and first == nearest for first in firsts)

    def _channel_runs(self, channel: int) -> tuple[np.ndarray, np.ndarray]:
        """First sample and level of every run of ``channel`` (built once, when first needed)."""
        runs = self._runs.get(channel)
        if runs is None:
            index = ChannelTransitions(self.channel_samples[channel][: self.sample_count], 1)
            runs = self._runs[channel] = (index.starts, index.values)
            self._indexes.append(index)  # keeps the arrays (on disk for a capture on disk) alive
        return runs

    def _term_at(self, channel: int, term: str, sample: int) -> bool:
        """Whether ``term`` holds for ``channel`` at ``sample`` (as :meth:`_condition_mask` does)."""
        samples = self.channel_samples[channel]
        current = int(samples[sample])
        previous = int(samples[sample - 1]) if sample > 0 else current
        if term == "l":
            return current == 0
        if term == "h":
            return current == 1
        if term == "r":
            return current == 1 and previous == 0
        if term == "f":
            return current == 0 and previous == 1
        if term == "e":
            return current != previous
        return current == previous  # "s"

    def _first_match(self, condition: Mapping[Any, Any], start: int, current_sample: int):
        """The first sample at or after ``start`` that satisfies ``condition``: a sample,
        ``None`` (never) or ``_WINDOWED`` (to be searched with :meth:`_condition_mask`)."""
        if len(condition) == 1:
            # by far the most common: one edge or level, or a number of samples
            (key, value), = condition.items()
            if key == "skip":
                target = current_sample + int(value)
                return target if start <= target < self.sample_count else None
            channel = int(key)
            term = str(value).lower()
            if channel not in self.channel_samples or term not in _TERMS:
                return None  # a channel that is not assigned, or an unknown term: never
            if term == "s":
                return _WINDOWED
        else:
            target: Optional[int] = None
            terms: list[tuple[int, str]] = []
            for key, value in condition.items():
                if key == "skip":
                    target = current_sample + int(value)
                    continue
                channel = int(key)
                term = str(value).lower()
                if channel not in self.channel_samples or term not in _TERMS:
                    return None
                terms.append((channel, term))
            if target is not None:
                if target < start or target >= self.sample_count:
                    return None
                return target if all(self._term_at(channel, term, target) for channel, term in terms) else None
            if not terms:
                return start
            return _WINDOWED

        starts, values = self._channel_runs(channel)
        count = len(starts)
        if term in ("l", "h"):
            wanted = 0 if term == "l" else 1
            if int(self.channel_samples[channel][start]) == wanted:
                return start
            index = int(np.searchsorted(starts, start, side="right"))
            while index < count and values[index] != wanted:
                index += 1
        else:
            # an edge is the first sample of a run (sample 0 is none: nothing before it)
            index = max(int(np.searchsorted(starts, start, side="left")), 1)
            if term == "r":
                while index < count and not (values[index] == 1 and values[index - 1] == 0):
                    index += 1
            elif term == "f":
                while index < count and not (values[index] == 0 and values[index - 1] == 1):
                    index += 1
        return int(starts[index]) if index < count else None

    def _pure_skip_match(
        self, current_sample: int, conditions: Sequence[Mapping[Any, Any]]
    ) -> tuple[bool, Optional[tuple[int, tuple[bool, ...]]]]:
        """Fast path for ``wait()``/``wait({'skip': n})``.

        Advancing a fixed number of samples is by far the most common condition
        (every decoder that walks a clock uses it), so it is resolved with plain
        arithmetic instead of building sample masks.
        """
        targets: list[Optional[int]] = []
        for condition in conditions:
            if len(condition) != 1 or "skip" not in condition:
                return False, None
            target = current_sample + int(condition["skip"])
            targets.append(target if target > current_sample else None)

        valid = [target for target in targets if target is not None and target < self.sample_count]
        if not valid:
            return True, None

        best = min(valid)
        return True, (best, tuple(target == best for target in targets))

    def _condition_mask(
        self, condition: Mapping[Any, Any], begin: int, end: int, current_sample: int
    ) -> Optional[np.ndarray]:
        """Boolean mask of the samples in ``[begin, end)`` satisfying ``condition``."""
        length = end - begin
        mask = np.ones(length, dtype=bool)

        for key, value in condition.items():
            if key == "skip":
                target = current_sample + int(value)
                if target < begin or target >= end:
                    return None
                skip_mask = np.zeros(length, dtype=bool)
                skip_mask[target - begin] = True
                mask &= skip_mask
                continue

            channel = int(key)
            samples = self.channel_samples.get(channel)
            if samples is None:
                # Condition on a channel the user did not assign: never matches.
                return None

            current = samples[begin:end]
            if begin == 0:
                previous = np.concatenate((current[:1], current[:-1]))
            else:
                previous = samples[begin - 1 : end - 1]

            term = str(value).lower()
            if term == "l":
                mask &= current == 0
            elif term == "h":
                mask &= current == 1
            elif term == "r":
                mask &= (current == 1) & (previous == 0)
            elif term == "f":
                mask &= (current == 0) & (previous == 1)
            elif term == "e":
                mask &= current != previous
            elif term == "s":
                mask &= current == previous
            else:
                return None

            if not mask.any():
                return mask

        return mask


class DecodeContext:
    """Per-run state shared between the engine and a decoder instance."""

    def __init__(
        self,
        matcher: ConditionMatcher,
        channel_count: int,
        sample_count: int,
        assigned_channels: Iterable[int] = (),
    ) -> None:
        self.matcher = matcher
        self.channel_count = channel_count
        self.sample_count = sample_count
        self.assigned_channels = set(assigned_channels)
        #: asked at every wait(): ``True`` ends the decoder (the result is no longer wanted)
        self.cancelled: Optional[Callable[[], bool]] = None
        self.current_sample = -1
        self.outputs: list[RegisteredOutput] = []
        self.recent_samples: deque[int] = deque(maxlen=SAMPLE_POINT_HISTORY)

    # ------------------------------------------------------------- decoder API
    def register(self, output_type: int, proto_id: Optional[str], meta: Any) -> int:
        output = RegisteredOutput(
            output_type=output_type, proto_id=proto_id, meta=meta, output_id=len(self.outputs)
        )
        self.outputs.append(output)
        return output.output_id

    def put(
        self, start_sample: int, end_sample: int, output_id: int, data: Any, sample_point: Optional[int] = None
    ) -> None:
        if not 0 <= output_id < len(self.outputs):
            raise DecoderError(f"Unknown output id {output_id}")
        start, end = int(start_sample), int(end_sample)
        if sample_point is None:
            sample_point = self._sample_point(start, end)
        self.outputs[output_id].values.append(
            OutputValue(start_sample=start, end_sample=end, value=data, sample_point=sample_point)
        )

    def pins_at(self, sample: int) -> tuple[int, ...]:
        """Pins at an earlier sample.

        openSciLab extension: libsigrokdecode only moves forward. Decoders use it through
        ``getattr(self, 'pins_at', None)`` so they keep working in PulseView.
        """
        sample = int(sample)
        if sample > self.current_sample:
            raise DecoderError("pins_at() can only look at samples that were already reached")
        sample = max(sample, 0)
        self.recent_samples.append(sample)
        return self.matcher.pins(sample, self.channel_count)

    def _sample_point(self, start: int, end: int) -> Optional[int]:
        """The last position the decoder waited for inside ``[start, end)``.

        A bus decoder waits for the clock edge that latches the value and then for the end of
        the cycle before it calls ``put()``, so this is the sample it read the value from.
        """
        for sample in reversed(self.recent_samples):
            if start <= sample < end or sample == start == end:
                return sample
        return None

    def has_channel(self, index: int) -> bool:
        return index in self.assigned_channels

    def wait(self, conditions: Any) -> tuple[tuple[int, ...], tuple[bool, ...], int]:
        if self.cancelled is not None and self.cancelled():
            raise DecoderStop("Cancelled")
        normalised = self._normalise(conditions)

        # {'skip': 0} means "give me the current sample again".
        if len(normalised) == 1 and normalised[0] == {"skip": 0}:
            sample = max(self.current_sample, 0)
            self.current_sample = sample
            self.recent_samples.append(sample)
            return self.matcher.pins(sample, self.channel_count), (True,), sample

        result = self.matcher.find(self.current_sample, normalised)
        if result is None:
            self.current_sample = self.sample_count
            raise DecoderStop("End of capture reached")

        sample, matched = result
        self.current_sample = sample
        self.recent_samples.append(sample)
        return self.matcher.pins(sample, self.channel_count), matched, sample

    @staticmethod
    def _normalise(conditions: Any) -> list[dict]:
        if conditions is None:
            return [{"skip": 1}]
        if isinstance(conditions, Mapping):
            return [dict(conditions)]
        if isinstance(conditions, (list, tuple)):
            if not conditions:
                return [{"skip": 1}]
            return [dict(condition) for condition in conditions]
        raise DecoderError(f"Unsupported wait() conditions: {conditions!r}")


class Decoder:
    """Base class every protocol decoder derives from."""

    id = ""
    name = ""
    longname = ""
    desc = ""
    license = "gplv2+"
    inputs: Sequence[str] = ["logic"]
    outputs: Sequence[str] = []
    tags: Sequence[str] = []
    channels: Sequence[Mapping[str, str]] = ()
    optional_channels: Sequence[Mapping[str, str]] = ()
    options: Sequence[Mapping[str, Any]] = ()
    annotations: Sequence[Sequence[str]] = ()
    annotation_rows: Sequence[Sequence[Any]] = ()
    binary: Sequence[Sequence[str]] = ()

    def __init__(self) -> None:
        self.srd_context: Optional[DecodeContext] = None
        self.samplenum = 0
        self.matched: tuple[bool, ...] = ()

    # ------------------------------------------------------ lifecycle helpers
    def start(self) -> None:  # pragma: no cover - decoders override this
        pass

    def reset(self) -> None:  # pragma: no cover - decoders override this
        pass

    def metadata(self, key: int, value: Any) -> None:  # pragma: no cover - overridden
        pass

    def decode(self, *args, **kwargs) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    # ------------------------------------------------------------ decoder API
    @property
    def _context(self) -> DecodeContext:
        if self.srd_context is None:
            raise DecoderError("The decoder is not attached to a capture")
        return self.srd_context

    def register(self, output_type: int, proto_id: Optional[str] = None, meta: Any = None) -> int:
        return self._context.register(output_type, proto_id, meta)

    def put(self, startsample: int, endsample: int, output_id: int, data: Any) -> None:
        # ``put_sample_point`` (openSciLab extension) names the sample a value was read from
        # when the decoder emits it later, e.g. a disassembled instruction.
        self._context.put(startsample, endsample, output_id, data, getattr(self, "put_sample_point", None))

    def pins_at(self, sample: int) -> tuple[int, ...]:
        return self._context.pins_at(sample)

    def wait(self, conds: Any = None):
        pins, matched, sample = self._context.wait(conds)
        self.matched = matched
        self.samplenum = sample
        return pins

    def has_channel(self, channel: int) -> bool:
        return self._context.has_channel(channel)


def install() -> types.ModuleType:
    """Expose this runtime under the name ``sigrokdecode``.

    Decoders do ``import sigrokdecode as srd``; libsigrokdecode provides that
    module natively, here the current module is aliased.
    """
    module = sys.modules[__name__]
    sys.modules.setdefault("sigrokdecode", module)
    return sys.modules["sigrokdecode"]
