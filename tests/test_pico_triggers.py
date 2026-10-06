"""Trigger sequences and state mode of the PiPiLogicAnalyzer firmware (trigger type 7, command 10).

The driver is exercised against a fake transport; the evaluation of the firmware
(``sequence.c``) is compiled on the computer and compared with a reference
implementation of the semantics in ``openscilab/driver/models.py``.
"""

from __future__ import annotations

import os
import random
import shutil
import struct
import subprocess
import threading
from typing import Optional

import numpy as np
import pytest

from openscilab.driver.base import (
    ACQUISITION_STREAM,
    CAPABILITY_STATE_MODE,
    CAPABILITY_TRIGGER_SEQUENCE,
    CaptureError,
    CaptureMode,
)
from openscilab.driver.models import (
    AnalyzerChannel,
    CaptureSession,
    ConditionKind,
    EdgeKind,
    TriggerCondition,
    TriggerSequence,
    TriggerStage,
    TriggerType,
)
from openscilab.driver.pico import analyzer, protocol
from openscilab.driver.pico.analyzer import PicoDriver
from openscilab.driver.pico.protocol import (
    SEQUENCE_NO_LIMIT,
    SequenceRequest,
    SequenceStage,
)
from openscilab.sigrok.engine import PROJECT_DIRECTORY

from test_driver import FakeTransport, build_capture_payload

FIRMWARE = os.path.join(PROJECT_DIRECTORY, "firmware", "pico")

NEW_CAPS = (
    "CAPS:SELFTEST,SIMULATION,DEVICEINFO,STREAM=800000,EDGE_TRIGGER_OUT,PATTERN_GROUPS=0-20/21-23,"
    "TRIGGER_SEQUENCE=8,TRIGGER_CONDITIONS=pattern/edge/pulse/gap,SEQUENCE_MAX_RATE=2500000,"
    "STATE_MODE,STATE_MAX_CLOCK=25000000"
)
#: The released firmware before trigger sequences
OLD_CAPS = "CAPS:SELFTEST,SIMULATION,DEVICEINFO,STREAM=800000,EDGE_TRIGGER_OUT,PATTERN_GROUPS=0-20/21-23"


def make_driver(monkeypatch, caps: Optional[str], version: str = "PIPI_LOGIC_ANALYZER_PICO_V7_1"):
    transport = FakeTransport()
    transport._responses[0] = version
    monkeypatch.setattr("openscilab.driver.pico.analyzer.SerialTransport", lambda *args, **kwargs: transport)
    driver = PicoDriver("/dev/fake")
    transport.queue_response(caps if caps is not None else "ERR_UNKNOWN_MSG")
    driver.capabilities()
    transport.written.clear()
    driver.test_transport = transport  # type: ignore[attr-defined]
    return driver


@pytest.fixture
def pico(monkeypatch):
    return make_driver(monkeypatch, NEW_CAPS)


@pytest.fixture
def old_pico(monkeypatch):
    return make_driver(monkeypatch, OLD_CAPS)


def sequence_session(stages, channels=(0, 1, 2, 3), pre=100, post=400, rate=1_000_000) -> CaptureSession:
    session = CaptureSession(frequency=rate, pre_trigger_samples=pre, post_trigger_samples=post)
    session.capture_channels = [AnalyzerChannel(channel_number=number) for number in channels]
    session.trigger_type = TriggerType.SEQUENCE
    session.trigger_sequence = TriggerSequence(list(stages))
    return session


def edge(channel=0, kind=EdgeKind.RISING, count=1, within_ns=None) -> TriggerStage:
    return TriggerStage(TriggerCondition(kind=ConditionKind.EDGE, channel=channel, edge=kind), count, within_ns)


def run_capture(driver, session):
    done = threading.Event()
    results = []
    error = driver.start_capture(session, lambda args: (results.append(args), done.set()))
    assert error is CaptureError.NONE
    assert done.wait(5), "the capture did not end"
    return results[0]


