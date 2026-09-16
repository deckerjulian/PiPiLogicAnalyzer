"""Simulated capture: parity with the firmware generator and decodability."""

from __future__ import annotations

import os
import shutil
import subprocess

import numpy as np
import pytest

from pipilogicanalyzer.core import simulation
from pipilogicanalyzer.core.simulation import MESSAGE, SimulationPattern
from pipilogicanalyzer.driver.models import AnalyzerChannel, CaptureSession, TriggerType
from pipilogicanalyzer.sigrok.engine import PROJECT_DIRECTORY

FIRMWARE = os.path.join(PROJECT_DIRECTORY, "firmware", "PiPiLogicAnalyzer")

HARNESS = r"""
#include <stdio.h>
#include <stdlib.h>
#include "PiPiLogicAnalyzer_Simulation.h"

int main(int argc, char** argv)
{
    if(argc != 4)
        return 2;
    int pattern = atoi(argv[1]);
    int channels = atoi(argv[2]);
    int count = atoi(argv[3]);
    for(int index = 0; index < count; index++)
        printf("%u\n", simulation_sample((uint8_t)pattern, (uint8_t)channels, (uint32_t)index));
    return 0;
}
"""


@pytest.fixture(scope="module")
def firmware_generator(tmp_path_factory):
    compiler = shutil.which("cc") or shutil.which("clang") or shutil.which("gcc")
    if compiler is None:
        pytest.skip("no C compiler available")

    directory = tmp_path_factory.mktemp("simulation")
    source = directory / "harness.c"
    source.write_text(HARNESS)
    binary = directory / "harness"
    subprocess.run(
        [
            compiler, "-std=c11", "-Wall", "-Wextra", "-Werror", "-I", FIRMWARE,
            str(source), os.path.join(FIRMWARE, "PiPiLogicAnalyzer_Simulation.c"), "-o", str(binary),
        ],
        check=True,
        capture_output=True,
    )

    def run(pattern: int, channels: int, count: int) -> np.ndarray:
        output = subprocess.run(
            [str(binary), str(pattern), str(channels), str(count)],
            check=True, capture_output=True, text=True,
        ).stdout
        return np.array([int(value) for value in output.split()], dtype=np.uint64)

    return run


@pytest.mark.parametrize("pattern", list(SimulationPattern))
@pytest.mark.parametrize("channels", [1, 5, 6, 8, 17, 32])
def test_python_generator_matches_the_firmware(firmware_generator, pattern, channels):
    count = 12_000  # several periods of every protocol frame
    expected = firmware_generator(int(pattern), channels, count)
    assert np.array_equal(simulation.sample_masks(pattern, channels, count), expected)


def test_counter_and_walking_one():
    counter = simulation.generate(SimulationPattern.COUNTER, 3, 8)
    assert counter[0].tolist() == [0, 1, 0, 1, 0, 1, 0, 1]
    assert counter[2].tolist() == [0, 0, 0, 0, 1, 1, 1, 1]

    walking = simulation.generate(SimulationPattern.WALKING_ONE, 2, 48)
    assert walking[0][:16].all() and not walking[1][:16].any()
    assert walking[1][16:32].all() and walking[0][32:].all()


def test_decoders_are_only_proposed_for_available_channels(decoder_registry):
    assert simulation.decoder_configuration(SimulationPattern.COUNTER, 8, 1_000_000, decoder_registry) == []


REAL_DECODERS = all(
    os.path.isfile(os.path.join(PROJECT_DIRECTORY, "decoders", name, "pd.py"))
    for name in ("uart", "spi", "i2c")
)


@pytest.mark.skipif(not REAL_DECODERS, reason="the sigrok decoders are not installed in ./decoders")
def test_protocol_pattern_is_decoded_by_the_sigrok_decoders():
    from pipilogicanalyzer.sigrok.engine import DecoderRegistry
    from pipilogicanalyzer.sigrok.provider import SigrokProvider

    registry = DecoderRegistry()
    registry.load()
    frequency, channels, count = 1_000_000, 8, 20_000

    session = CaptureSession(
        frequency=frequency,
        pre_trigger_samples=0,
        post_trigger_samples=count,
        trigger_type=TriggerType.SIMULATION,
        trigger_pattern=int(SimulationPattern.PROTOCOLS),
    )
    session.capture_channels = [
        AnalyzerChannel(channel_number=index, samples=samples)
        for index, samples in enumerate(simulation.generate(SimulationPattern.PROTOCOLS, channels, count))
    ]

    provider = SigrokProvider(registry)
    provider.load_configuration(
        simulation.decoder_configuration(SimulationPattern.PROTOCOLS, channels, frequency, registry)
    )
    groups = {group.instance.decoder_id: group for group in provider.run(session)}
    assert set(groups) == {"uart", "spi", "i2c"}

    def values(decoder_id: str, annotation_id: str) -> list[str]:
        info = registry.get(decoder_id)
        type_id = [annotation[0] for annotation in info.annotations].index(annotation_id)
        group = groups[decoder_id]
        assert group.error is None, group.error
        segments = [
            segment
            for row in group.annotations
            for segment in row.segments
            if segment.type_id == type_id
        ]
        return [segment.values[-1] for segment in sorted(segments, key=lambda item: item.first_sample)]

    hex_message = " ".join(f"{byte:02X}" for byte in MESSAGE)
    assert "PiPiLogicAnalyzer" in "".join(values("uart", "rx-data"))
    assert hex_message in " ".join(values("spi", "mosi-data"))
    assert hex_message in " ".join(values("i2c", "data-write"))
