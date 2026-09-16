"""Disassembly of 6510 bus cycles without a SYNC signal (decoders/c64bus/disasm.py)."""

from __future__ import annotations

import importlib.util
import os

import pytest

PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "decoders", "c64bus", "disasm.py")

pytestmark = pytest.mark.skipif(not os.path.isfile(PATH), reason="the c64bus decoder is not installed")

R, W = True, False

#: Cycle by cycle: reset, a subroutine call, a taken branch and an IRQ with RTI.
PROGRAM = [
    (0xFFFC, 0x00, R), (0xFFFD, 0xC0, R),                                   # RESET vector -> $C000
    (0xC000, 0xA9, R), (0xC001, 0x05, R),                                   # LDA #$05
    (0xC002, 0x20, R), (0xC003, 0x10, R), (0x01FD, 0x00, R),                # JSR $C010
    (0x01FD, 0xC0, W), (0x01FC, 0x04, W), (0xC004, 0xC0, R),
    (0xC010, 0xE8, R), (0xC011, 0x60, R),                                   # INX
    (0xC011, 0x60, R), (0xC012, 0x00, R), (0x01FB, 0x00, R),                # RTS
    (0x01FC, 0x04, R), (0x01FD, 0xC0, R), (0xC004, 0xC0, R),
    (0xC005, 0xD0, R), (0xC006, 0xF9, R), (0xC007, 0x00, R),                # BNE $C000 (taken)
    (0xC000, 0xA9, R), (0xC001, 0x05, R),                                   # LDA #$05
    (0xC002, 0x20, R), (0xC002, 0x20, R), (0x01FD, 0xC0, W),                # IRQ
    (0x01FC, 0x02, W), (0x01FB, 0x20, W), (0xFFFE, 0x48, R), (0xFFFF, 0xFF, R),
    (0xFF48, 0x78, R), (0xFF49, 0x40, R),                                   # SEI
    (0xFF49, 0x40, R), (0xFF4A, 0x00, R), (0x01FA, 0x00, R),                # RTI
    (0x01FB, 0x20, R), (0x01FC, 0x02, R), (0x01FD, 0xC0, R),
    (0xC002, 0x20, R), (0xC003, 0x10, R), (0x01FD, 0x00, R),                # JSR $C010
    (0x01FD, 0xC0, W), (0x01FC, 0x04, W), (0xC004, 0xC0, R),
    (0xC010, 0xE8, R), (0xC011, 0x60, R),                                   # INX
    (0xC011, 0x60, R), (0xC012, 0x00, R), (0x01FB, 0x00, R),                # RTS
    (0x01FC, 0x04, R), (0x01FD, 0xC0, R), (0xC004, 0xC0, R),
    (0xC005, 0xD0, R), (0xC006, 0xF9, R), (0xC007, 0x00, R),                # BNE $C000 (taken)
    (0xC000, 0xA9, R), (0xC001, 0x05, R),                                   # LDA #$05
]


@pytest.fixture(scope="module")
def disasm():
    spec = importlib.util.spec_from_file_location("c64bus_disasm", PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(disasm, steps):
    disassembler = disasm.Disassembler()
    events = []
    for number, (address, data, read) in enumerate(steps):
        cycle = disasm.Cycle(ss=number * 10, es=number * 10 + 10, sample=number * 10 + 7,
                             address=address, data=data, read=read)
        events += disassembler.feed(cycle)
    return events + disassembler.finish()


def test_the_opcode_table_is_complete(disasm):
    assert len(disasm.OPCODES) == 256
    assert disasm.OPCODES[0xA9][:3] == ("LDA", "imm", "R")
    assert disasm.OPCODES[0x20][:2] == ("JSR", "abs")
    assert disasm.OPCODES[0xA7] == ("LAX", "zp", "R", True)
    assert disasm.instruction_cycles(0x9D) == 5  # STA abs,X
    assert disasm.instruction_cycles(0xFE) == 7  # INC abs,X


def test_a_program_is_followed_through_calls_branches_and_interrupts(disasm):
    events = run(disasm, PROGRAM)

    assert [event.texts[0] for event in events] == [
        "RESET → $C000",
        "$C000  LDA #$05",
        "$C002  JSR $C010",
        "$C010  INX",
        "$C011  RTS",
        "$C005  BNE $C000",
        "$C000  LDA #$05",
        "IRQ → $FF48",
        "$FF48  SEI",
        "$FF49  RTI",
        "$C002  JSR $C010",
        "$C010  INX",
        "$C011  RTS",
        "$C005  BNE $C000",
    ]
    # An instruction spans from its opcode fetch to the next fetch and keeps the fetch sample.
    jsr = events[2]
    assert (jsr.ss, jsr.es, jsr.sample, jsr.address) == (40, 100, 47, 0xC002)
    assert jsr.texts[1:] == ["JSR $C010", "JSR"]


def test_it_synchronises_in_the_middle_of_a_program(disasm):
    events = run(disasm, PROGRAM[3:])  # starts with the operand of LDA

    texts = [event.texts[0] for event in events]
    assert texts[0] == "$C002  JSR $C010"
    assert "IRQ → $FF48" in texts


def test_undocumented_opcodes_are_marked(disasm):
    steps = [
        (0xFFFC, 0x00, R), (0xFFFD, 0xC0, R),
        (0xC000, 0xA7, R), (0xC001, 0x10, R), (0x0010, 0x55, R),            # LAX $10
        (0xC002, 0xEA, R), (0xC003, 0xEA, R),                               # NOP
        (0xC003, 0xEA, R), (0xC004, 0xEA, R),                               # NOP
        (0xC004, 0xEA, R), (0xC005, 0xEA, R),                               # NOP
        (0xC005, 0xEA, R), (0xC006, 0xEA, R),                               # NOP
    ]
    texts = [event.texts[0] for event in run(disasm, steps)]
    assert texts[:3] == ["RESET → $C000", "$C000  LAX $10  (undocumented)", "$C002  NOP"]
