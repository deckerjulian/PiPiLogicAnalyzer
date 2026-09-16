# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of PiPiLogicAnalyzer 7, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Named profiles: capture settings plus decoder configuration.

Port of ``Classes/ProfilesSet.cs`` introduced with LogicAnalyzer 6.5.  Every
profile is stored in ``profiles.json`` inside the settings directory -- the file
name the original uses -- and can additionally be exported to and imported
from any JSON file, so capture setups can be shared between computers.

Accepted import formats:

* ``{"Profiles": [...]}`` -- a profile set written by this application or by
  the original software;
* ``{"Name": ..., "CaptureSettings": ...}`` -- a single profile;
* a plain capture settings object (``{"Frequency": ..., "CaptureChannels": ...}``,
  as stored for the capture dialog), imported under the file name.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from ..driver.models import CaptureSession
from . import settings
from .capture_io import session_from_dict, session_to_dict

PROFILES_FILE = "profiles.json"
PROFILE_FILE_FILTER = "PiPiLogicAnalyzer profiles (*.json);;All files (*)"


@dataclass
class Profile:
    name: str
    capture_settings: Optional[CaptureSession] = None
    #: Decoder configuration: the list written by ``SigrokProvider.to_list`` or
    #: the ``SerializableDecodingTree`` object of the original software.
    decoder_configuration: Any = field(default_factory=list)
    #: Free text, e.g. how to connect the probes (not used by the original software).
    notes: str = ""

    def to_dict(self) -> dict:
        data = {
            "Name": self.name,
            "CaptureSettings": None
            if self.capture_settings is None
            else session_to_dict(self.capture_settings.clone_settings(), include_samples=False),
            "DecoderConfiguration": self.decoder_configuration,
        }
        if self.notes:
            data["Notes"] = self.notes
        return data

    @staticmethod
    def from_dict(data: Any, fallback_name: str = "") -> "Profile":
        if not isinstance(data, dict):
            raise ValueError("A profile must be a JSON object")

        if "CaptureChannels" in data and "Name" not in data:
            return Profile(name=fallback_name or "Imported", capture_settings=session_from_dict(data))

        name = str(data.get("Name") or fallback_name).strip()
        if not name:
            raise ValueError("The profile has no name")

        capture = data.get("CaptureSettings")
        try:
            session = session_from_dict(capture) if isinstance(capture, dict) else None
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"Invalid capture settings in profile '{name}': {error}") from error

        decoders = data.get("DecoderConfiguration")
        if not isinstance(decoders, (list, dict)):
            decoders = []
        notes = data.get("Notes")
        return Profile(
            name=name,
            capture_settings=session,
            decoder_configuration=decoders,
            notes=notes if isinstance(notes, str) else "",
        )


def strip_type_metadata(data: Any) -> Any:
    """Remove the Newtonsoft ``$type`` annotations the original software writes.

    ``TypeNameHandling.All`` turns every collection into
    ``{"$type": ..., "$values": [...]}`` and tags every object with ``$type``.
    """
    if isinstance(data, list):
        return [strip_type_metadata(item) for item in data]
    if isinstance(data, dict):
        if "$values" in data:
            return strip_type_metadata(data["$values"])
        return {key: strip_type_metadata(value) for key, value in data.items() if key != "$type"}
    return data


def profiles_from_json(data: Any, fallback_name: str = "") -> list[Profile]:
    data = strip_type_metadata(data)
    if isinstance(data, dict) and isinstance(data.get("Profiles"), list):
        return [Profile.from_dict(item, fallback_name) for item in data["Profiles"]]
    return [Profile.from_dict(data, fallback_name)]


def read_profiles_file(path: str) -> list[Profile]:
    """Read the profiles stored in ``path`` (raises ``OSError``/``ValueError``)."""
    with open(path, "r", encoding="utf-8-sig") as handle:
        data = json.load(handle)
    return profiles_from_json(data, os.path.splitext(os.path.basename(path))[0])


def write_profiles_file(path: str, profiles: Sequence[Profile]) -> None:
    """Write ``profiles`` as a profile set to ``path`` (raises ``OSError``)."""
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"Profiles": [profile.to_dict() for profile in profiles]}, handle, indent=2)


class ProfileStore:
    """The profiles persisted in the settings directory."""

    def __init__(self, file_name: str = PROFILES_FILE) -> None:
        self.file_name = file_name
        self.profiles: list[Profile] = []
        self.load()

    def load(self) -> None:
        data = settings.get_settings(self.file_name)
        self.profiles = []
        if data is None:
            return
        try:
            candidates = profiles_from_json(data)
        except (TypeError, ValueError):
            return
        for profile in candidates:
            self.add(profile)

    def save(self) -> bool:
        return settings.persist_settings(
            self.file_name, {"Profiles": [profile.to_dict() for profile in self.profiles]}
        )

    def get(self, name: str) -> Optional[Profile]:
        for profile in self.profiles:
            if profile.name == name:
                return profile
        return None

    def add(self, profile: Profile) -> None:
        """Add ``profile``, replacing a profile with the same name in place."""
        for index, existing in enumerate(self.profiles):
            if existing.name == profile.name:
                self.profiles[index] = profile
                return
        self.profiles.append(profile)

    def remove(self, name: str) -> bool:
        before = len(self.profiles)
        self.profiles = [profile for profile in self.profiles if profile.name != name]
        return len(self.profiles) != before