# ------------------------------------------------------------------ protocol
def test_the_sequence_request_is_packed_byte_for_byte():
    request = SequenceRequest(
        stages=[
            SequenceStage(kind=0, mask=0x00F0_000F, value=0x0000_0005, count=2),
            SequenceStage(kind=2, channel=23, edge=1, min_samples=10, max_samples=0x1234, within_samples=99),
        ],
        state_mode=True,
        clock_channel=5,
        clock_falling=True,
    )
    expected = bytes([1, 0x03, 5, 2])  # version, state mode | falling clock, clock channel, stages
    expected += bytes([0, 0, 0, 0]) + bytes.fromhex("0f00f000" "05000000" "00000000" "ffffffff" "02000000" "ffffffff")
    expected += bytes([2, 23, 1, 0]) + bytes.fromhex("00000000" "00000000" "0a000000" "34120000" "01000000" "63000000")
    assert request.pack() == expected
    assert protocol.SEQUENCE_STAGE_SIZE == 28
    # Escaped and framed like every command
    packet = protocol.command_packet(protocol.CMD_TRIGGER_SEQUENCE, request.pack())
    assert packet[:3] == b"\x55\xaa\x0a" and packet[-2:] == b"\xaa\x55"


def test_timing_mode_sends_no_clock_channel():
    request = SequenceRequest(stages=[SequenceStage(kind=1, channel=3)], state_mode=False, clock_channel=9)
    assert request.pack()[:4] == bytes([1, 0, 0, 1])


def test_the_longest_sequence_fits_into_the_firmware_receive_buffer():
    # Every byte escaped: start, command, payload, stop must stay within MESSAGE_BUFFER_SIZE (512)
    stages = [SequenceStage(kind=0, mask=0xAAAAAAAA, value=0x55555555, count=0xF0F0F0F0) for _ in range(8)]
    payload = SequenceRequest(stages=stages).pack()
    assert 2 + 2 * (1 + len(payload)) + 2 <= 512
    with open(os.path.join(FIRMWARE, "proto.h")) as source:
        assert "#define PROTO_FRAME_MAX 512" in source.read()


def test_nanoseconds_become_samples_without_float_rounding():
    assert protocol.ns_to_samples(1000, 1_000_000, False) == 1
    assert protocol.ns_to_samples(1500, 1_000_000, False) == 1
    assert protocol.ns_to_samples(1500, 1_000_000, True) == 2
    assert protocol.ns_to_samples(1000, 3_000_000, True) == 3
    assert protocol.ns_to_samples(10**12, 100_000_000, False) == 10**11


# -------------------------------------------------------------- capabilities
def test_the_new_firmware_reports_sequences_and_the_state_mode(pico):
    assert pico.trigger_sequence_limits() == (8, frozenset({"pattern", "edge", "pulse", "gap"}), 2_500_000)
    assert pico.supports_state_mode()
    assert pico.state_max_clock == 25_000_000
    assert pico.state_clock_channels() == list(range(24))
    assert CAPABILITY_TRIGGER_SEQUENCE in pico.capabilities()
    assert CAPABILITY_STATE_MODE in pico.capabilities()


def test_capability_variants(monkeypatch):
    # No rate reported: limited by the maximum frequency; unknown kinds are ignored
    driver = make_driver(monkeypatch, "CAPS:TRIGGER_SEQUENCE=4,TRIGGER_CONDITIONS=edge/pattern/glitch")
    assert driver.trigger_sequence_limits() == (4, frozenset({"edge", "pattern"}), driver.max_frequency)
    assert not driver.supports_state_mode() and driver.state_max_clock == 0
    # A rate above the maximum frequency is capped
    driver = make_driver(monkeypatch, "CAPS:TRIGGER_SEQUENCE=2,TRIGGER_CONDITIONS=edge,SEQUENCE_MAX_RATE=900000000")
    assert driver.trigger_sequence_limits() == (2, frozenset({"edge"}), driver.max_frequency)
    # Broken values are no sequences
    for caps in ("CAPS:TRIGGER_SEQUENCE=x,TRIGGER_CONDITIONS=edge", "CAPS:TRIGGER_SEQUENCE=3", "CAPS:TRIGGER_SEQUENCE"):
        assert make_driver(monkeypatch, caps).trigger_sequence_limits() is None


@pytest.mark.parametrize("caps", [OLD_CAPS, None], ids=["V7_1 before sequences", "original firmware"])
def test_older_firmware_refuses_both_features(monkeypatch, caps):
    driver = make_driver(monkeypatch, caps)
    transport = driver.test_transport
    assert driver.trigger_sequence_limits() is None
    assert not driver.supports_state_mode()
    assert driver.state_clock_channels() == []

    assert driver.start_capture(sequence_session([edge()])) is CaptureError.UNSUPPORTED
    session = sequence_session([])
    session.trigger_type = TriggerType.EDGE
    session.clock_channel = 4
    assert driver.start_capture(session) is CaptureError.UNSUPPORTED
    assert transport.written == bytearray()  # nothing was sent


