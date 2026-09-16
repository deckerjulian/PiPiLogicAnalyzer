#!/usr/bin/env python3
# Copyright (C) 2026 Julian Decker
#
# Part of PiPiLogicAnalyzer.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Generate ``examples/c64-demo.lac``: a Commodore 64 running a small program.

The capture uses the channels of ``examples/c64-expansion-port-profile.json`` (board B: clock
and control lines, board A: address and data bus, the A0 reference line) at 20 MHz. A
cycle-accurate model of the 6510 produces the bus, with the timing of a real C64: the address
changes 2 samples after the falling Φ2 edge, the memory drives the data in the second half of
the high phase and holds it one sample past the falling edge.

The program starts from a reset, writes "C64!!" to the screen, increments the border colour in
an endless loop and serves two CIA timer interrupts::

    python tools/make_c64_demo.py
    pipilogicanalyzer examples/c64-demo.lac     # then add the C64 bus decoder
"""

from __future__ import annotations

import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pipilogicanalyzer.core import capture_io  # noqa: E402
from pipilogicanalyzer.core.profiles import read_profiles_file  # noqa: E402
from pipilogicanalyzer.core.regions import SampleRegion  # noqa: E402
from pipilogicanalyzer.driver.models import AnalyzerChannel  # noqa: E402

CYCLE = 20  # samples per CPU cycle at 20 MHz (the C64 clock runs at about 1 MHz)
HALF = CYCLE // 2
ADDRESS_DELAY = 2  # address and R/W change after the falling Φ2 edge
READ_DATA_VALID = HALF + 3  # the memory drives the data late in the high phase
WRITE_DATA_VALID = HALF + 2
RESET_CYCLES = 50  # /RESET held low before the capture triggers (1,000 pre-trigger samples)
PROGRAM_CYCLES = 1500  # 30,000 post-trigger samples, as in the profile
IRQ_CYCLES = (400, 1000)  # cycles at which the CIA pulls /IRQ low
IRQ_REGION_CYCLES = 24  # interrupt sequence and handler

PROGRAM = {
    0xC000: [0x58],  # CLI
    0xC001: [0xA2, 0x00],  # LDX #$00
    0xC003: [0xBD, 0x20, 0xC0],  # LDA $C020,X
    0xC006: [0x9D, 0x00, 0x04],  # STA $0400,X
    0xC009: [0xE8],  # INX
    0xC00A: [0xE0, 0x05],  # CPX #$05
    0xC00C: [0xD0, 0xF5],  # BNE $C003
    0xC00E: [0xEE, 0x20, 0xD0],  # INC $D020
    0xC011: [0x4C, 0x01, 0xC0],  # JMP $C001
    0xC020: [0x03, 0x36, 0x34, 0x21, 0x21],  # "C64!!" in screen codes
    0xEA31: [0x48],  # PHA
    0xEA32: [0xAD, 0x0D, 0xDC],  # LDA $DC0D (reading the CIA acknowledges the IRQ)
    0xEA35: [0x68],  # PLA
    0xEA36: [0x40],  # RTI
    0xFFFC: [0x00, 0xC0],  # reset vector
    0xFFFE: [0x31, 0xEA],  # IRQ vector
}


class Cpu6510:
    """Just enough of the 6510 for the demo program, cycle by cycle."""

    def __init__(self, memory: bytearray) -> None:
        self.memory = memory
        self.cycles: list[tuple[int, int, bool, bool]] = []  # address, data, read, /IRQ active
        self.pc = 0xFFFF
        self.a = self.x = 0
        self.s = 0x00
        self.interrupts_disabled = True
        self.zero = False
        self.irq = False

    def read(self, address: int) -> int:
        address &= 0xFFFF
        data = self.memory[address]
        if address == 0xDC0D:
            data = 0x81 if self.irq else 0x00
        self.cycles.append((address, data, True, self.irq))
        if address == 0xDC0D:
            self.irq = False
        return data

    def write(self, address: int, data: int) -> None:
        address &= 0xFFFF
        self.memory[address] = data & 0xFF
        self.cycles.append((address, data & 0xFF, False, self.irq))

    def push(self, data: int) -> None:
        self.write(0x0100 + self.s, data)
        self.s = (self.s - 1) & 0xFF

    def fetch(self) -> int:
        value = self.read(self.pc)
        self.pc = (self.pc + 1) & 0xFFFF
        return value

    def reset(self) -> None:
        self.read(self.pc)
        self.read(self.pc)
        for _ in range(3):
            self.read(0x0100 + self.s)
            self.s = (self.s - 1) & 0xFF
        low = self.read(0xFFFC)
        high = self.read(0xFFFD)
        self.pc = low | (high << 8)
        self.interrupts_disabled = True

    def step(self) -> None:
        if self.irq and not self.interrupts_disabled:
            self.read(self.pc)
            self.read(self.pc)
            self.push(self.pc >> 8)
            self.push(self.pc & 0xFF)
            self.push(0x20 | (0x02 if self.zero else 0x00))
            self.interrupts_disabled = True
            low = self.read(0xFFFE)
            high = self.read(0xFFFF)
            self.pc = low | (high << 8)
            return

        address = self.pc
        opcode = self.fetch()
        if opcode == 0x58:  # CLI
            self.read(self.pc)
            self.interrupts_disabled = False
        elif opcode == 0xA2:  # LDX #
            self.x = self.fetch()
            self.zero = self.x == 0
        elif opcode == 0xE0:  # CPX #
            self.zero = self.x == self.fetch()
        elif opcode == 0xBD:  # LDA abs,X (no page crossing in this program)
            base = self.fetch() | (self.fetch() << 8)
            self.a = self.read(base + self.x)
            self.zero = self.a == 0
        elif opcode == 0x9D:  # STA abs,X
            base = self.fetch() | (self.fetch() << 8)
            self.read((base & 0xFF00) | ((base + self.x) & 0xFF))
            self.write(base + self.x, self.a)
        elif opcode == 0xE8:  # INX
            self.read(self.pc)
            self.x = (self.x + 1) & 0xFF
            self.zero = self.x == 0
        elif opcode == 0xD0:  # BNE
            offset = self.fetch()
            if not self.zero:
                self.read(self.pc)
                target = (self.pc + (offset - 256 if offset & 0x80 else offset)) & 0xFFFF
                if target >> 8 != self.pc >> 8:
                    self.read((self.pc & 0xFF00) | (target & 0xFF))
                self.pc = target
        elif opcode == 0xEE:  # INC abs
            target = self.fetch() | (self.fetch() << 8)
            value = self.read(target)
            self.write(target, value)
            value = (value + 1) & 0xFF
            self.write(target, value)
            self.zero = value == 0
        elif opcode == 0x4C:  # JMP abs
            self.pc = self.fetch() | (self.fetch() << 8)
        elif opcode == 0x48:  # PHA
            self.read(self.pc)
            self.push(self.a)
        elif opcode == 0xAD:  # LDA abs
            self.a = self.read(self.fetch() | (self.fetch() << 8))
            self.zero = self.a == 0
        elif opcode == 0x68:  # PLA
            self.read(self.pc)
            self.read(0x0100 + self.s)
            self.s = (self.s + 1) & 0xFF
            self.a = self.read(0x0100 + self.s)
            self.zero = self.a == 0
        elif opcode == 0x40:  # RTI
            self.read(self.pc)
            self.read(0x0100 + self.s)
            self.s = (self.s + 1) & 0xFF
            status = self.read(0x0100 + self.s)
            self.s = (self.s + 1) & 0xFF
            low = self.read(0x0100 + self.s)
            self.s = (self.s + 1) & 0xFF
            high = self.read(0x0100 + self.s)
            self.interrupts_disabled = bool(status & 0x04)
            self.zero = bool(status & 0x02)
            self.pc = low | (high << 8)
        else:
            raise ValueError(f"opcode ${opcode:02X} at ${address:04X} is not modelled")


def run_program() -> list[tuple[int, int, bool, bool]]:
    memory = bytearray(0x10000)
    for address, data in PROGRAM.items():
        memory[address:address + len(data)] = bytes(data)

    cpu = Cpu6510(memory)
    cpu.reset()
    pending = list(IRQ_CYCLES)
    while len(cpu.cycles) < PROGRAM_CYCLES:
        if pending and len(cpu.cycles) >= pending[0]:
            pending.pop(0)
            cpu.irq = True
        cpu.step()
    return cpu.cycles[:PROGRAM_CYCLES]


def build_signals(program: list[tuple[int, int, bool, bool]]) -> dict[str, np.ndarray]:
    # While /RESET is low the CPU keeps reading $FFFF.
    cycles = [(0xFFFF, 0xFF, True, False)] * RESET_CYCLES + program
    count = len(cycles) * CYCLE
    positions = np.arange(count)

    clock = ((positions % CYCLE) >= HALF).astype(np.uint8)  # each cycle starts with Φ2 falling
    address = np.full(count, 0xFFFF, dtype=np.int64)
    data = np.full(count, 0xFF, dtype=np.int64)
    read = np.ones(count, dtype=np.uint8)
    irq = np.ones(count, dtype=np.uint8)

    for index, (cycle_address, cycle_data, is_read, irq_active) in enumerate(cycles):
        start = index * CYCLE
        address[start + ADDRESS_DELAY:start + CYCLE + ADDRESS_DELAY] = cycle_address
        read[start + ADDRESS_DELAY:start + CYCLE + ADDRESS_DELAY] = 1 if is_read else 0
        irq[start + ADDRESS_DELAY:start + CYCLE + ADDRESS_DELAY] = 0 if irq_active else 1
        valid = start + (READ_DATA_VALID if is_read else WRITE_DATA_VALID)
        # The value stays on the bus (hold time, bus capacitance) until the next one is driven.
        data[valid:] = cycle_data

    signals = {f"A{bit}": ((address >> bit) & 1).astype(np.uint8) for bit in range(16)}
    signals.update({f"D{bit}": ((data >> bit) & 1).astype(np.uint8) for bit in range(8)})
    reset = np.ones(count, dtype=np.uint8)
    reset[:RESET_CYCLES * CYCLE] = 0
    high = np.ones(count, dtype=np.uint8)
    signals.update(
        {
            "Φ2": clock,
            "R/W": read,
            "/RESET": reset,
            "/IRQ": irq,
            "/NMI": high,
            "/ROML": high,
            "/ROMH": high,
            "/IO1": high,
            "/IO2": high,
            "BA": high,
            "/DMA": high,
            "/EXROM": high,
            "/GAME": high,
            # The dot clock runs at 8 times the CPU clock, far too fast for a clean capture.
            "Dot Clock": (((positions * 16) // CYCLE) % 2).astype(np.uint8),
        }
    )
    return signals


def main() -> int:
    profile = read_profiles_file(os.path.join(ROOT, "examples", "c64-expansion-port-profile.json"))[0]
    session = profile.capture_settings.clone_settings()
    program = run_program()
    signals = build_signals(program)

    channels = []
    for channel in session.capture_channels:
        name = channel.channel_name.split(" (", 1)[0]
        source = "A0" if channel.channel_name.endswith(" ref") else name
        channels.append(
            AnalyzerChannel(
                channel_number=channel.channel_number,
                channel_name=channel.channel_name,
                channel_color=channel.channel_color,
                samples=signals[source].copy(),
            )
        )
    session.capture_channels = channels
    session.pre_trigger_samples = RESET_CYCLES * CYCLE
    session.post_trigger_samples = PROGRAM_CYCLES * CYCLE

    regions = [
        SampleRegion(
            first_sample=RESET_CYCLES * CYCLE,
            last_sample=(RESET_CYCLES + 9) * CYCLE - 1,
            region_name="Reset",
        )
    ]
    for index, (address, _data, is_read, _irq) in enumerate(program):
        if is_read and address == 0xFFFE and program[index - 5][0] != 0xFFFE:
            first = RESET_CYCLES + index - 5
            regions.append(
                SampleRegion(
                    first_sample=first * CYCLE,
                    last_sample=(first + IRQ_REGION_CYCLES) * CYCLE - 1,
                    region_name="IRQ",
                )
            )

    path = os.path.join(ROOT, "examples", "c64-demo.lac")
    capture_io.save_capture(path, session, regions)
    print(f"wrote {path} ({session.pre_trigger_samples + session.post_trigger_samples} samples, "
          f"{len(channels)} channels, {len(regions)} regions)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
