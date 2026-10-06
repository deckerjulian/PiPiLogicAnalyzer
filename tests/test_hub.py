"""The device hub: instruments, status events, trigger routes, time base offsets."""

from __future__ import annotations

import numpy as np
import pytest

from openscilab.core import hub as hub_module
from openscilab.core.hub import Hub, HubError, reference_offset
from openscilab.core.instrument import (
    PIN_DIN,
    PIN_DOUT,
    CaptureFacet,
    GpioFacet,
    Instrument,
    InstrumentError,
    InstrumentStatus,
    PinInfo,
)
from openscilab.driver.base import CAPABILITY_EDGE_TRIGGER_OUT, facets_of
from openscilab.driver.emulated import EmulatedAnalyzerDriver


class FakeGpio(GpioFacet):
    def __init__(self) -> None:
        super().__init__()
        self.levels: dict[str, int] = {}

    def pins(self):
        return [PinInfo("D2", frozenset({PIN_DIN, PIN_DOUT})), PinInfo("D0", frozenset({PIN_DIN}), reserved="USB")]

    def write(self, pin, value):
        if not self.pin(pin).usable:
            raise InstrumentError(f"{pin} is reserved")
        self.levels[pin] = value


def fake(name: str, outputs=(), inputs=()) -> Instrument:
    instrument = Instrument(name, kind="Fake", uri=f"fake:{name}", trigger_outputs=outputs, trigger_inputs=inputs)
    instrument.add_facet(FakeGpio())
    return instrument


def test_two_instruments_are_open_at_the_same_time():
    hub = Hub()
    events = []
    hub.subscribe(events.append)

    first = hub.add(fake("A"))
    second = hub.add(fake("A"))  # same name: made unique

    assert (first, second) == ("A", "A 2")
    assert hub.names() == ["A", "A 2"]
    assert [event.kind for event in events] == ["added", "added"]
    assert hub.get("A 2").gpio is not None
    assert len(hub.with_facet(GpioFacet)) == 2
    with pytest.raises(HubError):
        hub.get("B")


def test_status_changes_and_removal_are_reported():
    hub = Hub()
    events = []
    unsubscribe = hub.subscribe(events.append)
    instrument = fake("A")
    hub.add(instrument)

    hub.set_status("A", InstrumentStatus.BUSY, "capturing")
    hub.remove("A")
    unsubscribe()
    hub.add(fake("B"))

    assert [(event.kind, event.status) for event in events][1:] == [
        ("status", InstrumentStatus.BUSY), ("removed", InstrumentStatus.DISCONNECTED),
    ]
    assert events[1].message == "capturing"
    assert instrument.status == InstrumentStatus.DISCONNECTED  # closed


def test_trigger_routes_need_the_connectors_and_make_offsets_exact():
    hub = Hub()
    hub.add(fake("Scope", outputs=("TRIG OUT",)))
    hub.add(fake("Pico", inputs=("TRIG IN",)))

    with pytest.raises(HubError):
        hub.add_route("Pico", "TRIG OUT", "Scope", "TRIG IN")
    route = hub.add_route("Scope", "TRIG OUT", "Pico", "TRIG IN", delay=40e-9)

    assert route.cable == "Connect TRIG OUT of Scope to TRIG IN of Pico"
    offset = hub.offset("Pico")
    assert offset.exact and offset.offset == pytest.approx(40e-9)
    assert hub.offset("Pico/CH1").origin == hub_module.ORIGIN_TRIGGER_LINE  # streams inherit
    # An estimate does not replace the exact offset.
    hub.stamp("Pico", 12.0)
    assert hub.offset("Pico").exact

    hub.remove("Scope")
    assert hub.routes() == []
    assert hub.offset("Pico").origin == hub_module.ORIGIN_ESTIMATED


def test_offsets_by_software_time_stamp_and_by_reference_edge():
    hub = Hub()
    hub.add(fake("A"))
    hub.add(fake("B"))
    estimated = hub.stamp("B", 0.25)
    assert estimated.origin == hub_module.ORIGIN_ESTIMATED and "estimated" in estimated.describe()

    rng = np.random.default_rng(1)
    edges = np.cumsum(rng.uniform(1e-4, 5e-4, 40))  # an irregular signal seen by both
    measured = hub.align_by_reference("A", edges, "B", edges - 3.2e-5, max_shift=1e-4)

    assert measured is not None and measured.origin == hub_module.ORIGIN_REFERENCE_EDGE
    assert measured.offset == pytest.approx(3.2e-5, abs=1e-9)
    assert np.allclose(hub.to_common_time("B", [1.0]), [1.0 + 3.2e-5])


def test_reference_offset_needs_matching_edges():
    edges = np.array([0.0, 0.3, 0.35, 0.9, 1.4])
    assert reference_offset(edges, edges + 0.01, max_shift=0.02) == pytest.approx(-0.01)
    assert reference_offset(edges, np.array([5.0, 6.0, 7.0]), max_shift=0.02) is None
    assert reference_offset(edges[:2], edges[:2], max_shift=0.02) is None


def test_the_clock_can_be_replaced():
    hub = Hub(clock=lambda: 42.0)
    assert hub.now() == 42.0
    hub.set_clock(lambda: 1.5)
    assert hub.now() == 1.5


# ------------------------------------------------------------------ instruments
def test_a_driver_becomes_an_instrument_with_a_capture_facet():
    driver = EmulatedAnalyzerDriver(1)
    instrument = Instrument.from_driver(driver, uri="file:x")

    assert isinstance(instrument.capture, CaptureFacet)
    assert instrument.capture.driver is driver  # the emulated driver does not stream
    assert instrument.status == InstrumentStatus.SIMULATED
    assert instrument.kind == "File"
    assert [pin.name for pin in instrument.pins()][:2] == ["CH1", "CH2"]
    assert instrument.details()[0][0] == "Instrument"
    with pytest.raises(InstrumentError):
        instrument.require(GpioFacet)


def test_gpio_pins_and_reserved_pins():
    instrument = fake("A")
    gpio = instrument.require(GpioFacet)
    gpio.write("D2", 1)
    assert gpio.levels == {"D2": 1}
    with pytest.raises(InstrumentError):
        gpio.write("D0", 1)
    assert [pin.usable for pin in instrument.pins()] == [True, False]


def test_capabilities_unlock_facets():
    assert facets_of({"GPIO", "ANALOG=3", "PATTERN_GEN=25000000,16", CAPABILITY_EDGE_TRIGGER_OUT}) == {
        "GpioFacet", "AnalogInFacet", "GeneratorFacet",
    }