def test_older_firmware_keeps_the_normal_captures(old_pico):
    session = sequence_session([], pre=2, post=6)
    session.trigger_type = TriggerType.EDGE
    old_pico.test_transport.queue_response("CAPTURE_STARTED")
    old_pico.test_transport.queue_response("CAPTURE_DATA")
    old_pico.test_transport.queue_data(build_capture_payload([1, 2, 3, 4, 5, 6, 7, 8], CaptureMode.CHANNELS_8))
    result = run_capture(old_pico, session)
    assert result.success
    assert bytes(old_pico.test_transport.written[:3]) == b"\x55\xaa\x01"  # only the capture request


# ---------------------------------------------------------------- validation
def pulse(channel=0, kind=EdgeKind.RISING, min_ns=None, max_ns=None) -> TriggerStage:
    return TriggerStage(TriggerCondition(kind=ConditionKind.PULSE, channel=channel, edge=kind, min_ns=min_ns, max_ns=max_ns))


def gap(channel=0, min_ns=None) -> TriggerStage:
    return TriggerStage(TriggerCondition(kind=ConditionKind.GAP, channel=channel, min_ns=min_ns))


def pattern(mask, value) -> TriggerStage:
    return TriggerStage(TriggerCondition(kind=ConditionKind.PATTERN, mask=mask, value=value))


@pytest.mark.parametrize(
    "change",
    [
        lambda s: setattr(s, "trigger_sequence", TriggerSequence([edge()] * 9)),  # more stages than reported
        lambda s: setattr(s, "trigger_sequence", TriggerSequence([])),  # no stage
        lambda s: setattr(s, "trigger_sequence", None),
        lambda s: setattr(s, "frequency", 2_500_001),  # faster than the evaluation
        lambda s: setattr(s, "frequency", 100),  # slower than the PIO divider allows
        lambda s: setattr(s, "loop_count", 1),
        lambda s: setattr(s, "post_trigger_samples", 0),
        lambda s: setattr(s, "pre_trigger_samples", 200_000),
        lambda s: setattr(s, "trigger_sequence", TriggerSequence([edge(channel=24)])),  # external trigger input
        lambda s: setattr(s, "trigger_sequence", TriggerSequence([edge(channel=-1)])),
        lambda s: setattr(s, "trigger_sequence", TriggerSequence([pattern(1 << 24, 0)])),
        lambda s: setattr(s, "trigger_sequence", TriggerSequence([edge(count=0)])),
        lambda s: setattr(s, "trigger_sequence", TriggerSequence([gap(min_ns=None)])),
        lambda s: setattr(s, "trigger_sequence", TriggerSequence([gap(min_ns=0)])),
        lambda s: setattr(s, "trigger_sequence", TriggerSequence([pulse(min_ns=5000, max_ns=1000)])),
        lambda s: setattr(s, "trigger_sequence", TriggerSequence([pulse(min_ns=-1)])),
        lambda s: setattr(s, "trigger_sequence", TriggerSequence([gap(min_ns=10**13)])),  # beyond 2^31 samples
        lambda s: setattr(s, "trigger_sequence", TriggerSequence([edge(), edge(within_ns=-5)])),
        lambda s: s.capture_channels.append(AnalyzerChannel(channel_number=24)),
    ],
)
def test_invalid_sequences_are_bad_parameters(pico, change):
    session = sequence_session([edge(), pulse(min_ns=1000)])
    change(session)
    assert pico.start_capture(session) is CaptureError.BAD_PARAMS
    assert pico.test_transport.written == bytearray()


def test_condition_kinds_the_firmware_lacks_are_unsupported(monkeypatch):
    driver = make_driver(monkeypatch, "CAPS:TRIGGER_SEQUENCE=4,TRIGGER_CONDITIONS=pattern/edge,SEQUENCE_MAX_RATE=1000000")
    assert driver.start_capture(sequence_session([edge(), gap(min_ns=1000)])) is CaptureError.UNSUPPORTED
    assert driver.start_capture(sequence_session([pulse(min_ns=1)])) is CaptureError.UNSUPPORTED


