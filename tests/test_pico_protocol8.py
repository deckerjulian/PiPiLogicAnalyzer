"""The Pico driver with protocol 8 (docs/protocols.md) against a fake board that reads the frames
and answers like the firmware: pins, outputs, monitor, analog channels, pattern generator,
sending, and commands while a capture runs."""

from __future__ import annotations

import struct
import threading
import time

import numpy as np
import pytest

from openscilab.core.instrument import (
    AnalogInFacet,
    GeneratorFacet,
    GpioFacet,
    Instrument,
    InstrumentError,
    MonitorFacet,
    parse_pin_line,
)
from openscilab.core import waveform as waves
from openscilab.driver.base import ACQUISITION_STREAM, CaptureError
from openscilab.driver.models import AnalogChannel, AnalyzerChannel, CaptureSession, EdgeKind, TriggerType
from openscilab.driver.pico import protocol
from openscilab.driver.pico.analyzer import PicoDriver
from openscilab.driver.pico.transport import Transport, TransportError, TransportTimeout

CAPS = ("CAPS:SELFTEST,DEVICEINFO,STREAM=800000,STATE_MODE,STREAM_STATE,GPIO,PWM,MONITOR,ANALOG=3,"
        "PATTERN_GEN=50000000,8,GEN_SQUARE,TX_UART,TX_SPI,TX_I2C")
PINS = ["PIN:GP0,DIN/DOUT,-,3300,-,reserved: trigger output",
        "PIN:GP2,DIN/DOUT/PULLUP/PULLDOWN/PWM/CLOCK,0,3300,-",
        "PIN:GP3,DIN/DOUT/PULLUP/PULLDOWN/PWM/CLOCK,1,3300,-",
        "PIN:GP4,DIN/DOUT/PULLUP/PULLDOWN/PWM/CLOCK,2,3300,-",
        "PIN:GP5,DIN/DOUT/PULLUP/PULLDOWN/PWM/CLOCK,3,3300,-",
        "PIN:GP22,DIN/DOUT/PWM,20,3300,-",
        "PIN:GP26,DIN/DOUT/ADC,21,3300,0",
        "PIN:GP25,DOUT,-,3300,-,reserved: LED"]


def unframe(data: bytes) -> tuple[list[bytes], bool]:
    """Frames in ``data`` (unescaped payloads) and whether a stop byte came outside them."""
    frames, stop, index = [], False, 0
    while index < len(data):
        if data[index:index + 2] == protocol.FRAME_START:
            end = data.index(protocol.FRAME_END, index + 2)
            raw, payload, position = data[index + 2:end], bytearray(), 0
            while position < len(raw):
                if raw[position] == protocol.ESCAPE:
                    payload.append(raw[position + 1] ^ protocol.ESCAPE)
                    position += 2
                else:
                    payload.append(raw[position])
                    position += 1
            frames.append(bytes(payload))
            index = end + 2
        else:
            stop |= data[index] == protocol.CMD_ABORT_CAPTURE
            index += 1
    return frames, stop


