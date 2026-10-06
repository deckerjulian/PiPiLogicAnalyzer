# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of openSciLab, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Named profiles: capture settings plus decoder configuration.

Port of ``Classes/ProfilesSet.cs`` introduced with LogicAnalyzer 6.5.  The profiles saved in the
application are stored in ``profiles.json`` inside the settings directory -- the file name the
original uses -- and can additionally be exported to and imported from any JSON file, so capture
setups can be shared between computers.

Every JSON file in the *profiles folder* (``profiles/`` in the settings directory) is loaded too,
without importing it. The standard profiles of openSciLab (``examples/profiles/``: the common buses
and the C64 expansion port) are copied there once; a standard profile that was deleted stays
deleted. A profile from the folder is changed and deleted in its own file.

Accepted import formats:

* ``{"Profiles": [...]}`` -- a profile set written by this application or by
  the original software;
* ``{"Name": ..., "CaptureSettings": ...}`` -- a single profile;
* a plain capture settings object (``{"Frequency": ..., "CaptureChannels": ...}``,
  as stored for the capture dialog), imported under the file name.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

from ..driver.models import CaptureSession, TriggerType
from . import settings
from .capture_io import session_from_dict, session_to_dict
from .files import atomic_open

log = logging.getLogger(__name__)

PROFILES_FILE = "profiles.json"
PROFILE_FILE_FILTER = "openSciLab profiles (*.json);;All files (*)"
#: the folder in the settings directory whose profiles are loaded without importing them
PROFILES_FOLDER = "profiles"
#: the standard profiles that were copied into the profiles folder already (file names)
INSTALLED_FILE = ".standard-profiles.json"
STANDARD_DIRECTORIES = [
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), os.pardir, "examples", "profiles"),
]


def _is_int(value: Any) -> bool:
    try:
        int(value)
    except (TypeError, ValueError):
        return False
    return True


def map_decoder_channels(configuration: Any, convert: Callable[[int], Optional[int]]) -> Any:
    """A copy of a decoder configuration with every capture channel passed through ``convert``.

    Understands the list of ``SigrokProvider.to_list`` (``channel_map``) and the
    ``SerializableDecodingTree`` of the original software (``CaptureIndex``, -1 for none).
    ``convert`` returns ``None`` for a channel that is not there: the decoder channel is then
    not assigned.
    """
    configuration = copy.deepcopy(configuration)

    def branch(item: Any) -> None:
        if not isinstance(item, dict):
            return
        for channel in item.get("Channels") or []:
            if isinstance(channel, dict) and "CaptureIndex" in channel:
                try:
                    index = int(channel["CaptureIndex"])
                except (TypeError, ValueError):
                    continue
                converted = convert(index) if index >= 0 else None
                channel["CaptureIndex"] = -1 if converted is None else converted
        for child in item.get("Children") or []:
            branch(child)

    if isinstance(configuration, dict):
        for item in configuration.get("Branches") or []:
            branch(item)
    elif isinstance(configuration, list):
        for item in configuration:
            if not isinstance(item, dict) or not isinstance(item.get("channel_map"), dict):
                continue
            mapped = {}
            for key, value in item["channel_map"].items():
                try:
                    converted = convert(int(value))
                except (TypeError, ValueError):
                    continue
                if converted is not None:
                    mapped[key] = converted
            item["channel_map"] = mapped
    return configuration


@dataclass
class Profile:
    """Capture settings and decoders under a name.

    In memory the decoders name their capture channels by channel number (what the application
    works with); in the file they are positions among the channels of the capture settings, as
    the original software stores them.
    """

    name: str
    capture_settings: Optional[CaptureSession] = None
    #: Decoder configuration: the list written by ``SigrokProvider.to_list`` or
    #: the ``SerializableDecodingTree`` object of the original software.
    decoder_configuration: Any = field(default_factory=list)
    #: Free text, e.g. how to connect the probes (not used by the original software).
    notes: str = ""
    #: The file in the profiles folder the profile comes from ("": ``profiles.json``).
    source: str = field(default="", compare=False)
    #: A standard profile of openSciLab (its file came with the application).
    standard: bool = field(default=False, compare=False)

    def to_dict(self) -> dict:
        data = {
            "Name": self.name,
            "CaptureSettings": None
            if self.capture_settings is None
            else session_to_dict(self.capture_settings.clone_settings(), include_samples=False),
            "DecoderConfiguration": self._decoders_by_position(),
        }
        if self.notes:
            data["Notes"] = self.notes
        return data

    def _decoders_by_position(self) -> Any:
        if self.capture_settings is None or not self.capture_settings.capture_channels:
            return self.decoder_configuration
        positions: dict[int, int] = {}
        for position, channel in enumerate(self.capture_settings.capture_channels):
            positions.setdefault(int(channel.channel_number), position)
        stored = map_decoder_channels(self.decoder_configuration, positions.get)
        if isinstance(stored, list) and isinstance(self.decoder_configuration, list):
            # A decoder channel assigned to a channel the profile does not capture has no
            # position: its number is kept beside the positions, so the profile is the same
            # after the next start. (The tree of the original software has no place for it.)
            for item, source in zip(stored, self.decoder_configuration):
                if not isinstance(source, dict) or not isinstance(source.get("channel_map"), dict):
                    continue
                numbers = {key: int(value) for key, value in source["channel_map"].items()
                           if _is_int(value) and int(value) not in positions}
                if numbers:
                    item["channel_numbers"] = numbers
        return stored

    @staticmethod
    def _decoders_by_number(configuration: Any, session: Optional[CaptureSession]) -> Any:
        if session is None or not session.capture_channels:
            return configuration
        numbers = [int(channel.channel_number) for channel in session.capture_channels]
        loaded = map_decoder_channels(
            configuration, lambda position: numbers[position] if 0 <= position < len(numbers) else None)
        if isinstance(loaded, list):
            for item in loaded:
                extra = item.pop("channel_numbers", None) if isinstance(item, dict) else None
                if isinstance(extra, dict) and isinstance(item.get("channel_map"), dict):
                    item["channel_map"].update({key: int(value) for key, value in extra.items() if _is_int(value)})
        return loaded

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
            decoder_configuration=Profile._decoders_by_number(decoders, session),
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
    with atomic_open(path) as handle:
        json.dump({"Profiles": [profile.to_dict() for profile in profiles]}, handle, indent=2)