def test_the_device_refusing_the_sequence_is_a_hardware_error(pico):
    pico.test_transport.queue_response("SEQUENCE_ERROR")
    assert pico.start_capture(sequence_session([edge()])) is CaptureError.HARDWARE_ERROR
    pico.test_transport.queue_response("SEQUENCE_OK")
    pico.test_transport.queue_response("CAPTURE_ERROR")
    assert pico.start_capture(sequence_session([edge()])) is CaptureError.HARDWARE_ERROR
    assert not pico.is_capturing


# ------------------------------------------------------------------ captures
def expected_packets(driver, sequence: SequenceRequest, request: protocol.CaptureRequest) -> bytes:
    return protocol.command_packet(protocol.CMD_TRIGGER_SEQUENCE, sequence.pack()) + protocol.command_packet(
        protocol.CMD_START_CAPTURE, request.pack(driver.request_layout)
    )


def test_a_sequence_capture_sends_the_stages_and_reads_the_samples(pico):
    stages = [
        pattern(0b0101, 0b0001),
        TriggerStage(TriggerCondition(kind=ConditionKind.EDGE, channel=2, edge=EdgeKind.FALLING), count=3, within_ns=50_000),
        pulse(channel=1, kind=EdgeKind.ANY, min_ns=1500, max_ns=2500),
        gap(channel=3, min_ns=10_500),
    ]
    session = sequence_session(stages, pre=100, post=400, rate=1_000_000)
    transport = pico.test_transport
    transport.queue_response("SEQUENCE_OK")
    transport.queue_response("CAPTURE_STARTED")
    transport.queue_response("CAPTURE_DATA")
    words = [(index * 7) & 0x0F for index in range(500)]
    transport.queue_data(build_capture_payload(words, CaptureMode.CHANNELS_8))

    result = run_capture(pico, session)
    assert result.success and result.error is None

    sequence = SequenceRequest(stages=[
        SequenceStage(kind=0, mask=0b0101, value=0b0001),
        SequenceStage(kind=1, channel=2, edge=1, count=3, within_samples=50),
        SequenceStage(kind=2, channel=1, edge=2, min_samples=1, max_samples=3),  # 1.5 µs -> 1, 2.5 µs -> 3
        SequenceStage(kind=3, channel=3, min_samples=11),  # 10.5 µs -> 11 samples
    ])
    request = protocol.CaptureRequest(
        trigger_type=7, channels=[0, 1, 2, 3], channel_count=4, frequency=1_000_000,
        pre_samples=100, post_samples=400, capture_mode=0,
    )
    assert bytes(transport.written) == expected_packets(pico, sequence, request)
    assert len(pico.request_layout.format) and pico.request_layout.size == 56

    samples = result.session.capture_channels
    assert np.array_equal(samples[2].samples, (np.array(words) >> 2) & 1)
    assert result.session.pre_trigger_samples == 100 and result.session.post_trigger_samples == 400
    assert result.session.bursts is None


def test_stage_channels_widen_the_capture_mode(pico):
    session = sequence_session([edge(channel=12)], channels=(0, 1), pre=10, post=20)
    transport = pico.test_transport
    transport.queue_response("SEQUENCE_OK")
    transport.queue_response("CAPTURE_STARTED")
    transport.queue_response("CAPTURE_DATA")
    transport.queue_data(build_capture_payload([3, 1] * 15, CaptureMode.CHANNELS_16))
    result = run_capture(pico, session)
    assert result.success
    assert np.array_equal(result.session.capture_channels[1].samples, [1, 0] * 15)
    request = protocol.CaptureRequest(
        trigger_type=7, channels=[0, 1], channel_count=2, frequency=1_000_000,
        pre_samples=10, post_samples=20, capture_mode=int(CaptureMode.CHANNELS_16),
    )
    assert bytes(transport.written).endswith(protocol.command_packet(protocol.CMD_START_CAPTURE, request.pack(pico.request_layout)))


def test_a_capture_without_samples_reports_the_overflow(pico):
    transport = pico.test_transport
    transport.queue_response("SEQUENCE_OK")
    transport.queue_response("CAPTURE_STARTED")
    transport.queue_response("CAPTURE_DATA")
    transport.queue_data(struct.pack("<I", 0) + bytes([0]))
    done = threading.Event()
    results = []
    assert pico.start_capture(sequence_session([edge()]), lambda args: (results.append(args), done.set())) is CaptureError.NONE
    assert done.wait(5)
    assert not results[0].success and results[0].error == analyzer.SEQUENCE_OVERFLOW_ERROR
    assert not pico.is_capturing
    assert transport._data == bytearray()  # the timestamp byte was read as well


