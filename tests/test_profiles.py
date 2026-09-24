"""Profiles: storage, file import/export, the original format and the GUI."""

from __future__ import annotations

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication, QInputDialog

from pipilogicanalyzer.core import settings
from pipilogicanalyzer.core.capture_io import session_to_dict
from pipilogicanalyzer.core.profiles import (
    Profile,
    ProfileStore,
    read_profiles_file,
    strip_type_metadata,
    write_profiles_file,
)
from pipilogicanalyzer.driver.models import AnalyzerChannel, CaptureSession, TriggerType
from pipilogicanalyzer.sigrok.provider import DecoderInstance, SigrokProvider


def make_settings() -> CaptureSession:
    session = CaptureSession(
        frequency=2_000_000,
        pre_trigger_samples=100,
        post_trigger_samples=900,
        trigger_type=TriggerType.COMPLEX,
        trigger_channel=2,
        trigger_bit_count=3,
        trigger_pattern=0b101,
    )
    session.capture_channels = [
        AnalyzerChannel(channel_number=0, channel_name="SCL"),
        AnalyzerChannel(channel_number=5, channel_name="SDA", channel_color=0xFF00FF00),
    ]
    return session


# ---------------------------------------------------------------------- store
def test_profiles_survive_a_restart():
    store = ProfileStore()
    store.add(Profile("I2C", make_settings(), [{"decoder_id": "i2c", "parent": None}]))
    assert store.save()

    profile = ProfileStore().get("I2C")
    assert profile is not None
    assert profile.capture_settings.frequency == 2_000_000
    assert profile.capture_settings.trigger_type is TriggerType.COMPLEX
    assert [c.channel_name for c in profile.capture_settings.capture_channels] == ["SCL", "SDA"]
    assert profile.capture_settings.capture_channels[1].channel_color == 0xFF00FF00
    assert profile.decoder_configuration == [{"decoder_id": "i2c", "parent": None}]


def test_a_profile_with_the_same_name_is_replaced():
    store = ProfileStore()
    store.add(Profile("Bus", make_settings()))
    store.add(Profile("Other"))
    store.add(Profile("Bus", None, [{"decoder_id": "uart"}]))
    assert [profile.name for profile in store.profiles] == ["Bus", "Other"]
    assert store.get("Bus").capture_settings is None
    assert store.remove("Bus") and not store.remove("Bus")


def test_samples_are_never_stored():
    session = make_settings()
    session.capture_channels[0].samples = np.ones(10, dtype=np.uint8)
    data = Profile("With samples", session).to_dict()
    assert all(channel["Samples"] is None for channel in data["CaptureSettings"]["CaptureChannels"])
    assert session.capture_channels[0].samples is not None  # the source stays untouched


def test_a_corrupt_profiles_file_is_ignored():
    settings.persist_settings("profiles.json", {"Profiles": ["not a profile"]})
    assert ProfileStore().profiles == []


# ---------------------------------------------------------------------- files
def test_export_and_import(tmp_path):
    path = tmp_path / "shared.json"
    write_profiles_file(str(path), [Profile("A", make_settings()), Profile("B")])
    imported = read_profiles_file(str(path))
    assert [profile.name for profile in imported] == ["A", "B"]
    assert imported[0].capture_settings.trigger_pattern == 0b101


def test_plain_capture_settings_are_imported_under_the_file_name(tmp_path):
    path = tmp_path / "uart-setup.json"
    path.write_text(json.dumps(session_to_dict(make_settings(), include_samples=False)))
    (profile,) = read_profiles_file(str(path))
    assert profile.name == "uart-setup"
    assert profile.capture_settings.pre_trigger_samples == 100


def test_invalid_files_raise_value_error(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("[1, 2, 3]")
    with pytest.raises(ValueError):
        read_profiles_file(str(path))


def test_the_original_profiles_file_is_understood(tmp_path):
    """LogicAnalyzer 6.5 writes Newtonsoft JSON with ``$type``/``$values``."""
    original = {
        "$type": "PiPiLogicAnalyzer.Classes.ProfilesSet, PiPiLogicAnalyzer",
        "Profiles": {
            "$type": "System.Collections.Generic.List`1[[PiPiLogicAnalyzer.Classes.Profile]]",
            "$values": [
                {
                    "$type": "PiPiLogicAnalyzer.Classes.Profile, PiPiLogicAnalyzer",
                    "Name": "SPI flash",
                    "CaptureSettings": {
                        "$type": "SharedDriver.CaptureSession, SharedDriver",
                        "Frequency": 24_000_000,
                        "PreTriggerSamples": 64,
                        "PostTriggerSamples": 4096,
                        "LoopCount": 0,
                        "MeasureBursts": False,
                        "CaptureChannels": {
                            "$type": "SharedDriver.AnalyzerChannel[], SharedDriver",
                            "$values": [
                                {"$type": "SharedDriver.AnalyzerChannel, SharedDriver",
                                 "ChannelNumber": 3, "ChannelName": "MOSI", "Samples": None},
                            ],
                        },
                        "TriggerType": 0,
                        "TriggerChannel": 3,
                        "TriggerInverted": True,
                    },
                    "DecoderConfiguration": {
                        "$type": "PiPiLogicAnalyzer.SigrokDecoderBridge.SerializableDecodingTree",
                        "Branches": {"$type": "List", "$values": []},
                    },
                }
            ],
        },
    }
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps(original), encoding="utf-8-sig")

    (profile,) = read_profiles_file(str(path))
    assert profile.name == "SPI flash"
    assert profile.capture_settings.frequency == 24_000_000
    assert profile.capture_settings.trigger_inverted
    assert profile.capture_settings.capture_channels[0].channel_name == "MOSI"
    assert profile.decoder_configuration == {"Branches": []}


