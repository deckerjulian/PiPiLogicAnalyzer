# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of PiPiLogicAnalyzer 7, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""libsigrokdecode compatible protocol decoding."""

from .engine import (
    Annotation,
    AnnotationSegment,
    DecoderChannel,
    DecoderInfo,
    DecoderOption,
    DecoderRegistry,
    OptionType,
    decoder_search_paths,
    run_decoder,
)
from .provider import AnnotationGroup, DecoderInstance, SigrokProvider
from .runtime import DecoderError, DecoderStop

__all__ = [
    "Annotation",
    "AnnotationGroup",
    "AnnotationSegment",
    "DecoderChannel",
    "DecoderError",
    "DecoderInfo",
    "DecoderInstance",
    "DecoderOption",
    "DecoderRegistry",
    "DecoderStop",
    "OptionType",
    "SigrokProvider",
    "decoder_search_paths",
    "run_decoder",
]