@pytest.mark.parametrize(
    "trigger, expected_stages",
    [
        (dict(trigger_type=TriggerType.IMMEDIATE), []),
        (dict(trigger_type=TriggerType.EDGE, trigger_channel=6), [SequenceStage(kind=1, channel=6, edge=0)]),
        (dict(trigger_type=TriggerType.EDGE, trigger_channel=6, trigger_inverted=True), [SequenceStage(kind=1, channel=6, edge=1)]),
        (
            dict(trigger_type=TriggerType.COMPLEX, trigger_channel=4, trigger_bit_count=3, trigger_pattern=0b101),
            [SequenceStage(kind=0, mask=0b111 << 4, value=0b101 << 4)],
        ),
        (
            dict(trigger_type=TriggerType.FAST, trigger_channel=0, trigger_bit_count=2, trigger_pattern=0b110),
            [SequenceStage(kind=0, mask=0b11, value=0b10)],
        ),
        (
            dict(trigger_type=TriggerType.SEQUENCE, trigger_sequence=TriggerSequence([pattern(0b11, 0b01), edge(channel=1, count=4)])),
            [SequenceStage(kind=0, mask=0b11, value=0b01), SequenceStage(kind=1, channel=1, count=4)],
        ),
    ],
)
def test_state_mode_captures(pico, trigger, expected_stages):
    session = sequence_session([], channels=(0, 1, 2, 3, 4, 5, 6), pre=8, post=24)
    for name, value in trigger.items():
        setattr(session, name, value)
    session.clock_channel = 7
    session.clock_edge = EdgeKind.FALLING
    session.frequency = 0  # no meaning in state mode
    transport = pico.test_transport
    transport.queue_response("SEQUENCE_OK")
    transport.queue_response("CAPTURE_STARTED")
    transport.queue_response("CAPTURE_DATA")
    transport.queue_data(build_capture_payload(list(range(32)), CaptureMode.CHANNELS_8))

    result = run_capture(pico, session)
    assert result.success
    assert np.array_equal(result.session.capture_channels[0].samples, np.arange(32) & 1)

    sequence = SequenceRequest(stages=expected_stages, state_mode=True, clock_channel=7, clock_falling=True)
    request = protocol.CaptureRequest(
        trigger_type=7, channels=list(range(7)), channel_count=7, frequency=0, pre_samples=8, post_samples=24,
    )
    assert bytes(transport.written) == expected_packets(pico, sequence, request)


@pytest.mark.parametrize(
    "change",
    [
        lambda s: setattr(s, "trigger_type", TriggerType.BLAST),
        lambda s: setattr(s, "trigger_type", TriggerType.EDGE_OUT),
        lambda s: setattr(s, "trigger_type", TriggerType.SIMULATION),
        lambda s: setattr(s, "trigger_sequence", TriggerSequence([pulse(min_ns=100)])),  # no time base
        lambda s: setattr(s, "trigger_sequence", TriggerSequence([gap(min_ns=100)])),
        lambda s: setattr(s, "trigger_sequence", TriggerSequence([edge(), edge(within_ns=1000)])),
        lambda s: setattr(s, "clock_channel", 24),  # the external trigger input is no channel
        lambda s: setattr(s, "clock_channel", -1),
        lambda s: setattr(s, "clock_edge", EdgeKind.ANY),
        lambda s: setattr(s, "loop_count", 2),
    ],
)
def test_invalid_state_mode_captures(pico, change):
    session = sequence_session([edge()])
    session.clock_channel = 5
    change(session)
    assert pico.start_capture(session) is CaptureError.BAD_PARAMS
    assert pico.test_transport.written == bytearray()


def test_the_state_mode_ignores_the_sequence_rate(pico):
    session = sequence_session([edge()], rate=100_000_000)
    session.clock_channel = 5
    pico.test_transport.queue_response("SEQUENCE_OK")
    pico.test_transport.queue_response("CAPTURE_STARTED")
    pico.test_transport.queue_response("CAPTURE_DATA")
    pico.test_transport.queue_data(build_capture_payload([0] * 500, CaptureMode.CHANNELS_8))
    assert run_capture(pico, session).success


