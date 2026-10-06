"""Standard profiles and the profiles folder: the standard profiles are copied there once and
loaded without importing, so is every file put there; a profile from the folder changes in its own
file; profiles that need more channels than a device has are off, the others are fitted to it."""

from __future__ import annotations

import importlib.util
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from openscilab.core.profiles import (
    INSTALLED_FILE,
    Profile,
    ProfileStore,
    fit_session,
    install_standard_profiles,
    profile_problem,
    profiles_directory,
    read_profiles_file,
    standard_directory,
    standard_files,
    write_profiles_file,
)
from openscilab.driver.models import AnalyzerChannel, CaptureSession, TriggerType
from openscilab.driver.simulated import open_simulated
from openscilab.sigrok.provider import DecoderRegistry

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUSES = {"uart-9600", "uart-115200", "i2c-100k", "i2c-400k", "spi-mode0", "onewire", "can-500k", "pwm", "ws2812",
         "i2s", "parallel-8bit", "swd", "jtag", "ir-nec", "usb-low-speed", "usb-full-speed", "midi", "dmx512", "lin"}


def driver(name: str):
    return open_simulated(name, fast=True).capture.driver


def standard(name: str) -> Profile:
    return read_profiles_file(os.path.join(standard_directory(), name + ".json"))[0]


# ------------------------------------------------------------ the profiles
def test_the_standard_profiles_cover_the_buses_and_the_c64():
    names = {os.path.splitext(name)[0] for name in standard_files()}
    assert BUSES | {"c64-expansion-port"} <= names


@pytest.mark.parametrize("name", sorted(BUSES | {"c64-expansion-port"}))
def test_a_standard_profile_is_complete(name):
    profile = standard(name)
    registry = DecoderRegistry()
    captured = {channel.channel_number for channel in profile.capture_settings.capture_channels}
    assert profile.notes and profile.decoder_configuration
    for item in profile.decoder_configuration:
        info = registry.get(item["decoder_id"])
        assert info is not None, item["decoder_id"]
        indexes = {channel.index for channel in info.channels}
        assert {int(key) for key in item["channel_map"]} <= indexes
        assert set(item["channel_map"].values()) <= captured  # every decoder channel is captured
        assert set(item["options"]) <= {option.id for option in info.options}
        if item["parent"] is not None:
            assert item["channel_map"] == {} and 0 <= item["parent"] < len(profile.decoder_configuration)


def test_the_files_are_what_the_tool_makes():
    spec = importlib.util.spec_from_file_location("make_standard_profiles",
                                                  os.path.join(ROOT, "tools", "make_standard_profiles.py"))
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    registry = DecoderRegistry()
    for entry in tool.PROFILES:
        with open(os.path.join(standard_directory(), entry[0] + ".json"), encoding="utf-8") as handle:
            assert json.load(handle) == {"Profiles": [tool.make(registry, entry).to_dict()]}, entry[0]


# ------------------------------------------------------------- the folder
def test_the_standard_profiles_are_copied_once(tmp_path):
    folder = str(tmp_path)
    copied = install_standard_profiles(folder)
    assert sorted(copied) == standard_files()
    os.remove(os.path.join(folder, "pwm.json"))  # deleted: it stays deleted
    assert install_standard_profiles(folder) == []
    assert not os.path.exists(os.path.join(folder, "pwm.json"))
    with open(os.path.join(folder, INSTALLED_FILE), "w", encoding="utf-8") as handle:
        json.dump([name for name in standard_files() if name != "jtag.json"], handle)
    os.remove(os.path.join(folder, "jtag.json"))
    assert install_standard_profiles(folder) == ["jtag.json"]  # one that came with a new version


def test_the_store_loads_the_folder_without_importing():
    folder = profiles_directory()
    session = CaptureSession(frequency=1_000_000)
    session.capture_channels = [AnalyzerChannel(channel_number=0)]
    write_profiles_file(os.path.join(folder, "lab.json"), [Profile("Lab bench", session), Profile("Lab two", session)])
    store = ProfileStore()
    sources = {os.path.basename(profile.source) for profile in store.profiles if profile.standard}
    assert sources == set(standard_files())  # the C64 file holds two profiles: both boards, board B only
    assert store.get("UART 9600 baud").standard
    mine = store.user_profiles()
    assert [profile.name for profile in mine] == ["Lab bench", "Lab two"]
    assert mine[0].source == os.path.join(folder, "lab.json") and not mine[0].standard
    # a profile of the folder changes and goes in its own file, not in profiles.json
    store.remove("Lab bench")
    store.add(Profile("Saved", session))
    assert store.save()
    assert [profile.name for profile in read_profiles_file(os.path.join(folder, "lab.json"))] == ["Lab two"]
    store.remove("Lab two")
    store.remove("I²C 100 kHz (standard mode)")
    assert store.save()
    assert not os.path.exists(os.path.join(folder, "lab.json")) and not os.path.exists(os.path.join(folder, "i2c-100k.json"))
    again = ProfileStore()
    assert [profile.name for profile in again.user_profiles()] == ["Saved"]
    assert again.get("I²C 100 kHz (standard mode)") is None  # a deleted standard profile stays deleted


