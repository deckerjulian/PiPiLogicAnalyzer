"""examples/c64-demo.lac decodes into the program that generated it."""

from __future__ import annotations

import os

import pytest

from pipilogicanalyzer.core import alignment, capture_io
from pipilogicanalyzer.core.profiles import read_profiles_file
from pipilogicanalyzer.sigrok.provider import SigrokProvider

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEMO = os.path.join(ROOT, "examples", "c64-demo.lac")
PROFILE = os.path.join(ROOT, "examples", "c64-expansion-port-profile.json")

pytestmark = pytest.mark.skipif(
    not (os.path.isfile(DEMO) and os.path.isfile(os.path.join(ROOT, "decoders", "c64bus", "pd.py"))),
    reason="the demo capture or the c64bus decoder is missing",
)


@pytest.fixture(scope="module")
def decoded():
    from pipilogicanalyzer.sigrok.engine import DecoderRegistry

    registry = DecoderRegistry()
    registry.load()
    capture = capture_io.load_capture(DEMO)
    provider = SigrokProvider(registry)
    provider.load_configuration(read_profiles_file(PROFILE)[0].decoder_configuration)
    (group,) = [group for group in provider.run(capture.session) if group.instance.decoder_id == "c64bus"]
    assert group.error is None, group.error
    return capture, {row.name: row for row in group.annotations}


def texts(rows, name):
    return [segment.values[0] for segment in sorted(rows[name].segments, key=lambda item: item.first_sample)]


def test_the_demo_matches_the_profile(decoded):
    capture, _rows = decoded
    session = capture.session
    profile = read_profiles_file(PROFILE)[0].capture_settings
    assert session.frequency == profile.frequency == 20_000_000
    assert [channel.channel_name for channel in session.capture_channels] == [
        channel.channel_name for channel in profile.capture_channels
    ]
    assert [region.region_name for region in capture.regions] == ["Reset", "IRQ", "IRQ"]


def test_the_program_is_disassembled(decoded):
    _capture, rows = decoded
    assembly = texts(rows, "Disassembly")

    assert assembly[:8] == [
        "RESET → $C000",
        "$C000  CLI",
        "$C001  LDX #$00",
        "$C003  LDA $C020,X",
        "$C006  STA $0400,X",
        "$C009  INX",
        "$C00A  CPX #$05",
        "$C00C  BNE $C003",
    ]
    for statement in ("$C00E  INC $D020", "$C011  JMP $C001", "IRQ → $EA31", "$EA32  LDA $DC0D", "$EA36  RTI"):
        assert statement in assembly
    assert assembly.count("IRQ → $EA31") == 2


def test_the_bus_is_clean_and_the_boards_are_aligned(decoded):
    capture, rows = decoded
    assert "Warnings" not in rows
    assert "W $0400 = $03" in texts(rows, "Bus cycles")  # "C" written to the screen

    (result,) = alignment.align_devices(capture.session, 24, apply=False)
    assert result.method == "reference" and not result.changed