def test_a_stream_has_no_state_mode(pico):
    session = sequence_session([])
    session.trigger_type = TriggerType.IMMEDIATE
    session.acquisition_mode = ACQUISITION_STREAM
    session.clock_channel = 3
    assert pico.start_capture(session) is CaptureError.UNSUPPORTED


def test_the_state_mode_without_firmware_sequences_is_immediate_only(monkeypatch):
    driver = make_driver(monkeypatch, "CAPS:STATE_MODE")
    session = sequence_session([])
    session.trigger_type = TriggerType.EDGE
    session.clock_channel = 3
    assert driver.start_capture(session) is CaptureError.UNSUPPORTED
    session.trigger_type = TriggerType.IMMEDIATE
    driver.test_transport.queue_response("SEQUENCE_OK")
    driver.test_transport.queue_response("CAPTURE_STARTED")
    driver.test_transport.queue_response("CAPTURE_DATA")
    driver.test_transport.queue_data(build_capture_payload([0] * 500, CaptureMode.CHANNELS_8))
    assert run_capture(driver, session).success


def test_firmware_sources_report_the_capabilities():
    with open(os.path.join(FIRMWARE, "main.c")) as source:
        text = source.read()
    # the strings come from capabilities.h (tests/test_protocol_spec.py compiles and parses the line)
    assert ('CAP_TRIGGER_SEQUENCE "=%d," CAP_TRIGGER_CONDITIONS "pattern/edge/pulse/gap," CAP_SEQUENCE_MAX_RATE "%lu,"'
            in text)
    assert "case CMD_SEQUENCE:" in text and "req->triggerType == 7" in text


# ------------------------------------------- the firmware evaluation (C, on the computer)
NO_LIMIT = SEQUENCE_NO_LIMIT

HARNESS = r"""
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "sequence.h"

/* Input per case: ring bytes seed stages samples, the stages (kind edge bit mask value min max
   count within), the samples. The samples are written into a ring buffer in chunks of random
   length, like the DMA does, and evaluated behind them. Output: the trigger sample or -1. */
int main(void)
{
    unsigned ring, bytes, seed, stageCount, count;
    static SEQ_STATE state;
    while(scanf("%u %u %u %u %u", &ring, &bytes, &seed, &stageCount, &count) == 5)
    {
        memset(&state, 0, sizeof(state));
        state.stageCount = (uint8_t)stageCount;
        for(unsigned i = 0; i < stageCount; i++)
        {
            unsigned kind, edge, bit;
            SEQ_STAGE* s = &state.stages[i];
            if(scanf("%u %u %u %x %x %x %x %x %x", &kind, &edge, &bit, &s->mask, &s->value, &s->minSamples,
                     &s->maxSamples, &s->count, &s->withinSamples) != 9)
                return 2;
            s->kind = (uint8_t)kind;
            s->edge = (uint8_t)edge;
            s->bit = (uint8_t)bit;
        }
        uint32_t* samples = malloc(sizeof(uint32_t) * (count + 1));
        for(unsigned i = 0; i < count; i++)
            if(scanf("%x", &samples[i]) != 1)
                return 2;
        if(!seq_validate(state.stages, state.stageCount, (uint8_t)(bytes * 8)))
        {
            printf("invalid\n");
            free(samples);
            continue;
        }
        void* buffer = calloc(ring, 4);
        seq_start(&state);
        unsigned written = 0;
        long long trigger = -1;
        srand(seed);
        while(written < count && trigger < 0)
        {
            unsigned chunk = 1 + (unsigned)rand() % (ring / 2);
            for(unsigned i = 0; i < chunk && written < count; i++, written++)
            {
                if(bytes == 1) ((uint8_t*)buffer)[written % ring] = (uint8_t)samples[written];
                else if(bytes == 2) ((uint16_t*)buffer)[written % ring] = (uint16_t)samples[written];
                else ((uint32_t*)buffer)[written % ring] = samples[written];
            }
            if(seq_scan(&state, buffer, ring, (uint8_t)bytes, written))
                trigger = (long long)state.triggerSample;
        }
        printf("%lld\n", trigger);
        free(buffer);
        free(samples);
    }
    return 0;
}
"""