def test_type_metadata_is_stripped_recursively():
    data = {"$type": "x", "a": {"$type": "y", "$values": [{"$type": "z", "b": 1}]}}
    assert strip_type_metadata(data) == {"a": [{"b": 1}]}


# ------------------------------------------------------------------- decoders
def test_the_original_decoder_tree_is_mapped_through_the_registry(decoder_registry):
    provider = SigrokProvider(decoder_registry)
    tree = {
        "Branches": [
            {
                "Name": "Clock edges",
                "DecoderId": "testdec",
                "Options": [{"OptionIndex": 2, "Value": "first"}],
                "Channels": [{"CaptureIndex": 3, "SigrokIndex": 0}],
                "Children": [
                    {"Name": "Stacked", "DecoderId": "testdec", "Options": [], "Channels": [],
                     "Children": []},
                ],
            },
            {"Name": "Gone", "DecoderId": "not-installed", "Options": [], "Channels": [],
             "Children": []},
        ]
    }

    provider.load_configuration(tree)

    assert [instance.label for instance in provider.instances] == ["Clock edges", "Stacked"]
    root, child = provider.instances
    assert root.options == {"label": "edge", "skip": 0, "mode": "first"}
    assert root.channel_map == {0: 3}
    assert child.parent is root


def test_the_own_decoder_list_is_restored(decoder_registry):
    source = SigrokProvider(decoder_registry)
    parent = source.add_instance(DecoderInstance(decoder_id="testdec", channel_map={0: 1}))
    source.add_instance(DecoderInstance(decoder_id="testdec", parent=parent))

    target = SigrokProvider(decoder_registry)
    target.load_configuration(source.to_list())
    assert len(target.instances) == 2
    assert target.instances[1].parent is target.instances[0]
    assert target.instances[0].channel_map == {0: 1}


# ------------------------------------------------------------------------ GUI
@pytest.fixture(scope="module")
def application():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(application, monkeypatch):
    from pipilogicanalyzer.ui.main_window import MainWindow
    from test_ui import make_session

    monkeypatch.setattr("pipilogicanalyzer.ui.messages.confirm", lambda *args, **kwargs: True)
    monkeypatch.setattr("pipilogicanalyzer.ui.messages.info", lambda *args, **kwargs: None)
    main = MainWindow()
    main.load_session(make_session())
    yield main
    main.close()


def test_main_window_profiles_menu(window, monkeypatch):
    monkeypatch.setattr(QInputDialog, "getText", lambda *args, **kwargs: ("Demo", True))
    window.provider.add_instance(DecoderInstance(decoder_id="i2c", channel_map={0: 0, 1: 1}))

    window.add_profile()

    stored = ProfileStore().get("Demo")
    assert stored is not None
    assert stored.capture_settings.frequency == 1_000_000
    assert stored.capture_settings.capture_channels[0].samples is None
    assert stored.decoder_configuration[0]["decoder_id"] == "i2c"

    window._rebuild_profiles_menu()
    assert "Demo" in [action.text() for action in window.profiles_menu.actions()]

    window.provider.clear()
    window.load_profile(stored)
    assert [instance.decoder_id for instance in window.provider.instances] == ["i2c"]
    for driver_file in ("capture-settings-serial.json", "capture-settings-multi.json"):
        assert settings.get_settings(driver_file)["Frequency"] == 1_000_000

    window.delete_profile(window.profiles.get("Demo"))
    assert ProfileStore().get("Demo") is None


def test_adding_a_profile_without_anything_to_store_is_refused(application, monkeypatch):
    from pipilogicanalyzer.ui.main_window import MainWindow

    asked = []
    monkeypatch.setattr("pipilogicanalyzer.ui.messages.info", lambda *args, **kwargs: asked.append(True))
    monkeypatch.setattr(QInputDialog, "getText", lambda *args, **kwargs: pytest.fail("asked"))
    main = MainWindow()
    try:
        main.add_profile()
    finally:
        main.close()
    assert asked and ProfileStore().profiles == []


def test_importing_profiles_keeps_existing_ones_on_request(window, tmp_path, monkeypatch):
    window.profiles.add(Profile("Keep", make_settings()))
    path = tmp_path / "incoming.json"
    write_profiles_file(str(path), [Profile("Keep"), Profile("New", make_settings())])

    monkeypatch.setattr(
        "pipilogicanalyzer.ui.main_window.QFileDialog.getOpenFileName",
        lambda *args, **kwargs: (str(path), ""),
    )
    monkeypatch.setattr("pipilogicanalyzer.ui.messages.choose", lambda *args, **kwargs: 1)  # keep existing
    window.import_profiles()

    assert window.profiles.get("Keep").capture_settings is not None
    assert ProfileStore().get("New") is not None
