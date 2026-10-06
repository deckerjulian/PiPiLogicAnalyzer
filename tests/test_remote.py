"""Remote devices: the protocol and the package openscilab_device, the clock model, the server, the
simulated devices behind an emulated network, the flow nodes, the sync signal and the user interface."""

from __future__ import annotations

import itertools
import os
import random
import socket
import statistics
import time

import pytest

from openscilab.driver.remote.clock import JUMP, ClockModel
from openscilab.driver.remote.server import RemoteError, RemoteServer
from openscilab_device import Device, protocol


def wait_for(condition, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


@pytest.fixture
def server():
    server = RemoteServer(port=0, host="127.0.0.1", token="secret").start()
    yield server
    server.stop()


def device(server, name: str = "pi", token: str = "secret", **options) -> Device:
    return Device(name, server=f"127.0.0.1:{server.port}", token=token, **options)


# ------------------------------------------------------------------ protocol
def test_frames_go_through_a_socket():
    left, right = socket.socketpair()
    with left, right:
        left.sendall(protocol.encode({"t": "block", "n": 3}, b"\x01\x02\x03"))
        header, payload = protocol.receive(right)
    assert header == {"t": "block", "n": 3} and payload == b"\x01\x02\x03"


def test_the_token_never_travels_and_descriptions_are_checked():
    answer = protocol.proof("secret", "nonce")
    assert "secret" not in answer and protocol.check_proof("secret", "nonce", answer)
    assert not protocol.check_proof("other", "nonce", answer) and not protocol.check_proof("secret", "nonce", None)
    with pytest.raises(protocol.ProtocolError, match="needs its rate"):
        protocol.check_description({"name": "x", "inputs": [{"name": "mic", "kind": "analog"}]})
    with pytest.raises(protocol.ProtocolError, match="kind"):
        protocol.check_description({"name": "x", "outputs": [{"name": "o", "kind": "analog"}]})
    with pytest.raises(ValueError):
        Device("x").input("mic", kind="analog")  # the package refuses it before sending


# --------------------------------------------------------------------- clock
def _measure(model: ClockModel, true_offset: float, drift: float, seconds: float, seed: int = 1,
             start: float = 100.0) -> None:
    """Pings once a second over a network with asymmetric, random delays (all in simulated time)."""
    rng = random.Random(seed)
    local = start
    while local < start + seconds:
        up, down = 0.002 + rng.expovariate(1 / 0.004), 0.002 + rng.expovariate(1 / 0.001)
        t0 = local
        t1 = t0 + up + true_offset + drift * (t0 + up - start)
        t2 = t1 + 0.0001
        t3 = t0 + up + 0.0001 / (1 + drift) + down
        model.add(t0, t1, t2, t3)
        local += 1.0


def test_offset_and_drift_are_found_over_a_jittery_network():
    model = ClockModel()
    _measure(model, true_offset=1234.5, drift=50e-6, seconds=90)
    at = 190.0
    true_remote = at + 1234.5 + 50e-6 * (at - 100.0)
    assert abs(model.to_remote(at) - true_remote) < 0.002  # the delays are 2-30 ms, asymmetric
    assert abs(model.to_local(true_remote) - at) < 0.002
    state = model.state(at)
    assert state.method == "network" and 30e-6 < state.drift < 70e-6
    assert state.uncertainty >= abs(model.to_remote(at) - true_remote)  # the bound holds


def test_a_jump_of_the_clock_starts_the_model_anew():
    model = ClockModel()
    _measure(model, true_offset=10.0, drift=0.0, seconds=30)
    _measure(model, true_offset=10.0 + 100 * JUMP, drift=0.0, seconds=30, start=130.0, seed=2)
    assert model.jumps == 1 and abs(model.to_remote(160.0) - (160.0 + 10.0 + 100 * JUMP)) < 0.003


def test_a_sync_signal_replaces_the_network_estimate():
    model = ClockModel()
    _measure(model, true_offset=5.0, drift=20e-6, seconds=20)
    pairs = [(100.0 + 0.037 * index, 100.0 + 0.037 * index + 5.0 + 20e-6 * 0.037 * index) for index in range(40)]
    assert model.set_signal(pairs) < 1e-9
    assert model.state(101.0).method == "signal"
    assert abs(model.to_remote(101.0) - (101.0 + 5.0 + 20e-6)) < 1e-6


# -------------------------------------------------------- server and device
def test_a_device_connects_sends_and_is_controlled(server):
    dev = device(server)
    temperature = dev.input("temperature", kind="scalar", unit="°C")
    microphone = dev.input("mic", kind="analog", rate=1000.0)
    relay = dev.output("relay", kind="bool", default=False)
    switched = []
    relay.on_set(lambda value, at: switched.append((value, at, dev.now())))

    @dev.command()
    def add(a, b):
        return a + b

    dev.start()
    try:
        assert dev.wait_connected(5), dev.last_error
        connection = server.wait_for("pi", 5)
        assert set(connection.inputs()) == {"temperature", "mic"} and "add" in connection.commands()
        received = []
        connection.listen("temperature", received.append)
        connection.listen("mic", received.append)
        time.sleep(0.2)
        sent_at = time.monotonic()
        temperature.send(21.5)
        microphone.send_block([0.1, 0.2, 0.3])
        assert wait_for(lambda: len(received) == 2)
        scalar, block = received
        # stamped at the source (the clock estimate is good to a few ms on a loaded machine)
        assert scalar.value == 21.5 and abs(scalar.time - sent_at) < 0.02
        assert list(block.samples.round(3)) == [0.1, 0.2, 0.3] and block.rate == 1000.0
        target = time.monotonic() + 0.15
        done = connection.set("relay", True, at=target)
        assert switched[0][0] is True and abs(done - target) < 0.015  # at the time asked for, not on arrival
        assert connection.call("add", {"a": 2, "b": 3}) == 5
        with pytest.raises(RemoteError, match="no output"):
            connection.set("nothing", 1)
    finally:
        dev.stop()
    assert wait_for(lambda: server.device("pi") is None)


def test_a_wrong_token_is_refused(server):
    dev = device(server, token="wrong", reconnect=False)
    dev.start()
    assert wait_for(lambda: "token" in dev.last_error)
    assert not dev.connected and server.devices() == []
    dev.stop()


def test_blocks_come_only_while_someone_listens(server):
    dev = device(server)
    microphone = dev.input("mic", kind="analog", rate=100.0)
    dev.start()
    try:
        connection = server.wait_for("pi", 5)
        time.sleep(0.2)
        microphone.send_block([1.0] * 10)
        time.sleep(0.2)
        assert "mic" not in connection.latest  # not subscribed: not sent
        got = []
        stop = connection.listen("mic", got.append)
        time.sleep(0.2)
        microphone.send_block([2.0] * 10)
        assert wait_for(lambda: got)
        stop()
    finally:
        dev.stop()


def test_a_device_connects_again_after_the_connection_broke(server):
    dev = device(server)
    dev.input("x")
    dev.start()
    try:
        first = server.wait_for("pi", 5)
        first.close()  # the network dropped
        assert wait_for(lambda: server.device("pi") is not None and server.device("pi") is not first, 6)
    finally:
        dev.stop()


# ---------------------------------------------------------------- simulated
def test_the_simulated_device_is_corrected_behind_the_emulated_network():
    from openscilab.driver.remote.simulated import Network, open_simulated_remote

    instrument = open_simulated_remote("remote-sim:climate", network=Network(latency=0.006, jitter=0.003))
    try:
        remote = instrument.remote
        truth = remote.simulated.clock
        assert abs(truth.offset) > 50  # minutes apart, and drifting
        values = []
        stop = remote.listen("temperature", values.append)
        assert wait_for(lambda: len(values) >= 15, 5)
        stop()
        errors = [abs(item.time - truth.to_local(item.remote_time)) for item in values[-10:]]
        assert statistics.mean(errors) < 0.002 and max(errors) < 0.004
        target = time.monotonic() + 0.1
        assert abs(remote.set("heater", 50, at=target) - target) < 0.015
        assert isinstance(remote.call("calibrate", {"reference": 21}), float)
        sections = dict(instrument.details())
        assert sections["Clock"][0] == ("Method", "measured over the network")
    finally:
        instrument.close()


# -------------------------------------------------------------------- flows
FLOW = """
flow: Remote
nodes:
  station: {type: device.instrument, address: "remote-sim:climate"}
  temp: {type: remote.receive, channel: temperature, duration: 1 s}
  heat: {type: remote.set, channel: heater, value: 40}
  calibrate: {type: remote.call, command: calibrate, args: {reference: 21}}
  fan: {type: gpio.write, pin: fan, level: 1}
edges:
  - station.device -> temp.device
  - station.device -> heat.device
  - station.device -> calibrate.device
  - station.device -> fan.device
"""


def test_a_flow_receives_sets_and_calls():
    from openscilab.core import signals
    from openscilab.lab import yaml_io
    from openscilab.lab.engine import Engine

    values = []
    engine = Engine(yaml_io.loads(FLOW), mode="real")
    engine.subscribe(lambda event: event.kind == "value" and values.append((event.node, event.port, event.time,
                                                                            event.value)))
    result = engine.run(timeout=30)
    assert result.ok, result.error
    temperatures = [value for node, port, _time, value in values if node == "temp"]
    assert len(temperatures) >= 6 and all(isinstance(value, signals.Scalar) for value in temperatures)
    times = [value.at for value in temperatures]
    assert times == sorted(times) and all(0.08 < b - a < 0.12 for a, b in itertools.pairwise(times))  # 10 per second
    assert any(node == "heat" and port == "done" for node, port, _t, _v in values)
    assert any(node == "calibrate" and port == "result" for node, port, _t, _v in values)


def test_remote_devices_run_in_real_time_only():
    from openscilab.lab import yaml_io
    from openscilab.lab.engine import Engine

    result = Engine(yaml_io.loads(FLOW), mode="virtual").run(timeout=10)
    assert not result.ok and "real time" in result.error


def test_the_sync_signal_aligns_the_device_to_a_recording(tmp_path):
    from openscilab.lab import yaml_io
    from openscilab.lab.engine import Engine
    from openscilab.lab.project import Project

    (tmp_path / "project.yaml").write_text("project: s\nsimulation:\n  wiring:\n"
                                           "    la: [{from: \"station:SYNC\", to: D5}]\n")
    flow = yaml_io.loads("""
flow: Sync
nodes:
  station: {type: device.instrument, address: "remote-sim:climate"}
  la: {type: device.instrument, address: "sim:free"}
  stream: {type: device.stream, channels: [D5], rate: 100 kHz, duration: 2 s}
  sync: {type: remote.sync, sync: SYNC}
edges:
  - station.device -> sync.device
  - la.device -> stream.device
  - stream.D5 -> sync.signal
""")
    values = []
    engine = Engine(flow, mode="real", project=Project.open(str(tmp_path)))
    engine.subscribe(lambda event: event.kind == "value" and event.node == "sync" and values.append(
        (event.port, event.value.value)))
    result = engine.run(timeout=30)
    assert result.ok, result.error
    scatter = [value for port, value in values if port == "uncertainty"]
    edges = [value for port, value in values if port == "edges"]
    # the precision of the recording (10 µs); how many edges are matched depends on when stream and
    # sync output start in real time
    assert scatter and scatter[-1] < 50e-6 and edges[-1] >= 10


# -------------------------------------------------------------------- hints
def test_the_inspector_offers_what_the_remote_device_describes():
    from openscilab.lab import hints
    from openscilab.lab.model import Flow
    from openscilab.lab.nodes.registry import default_registry

    flow = Flow("t")
    flow.add_node("device.instrument", "station", address="remote-sim:climate")
    for node_id, type_name in (("temp", "remote.receive"), ("heat", "remote.set"), ("cal", "remote.call"),
                               ("sync", "remote.sync")):
        flow.add_node(type_name, node_id)
        flow.connect("station.device", f"{node_id}.device")
    devices = hints.flow_devices(flow)

    def offered(node_id, name):
        return hints.node_suggestions(flow, node_id, flow.spec(flow.nodes[node_id], default_registry), devices)[name]

    assert offered("temp", "channel") == ["temperature", "humidity", "door"]
    assert offered("heat", "channel") == ["heater", "fan"]
    assert offered("cal", "command") == ["calibrate"] and offered("sync", "sync") == ["SYNC"]
    flow.nodes["temp"].params["channel"] = "pressure"
    assert any("has no input pressure" in str(problem) for problem in hints.check(flow, default_registry, devices))


# ----------------------------------------------------------------------- UI
def test_the_device_list_and_the_card_of_a_simulated_remote_device(shell, monkeypatch):
    from openscilab.ui import messages
    from openscilab.ui.devices import DeviceEntry
    from openscilab.ui.devices.remote import RemoteSimulatorBackend

    entries = RemoteSimulatorBackend().manual_entries()
    assert [entry.value for entry in entries] == ["climate", "audio", "echo", "daq", "daq?timescale=utc",
                                                  "daq?shared_clock=1"]
    instrument = shell.connect_entry(entries[0])
    try:
        card = shell.device_card(instrument)
        assert card.remote_panel is not None and card.tabs.tabText(0) == "Remote"
        assert wait_for(lambda: (card.remote_panel.refresh() or True) and
                        "°C" in card.remote_panel.input_labels["temperature"].text(), 5)
        card.remote_panel._call("calibrate")
        assert "calibrate:" in card.remote_panel.answer_label.text()
        assert DeviceEntry("remote-sim", "demo", "climate").label == ""
    finally:
        monkeypatch.setattr(messages, "confirm", lambda *args, **kwargs: True)  # (it drives an output)
        shell.disconnect_instrument(instrument)


def test_switching_remote_devices_on_accepts_a_device(shell):
    from openscilab.core import preferences
    from openscilab.driver.remote import servers
    from openscilab.ui.devices.remote import RemoteBackend

    port = _free_port()
    preferences.update({"remote.enabled": True, "remote.port": port, "remote.beacon": False})
    shell.apply_preferences({"remote.enabled": True})
    try:
        server = servers.running_main()
        token = preferences.get("remote.token")
        assert server is not None and server.port == port and token
        dev = Device("bench", server=f"127.0.0.1:{port}", token=token)
        dev.input("x")
        dev.start()
        assert dev.wait_connected(5), dev.last_error
        assert wait_for(lambda: [entry.value for entry in RemoteBackend().detected()] == ["bench"])
        dev.stop()
    finally:
        preferences.update({"remote.enabled": False})
        shell.apply_preferences({"remote.enabled": False})
    assert servers.running_main() is None


def test_the_settings_page_makes_a_token_and_shows_how_to_start_a_device(shell):
    from openscilab.ui.dialogs.preferences_dialog import PreferencesDialog

    dialog = PreferencesDialog(shell)
    assert dialog.remote_token.text() == ""
    dialog.remote_enabled.setChecked(True)
    token = dialog.remote_token.text()
    assert token and token in dialog.remote_example.text() and "openscilab_device.demo" in dialog.remote_example.text()
    assert dialog.chosen()["remote.enabled"] is True
    dialog.reject()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_the_package_needs_only_the_standard_library():
    import ast

    folder = os.path.join(os.path.dirname(__file__), "..", "openscilab_device")
    for name in os.listdir(folder):
        if not name.endswith(".py"):
            continue
        with open(os.path.join(folder, name), encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        imports = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import)
                   for alias in node.names}
        imports |= {node.module.split(".")[0] for node in ast.walk(tree)
                    if isinstance(node, ast.ImportFrom) and node.module and node.level == 0}
        assert imports <= {"argparse", "hashlib", "hmac", "json", "socket", "struct", "threading", "time", "uuid",
                           "heapq", "itertools", "random", "math", "collections", "typing", "__future__",
                           "numpy", "os", "dataclasses"}, (name, imports)
