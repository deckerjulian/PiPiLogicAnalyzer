# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of PiPiLogicAnalyzer, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Discovery and execution of libsigrokdecode protocol decoders.

Decoders are plain Python packages (``<id>/pd.py``).  They are imported with the
decoder directory on ``sys.path`` -- exactly like libsigrokdecode does -- so the
shared ``common`` helper package resolves as usual.
"""

from __future__ import annotations

import importlib
import os
import sys
import traceback
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping, Optional, Sequence

import numpy as np

from ..core.settings import settings_directory
from . import runtime
from .runtime import ConditionMatcher, DecodeContext, DecoderStop, OutputValue

#: Directories searched for decoders when nothing else is configured.
DEFAULT_SEARCH_PATHS = (
    "/usr/share/libsigrokdecode/decoders",
    "/usr/local/share/libsigrokdecode/decoders",
    "/usr/share/libsigrokdecode4DSL/decoders",
    "/opt/homebrew/share/libsigrokdecode/decoders",
    "/usr/local/opt/libsigrokdecode/share/libsigrokdecode/decoders",
    r"C:\Program Files\sigrok\PulseView\share\libsigrokdecode\decoders",
    r"C:\Program Files (x86)\sigrok\PulseView\share\libsigrokdecode\decoders",
)


class OptionType(Enum):
    BOOLEAN = "bool"
    INTEGER = "int"
    DOUBLE = "float"
    STRING = "str"
    LIST = "list"


@dataclass
class DecoderChannel:
    id: str
    name: str
    desc: str
    index: int
    required: bool


@dataclass
class DecoderOption:
    id: str
    caption: str
    index: int
    option_type: OptionType = OptionType.STRING
    default: Any = None
    values: tuple = ()


@dataclass
class AnnotationSegment:
    type_id: int
    first_sample: int
    last_sample: int
    values: list[str]
    #: Sample the decoder read the value from (see ``runtime.OutputValue.sample_point``).
    sample_point: Optional[int] = None

    @property
    def sample_count(self) -> int:
        return max(self.last_sample - self.first_sample, 0)


@dataclass
class Annotation:
    """One row of annotations produced by a decoder."""

    name: str
    segments: list[AnnotationSegment] = field(default_factory=list)


@dataclass
class DecoderInfo:
    """Static description of a decoder, read from its class attributes."""

    id: str
    name: str
    longname: str
    desc: str
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    tags: tuple[str, ...]
    channels: list[DecoderChannel]
    options: list[DecoderOption]
    annotations: tuple
    annotation_rows: tuple
    decoder_class: type
    path: str = ""

    @property
    def is_base_decoder(self) -> bool:
        """True when the decoder consumes logic samples (not another decoder)."""
        return not self.inputs or self.inputs[0] == "logic"

    @property
    def required_channels(self) -> list[DecoderChannel]:
        return [channel for channel in self.channels if channel.required]

    def default_options(self) -> dict[str, Any]:
        return {option.id: option.default for option in self.options}


@dataclass
class DecodeRun:
    """Result of running one decoder over a capture."""

    annotations: list[Annotation] = field(default_factory=list)
    python_output: list[OutputValue] = field(default_factory=list)
    binary_output: list[OutputValue] = field(default_factory=list)
    error: Optional[str] = None


#: ``pipilogicanalyzer/`` (the package) and the project directory holding it.
PACKAGE_DIRECTORY = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT_DIRECTORY = os.path.dirname(PACKAGE_DIRECTORY)


def decoder_search_paths(extra: Sequence[str] = ()) -> list[str]:
    """Every directory that may contain decoders, most specific first.

    The ``decoders`` folder next to the application (``./decoders`` in the
    project, or in the working directory) is preferred over the settings
    directory and the system wide libsigrokdecode installation, so the decoder
    set shipped with the original software is the one used by default.
    """
    paths: list[str] = []

    def add(path: Optional[str]) -> None:
        if not path:
            return
        path = os.path.abspath(os.path.expanduser(path))
        if os.path.isdir(path) and path not in paths:
            paths.append(path)

    for path in extra:
        add(path)

    env = (os.environ.get("PIPILOGICANALYZER_DECODERS")
           or os.environ.get("LOGICANALYZER_DECODERS"))
    if env:
        for path in env.split(os.pathsep):
            add(path)

    add(os.path.join(os.getcwd(), "decoders"))
    add(os.path.join(PROJECT_DIRECTORY, "decoders"))
    add(os.path.join(PACKAGE_DIRECTORY, "decoders"))
    add(os.path.join(settings_directory(), "decoders"))

    for path in DEFAULT_SEARCH_PATHS:
        add(path)

    return paths


class DecoderRegistry:
    """Loads every decoder found in the search paths."""

    def __init__(self, search_paths: Sequence[str] = ()) -> None:
        runtime.install()
        self.search_paths = decoder_search_paths(search_paths)
        self._decoders: dict[str, DecoderInfo] = {}
        self.load_errors: dict[str, str] = {}
        self._loaded = False

    # ------------------------------------------------------------------ load
    def load(self, force: bool = False) -> None:
        if self._loaded and not force:
            return
        self._decoders.clear()
        self.load_errors.clear()

        for root in self.search_paths:
            if root not in sys.path:
                sys.path.insert(0, root)

            for entry in sorted(os.listdir(root)):
                directory = os.path.join(root, entry)
                if not os.path.isfile(os.path.join(directory, "pd.py")):
                    continue
                if entry in self._decoders:
                    continue  # earlier search paths win
                try:
                    info = self._load_decoder(entry, directory)
                except Exception as error:  # noqa: BLE001 - reported in the UI
                    self.load_errors[entry] = f"{error}\n{traceback.format_exc(limit=3)}"
                    continue
                if info is not None:
                    self._decoders[info.id] = info

        self._loaded = True

    def _load_decoder(self, package_name: str, directory: str) -> Optional[DecoderInfo]:
        module = importlib.import_module(package_name)
        decoder_class = getattr(module, "Decoder", None)
        if decoder_class is None:
            pd_module = importlib.import_module(f"{package_name}.pd")
            decoder_class = getattr(pd_module, "Decoder", None)
        if decoder_class is None:
            return None
        return describe_decoder(decoder_class, path=directory, fallback_id=package_name)

    # --------------------------------------------------------------- queries
    @property
    def decoders(self) -> list[DecoderInfo]:
        self.load()
        return sorted(self._decoders.values(), key=lambda info: info.longname.lower())

    def get(self, decoder_id: str) -> Optional[DecoderInfo]:
        self.load()
        return self._decoders.get(decoder_id)

    def provider_chain(self, info: DecoderInfo) -> list[DecoderInfo]:
        """Shortest chain of decoders, starting at a logic decoder, that feeds ``info``.

        ``[i2c]`` for ``eeprom24xx``, ``[usb_signalling, usb_packet]`` for
        ``usb_request``; empty when no installed decoder produces the input.
        Among equally long chains the decoder named like the input is preferred.
        """
        queue: deque[tuple[DecoderInfo, list[DecoderInfo]]] = deque([(info, [])])
        seen = {info.id}
        while queue:
            current, path = queue.popleft()
            wanted = set(current.inputs)
            producers = [
                candidate
                for candidate in self.decoders
                if candidate.id not in seen and wanted & set(candidate.outputs)
            ]
            producers.sort(key=lambda item: (item.id not in wanted, not item.is_base_decoder, item.id))
            for producer in producers:
                chain = [producer] + path
                if producer.is_base_decoder:
                    return chain
                seen.add(producer.id)
                queue.append((producer, chain))
        return []


def describe_decoder(decoder_class: type, path: str = "", fallback_id: str = "") -> DecoderInfo:
    """Read the static description of a decoder class."""
    channels: list[DecoderChannel] = []
    for required, collection in (
        (True, getattr(decoder_class, "channels", ()) or ()),
        (False, getattr(decoder_class, "optional_channels", ()) or ()),
    ):
        for channel in collection:
            channels.append(
                DecoderChannel(
                    id=str(channel.get("id", "")),
                    name=str(channel.get("name", "")),
                    desc=str(channel.get("desc", "")),
                    index=len(channels),
                    required=required,
                )
            )

    options: list[DecoderOption] = []
    for option in getattr(decoder_class, "options", ()) or ():
        options.append(_describe_option(option, len(options)))

    inputs = tuple(getattr(decoder_class, "inputs", ()) or ())
    outputs = tuple(getattr(decoder_class, "outputs", ()) or ())

    return DecoderInfo(
        id=str(getattr(decoder_class, "id", "") or fallback_id),
        name=str(getattr(decoder_class, "name", "") or fallback_id),
        longname=str(getattr(decoder_class, "longname", "") or getattr(decoder_class, "name", "")),
        desc=str(getattr(decoder_class, "desc", "")),
        inputs=inputs,
        outputs=outputs,
        tags=tuple(str(tag) for tag in (getattr(decoder_class, "tags", ()) or ())),
        channels=channels,
        options=options,
        annotations=tuple(getattr(decoder_class, "annotations", ()) or ()),
        annotation_rows=tuple(getattr(decoder_class, "annotation_rows", ()) or ()),
        decoder_class=decoder_class,
        path=path,
    )


def _describe_option(option: Mapping[str, Any], index: int) -> DecoderOption:
    default = option.get("default")
    values = tuple(option.get("values", ()) or ())

    if values:
        option_type = OptionType.LIST
    elif isinstance(default, bool):
        option_type = OptionType.BOOLEAN
    elif isinstance(default, int):
        option_type = OptionType.INTEGER
    elif isinstance(default, float):
        option_type = OptionType.DOUBLE
    else:
        option_type = OptionType.STRING

    return DecoderOption(
        id=str(option.get("id", f"option{index}")),
        caption=str(option.get("desc", option.get("id", ""))),
        index=index,
        option_type=option_type,
        default=default,
        values=values,
    )


def coerce_option(option: DecoderOption, value: Any) -> Any:
    """Convert a UI value to the type the decoder expects."""
    if value is None:
        return option.default

    if option.option_type == OptionType.LIST:
        reference = option.default
        if isinstance(reference, bool):
            return str(value).lower() in ("true", "1", "yes")
        if isinstance(reference, int):
            return int(value)
        if isinstance(reference, float):
            return float(value)
        return str(value)
    if option.option_type == OptionType.BOOLEAN:
        return bool(value)
    if option.option_type == OptionType.INTEGER:
        return int(value)
    if option.option_type == OptionType.DOUBLE:
        return float(value)
    return str(value)


def run_decoder(
    info: DecoderInfo,
    channel_samples: Mapping[int, np.ndarray],
    sample_count: int,
    samplerate: int,
    options: Optional[Mapping[str, Any]] = None,
    python_input: Optional[Iterable[OutputValue]] = None,
) -> DecodeRun:
    """Execute ``info``'s decoder over the given samples.

    ``channel_samples`` maps *decoder* channel indexes to sample arrays.
    """
    result = DecodeRun()

    instance = info.decoder_class()
    matcher = ConditionMatcher(channel_samples, sample_count)
    context = DecodeContext(
        matcher=matcher,
        channel_count=len(info.channels),
        sample_count=sample_count,
        assigned_channels=channel_samples.keys(),
    )
    instance.srd_context = context

    merged_options = info.default_options()
    option_by_id = {option.id: option for option in info.options}
    for key, value in (options or {}).items():
        if key in option_by_id:
            merged_options[key] = coerce_option(option_by_id[key], value)
    instance.options = merged_options

    try:
        if hasattr(instance, "reset"):
            instance.reset()
        instance.start()
        if hasattr(instance, "metadata"):
            instance.metadata(runtime.SRD_CONF_SAMPLERATE, int(samplerate))

        if info.is_base_decoder:
            try:
                instance.decode()
            except DecoderStop:
                pass
        else:
            for value in python_input or ():
                try:
                    instance.decode(value.start_sample, value.end_sample, value.value)
                except DecoderStop:
                    break
    except DecoderStop:
        pass
    except Exception as error:  # noqa: BLE001 - surfaced in the UI
        result.error = f"{type(error).__name__}: {error}"

    for output in context.outputs:
        if output.output_type == runtime.OUTPUT_PYTHON:
            result.python_output.extend(output.values)
        elif output.output_type == runtime.OUTPUT_BINARY:
            result.binary_output.extend(output.values)

    result.annotations = build_annotations(info, context)
    return result


def build_annotations(info: DecoderInfo, context: DecodeContext) -> list[Annotation]:
    """Group the ``put()`` calls of a run into annotation rows."""
    segments_by_class: dict[int, list[AnnotationSegment]] = {}

    for output in context.outputs:
        if output.output_type != runtime.OUTPUT_ANN:
            continue
        for value in output.values:
            data = value.value
            if not isinstance(data, (list, tuple)) or len(data) < 2:
                continue
            try:
                class_id = int(data[0])
            except (TypeError, ValueError):
                continue
            texts = [str(item) for item in data[1]] if isinstance(data[1], (list, tuple)) else [str(data[1])]
            segments_by_class.setdefault(class_id, []).append(
                AnnotationSegment(
                    type_id=class_id,
                    first_sample=value.start_sample,
                    last_sample=value.end_sample,
                    values=texts,
                    sample_point=value.sample_point,
                )
            )

    if not segments_by_class:
        return []

    rows: list[tuple[str, set[int]]] = []
    for row in info.annotation_rows:
        if len(row) >= 3:
            rows.append((str(row[1]), {int(index) for index in row[2]}))

    used_classes = {class_id for _, classes in rows for class_id in classes}
    orphans = sorted(set(segments_by_class) - used_classes)
    if orphans:
        rows.append((info.name or info.id, set(orphans)))

    annotations: list[Annotation] = []
    for name, classes in rows:
        segments: list[AnnotationSegment] = []
        for class_id in sorted(classes):
            segments.extend(segments_by_class.get(class_id, ()))
        if not segments:
            continue
        segments.sort(key=lambda segment: segment.first_sample)
        annotations.append(Annotation(name=name, segments=segments))

    return annotations
