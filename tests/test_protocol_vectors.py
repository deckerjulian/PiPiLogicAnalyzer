"""The shared test vectors of the Arduino frames and the bridge codec (``docs/protocols.md``)."""

import json
from pathlib import Path

import numpy as np
import pytest

from openscilab.driver.arduino import protocol as p
from openscilab.driver.rigoldho import codec

FIXTURES = Path(__file__).parent / "fixtures"
ARDUINO = json.loads((FIXTURES / "arduino_frames.json").read_text())
BRIDGE = json.loads((FIXTURES / "bridge_codec.json").read_text())


def test_crc8_check_value():
    assert p.crc8(b"123456789") == 0xF4
    for case in ARDUINO["crc8"]:
        assert p.crc8(bytes.fromhex(case["data"])) == case["crc"]


@pytest.mark.parametrize("case", ARDUINO["cobs"])
def test_cobs(case):
    data, encoded = bytes.fromhex(case["data"]), bytes.fromhex(case["encoded"])
    assert p.cobs_encode(data) == encoded
    assert 0 not in encoded
    assert p.cobs_decode(encoded) == data


@pytest.mark.parametrize("case", ARDUINO["frames"])
def test_frames(case):
    wire = bytes.fromhex(case["wire"])
    assert p.encode(case["type"], case["sequence"], bytes.fromhex(case["payload"])) == wire
    frame = p.decode(wire[:-1])
    assert (frame.type, frame.sequence, frame.payload.hex()) == (case["type"], case["sequence"], case["payload"])


def test_decoder_splits_and_counts_damage():
    wires = [bytes.fromhex(case["wire"]) for case in ARDUINO["frames"]]
    stream = b"".join(wires)
    damaged = bytearray(wires[1])
    damaged[2] ^= 0x01
    decoder = p.Decoder()
    frames = []
    for index in range(0, len(stream), 3):
        frames += decoder.feed(stream[index:index + 3])
    frames += decoder.feed(b"\x00" + bytes(damaged))
    assert len(frames) == len(wires)
    assert decoder.damaged == 1


def test_frame_size_limit():
    p.encode(p.REQUEST, 0, bytes(p.MAX_PAYLOAD))
    with pytest.raises(ValueError):
        p.encode(p.REQUEST, 0, bytes(p.MAX_PAYLOAD + 1))


def test_runs_round_trip():
    levels = [0] * 70000 + [5, 5, 3]
    items = p.runs(levels)
    assert items[0] == (0xFFFF, 0) and sum(count for count, _ in items) == len(levels)
    assert p.unpack_runs(p.pack_runs(items)) == items


@pytest.mark.parametrize("case", BRIDGE["varint"])
def test_varint(case):
    assert codec.varint(case["value"]).hex() == case["encoded"]
    assert codec.read_varint(bytes.fromhex(case["encoded"]), 0)[0] == case["value"]


@pytest.mark.parametrize("case", BRIDGE["zigzag"])
def test_zigzag(case):
    assert codec.zigzag(case["value"]) == case["encoded"]
    assert codec.unzigzag(case["encoded"]) == case["value"]


@pytest.mark.parametrize("case", BRIDGE["digital"])
def test_digital(case):
    levels = np.asarray(case["levels"], dtype=np.uint16)
    assert codec.encode_digital(levels).hex() == case["encoded"]
    assert codec.decode_digital(bytes.fromhex(case["encoded"])).tolist() == case["levels"]


@pytest.mark.parametrize("case", BRIDGE["analog"])
def test_analog(case):
    samples = np.asarray(case["samples"], dtype=np.uint8)
    assert codec.encode_analog(samples).hex() == case["encoded"]
    assert codec.decode_analog(bytes.fromhex(case["encoded"])).tolist() == case["samples"]


def test_overviews():
    case = BRIDGE["overview_analog"][0]
    assert codec.overview_analog(np.asarray(case["samples"], np.uint8), case["level"]).tolist() == case["minmax"]
    case = BRIDGE["overview_digital"][0]
    assert codec.overview_digital(np.asarray(case["levels"], np.uint16), case["level"]).tolist() == case["and_or"]
