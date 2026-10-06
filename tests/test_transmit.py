"""Sending UART, SPI and I²C with the device's own hardware (capabilities TX_*): the facet, the
simulator, the flow nodes and the *Send* tab of the device card."""

from __future__ import annotations

import numpy as np
import pytest

from openscilab.core.instrument import GeneratorFacet, InstrumentError
from openscilab.driver.simulated import open_simulated
from openscilab.lab import Flow
from openscilab.lab.engine import Engine
from openscilab.ui.devices.send_panel import describe_bytes, parse_data


class Clock:
    time = 0.0

    def __call__(self) -> float:
        return self.time


@pytest.fixture
def pico():
    clock = Clock()
    return open_simulated("pico", clock=clock, fast=True), clock


def test_uart_frames_appear_on_the_pin(pico):
    instrument, clock = pico
    generator = instrument.facet(GeneratorFacet)
    assert generator.transmits("uart") and generator.transmits("spi") and generator.transmits("i2c")
    from openscilab.driver.simulated import scenarios

    scenarios.apply(instrument.simulated_driver, {"scenario": "idle"})  # nothing else on the pin
    instrument.gpio.write("GP4", 1)  # idle high before the frame
    assert generator.transmit("uart", b"A", {"tx": "GP4"}, baud=100_000) == b""
    circuit = instrument.simulated_driver.circuit
    begin = circuit.sources["GP4"].start  # the device sends after its latency
    levels = circuit.digital("GP4", begin, 1_600_000, 16 * 14)  # 16 samples per bit
    start = int(np.flatnonzero(levels == 0)[0])  # the start bit (after the latency of the device)
    frame = list(levels[start + 8::16][:10])  # the middle of start, 8 data bits (LSB first), stop
    assert frame == [0, 1, 0, 0, 0, 0, 0, 1, 0, 1]  # "A" = 0x41
    assert levels[-1] == 1  # idle high afterwards


def test_i2c_reads_the_device_of_the_profile(pico):
    instrument, _clock = pico
    generator = instrument.facet(GeneratorFacet)
    assert generator.transmit("i2c", b"\x00", {"scl": "GP3", "sda": "GP2"}, address=0x48, read=2) == b"\x19\x00"
    with pytest.raises(InstrumentError, match="0x50"):
        generator.transmit("i2c", b"\x00", {"scl": "GP3", "sda": "GP2"}, address=0x50, read=1)


def test_spi_reads_miso_from_the_circuit(pico):
    instrument, _clock = pico
    generator = instrument.facet(GeneratorFacet)
    from openscilab.driver.simulated import scenarios

    scenarios.apply(instrument.simulated_driver, {"scenario": "idle"})  # nothing on the pins
    assert generator.transmit("spi", b"\x12\x34", {"sck": "GP2", "mosi": "GP3", "miso": "GP4"}) == b"\x00\x00"
    instrument.gpio.write("GP4", 1)  # MISO held high
    assert generator.transmit("spi", b"\x12\x34", {"sck": "GP2", "mosi": "GP3", "miso": "GP4"}) == b"\xff\xff"


def test_reserved_pins_and_devices_without_it():
    instrument = open_simulated("pico", fast=True)
    generator = instrument.facet(GeneratorFacet)
    with pytest.raises(InstrumentError, match="reserved"):
        generator.transmit("uart", b"x", {"tx": "GP1"})
    dho = open_simulated("dho924s", fast=True).facet(GeneratorFacet)
    assert not dho.transmits("uart")
    with pytest.raises(InstrumentError):
        dho.transmit("uart", b"x", {"tx": "D0"})


def test_the_node_uses_the_device_and_reports_the_answer():
    flow = Flow("x")
    flow.add_node("device.instrument", "pico", address="sim:pico")
    flow.add_node("gen.tx_i2c", "read", pins=["GP3", "GP2"], address=0x48, data=[0], read=2)
    flow.connect("pico.device", "read.device")
    engine = Engine(flow, mode="virtual")
    result = engine.run(timeout=20)
    assert result.ok, result.error
    assert result.values[("read", "received")].data == [b"\x19\x00"]


def test_without_tx_the_node_sends_a_pattern():
    flow = Flow("x")
    flow.add_node("device.instrument", "afg", address="sim:dho924s")
    flow.add_node("gen.tx_uart", "uart", pin="D0", baud="9600 Hz", data="Hi", output="")
    flow.connect("afg.device", "uart.device")
    result = Engine(flow, mode="virtual").run(timeout=20)
    assert ("uart", "received") not in result.values  # (played as a pattern: nothing comes back)


def test_data_of_the_send_tab():
    assert parse_data("Hello\\n") == b"Hello\n"
    assert parse_data("19 00") == b"\x19\x00"
    assert parse_data("0x19 0x00") == b"\x19\x00"
    assert parse_data("abc") == b"abc"
    assert describe_bytes(b"\x19A") == "19 41   “.A”"


def test_the_send_tab(shell):
    instrument = open_simulated("pico", fast=True)
    shell.hub.add(instrument)
    card = shell.open_device_card(instrument)
    panel = card.send_panel
    assert panel is not None and [panel.protocol_box.itemData(i) for i in range(panel.protocol_box.count())] == \
        ["uart", "spi", "i2c"]
    panel.protocol_box.setCurrentIndex(2)
    panel.pin_edits["scl"].setText("GP3")
    panel.pin_edits["sda"].setText("GP2")
    panel.address_box.setValue(0x48)
    panel.read_box.setValue(2)
    panel.data_edit.setText("00")
    assert panel.send() == b"\x19\x00"
    assert "19 00" in panel.answer_label.text()
    uno = open_simulated("dho924s", fast=True)
    shell.hub.add(uno)
    assert shell.open_device_card(uno).send_panel is None


def test_a_flow_keeps_an_output_beyond_the_watchdog():
    """The driver keeps the device alive, not the device card: an output a flow set stays."""
    from openscilab.lab import yaml_io

    flow = yaml_io.loads("""\
flow: Hold an output
nodes:
  uno: {type: device.instrument, address: "sim:uno"}
  high: {type: gpio.write, pin: D7, level: 1}
  wait: {type: control.timer, interval: 3 s, count: 2}
  read: {type: gpio.read, pin: D7}
edges:
  - uno.device -> high.device
  - uno.device -> read.device
  - wait.tick -> read.trigger
""")
    engine = Engine(flow, mode="virtual")
    values = []
    engine.subscribe(lambda event: event.kind == "value" and event.node == "read" and values.append(event.value.value))
    assert engine.run(timeout=30).ok
    assert values == [1.0, 1.0]  # (it was 0 after the watchdog's second)
    assert np.isfinite(values[-1])


def test_disconnecting_asks_while_outputs_are_driven(shell, monkeypatch):
    from openscilab.ui import messages
    from openscilab.ui.shell.main_window import drives_outputs

    instrument = open_simulated("uno", fast=True)
    shell.hub.add(instrument)
    assert not drives_outputs(instrument)
    instrument.gpio.write("D7", 1)  # set by a flow or a script, not by the card
    assert drives_outputs(instrument)
    asked = []
    monkeypatch.setattr(messages, "confirm", lambda *args, **kwargs: asked.append(" ".join(map(str, args))) or False)
    assert not shell.disconnect_instrument(instrument)
    assert asked and "drives outputs" in asked[0]