class FakeBoard(Transport):
    """A Pico with firmware 8, as far as the driver can tell."""

    def __init__(self) -> None:
        self.out = bytearray()
        self.cond = threading.Condition()
        self.commands: list[tuple[int, bytes]] = []
        self.capturing = self.streaming = False
        self.levels = 0
        self.pattern: list[int] = []
        self.analog_mask = 0
        self.monitor = None
        self.i2c_present = {0x48: b"\x07\x6c"}
        self.closed = False
        self.reply("OPENSCILAB_PICO_PICO_2_V8_0", "FREQ:100000000", "BLASTFREQ:200000000", "BUFFER:393216",
                   "CHANNELS:24", f"PROTOCOL:{protocol.FIRMWARE_PROTOCOL}")
        self.identified = False

    # -- the board
    def reply(self, *lines: str) -> None:
        self.send(b"".join(line.encode() + b"\n" for line in lines))

    def send(self, data: bytes) -> None:
        with self.cond:
            self.out += data
            self.cond.notify_all()

    def handle(self, command: int, data: bytes) -> None:
        self.commands.append((command, data))
        if command == protocol.CMD_GET_ID:
            if self.identified:
                self.reply("OPENSCILAB_PICO_PICO_2_V8_0", "FREQ:100000000", "BLASTFREQ:200000000",
                           "BUFFER:393216", "CHANNELS:24", f"PROTOCOL:{protocol.FIRMWARE_PROTOCOL}")
            self.identified = True
        elif command == protocol.CMD_CAPABILITIES:
            self.reply(CAPS)
        elif command == protocol.CMD_PINS:
            self.reply(f"PINS:{len(PINS)}", *PINS)
        elif command == protocol.CMD_HEARTBEAT:
            pass
        elif self.capturing and command not in (13, 14, 15, 16, 17, 18, 19, 20, 23, 24):
            self.reply("ERR:BUSY")
        elif command == protocol.CMD_PIN_MODE:
            self.reply("OK" if data[0] != 25 else "ERR:RESERVED")
        elif command == protocol.CMD_WRITE:
            mask, levels = struct.unpack("<II", data)
            self.levels = (self.levels & ~mask) | (levels & mask)
            self.reply("OK")
        elif command == protocol.CMD_READ:
            (mask,) = struct.unpack("<I", data)
            self.reply(f"LEVELS:{self.levels & mask:X}")
        elif command == protocol.CMD_PWM:
            pin, frequency, duty = struct.unpack("<BfH", data)
            self.reply(f"PWM:{frequency * 0.999:.3f}")
        elif command == protocol.CMD_PULSE:
            self.reply("OK")
        elif command == protocol.CMD_MONITOR:
            self.monitor = struct.unpack("<IIB", data)
            self.reply("OK")
        elif command == protocol.CMD_ADC_READ:
            values = [str(100 * (bit + 1)) for bit in range(3) if data[0] >> bit & 1]
            self.reply("ADC:" + ",".join(values))
        elif command == protocol.CMD_SAFE:
            self.reply("OK")
        elif command == protocol.CMD_GEN_LOAD:
            offset, flags = struct.unpack_from("<IB", data)
            width = (1, 2, 4)[(flags >> 1) & 3]
            samples = protocol.unpack_runs(data[5:], {1: 8, 2: 16, 4: 24}[width]) if flags & 1 else list(data[5:])
            del self.pattern[offset:]
            self.pattern += samples
            self.reply(f"GEN_LOADED:{len(self.pattern)}")
        elif command == protocol.CMD_GEN_START:
            rate = struct.unpack_from("<f", data)[0]
            self.reply(f"GEN_STARTED:{rate:.1f}")
        elif command == protocol.CMD_GEN_STOP:
            self.reply("OK")
        elif command == protocol.CMD_GEN_STATUS:
            self.reply("GEN:1,3")
        elif command == protocol.CMD_CAPTURE_ANALOG:
            self.analog_mask, rate = struct.unpack("<BI", data)
            self.reply(f"ANALOG_OK:{rate}")
        elif command == protocol.CMD_TRIGGER_SEQUENCE:
            self.reply("SEQUENCE_OK")
        elif command == protocol.CMD_START_CAPTURE:
            if data[0] == 6:
                self.streaming = True
                count = data[38]
                self.reply("STREAM_STARTED:" + ",".join(str(bit) for bit in data[6:6 + count]))
            else:
                self.capturing = True
                self.reply("CAPTURE_STARTED")
        elif command == protocol.CMD_TX_UART:
            self.reply("OK")
        elif command == protocol.CMD_TX_SPI:
            self.reply("RX:" + bytes(byte ^ 0xFF for byte in data[9:]).hex())
        elif command == protocol.CMD_TX_I2C:
            sda, scl, address, frequency, read = struct.unpack_from("<BBBIH", data)
            if address not in self.i2c_present:
                self.reply("ERR:NACK")
            else:
                self.reply("RX:" + self.i2c_present[address][:read].hex())
        else:
            self.reply("ERR:UNKNOWN")

    def complete(self, samples: list[int], analog: list[int] = (), rate_mhz: int = 0) -> None:
        """The capture ends: ``CAPTURE_DATA`` and the data (8 channels)."""
        self.capturing = False
        data = b"CAPTURE_DATA\n" + struct.pack("<I", len(samples)) + bytes(samples) + bytes([0])
        if self.analog_mask:
            channels = bin(self.analog_mask).count("1")
            data += struct.pack("<II", len(analog) // channels, rate_mhz) + np.asarray(analog, "<u2").tobytes()
        self.send(data)

    # -- Transport
    def write(self, data: bytes) -> None:
        frames, stop = unframe(bytes(data))
        if stop and self.streaming:
            self.streaming = False
            self.send(struct.pack("<I", 0))
        elif stop and self.capturing:
            self.capturing = False
        for frame in frames:
            if self.streaming:
                self.streaming = False  # any byte stops a stream
                self.send(struct.pack("<I", 0))
                continue
            self.handle(frame[0], frame[1:])

    def read_line(self, timeout=None) -> str:
        deadline = time.monotonic() + min(timeout if timeout is not None else 2.0, 2.0)
        with self.cond:
            while b"\n" not in self.out:
                if self.closed:
                    raise TransportError("closed")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TransportTimeout("timeout")
                self.cond.wait(remaining)
            index = self.out.index(b"\n")
            line = bytes(self.out[:index]).decode()
            del self.out[:index + 1]
            return line

    def read_exactly(self, count: int, timeout=None) -> bytes:
        deadline = time.monotonic() + 3.0
        with self.cond:
            while len(self.out) < count:
                if self.closed:
                    raise TransportError("closed")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TransportTimeout("timeout")
                self.cond.wait(remaining)
            data = bytes(self.out[:count])
            del self.out[:count]
            return data

    def reset_input(self) -> None:
        pass

    def reopen(self) -> None:
        pass

    def close(self) -> None:
        with self.cond:
            self.closed = True
            self.cond.notify_all()

    @property
    def is_open(self) -> bool:
        return not self.closed

    def sent(self, command: int) -> list[bytes]:
        return [data for number, data in self.commands if number == command]


@pytest.fixture
def board(monkeypatch):
    fake = FakeBoard()
    monkeypatch.setattr("openscilab.driver.pico.analyzer.SerialTransport", lambda *args, **kwargs: fake)
    return fake


@pytest.fixture
def pico(board):
    driver = PicoDriver("/dev/fake")
    instrument = Instrument.from_driver(driver, uri="pico:/dev/fake")
    yield instrument
    instrument.close()


def wait_for(condition, seconds: float = 3.0) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.005)


