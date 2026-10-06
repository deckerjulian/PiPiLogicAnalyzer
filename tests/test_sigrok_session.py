"""sigrok session files (.sr)."""

from __future__ import annotations

import zipfile

import numpy as np
import pytest

from openscilab.core import sigrok_session
from openscilab.core.sigrok_session import (
    SigrokSessionError,
    load_session,
    parse_samplerate,
    samplerate_string,
    save_session,
)
from openscilab.driver.models import AnalyzerChannel, CaptureSession


@pytest.mark.parametrize(
    "rate, text",
    [
        (500, "500 Hz"),
        (1_000, "1 kHz"),
        (1_500, "1.5 kHz"),
        (10_000_000, "10 MHz"),
        (24_000_000, "24 MHz"),
        (1_234_567, "1.234567 MHz"),
        (200_000_000, "200 MHz"),
        (1_000_000_000, "1 GHz"),
        (999, "999 Hz"),
    ],
)
def test_samplerate_strings_match_libsigrok(rate, text):
    assert samplerate_string(rate) == text
    assert parse_samplerate(text) == rate


@pytest.mark.parametrize(
    "text, rate",
    [("24000000", 24_000_000), ("100 kHz", 100_000), ("2.5MHz", 2_500_000), ("1 mhz", 1_000_000), ("8 k", 8_000)],
)
def test_samplerate_parsing(text, rate):
    assert parse_samplerate(text) == rate


def test_invalid_samplerate():
    with pytest.raises(SigrokSessionError):
        parse_samplerate("fast")


def random_session(channel_count: int, samples: int, pre: int = 0) -> CaptureSession:
    generator = np.random.default_rng(1)
    session = CaptureSession(frequency=12_500_000, pre_trigger_samples=pre, post_trigger_samples=samples - pre)
    session.capture_channels = [
        AnalyzerChannel(
            channel_number=n,
            channel_name=f"D{n}" if n % 2 else "",
            samples=generator.integers(0, 2, samples, dtype=np.uint8),
        )
        for n in range(channel_count)
    ]
    return session


def test_round_trip_two_byte_samples(tmp_path):
    session = random_session(10, 5000, pre=123)
    path = str(tmp_path / "capture.sr")
    save_session(path, session)

    with zipfile.ZipFile(path) as archive:
        assert archive.read("version") == b"2"
        assert archive.getinfo("version").compress_type == zipfile.ZIP_STORED
        metadata = archive.read("metadata").decode()
        assert "unitsize=2" in metadata
        assert "samplerate=12.5 MHz" in metadata
        assert "probe2=D1" in metadata and "probe1=Channel 1" in metadata
        assert archive.getinfo("logic-1-1").file_size == 2 * 5000

    loaded = load_session(path)
    assert loaded.frequency == 12_500_000
    assert loaded.pre_trigger_samples == 123
    assert loaded.post_trigger_samples == 5000 - 123
    assert [c.display_name for c in loaded.capture_channels] == [c.display_name for c in session.capture_channels]
    for original, channel in zip(session.capture_channels, loaded.capture_channels):
        np.testing.assert_array_equal(original.samples, channel.samples)


def test_unnamed_channels_keep_their_numbers(tmp_path):
    session = random_session(2, 64)
    session.capture_channels[0].channel_number = 5
    session.capture_channels[1].channel_number = 9
    session.capture_channels[1].channel_name = ""
    path = str(tmp_path / "capture.sr")
    save_session(path, session)
    assert [c.channel_number for c in load_session(path).capture_channels] == [5, 9]


def test_large_captures_are_split_into_chunks(tmp_path, monkeypatch):
    monkeypatch.setattr(sigrok_session, "CHUNK_BYTES", 1000)
    monkeypatch.setattr(sigrok_session, "_BLOCK_SAMPLES", 96)
    session = random_session(3, 2600)
    path = str(tmp_path / "capture.sr")
    save_session(path, session)
    with zipfile.ZipFile(path) as archive:
        assert sorted(name for name in archive.namelist() if name.startswith("logic")) == [
            "logic-1-1", "logic-1-2", "logic-1-3",
        ]
    loaded = load_session(path)
    for original, channel in zip(session.capture_channels, loaded.capture_channels):
        np.testing.assert_array_equal(original.samples, channel.samples)


def test_memory_mapped_samples_are_exported(tmp_path):
    mapped = np.memmap(str(tmp_path / "samples.bin"), dtype=np.uint8, mode="w+", shape=(1000,))
    mapped[::3] = 1
    session = CaptureSession(frequency=1000, pre_trigger_samples=0, post_trigger_samples=1000)
    session.capture_channels = [AnalyzerChannel(channel_number=0, samples=mapped)]
    save_session(str(tmp_path / "mapped.sr"), session)
    np.testing.assert_array_equal(load_session(str(tmp_path / "mapped.sr")).capture_channels[0].samples, mapped)