def standard_directory() -> Optional[str]:
    """The standard profiles that come with openSciLab."""
    for directory in STANDARD_DIRECTORIES:
        directory = os.path.normpath(directory)
        if os.path.isdir(directory):
            return directory
    return None


def standard_files() -> list[str]:
    directory = standard_directory()
    if directory is None:
        return []
    return sorted(name for name in os.listdir(directory) if name.lower().endswith(".json"))


def profiles_directory() -> str:
    """The profiles folder (created when it is not there)."""
    path = os.path.join(settings.settings_directory(), PROFILES_FOLDER)
    os.makedirs(path, exist_ok=True)
    return path


def install_standard_profiles(folder: Optional[str] = None) -> list[str]:
    """Copy the standard profiles that are new into the profiles folder; one that was copied before
    is not copied again (deleted, it stays deleted). Returns the names of the copied files."""
    folder = folder or profiles_directory()
    source = standard_directory()
    if source is None:
        return []
    index = os.path.join(folder, INSTALLED_FILE)
    try:
        with open(index, encoding="utf-8") as handle:
            installed = set(json.load(handle))
    except (OSError, ValueError, TypeError):
        installed = set()
    copied = []
    for name in standard_files():
        if name in installed:
            continue
        target = os.path.join(folder, name)
        if not os.path.exists(target):
            try:
                shutil.copyfile(os.path.join(source, name), target)
            except OSError as error:
                log.warning("The standard profile %s cannot be copied: %s", name, error)
                continue
            copied.append(name)
        installed.add(name)
    if copied or not os.path.exists(index):
        try:
            with atomic_open(index) as handle:
                json.dump(sorted(installed), handle, indent=2)
        except OSError as error:
            log.warning("%s cannot be written: %s", index, error)
    return copied


def profile_problem(profile: Profile, driver: Any) -> str:
    """Why ``profile`` does not fit the device of ``driver`` ("" when it fits): it captures channels
    the device does not have."""
    session = profile.capture_settings
    if session is None or driver is None:
        return ""
    count = int(getattr(driver, "channel_count", 0) or 0)
    needed = max((channel.channel_number for channel in session.capture_channels), default=-1) + 1
    if needed > count:
        return f"needs {needed} channels, the device has {count}"
    analog = int(getattr(driver, "analog_channel_count", 0) or 0)
    analog_needed = max((channel.channel_number for channel in session.analog_channels), default=-1) + 1
    if analog_needed > analog:
        return f"needs {analog_needed} analog channels, the device has {analog}"
    return ""


def fit_session(session: CaptureSession, driver: Any) -> tuple[CaptureSession, list[str]]:
    """A copy of ``session`` the device of ``driver`` can capture: the rate and the length within its
    limits (a trigger on a channel it does not have: none). Returns the copy and what was changed."""
    fitted = session.clone_settings()
    changes: list[str] = []
    if driver is None:
        return fitted, changes
    count = int(getattr(driver, "channel_count", 0) or 0)
    fitted.capture_channels = [channel for channel in fitted.capture_channels if channel.channel_number < count]
    channels = fitted.channel_numbers or [0]
    mode = getattr(fitted, "acquisition_mode", None)
    try:
        highest = int(driver.max_frequency_for(channels, mode))
        lowest = int(getattr(driver, "min_frequency", 1) or 1)
    except (AttributeError, TypeError, ValueError):
        return fitted, changes
    if fitted.frequency > highest:
        changes.append(f"rate {units_text(fitted.frequency)} -> {units_text(highest)}")
        fitted.frequency = highest
    elif fitted.frequency < lowest:
        changes.append(f"rate {units_text(fitted.frequency)} -> {units_text(lowest)}")
        fitted.frequency = lowest
    try:
        limits = driver.get_limits(channels, mode)
    except (AttributeError, TypeError, ValueError):
        limits = None
    if limits is not None and limits.max_post_samples:
        total = fitted.pre_trigger_samples + fitted.post_trigger_samples
        pre = min(max(fitted.pre_trigger_samples, limits.min_pre_samples), limits.max_pre_samples)
        post = min(max(fitted.post_trigger_samples, limits.min_post_samples), limits.max_post_samples)
        if limits.max_total_samples and pre + post > limits.max_total_samples:
            share = fitted.pre_trigger_samples / total if total else 0.0  # the profile's share before the trigger
            pre = min(int(limits.max_total_samples * share), limits.max_pre_samples)
            post = max(min(limits.max_total_samples - pre, limits.max_post_samples), limits.min_post_samples)
        if pre + post != total:
            changes.append(f"length {total:,} -> {pre + post:,} samples")
        fitted.pre_trigger_samples, fitted.post_trigger_samples = pre, post
    if fitted.trigger_type != TriggerType.IMMEDIATE and fitted.trigger_channel >= count:
        fitted.trigger_type = TriggerType.IMMEDIATE
        changes.append("no trigger")
    return fitted, changes