# ------------------------------------------------------------------- facets
def test_the_capabilities_unlock_the_facets(pico):
    assert {type(facet).__name__ for facet in pico.facets()} >= {
        "PicoGpio", "PicoMonitor", "PicoAnalogIn", "PicoGenerator"}
    assert isinstance(pico.gpio, GpioFacet) and isinstance(pico.monitor, MonitorFacet)
    assert pico.facet(AnalogInFacet) is not None and isinstance(pico.generator, GeneratorFacet)
    assert "PATTERN_GEN=50000000,8" in pico.capabilities()


def test_the_pin_table(pico, board):
    pins = {pin.name: pin for pin in pico.pins()}
    assert pins["GP2"].channel == 0 and "CLOCK" in pins["GP2"].capabilities
    assert pins["GP0"].reserved == "trigger output" and pins["GP25"].reserved == "LED"
    assert pins["GP26"].analog_channel == 0
    assert len(board.sent(protocol.CMD_PINS)) == 1  # read once
    with pytest.raises(InstrumentError, match="reserved"):
        pico.gpio.write("GP25", 1)


def test_parse_pin_line():
    pin = parse_pin_line("PIN:GP0,DIN/DOUT,-,3300,-,reserved: trigger output of a multi-board set")
    assert pin.reserved == "trigger output of a multi-board set" and pin.channel is None
    assert parse_pin_line("PIN:D13,DIN/DOUT/PWM,5,5000,-").logic_level == 5.0
    assert parse_pin_line("STATE:1,0") is None and parse_pin_line("PIN:bad") is None


