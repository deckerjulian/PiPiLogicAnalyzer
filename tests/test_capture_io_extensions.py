"""The session fields of this application in capture settings, profiles and .lac files."""

from __future__ import annotations

import json

import numpy as np

from openscilab.core import capture_io
from openscilab.driver.models import (
    AnalyzerChannel,
    BusDefinition,
    BusFormat,
    CaptureSession,
    ConditionKind,
    EdgeKind,
    TriggerCondition,
    TriggerSequence,
    TriggerStage,
    TriggerType,
)

#: Keys of the original C# CaptureSession; everything else is an extension it ignores.
ORIGINAL_KEYS = {
    "Frequency", "PreTriggerSamples", "PostTriggerSamples", "TotalSamples", "LoopCount",
    "MeasureBursts", "CaptureChannels", "Bursts", "TriggerType", "TriggerChannel",
    "TriggerInverted", "TriggerBitCount", "TriggerPattern",
}


def extended_session() -> CaptureSession:
    session = CaptureSession(frequency=4_000_000, pre_trigger_samples=10, post_trigger_samples=90)
    session.capture_channels = [
        AnalyzerChannel(channel_number=n, samples=np.arange(100, dtype=np.uint8) % 2) for n in range(4)
    ]
    session.trigger_type = TriggerType.SEQUENCE
    session.trigger_sequence = TriggerSequence(
        stages=[
            TriggerStage(TriggerCondition(ConditionKind.PATTERN, mask=0b1011, value=0b0001)),
            TriggerStage(
                TriggerCondition(ConditionKind.PULSE, channel=2, edge=EdgeKind.FALLING, min_ns=100, max_ns=900),
                count=3,
                within_ns=50_000,
            ),
            TriggerStage(TriggerCondition(ConditionKind.GAP, channel=1, min_ns=2_000)),
        ]
    )
    session.software_trigger = True
    session.clock_channel = 3
    session.clock_edge = EdgeKind.FALLING
    session.buses = [
        BusDefinition(
            name="Address", channels=[0, 1, 2], format=BusFormat.SIGNED,
            symbols={0xD020: "BORDER", 7: "SEVEN"}, color=0xFF8800,
        ),
        BusDefinition(name="Group", channels=[3]),
    ]
    return session


def test_new_fields_survive_json():
    data = json.loads(json.dumps(capture_io.session_to_dict(extended_session())))
    session = capture_io.session_from_dict(data)

    assert session.trigger_type == TriggerType.SEQUENCE
    assert session.trigger_sequence == extended_session().trigger_sequence
    assert session.software_trigger is True
    assert session.clock_channel == 3
    assert session.clock_edge == EdgeKind.FALLING
    assert session.buses == extended_session().buses
    assert session.buses[0].symbols == {0xD020: "BORDER", 7: "SEVEN"}


def test_extension_keys_follow_the_convention():
    data = capture_io.session_to_dict(extended_session())
    extensions = set(data) - ORIGINAL_KEYS
    assert {"TriggerSequence", "SoftwareTrigger", "ClockChannel", "ClockEdge", "Buses"} <= extensions
    # every key is PascalCase like the original ones (Newtonsoft ignores the unknown ones)
    assert all(key[0].isupper() and "_" not in key for key in data)
    assert data["Buses"][0]["Symbols"] == {"53280": "BORDER", "7": "SEVEN"}
    assert data["TriggerSequence"]["Stages"][1]["Condition"]["Kind"] == "pulse"


def test_defaults_serialise_to_plain_values():
    data = capture_io.session_to_dict(CaptureSession())
    assert data["TriggerSequence"] is None
    assert data["SoftwareTrigger"] is False
    assert data["ClockChannel"] is None
    assert data["ClockEdge"] == "rising"
    assert data["Buses"] == []


def test_files_without_the_keys_load_with_defaults():
    data = capture_io.session_to_dict(CaptureSession(frequency=1000))
    for key in set(data) - ORIGINAL_KEYS:
        del data[key]
    session = capture_io.session_from_dict(data)
    assert session.frequency == 1000
    assert session.trigger_sequence is None
    assert session.software_trigger is False
    assert session.clock_channel is None
    assert session.clock_edge == EdgeKind.RISING
    assert session.buses == []


def test_unknown_or_hand_written_values_are_tolerated():
    data = capture_io.session_to_dict(CaptureSession())
    data["ClockEdge"] = "sideways"
    data["Buses"] = [{"Name": "B", "Channels": [1, 0], "Format": "octal", "Symbols": {"0x10": "X", "bad": "Y"}}, 5]
    data["TriggerSequence"] = {"Stages": [{"Condition": {"Kind": "unknown"}}, "junk"]}
    session = capture_io.session_from_dict(data)
    assert session.clock_edge == EdgeKind.RISING
    assert session.buses == [BusDefinition(name="B", channels=[1, 0], format=BusFormat.HEX, symbols={16: "X"})]
    assert session.trigger_sequence == TriggerSequence(stages=[TriggerStage()])


def test_lac_file_round_trip(tmp_path):
    path = str(tmp_path / "capture.lac.gz")
    capture_io.save_capture(path, extended_session())
    loaded = capture_io.load_capture(path).session
    assert loaded.trigger_sequence == extended_session().trigger_sequence
    assert loaded.buses == extended_session().buses
    assert loaded.clock_channel == 3
    np.testing.assert_array_equal(loaded.capture_channels[1].samples, np.arange(100) % 2)


def test_settings_without_samples_keep_the_fields():
    data = capture_io.session_to_dict(extended_session().clone_settings(), include_samples=False)
    session = capture_io.session_from_dict(json.loads(json.dumps(data)))
    assert session.buses[0].name == "Address"
    assert len(session.trigger_sequence.stages) == 3
