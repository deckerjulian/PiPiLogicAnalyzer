# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of PiPiLogicAnalyzer 7, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Management of the decoders applied to a capture.

Holds the list of decoder instances the user configured (including decoders
stacked on top of another decoder's Python output), runs them over a capture and
returns the annotation groups the viewer renders.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

import numpy as np

from ..driver.models import AnalyzerChannel, CaptureSession
from .engine import (
    Annotation,
    DecodeRun,
    DecoderInfo,
    DecoderRegistry,
    run_decoder,
)
from .runtime import OutputValue


@dataclass
class DecoderInstance:
    """A configured decoder: channel assignment plus option values."""

    decoder_id: str
    label: str = ""
    #: decoder channel index -> index inside ``session.capture_channels``
    channel_map: dict[int, int] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)
    color_index: int = 0
    parent: Optional["DecoderInstance"] = None
    enabled: bool = True

    def to_dict(self) -> dict:
        return {
            "decoder_id": self.decoder_id,
            "label": self.label,
            "channel_map": {str(key): value for key, value in self.channel_map.items()},
            "options": dict(self.options),
            "color_index": self.color_index,
            "enabled": self.enabled,
        }

    @staticmethod
    def from_dict(data: dict) -> "DecoderInstance":
        return DecoderInstance(
            decoder_id=str(data.get("decoder_id", "")),
            label=str(data.get("label", "")),
            channel_map={int(key): int(value) for key, value in (data.get("channel_map") or {}).items()},
            options=dict(data.get("options") or {}),
            color_index=int(data.get("color_index", 0)),
            enabled=bool(data.get("enabled", True)),
        )


@dataclass
class AnnotationGroup:
    """Annotations produced by one decoder instance."""

    instance: DecoderInstance
    decoder_name: str
    color_index: int
    annotations: list[Annotation] = field(default_factory=list)
    error: Optional[str] = None
    #: Description of the decoder (channel names for the bit composition).
    info: Optional[DecoderInfo] = None

    @property
    def row_count(self) -> int:
        return len(self.annotations)