def test_outputs_read_write_pulse_pwm(pico, board):
    gpio = pico.gpio
    gpio.write_many({"GP2": 1, "GP4": 1})
    assert struct.unpack("<II", board.sent(protocol.CMD_WRITE)[-1]) == (0b10100, 0b10100)
    assert gpio.read_many(["GP2", "GP3", "GP4"]) == {"GP2": 1, "GP3": 0, "GP4": 1}
    gpio.pulse("GP3", 2e-6, level=1, count=4, period=10e-6)
    assert struct.unpack("<BBIHI", board.sent(protocol.CMD_PULSE)[-1]) == (3, 1, 2000, 4, 10000)
    assert gpio.pwm("GP5", 1000.0, 0.25) == pytest.approx(999.0, rel=1e-3)
    pin, frequency, duty = struct.unpack("<BfH", board.sent(protocol.CMD_PWM)[-1])
    assert (pin, duty) == (5, round(0.25 * 65535))
    gpio.set_mode("GP4", "input_pullup")
    assert board.sent(protocol.CMD_PIN_MODE)[-1] == bytes([4, 1])
    gpio.safe_all()
    assert board.sent(protocol.CMD_SAFE)


def test_the_board_refusing_raises(pico, board):
    board.handle = lambda command, data: board.reply("ERR:PIN") if command == protocol.CMD_WRITE else \
        FakeBoard.handle(board, command, data)
    with pytest.raises(InstrumentError, match="PIN"):
        pico.gpio.write("GP2", 1)


def test_the_heartbeat_runs_in_the_driver(pico, board):
    wait_for(lambda: len(board.sent(protocol.CMD_HEARTBEAT)) >= 2)
    pico.gpio.keepalive = False
    time.sleep(0.15)
    count = len(board.sent(protocol.CMD_HEARTBEAT))
    time.sleep(0.5)
    assert len(board.sent(protocol.CMD_HEARTBEAT)) == count


def test_monitor_reports_arrive_while_idle(pico, board):
    states = []
    pico.monitor.on_state(states.append)
    pico.monitor.start(50.0, ["GP2", "GP3"], analog=("A1",))
    assert struct.unpack("<IIB", board.sent(protocol.CMD_MONITOR)[-1]) == (50000, 0b1100, 0b10)
    board.reply("STATE:1500000,8,2048")
    wait_for(lambda: states)
    assert states[0].time == 1.5 and states[0].digital == {"GP2": 0, "GP3": 1}
    assert states[0].analog["A1"] == pytest.approx(1.65, rel=1e-3)
    pico.monitor.stop()
    assert struct.unpack("<IIB", board.sent(protocol.CMD_MONITOR)[-1]) == (0, 0, 0)


def test_analog_read(pico):
    values = pico.facet(AnalogInFacet).read(["A2", "A0"])
    assert values == {"A0": pytest.approx(100 * 3.3 / 4096), "A2": pytest.approx(300 * 3.3 / 4096)}


# ---------------------------------------------------------- during a capture
def make_session(channels: int = 4, analog: tuple[int, ...] = ()) -> CaptureSession:
    session = CaptureSession(frequency=1_000_000, pre_trigger_samples=2, post_trigger_samples=6)
    session.capture_channels = [AnalyzerChannel(channel_number=index) for index in range(channels)]
    session.analog_channels = [AnalogChannel(channel_number=number) for number in analog]
    return session