@pytest.fixture(scope="module")
def firmware_sequence(tmp_path_factory):
    compiler = shutil.which("cc") or shutil.which("clang") or shutil.which("gcc")
    if compiler is None:
        pytest.skip("no C compiler available")
    directory = tmp_path_factory.mktemp("sequence")
    source = directory / "harness.c"
    source.write_text(HARNESS)
    binary = directory / "harness"
    subprocess.run(
        [
            compiler, "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", "-I", FIRMWARE,
            str(source), os.path.join(FIRMWARE, "sequence.c"), "-o", str(binary),
        ],
        check=True,
        capture_output=True,
    )

    def run(cases) -> list[str]:
        lines = []
        for ring, width, seed, stages, samples in cases:
            lines.append(f"{ring} {width} {seed} {len(stages)} {len(samples)}")
            for stage in stages:
                lines.append(" ".join(
                    [str(stage["kind"]), str(stage.get("edge", 0)), str(stage.get("bit", 0))]
                    + [format(stage.get(key, default), "x") for key, default in (
                        ("mask", 0), ("value", 0), ("min", 0), ("max", NO_LIMIT), ("count", 1), ("within", NO_LIMIT)
                    )]
                ))
            lines.append(" ".join(format(value, "x") for value in samples))
        output = subprocess.run([str(binary)], input="\n".join(lines) + "\n", check=True, capture_output=True, text=True)
        return output.stdout.split()

    return run


def reference_trigger(stages, samples) -> Optional[int]:
    """The semantics of models.TriggerSequence in samples, sample by sample (no skipping)."""
    tracked = 0
    for stage in stages:
        if stage["kind"] in (2, 3):
            tracked |= 1 << stage.get("bit", 0)
    current = occurrences = stage_start = first = gap_start = 0
    last_edge: dict[int, int] = {}
    for t, v in enumerate(samples):
        p = samples[t - 1] if t else v
        changed = v ^ p
        stage = stages[current]
        within = stage.get("within", NO_LIMIT)
        if current and within != NO_LIMIT and t - stage_start > within:
            current = occurrences = 0
            stage_start = first = gap_start = t
            stage = stages[0]
        kind, bit = stage["kind"], stage.get("bit", 0)
        level = v >> bit & 1
        occurred = False
        if kind == 0:
            mask, value = stage.get("mask", 0), stage.get("value", 0)
            occurred = (v & mask) == value and (t == first or (p & mask) != value)
        elif kind == 1:
            occurred = bool(changed >> bit & 1) and (stage.get("edge", 0) == 2 or level == (stage.get("edge", 0) == 0))
        elif kind == 2:
            if changed >> bit & 1 and bit in last_edge and (
                stage.get("edge", 0) == 2 or (level == 0) == (stage.get("edge", 0) == 0)
            ):
                width = t - last_edge[bit]
                maximum = stage.get("max", NO_LIMIT)
                occurred = width >= stage.get("min", 0) and (maximum == NO_LIMIT or width <= maximum)
        else:
            if not changed >> bit & 1:
                start = max(gap_start, last_edge.get(bit, gap_start))
                if t - start >= stage["min"]:
                    occurred = True
                    gap_start = t
        if occurred:
            occurrences += 1
            if occurrences >= stage.get("count", 1):
                current += 1
                if current == len(stages):
                    return t
                occurrences = 0
                stage_start = gap_start = t
                first = t + 1
        for index in range(32):
            if (tracked & changed) >> index & 1:
                last_edge[index] = t
    return None


def check(firmware_sequence, stages, samples, ring=64, width=1):
    expected = reference_trigger(stages, samples)
    assert firmware_sequence([(ring, width, 1, stages, samples)]) == [str(-1 if expected is None else expected)]
    return expected