class SigrokProvider:
    """Runs the configured decoders over a capture."""

    def __init__(self, registry: Optional[DecoderRegistry] = None) -> None:
        self.registry = registry or DecoderRegistry()
        self.instances: list[DecoderInstance] = []

    # ------------------------------------------------------------- instances
    def add_instance(self, instance: DecoderInstance) -> DecoderInstance:
        self.instances.append(instance)
        return instance

    def remove_instance(self, instance: DecoderInstance) -> None:
        # Remove the instance and everything stacked on top of it.
        removed = {id(instance)}
        remaining: list[DecoderInstance] = []
        for candidate in self.instances:
            if id(candidate) in removed or (candidate.parent is not None and id(candidate.parent) in removed):
                removed.add(id(candidate))
                continue
            remaining.append(candidate)
        self.instances = remaining

    def clear(self) -> None:
        self.instances.clear()

    def info_for(self, instance: DecoderInstance) -> Optional[DecoderInfo]:
        return self.registry.get(instance.decoder_id)

    def stackable_decoders(self, instance: DecoderInstance) -> list[DecoderInfo]:
        """Decoders that can consume the Python output of ``instance``."""
        info = self.info_for(instance)
        if info is None or not info.outputs:
            return []
        produced = set(info.outputs)
        return [
            candidate
            for candidate in self.registry.decoders
            if candidate.inputs and set(candidate.inputs) & produced
        ]

    # --------------------------------------------------------------- running
    def run(self, session: CaptureSession) -> list[AnnotationGroup]:
        """Execute every enabled decoder and return their annotation groups."""
        groups: list[AnnotationGroup] = []
        if not session or not session.capture_channels:
            return groups

        sample_count = session.sample_count()
        if sample_count == 0:
            return groups

        results: dict[int, DecodeRun] = {}

        for instance in self.instances:
            if not instance.enabled:
                continue

            info = self.info_for(instance)
            if info is None:
                groups.append(
                    AnnotationGroup(
                        instance=instance,
                        decoder_name=instance.decoder_id,
                        color_index=instance.color_index,
                        error=f"Decoder '{instance.decoder_id}' is not available.",
                    )
                )
                continue

            channel_samples = self._channel_samples(info, instance, session.capture_channels)
            python_input: Optional[list[OutputValue]] = None
            if not info.is_base_decoder:
                parent_result = results.get(id(instance.parent)) if instance.parent else None
                python_input = list(parent_result.python_output) if parent_result else []

            run = run_decoder(
                info,
                channel_samples=channel_samples,
                sample_count=sample_count,
                samplerate=session.frequency,
                options=instance.options,
                python_input=python_input,
            )
            results[id(instance)] = run

            groups.append(
                AnnotationGroup(
                    instance=instance,
                    decoder_name=instance.label or info.name,
                    color_index=instance.color_index,
                    annotations=run.annotations,
                    error=run.error,
                    info=info,
                )
            )

        return groups

    @staticmethod
    def _channel_samples(
        info: DecoderInfo, instance: DecoderInstance, channels: Sequence[AnalyzerChannel]
    ) -> dict[int, np.ndarray]:
        mapping: dict[int, np.ndarray] = {}
        for decoder_index, capture_index in instance.channel_map.items():
            if capture_index is None or not (0 <= capture_index < len(channels)):
                continue
            samples = channels[capture_index].samples
            if samples is None:
                continue
            mapping[int(decoder_index)] = samples
        return mapping

    def missing_channels(self, instance: DecoderInstance) -> list[str]:
        """Names of the required channels that are not assigned yet."""
        info = self.info_for(instance)
        if info is None:
            return []
        return [
            channel.name
            for channel in info.required_channels
            if instance.channel_map.get(channel.index) is None
        ]

    # --------------------------------------------------------- serialisation
    def to_list(self) -> list[dict]:
        indexes = {id(instance): position for position, instance in enumerate(self.instances)}
        serialised = []
        for instance in self.instances:
            data = instance.to_dict()
            data["parent"] = indexes.get(id(instance.parent)) if instance.parent else None
            serialised.append(data)
        return serialised

    def load_configuration(self, configuration: Any) -> None:
        """Restore decoders stored in a profile.

        Accepts the list written by :meth:`to_list` as well as the
        ``SerializableDecodingTree`` of the original software (``Branches`` with
        option/channel indexes), which is mapped through the decoder registry.
        Decoders that are not installed are skipped, as the original does.
        """
        if isinstance(configuration, dict):
            self.instances = []
            for branch in configuration.get("Branches") or []:
                self._add_original_branch(branch, None)
        else:
            self.from_list(configuration or [])

    def _add_original_branch(self, branch: Any, parent: Optional[DecoderInstance]) -> None:
        if not isinstance(branch, dict):
            return
        info = self.registry.get(str(branch.get("DecoderId", "")))
        if info is None:
            return

        options = info.default_options()
        option_ids = {option.index: option.id for option in info.options}
        for value in branch.get("Options") or []:
            option_id = option_ids.get(int(value.get("OptionIndex", -1)))
            if option_id is not None:
                options[option_id] = value.get("Value")

        channel_map: dict[int, int] = {}
        for channel in branch.get("Channels") or []:
            capture_index = int(channel.get("CaptureIndex", -1))
            if capture_index >= 0 and "SigrokIndex" in channel:
                channel_map[int(channel["SigrokIndex"])] = capture_index

        instance = DecoderInstance(
            decoder_id=info.id,
            label=str(branch.get("Name") or info.name),
            channel_map=channel_map,
            options=options,
            color_index=len(self.instances),
            parent=parent,
        )
        self.instances.append(instance)
        for child in branch.get("Children") or []:
            self._add_original_branch(child, instance)

    def from_list(self, data: Iterable[dict]) -> None:
        self.instances = []
        parents: list[Optional[int]] = []
        for item in data or ():
            instance = DecoderInstance.from_dict(item)
            self.instances.append(instance)
            parent = item.get("parent")
            parents.append(None if parent is None else int(parent))

        for instance, parent in zip(self.instances, parents):
            if parent is not None and 0 <= parent < len(self.instances):
                instance.parent = self.instances[parent]