def test_commands_and_reports_while_a_capture_runs(pico, board):
    driver = pico.capture.driver
    states, results = [], []
    done = threading.Event()
    pico.monitor.on_state(states.append)
    pico.monitor.start(10.0, ["GP22"])
    assert driver.start_capture(make_session(), lambda args: (results.append(args), done.set())) == CaptureError.NONE
    # a pulse armed after the capture: the answer comes through the capture thread
    pico.gpio.pulse("GP22", 1e-6)
    board.reply("STATE:10,400000")
    wait_for(lambda: states)
    assert states[0].digital == {"GP22": 1}
    with pytest.raises(InstrumentError, match="LA channel 3"):
        pico.gpio.write("GP5", 1)
    # the pins of the capture are busy, the pin table says so
    assert pico.gpio.pin("GP2").reserved == "LA channel 0 while capturing"
    with pytest.raises(InstrumentError, match="capturing"):
        pico.generator.start("PATTERN", waves.pattern({"GP22": [0, 1]}))
    board.complete([1, 2, 3, 4, 5, 6, 7, 8])
    assert done.wait(5) and results[0].success
    assert results[0].session.capture_channels[1].samples.tolist() == [0, 1, 1, 0, 0, 1, 1, 0]
    assert pico.gpio.read("GP22") == 0  # after the capture: answers read directly again


def test_analog_channels_in_a_buffer_capture(pico, board):
    driver = pico.capture.driver
    results = []
    done = threading.Event()
    session = make_session(analog=(0, 2))
    assert driver.start_capture(session, lambda args: (results.append(args), done.set())) == CaptureError.NONE
    assert struct.unpack("<BI", board.sent(protocol.CMD_CAPTURE_ANALOG)[-1]) == (0b101, 250_000)
    board.complete([0] * 8, analog=[10, 4000, 11, 4001, 12, 4002], rate_mhz=250_000_000)
    assert done.wait(5) and results[0].success
    first, third = session.analog_channels
    assert first.raw.tolist() == [10, 11, 12] and third.raw.tolist() == [4000, 4001, 4002]
    assert first.rate == 250_000 and first.scale == pytest.approx(3.3 / 4096)


def test_analog_channels_the_board_does_not_have_are_refused(pico):
    assert pico.capture.driver.start_capture(make_session(analog=(3,))) == CaptureError.BAD_PARAMS


def test_a_stream_with_analog_chunks_and_live_state(pico, board):
    driver = pico.capture.driver
    results = []
    done = threading.Event()
    session = make_session(channels=2, analog=(1,))
    session.acquisition_mode = ACQUISITION_STREAM
    session.post_trigger_samples = 1_000_000
    session.continuous = True
    session.clock_channel, session.clock_edge = 1, EdgeKind.RISING
    session.trigger_type = TriggerType.IMMEDIATE
    assert driver.start_capture(session, lambda args: (results.append(args), done.set())) == CaptureError.NONE
    header, *_ = board.sent(protocol.CMD_TRIGGER_SEQUENCE)[-1]
    assert header == protocol.SEQUENCE_FORMAT_VERSION and board.sent(protocol.CMD_TRIGGER_SEQUENCE)[-1][1] & 1
    with pytest.raises(InstrumentError, match="streaming"):
        pico.gpio.write("GP5", 1)
    board.send(struct.pack("<I", 4) + bytes([0, 1, 2, 3]))
    board.send(struct.pack("<I", 0x80000000 | 6) + np.asarray([7, 8, 9], "<u2").tobytes())
    board.send(struct.pack("<I", 2) + bytes([3, 0]))
    time.sleep(0.05)
    driver.stop_capture()
    assert done.wait(5) and results[0].success
    assert session.capture_channels[0].samples.tolist() == [0, 1, 0, 1, 1, 0]
    assert session.analog_channels[0].raw.tolist() == [7, 8, 9]