def write_archive(path, metadata: str, files: dict[str, bytes], version: bytes = b"2") -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("version", version)
        archive.writestr("metadata", metadata)
        for name, data in files.items():
            archive.writestr(name, data)


def test_hand_built_session(tmp_path):
    """Like sigrok-cli writes it: probes named D0, D1, ..., chunks of whole samples."""
    metadata = (
        "[global]\nsigrok version=0.5.2\n\n[device 1]\ncapturefile=logic-1\ntotal probes=3\n"
        "samplerate=1 MHz\ntotal analog=0\nprobe1=D0\nprobe2=D1\nprobe3=CS#\nunitsize=1\n"
    )
    path = str(tmp_path / "hand.sr")
    write_archive(path, metadata, {"logic-1-1": bytes([0b001, 0b010, 0b100]), "logic-1-2": bytes([0b111, 0])})

    session = load_session(path)
    assert session.frequency == 1_000_000
    assert session.pre_trigger_samples == 0
    assert session.sample_count() == 5
    assert [c.channel_name for c in session.capture_channels] == ["D0", "D1", "CS#"]
    assert [c.channel_number for c in session.capture_channels] == [0, 1, 2]
    assert session.capture_channels[0].samples.tolist() == [1, 0, 0, 1, 0]
    assert session.capture_channels[1].samples.tolist() == [0, 1, 0, 1, 0]
    assert session.capture_channels[2].samples.tolist() == [0, 0, 1, 1, 0]


def test_version_one_and_trigger(tmp_path):
    metadata = (
        "[device 1]\ncapturefile=logic-1\ntotal probes=16\nsamplerate=200 kHz\n"
        "probe1=A\nprobe12=B\nunitsize=2\ntrigger=2\n"
    )
    path = str(tmp_path / "v1.sr")
    samples = np.array([0x0001, 0x0800, 0x0801, 0], dtype="<u2").tobytes()
    write_archive(path, metadata, {"logic-1": samples}, version=b"1")
    session = load_session(path)
    assert session.frequency == 200_000
    assert session.pre_trigger_samples == 2
    assert [c.channel_number for c in session.capture_channels] == [0, 11]
    assert session.capture_channels[0].samples.tolist() == [1, 0, 1, 0]
    assert session.capture_channels[1].samples.tolist() == [0, 1, 1, 0]


def test_chunks_split_inside_a_sample(tmp_path):
    metadata = "[device 1]\ncapturefile=logic-1\nsamplerate=1 kHz\nprobe1=A\nprobe9=B\nunitsize=2\n"
    path = str(tmp_path / "odd.sr")
    write_archive(path, metadata, {"logic-1-1": b"\x01\x01\x00", "logic-1-2": b"\x01"})
    session = load_session(path)
    assert session.capture_channels[0].samples.tolist() == [1, 0]
    assert session.capture_channels[1].samples.tolist() == [1, 1]


def test_invalid_files(tmp_path):
    (tmp_path / "plain.sr").write_text("no zip")
    with pytest.raises(SigrokSessionError):
        load_session(str(tmp_path / "plain.sr"))
    path = str(tmp_path / "v3.sr")
    write_archive(path, "[device 1]\ncapturefile=logic-1\n", {}, version=b"3")
    with pytest.raises(SigrokSessionError, match="version"):
        load_session(path)
    path = str(tmp_path / "analog.sr")
    write_archive(path, "[device 1]\ntotal analog=1\n", {})
    with pytest.raises(SigrokSessionError, match="no logic"):
        load_session(path)


def test_export_needs_samples(tmp_path):
    with pytest.raises(ValueError):
        save_session(str(tmp_path / "empty.sr"), CaptureSession())


def test_channel_numbers_without_the_extension_key(tmp_path):
    metadata = (
        "[device 1]\ncapturefile=logic-1\nsamplerate=1 kHz\nprobe1=SCL\nprobe2=Channel 6\nunitsize=1\n"
        "channel numbers=1,1\n"  # invalid: ignored
    )
    path = str(tmp_path / "mixed.sr")
    write_archive(path, metadata, {"logic-1-1": b"\x01\x02"})
    session = load_session(path)
    assert [c.channel_number for c in session.capture_channels] == [0, 5]
    assert [c.channel_name for c in session.capture_channels] == ["SCL", ""]
