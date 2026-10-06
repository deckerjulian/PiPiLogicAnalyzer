# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of openSciLab, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""User defined sample regions (port of ``Classes/SampleRegion.cs``)."""

from __future__ import annotations

from dataclasses import dataclass

# Regions are drawn on top of the waveforms; keep them translucent enough to
# read the signals through them (the original used an alpha of 128).
#: red, green, blue, alpha (0..255)
DEFAULT_REGION_COLOR = (255, 255, 255, 64)


@dataclass
class SampleRegion:
    first_sample: int = 0
    last_sample: int = 0
    region_name: str = ""
    #: red, green, blue, alpha (0..255); the user interface makes a colour of it
    region_color: tuple[int, int, int, int] = DEFAULT_REGION_COLOR

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
            region_color=tuple(self.region_color),
        )

    def to_dict(self) -> dict:
        red, green, blue, alpha = self.region_color
        return {
            "FirstSample": self.first_sample,
            "LastSample": self.last_sample,
            "RegionName": self.region_name,
            "R": int(red),
            "G": int(green),
            "B": int(blue),
            "A": int(alpha),
        }

    @staticmethod
    def from_dict(data: dict) -> "SampleRegion":
        return SampleRegion(
            first_sample=int(data.get("FirstSample", 0)),
            last_sample=int(data.get("LastSample", 0)),
            region_name=data.get("RegionName") or "",
            region_color=(
                int(data.get("R", 255)),
                int(data.get("G", 255)),
                int(data.get("B", 255)),
                int(data.get("A", 128)),
            ),
        )