# ----------------------------------------------------------------- generator
def test_pattern_output(pico, board):
    generator = pico.generator
    outputs = {output.name: output for output in generator.outputs()}
    assert outputs["PATTERN"].max_rate == 50e6 and outputs["PATTERN"].resolution == 8
    assert outputs["SQUARE"].pins == ("GP22",)
    tracks = {"GP3": [0, 1] * 300, "GP5": [1] * 600}
    rate = generator.start("PATTERN", waves.pattern(tracks, rate=1e6))
    assert rate == 1e6
    expected = [(level << 0) | (1 << 2) for level in tracks["GP3"]]
    assert board.pattern == expected
    rate_, first, count, length, passes, flags, sync = struct.unpack("<fBBIIBB", board.sent(protocol.CMD_GEN_START)[-1])
    assert (first, count, length, passes, sync) == (3, 3, 600, 0, 0xFF)
    assert all(len(protocol.escape_payload(bytes([protocol.CMD_GEN_LOAD]) + data)) <= 512
               for data in board.sent(protocol.CMD_GEN_LOAD))
    assert struct.unpack_from("<IB", board.sent(protocol.CMD_GEN_LOAD)[0])[1] == 0b001  # runs, 1 byte
    assert pico.capture.driver.buffer_size == 393216 - 600  # the pattern takes capture memory
    assert pico.capture.driver.memory_depth(4, 2) == (393216 - 600) // 5
    assert generator.running("PATTERN")
    generator.stop("PATTERN")
    assert board.sent(protocol.CMD_GEN_STOP)


def test_square_output_is_the_pwm_of_gp22(pico, board):
    pico.generator.start("SQUARE", waves.standard("square", 1000.0, amplitude=1.65, offset=1.65, duty=0.5))
    pin, frequency, duty = struct.unpack("<BfH", board.sent(protocol.CMD_PWM)[-1])
    assert (pin, frequency, duty) == (22, 1000.0, 32768)


def test_transmit(pico, board):
    generator = pico.generator
    assert generator.transmits("uart") and generator.transmits("spi") and generator.transmits("i2c")
    generator.transmit("uart", b"x" * 450, {"tx": "GP4"}, baud=9600)
    pieces = board.sent(protocol.CMD_TX_UART)
    assert [len(piece) - 5 for piece in pieces] == [200, 200, 50]
    assert struct.unpack_from("<BI", pieces[0]) == (4, 9600)
    assert generator.transmit("spi", b"\x0f\xf0", {"sck": "GP2", "mosi": "GP3", "miso": "GP4"}) == b"\xf0\x0f"
    assert struct.unpack_from("<BBBBIB", board.sent(protocol.CMD_TX_SPI)[-1]) == (2, 3, 4, 0xFF, 1_000_000, 0)
    assert generator.transmit("i2c", b"\x00", {"sda": "GP4", "scl": "GP5"}, address=0x48, read=2) == b"\x07\x6c"
    with pytest.raises(InstrumentError, match="0x49"):
        generator.transmit("i2c", b"\x00", {"sda": "GP4", "scl": "GP5"}, address=0x49)


def test_pattern_blocks_round_trip():
    samples = [0] * 70000 + [1, 2, 2, 3] * 100
    blocks = protocol.pattern_blocks(samples, 8)
    unpacked = []
    for offset, data in blocks:
        assert offset == len(unpacked) and len(data) <= protocol.MAX_DATA_PER_FRAME
        unpacked += protocol.unpack_runs(data, 8)
    assert unpacked == samples


def test_capability_values_with_a_comma():
    assert protocol.split_capabilities("GPIO,PATTERN_GEN=50000000,8,STREAM=800000,TX_UART") == [
        "GPIO", "PATTERN_GEN=50000000,8", "STREAM=800000", "TX_UART"]
