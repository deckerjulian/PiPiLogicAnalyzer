# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of PiPiLogicAnalyzer 7, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""User defined sample regions (port of ``Classes/SampleRegion.cs``)."""

from __future__ import annotations

from dataclasses import dataclass, field

from PySide6.QtGui import QColor

from .colors import DEFAULT_REGION_COLOR


@dataclass
class SampleRegion:
    first_sample: int = 0
    last_sample: int = 0
    region_name: str = ""
    region_color: QColor = field(default_factory=lambda: QColor(DEFAULT_REGION_COLOR))

    @property
    def start(self) -> int:
        return min(self.first_sample, self.last_sample)

    @property
    def end(self) -> int:
        return max(self.first_sample, self.last_sample)

    @property
    def sample_count(self) -> int:
        return self.end - self.start

    def contains(self, sample: int) -> bool:
        return self.start <= sample <= self.end

    def shifted(self, offset: int) -> "SampleRegion":
        return SampleRegion(
            first_sample=self.first_sample + offset,
            last_sample=self.last_sample + offset,
            region_name=self.region_name,
            region_color=QColor(self.region_color),
        )

    def to_dict(self) -> dict:
        color = self.region_color
        return {
            "FirstSample": self.first_sample,
            "LastSample": self.last_sample,
            "RegionName": self.region_name,
            "R": color.red(),
            "G": color.green(),
            "B": color.blue(),
            "A": color.alpha(),
        }

    @staticmethod
    def from_dict(data: dict) -> "SampleRegion":
        return SampleRegion(
            first_sample=int(data.get("FirstSample", 0)),
            last_sample=int(data.get("LastSample", 0)),
            region_name=data.get("RegionName") or "",
            region_color=QColor(
                int(data.get("R", 255)),
                int(data.get("G", 255)),
                int(data.get("B", 255)),
                int(data.get("A", 128)),
            ),
        )