def test_semantics_of_hand_made_sequences(firmware_sequence):
    # Pattern CH1=1,CH2=0 and then a rising edge on CH3
    samples = [0b000, 0b001, 0b011, 0b001, 0b001, 0b101]
    assert check(firmware_sequence, [dict(kind=0, mask=0b011, value=0b001), dict(kind=1, bit=2)], samples) == 5
    # A pattern already present when the stage starts counts at once
    assert check(firmware_sequence, [dict(kind=1, bit=0), dict(kind=0, mask=0b10, value=0b10)], [0, 0b11, 0b11]) == 2
    # Third falling edge
    samples = [1, 0, 1, 0, 1, 1, 0, 1]
    assert check(firmware_sequence, [dict(kind=1, bit=0, edge=1, count=3)], samples) == 6
    # High pulse of 3 samples (2 and 5 samples are ignored), recognised at the falling edge
    samples = [0, 1, 1, 0, 1, 1, 1, 1, 1, 0, 1, 1, 1, 0, 0]
    assert check(firmware_sequence, [dict(kind=2, bit=0, edge=0, min=3, max=3)], samples) == 13
    # Low pulse at least 4 samples
    samples = [1, 0, 1, 0, 0, 0, 0, 1, 1]
    assert check(firmware_sequence, [dict(kind=2, bit=0, edge=1, min=4)], samples) == 7
    # No edge on CH1 for 5 samples after a rising edge of CH2
    samples = [0, 2, 3, 2, 3, 2, 2, 2, 2, 2, 2, 2]
    assert check(firmware_sequence, [dict(kind=1, bit=1), dict(kind=3, bit=0, min=5)], samples) == 10
    # The second edge has to follow within 2 samples, else the sequence starts again
    samples = [0, 1, 1, 1, 1, 3, 3, 2, 2, 0, 1, 3]
    assert check(firmware_sequence, [dict(kind=1, bit=0), dict(kind=1, bit=1, within=2)], samples) == 11
    # Never complete
    assert check(firmware_sequence, [dict(kind=1, bit=3)], [0, 1, 2, 3] * 10) is None


def random_stage(rng: random.Random, bits: int) -> dict:
    kind = rng.randrange(4)
    stage = dict(kind=kind, bit=rng.randrange(bits), edge=rng.randrange(3), count=rng.choice([1, 1, 2, 3]))
    if kind == 0:
        stage["mask"] = rng.randrange(1, 1 << bits)
        stage["value"] = rng.randrange(1 << bits) & stage["mask"]
    elif kind == 2:
        low = rng.randrange(0, 6)
        stage["min"] = low
        stage["max"] = rng.choice([NO_LIMIT, low + rng.randrange(0, 6)])
    elif kind == 3:
        stage["min"] = rng.randrange(1, 12)
    if rng.random() < 0.4:
        stage["within"] = rng.randrange(0, 40)
    return stage


@pytest.mark.parametrize("width", [1, 2, 4])
def test_the_firmware_evaluation_matches_the_reference(firmware_sequence, width):
    rng = random.Random(1234 + width)
    cases = []
    expected = []
    for case in range(400):
        bits = rng.choice([2, 3, 4])
        offset = rng.choice([0, 0, width * 8 - bits])  # also the top bits of the sample
        stages = [random_stage(rng, bits) for _ in range(rng.randrange(1, 9))]
        for stage in stages:
            stage["bit"] = stage["bit"] + offset
            if "mask" in stage:
                stage["mask"] <<= offset
                stage["value"] <<= offset
        # Signals from very busy to mostly idle; the other bits of the sample change as well
        toggle = rng.choice([0.02, 0.1, 0.3, 0.7])
        value = rng.randrange(1 << (width * 8))
        samples = []
        for _ in range(rng.randrange(50, 800)):
            for bit in range(bits):
                if rng.random() < toggle:
                    value ^= 1 << (bit + offset)
            if rng.random() < 0.3:
                value ^= 1 << rng.randrange(width * 8)
            samples.append(value)
        ring = rng.choice([4, 7, 16, 64, 1000])
        cases.append((ring, width, case, stages, samples))
        result = reference_trigger(stages, samples)
        expected.append(str(-1 if result is None else result))
    assert firmware_sequence(cases) == expected
    assert sum(value != "-1" for value in expected) > 100  # most sequences complete


def test_the_firmware_refuses_invalid_stages(firmware_sequence):
    assert firmware_sequence([(16, 1, 1, [dict(kind=1, bit=8)], [0, 1])]) == ["invalid"]  # bit beyond 8 bits
    assert firmware_sequence([(16, 1, 1, [dict(kind=3, bit=0, min=0)], [0, 1])]) == ["invalid"]
    assert firmware_sequence([(16, 1, 1, [dict(kind=2, bit=0, min=5, max=4)], [0, 1])]) == ["invalid"]
    assert firmware_sequence([(16, 1, 1, [dict(kind=0, mask=1, value=2)], [0, 1])]) == ["invalid"]
    assert firmware_sequence([(16, 1, 1, [dict(kind=1, bit=0, count=0)], [0, 1])]) == ["invalid"]
    assert firmware_sequence([(16, 1, 1, [dict(kind=4, bit=0)], [0, 1])]) == ["invalid"]