def units_text(frequency: float) -> str:
    from .units import format_quantity

    return format_quantity(frequency, "Hz")


class ProfileStore:
    """The profiles saved in the application (``profiles.json``) and those of the profiles folder."""

    def __init__(self, file_name: str = PROFILES_FILE, folder: Optional[str] = "") -> None:
        """``folder``: the profiles folder ("": the one in the settings directory, ``None``: none)."""
        self.file_name = file_name
        self.folder = profiles_directory() if folder == "" else folder
        self.profiles: list[Profile] = []
        #: folder files whose profiles changed since they were read
        self._changed: set[str] = set()
        self.load()

    def load(self) -> None:
        self.profiles = []
        self._changed = set()
        data = settings.get_settings(self.file_name)
        if data is not None:
            data = strip_type_metadata(data)
            items = data["Profiles"] if isinstance(data, dict) and isinstance(data.get("Profiles"), list) else [data]
            skipped = 0
            for item in items:
                # one profile that cannot be read does not take the others with it
                try:
                    self.add(Profile.from_dict(item))
                except (TypeError, ValueError) as error:
                    skipped += 1
                    log.warning("A profile in %s cannot be read: %s", self.file_name, error)
            if skipped:
                settings.keep_copy(self.file_name)  # the next save writes only the readable ones
        if self.folder:
            self._load_folder()
        self._changed = set()

    def reload_folder(self) -> None:
        """Read the profiles folder again (files put there show up), unless its profiles were changed
        and not saved yet."""
        if not self.folder or self._changed:
            return
        self.profiles = [profile for profile in self.profiles if not profile.source]
        self._load_folder()

    def _load_folder(self) -> None:
        install_standard_profiles(self.folder)
        standard = set(standard_files())
        try:
            names = sorted(name for name in os.listdir(self.folder) if name.lower().endswith(".json")
                           and not name.startswith("."))
        except OSError:
            return
        for name in names:
            path = os.path.join(self.folder, name)
            try:
                found = read_profiles_file(path)
            except (OSError, ValueError, TypeError) as error:
                log.warning("The profiles in %s cannot be read: %s", path, error)
                continue
            for profile in found:
                if self.get(profile.name) is not None:
                    log.info("The profile %r of %s is there already", profile.name, path)
                    continue
                profile.source, profile.standard = path, name in standard
                self.profiles.append(profile)

    def save(self) -> bool:
        ok = settings.persist_settings(
            self.file_name, {"Profiles": [profile.to_dict() for profile in self.profiles if not profile.source]}
        )
        for path in sorted(self._changed):
            remaining = [profile for profile in self.profiles if profile.source == path]
            try:
                if remaining:
                    write_profiles_file(path, remaining)
                elif os.path.exists(path):
                    os.remove(path)
            except OSError as error:
                log.warning("%s cannot be written: %s", path, error)
                ok = False
        self._changed = set()
        return ok

    def user_profiles(self) -> list[Profile]:
        """The profiles saved, imported or put into the folder (not the standard profiles)."""
        return [profile for profile in self.profiles if not profile.standard]

    def get(self, name: str) -> Optional[Profile]:
        for profile in self.profiles:
            if profile.name == name:
                return profile
        return None

    def add(self, profile: Profile) -> None:
        """Add ``profile``, replacing a profile with the same name in place (in the file of that one
        unless ``profile`` has a file of its own)."""
        for index, existing in enumerate(self.profiles):
            if existing.name == profile.name:
                if not profile.source:
                    profile.source, profile.standard = existing.source, existing.standard
                self._touch(existing)
                self._touch(profile)
                self.profiles[index] = profile
                return
        self._touch(profile)
        self.profiles.append(profile)

    def remove(self, name: str) -> bool:
        removed = [profile for profile in self.profiles if profile.name == name]
        for profile in removed:
            self._touch(profile)
        self.profiles = [profile for profile in self.profiles if profile.name != name]
        return bool(removed)

    def _touch(self, profile: Profile) -> None:
        if profile.source:
            self._changed.add(profile.source)