def test_a_file_put_into_the_folder_shows_up_in_the_menu(make_dataview):
    from PySide6.QtWidgets import QMenu

    from openscilab.ui.devices.capture import capture_controller

    window = make_dataview()
    controller = capture_controller(open_simulated("free"))
    window.attach_source(controller)
    try:
        session = CaptureSession(frequency=1_000_000)
        session.capture_channels = [AnalyzerChannel(channel_number=0)]
        write_profiles_file(os.path.join(profiles_directory(), "dropped.json"), [Profile("Dropped", session)])
        menu = QMenu()
        controller.fill_profiles_menu(menu, window)
        assert "Dropped" in [action.text() for action in menu.actions()]
    finally:
        window.close()


# ------------------------------------------------------ fitting a device
def test_a_profile_needs_the_channels_it_captures():
    c64 = standard("c64-expansion-port")
    assert "needs 48 channels" in profile_problem(c64, driver("pico"))  # two boards of 24
    assert profile_problem(standard("uart-115200"), driver("uno")) == ""
    assert "needs 9 channels" in profile_problem(standard("parallel-8bit"), _small(8))
    assert profile_problem(c64, None) == ""


def _small(channels: int):
    class Small:
        channel_count = channels
        analog_channel_count = 0

    return Small()


def test_rate_and_length_are_fitted_to_the_device():
    usb = standard("usb-full-speed").capture_settings
    fitted, changes = fit_session(usb, driver("uno"))
    assert fitted.frequency == 100_000 and fitted.pre_trigger_samples + fitted.post_trigger_samples <= 448
    assert fitted.pre_trigger_samples == 44  # the profile's tenth before the trigger
    assert any(change.startswith("rate") for change in changes) and any(change.startswith("length") for change in changes)
    same, nothing = fit_session(standard("uart-9600").capture_settings, driver("free"))
    assert nothing == [] and same.frequency == 1_000_000
    assert fit_session(usb, None)[0].frequency == usb.frequency


def test_a_trigger_on_a_missing_channel_becomes_none():
    session = CaptureSession(frequency=1000, trigger_type=TriggerType.EDGE, trigger_channel=20)
    session.capture_channels = [AnalyzerChannel(channel_number=0)]
    fitted, changes = fit_session(session, driver("uno"))
    assert fitted.trigger_type == TriggerType.IMMEDIATE and "no trigger" in changes


# --------------------------------------------------------------- the menu
def test_the_standard_profiles_menu_loads_and_disables(make_dataview):
    from PySide6.QtWidgets import QMenu

    from openscilab.core import capture_io, settings
    from openscilab.ui.devices.capture import capture_controller
    from openscilab.ui.dialogs.capture_dialog import capture_settings_file

    window = make_dataview()
    instrument = open_simulated("uno")
    controller = capture_controller(instrument)
    window.attach_source(controller)
    try:
        menu = QMenu()
        controller.fill_profiles_menu(menu, window)
        assert "Open the profiles folder" in [action.text() for action in menu.actions()]
        entries = {action.text(): action for action in controller.standard_menu.actions()}
        c64 = next(action for text, action in entries.items() if text.startswith("C64"))
        assert not c64.isEnabled() and "needs 48 channels" in c64.text()
        entries["UART 115200 baud (Arduino serial)"].trigger()
        stored = capture_io.session_from_dict(settings.get_settings(capture_settings_file(controller.driver)))
        assert stored.frequency == 100_000  # the Uno's highest rate
        assert [channel.channel_name for channel in stored.capture_channels] == ["RX", "TX"]
        assert [instance.decoder_id for instance in controller.view.provider.instances] == ["uart"]
        assert controller.view.provider.instances[0].options["baudrate"] == 115200
    finally:
        window.close()
