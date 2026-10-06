#!/usr/bin/env python3
# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Generate ``examples/c64-demo.lac``: a Commodore 64 running a small program.

The capture uses the channels of ``examples/profiles/c64-expansion-port.json`` (board B: clock
and control lines, board A: address and data bus, the A0 reference line) at 20 MHz. A
cycle-accurate model of the 6510 produces the bus, with the timing of a real C64: the address
changes 2 samples after the falling Φ2 edge, the memory drives the data in the second half of
the high phase and holds it one sample past the falling edge.

The program starts from a reset, writes "C64!!" to the screen, increments the border colour in
an endless loop and serves two CIA timer interrupts::

    python tools/make_c64_demo.py
    openscilab examples/c64-demo.lac     # then add the C64 bus decoder
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from openscilab.core import capture_io  # noqa: E402
from openscilab.core.profiles import read_profiles_file  # noqa: E402
from openscilab.core.regions import SampleRegion  # noqa: E402
from openscilab.driver.models import AnalyzerChannel  # noqa: E402

from openscilab.core.c64_model import (  # noqa: E402
    CYCLE,
    IRQ_REGION_CYCLES,
    PROGRAM_CYCLES,
    RESET_CYCLES,
    build_signals,
    run_program,
)


def main() -> int:
    profile = read_profiles_file(os.path.join(ROOT, "examples", "profiles", "c64-expansion-port.json"))[0]
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
