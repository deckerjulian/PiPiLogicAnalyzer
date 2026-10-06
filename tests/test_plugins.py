"""Plugins (openscilab/plugins/, driver/kinds.py): a device that is not built in, added from the
plugins folder, works like a built-in one - in the device list, flows, the command line, the Python
API and a device process. A plugin that fails is reported, the others load; an address of a kind
nobody knows says so."""

from __future__ import annotations

import os
import shutil
from importlib.metadata import EntryPoint

import numpy as np
import pytest

from openscilab import plugins
from openscilab.driver import discovery, kinds
from openscilab.driver.base import DeviceConnectionError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLE = os.path.join(ROOT, "examples", "plugins", "counter_device.py")


def setup_entry() -> None:  # an entry point's function (see test_installed_packages_add_plugins)
    from openscilab_plugin_counter_device import open_counter

    kinds.register("counter-pkg", open_counter, title="Counter of a package")


@pytest.fixture(autouse=True)
def fresh_plugins():
    plugins.reset()
    yield
    plugins.reset()
    kinds.unregister("counter-pkg")


@pytest.fixture
def folder():
    from openscilab.core import settings

    path = os.path.join(settings.settings_directory(), plugins.FOLDER)
    os.makedirs(path, exist_ok=True)
    shutil.copy(EXAMPLE, path)
    return path


def test_a_plugin_of_the_plugins_folder_adds_a_kind_of_device(folder):
    found = plugins.load()
    assert [plugin.name for plugin in found if not plugin.builtin] == ["counter_device"] and not plugins.problems()
    assert [plugin.name for plugin in found if plugin.builtin] == list(plugins.BUILT_IN)
    counter = kinds.find("counter")
    assert counter is not None and counter.title == "Counter" and not counter.process
    assert any(info.id == "counter:8" and info.label == "Counter (8 channels)" for info in discovery.list_devices())
    driver = discovery.open_device("counter:4")  # (not a host and port)
    assert driver.channel_count == 4 and driver.device_version == "Counter (4 channels)"
    with pytest.raises(DeviceConnectionError, match="1 to 24"):
        discovery.open_device("counter:99")


def test_the_python_api_and_the_command_line_open_it(folder, tmp_path, capsys):
    from openscilab import api, cli

    with api.open("counter:8") as device:
        capture = device.capture(channels=[0, 1, 2], rate="1M", samples=64)
    assert list(capture.samples(1)[:8]) == [0, 0, 1, 1, 0, 0, 1, 1]
    output = tmp_path / "counter.csv"
    assert cli.main(["capture", "-d", "counter:4", "-c", "0-3", "-n", "100", "-q", "-o", str(output)]) == 0
    assert output.read_text().splitlines()[2] == "1,0,0,0"
    assert cli.main(["plugins"]) == 0
    listed = capsys.readouterr().out
    assert "counter_device\tloaded" in listed and "counter:\tCounter" in listed


def test_a_flow_opens_it_by_its_address_and_simulates_it(folder):
    from openscilab.lab import yaml_io
    from openscilab.lab.engine import Engine
    from openscilab.lab.engine.devices import simulation_profile_for

    flow = yaml_io.loads("""
flow: Counter
nodes:
  counter: {type: device.instrument, address: "counter:8"}
  cap: {type: device.capture, channels: [0, 1, 2], rate: 1 MHz, samples: 64}
edges:
  - counter.device -> cap.device
""")
    engine = Engine(flow)
    captures = []
    engine.subscribe(lambda event: event.kind == "value" and event.port == "capture" and captures.append(event.value))
    assert engine.run(timeout=20).ok
    assert type(engine.devices["counter"].capture.driver).__name__ in ("CounterDriver", "SoftwareTriggerDriver")
    samples = captures[0].session.capture_channels[2].samples
    assert np.array_equal(samples[:8], [0, 0, 0, 0, 1, 1, 1, 1])
    assert simulation_profile_for("counter:8") == "free"
    assert Engine(flow, simulate=True).run(timeout=20).ok  # (the simulator profile of the kind)


def test_it_opens_in_a_device_process(folder):
    from openscilab.api import open as open_device
    from openscilab.driver import process

    kinds.register("counter-usb", kinds.find("counter").open, process=True)  # (as a device on USB)
    from openscilab.core import preferences

    preferences._load()["devices.process"] = True
    assert process.wanted("counter-usb:8") and not process.wanted("counter:8")
    # the device process loads the plugins itself (the settings directory is passed on)
    with open_device("counter:8", process=True) as device:
        capture = device.capture(channels=[0, 3], rate="1M", samples=32)
        assert device.driver.process.pid != os.getpid()
    assert list(capture.samples(3)[:16]) == [0] * 8 + [1] * 8
    kinds.unregister("counter-usb")


def test_a_plugin_that_fails_is_reported_and_the_others_load(folder, capsys):
    with open(os.path.join(folder, "broken.py"), "w") as handle:
        handle.write("raise RuntimeError('no hardware library')\n")
    with open(os.path.join(folder, "wrong_kind.py"), "w") as handle:
        handle.write("from openscilab.driver import kinds\nkinds.register('pico', print)\n")
    found = {plugin.name: plugin for plugin in plugins.load()}
    assert found["counter_device"].error == "" and kinds.find("counter") is not None
    assert "no hardware library" in found["broken"].error and "Traceback" in found["broken"].details
    assert "of openSciLab itself" in found["wrong_kind"].error
    from openscilab import cli

    assert cli.main(["plugins"]) == 1
    assert cli.main(["sim"]) == 0
    assert "warning: the plugin broken could not be loaded" in capsys.readouterr().err


def test_kinds_are_checked_and_an_unknown_kind_says_so():
    with pytest.raises(ValueError, match="openSciLab itself"):
        kinds.register("dslogic", print)
    with pytest.raises(ValueError, match="cannot name"):
        kinds.register("my device", print)
    with pytest.raises(DeviceConnectionError, match="Unknown kind of device 'nokind'"):
        discovery.open_device("nokind:/dev/ttyUSB0")
    assert kinds.split("COM5") == ("pico", "COM5") and kinds.split("192.168.1.5:4045") == ("pico", "192.168.1.5:4045")
    assert kinds.split("C:\\device") == ("pico", "C:\\device")
    assert kinds.split("mydevice:/dev/ttyUSB0") == ("mydevice", "/dev/ttyUSB0")
    assert kinds.split("dslogic") == ("dslogic", "")


def test_the_devices_of_openscilab_are_plugins_as_well():
    from openscilab.lab.engine.devices import open_instrument, simulation_profile_for

    pico = kinds.find("pico")
    assert pico.builtin and pico.module == "openscilab.plugins.pico" and pico.process and pico.simulation == "pico"
    assert [kind.kind for kind in kinds.registered() if kind.builtin] == [
        "pico", "pico-net", "pico-multi", "arduino", "arduino-sim", "dslogic", "rigol", "rigol-sim", "sim", "emulated",
        "remote", "remote-sim"]
    assert simulation_profile_for("COM5") == "pico" and simulation_profile_for("arduino:COM3") == "uno"
    assert simulation_profile_for("rigol:192.168.1.20") == "dho924s"
    with pytest.raises(DeviceConnectionError, match="no capture driver"):
        discovery.open_device("remote:pi")
    with pytest.raises(ValueError, match="has no simulator"):
        open_instrument("remote:pi", simulate=True)
    with pytest.raises(DeviceConnectionError, match="serial port"):
        discovery.open_device("arduino:")


def test_a_kind_opens_instruments_with_what_its_opener_takes(folder):
    from openscilab.driver.simulated import open_simulated
    from openscilab.lab.engine.devices import open_instrument

    opened = []

    def open_box(rest, seed=1):
        opened.append((rest, seed))
        return open_simulated("free", seed=seed)

    kinds.register("box", instrument=open_box, simulation=None)
    try:
        open_instrument("box:7", seed=3, fast=True).close()  # (fast: not taken)
        assert opened == [("7", 3)]
        with pytest.raises(DeviceConnectionError, match="no capture driver"):
            discovery.open_device("box:7")
    finally:
        kinds.unregister("box")
    with pytest.raises(ValueError, match="needs a function"):
        kinds.register("box")
    # an option the opener does not take is left out
    assert discovery.open_device("counter:4", download_bitstream=True).channel_count == 4


def test_installed_packages_add_plugins(folder, monkeypatch):
    import importlib.metadata

    points = [EntryPoint("counter-pkg", f"{__name__}:setup_entry", plugins.GROUP),
              EntryPoint("missing", "no_such_module_here", plugins.GROUP)]
    monkeypatch.setattr(importlib.metadata, "entry_points", lambda group=None: points if group == plugins.GROUP else [])
    found = {plugin.name: plugin for plugin in plugins.load()}
    assert kinds.find("counter-pkg").title == "Counter of a package" and not found["counter-pkg"].error
    assert "ModuleNotFoundError" in found["missing"].error


def test_the_device_list_shows_the_devices_of_a_plugin(folder, shell, monkeypatch):
    from openscilab.ui import devices
    from openscilab.ui.devices import pico as pico_devices

    monkeypatch.setattr(pico_devices.detector, "detect", list)
    plugins.load(ui=True)
    backend = next(backend for backend in devices.backends() if backend.id == "counter")
    assert isinstance(backend, devices.KindBackend)
    section = shell.devices_section
    section.refresh()
    labels = [section.list.item(row).text() for row in range(section.list.count())]
    assert "Counter (8 channels)" in labels
    entry = backend.detected()[0]
    instrument = shell.connect_entry(entry)
    try:
        assert instrument.uri == "counter:8" and instrument.capture.driver.channel_count == 8
    finally:
        shell.disconnect_instrument(instrument)


def test_a_plugin_brings_a_backend_of_its_own_in_setup_ui(folder, monkeypatch):
    from openscilab.ui import devices

    devices.backends()  # (openSciLab's own register theirs)
    monkeypatch.setattr(devices, "_added", list(devices._added))
    with open(os.path.join(folder, "counter_ui.py"), "w") as handle:
        handle.write(
            "from openscilab.ui.devices import DeviceBackend, DeviceEntry, register_backend\n"
            "class CounterBackend(DeviceBackend):\n"
            "    id = 'counter'\n"
            "    def manual_entries(self):\n"
            "        return [DeviceEntry(self.id, 'ask', None, 'Counter with channels...')]\n"
            "def setup_ui():\n"
            "    register_backend(CounterBackend())\n")
    plugins.reset()  # (loaded with openSciLab's own above)
    before = list(devices._added)
    plugins.load()
    assert devices._added == before  # (the command line and device processes stay without Qt)
    plugins.load(ui=True)
    ids = [backend.id for backend in devices.backends()]
    assert ids.count("counter") == 1  # its own backend replaces the generic one
    assert type(next(b for b in devices.backends() if b.id == "counter")).__name__ == "CounterBackend"


def test_help_plugins_shows_what_is_loaded(folder, shell, monkeypatch):
    from openscilab.ui import messages

    with open(os.path.join(folder, "broken.py"), "w") as handle:
        handle.write("raise RuntimeError('no hardware library')\n")
    plugins.reset()  # (the shell loaded them before: plugins load once, when openSciLab starts)
    shown = []
    monkeypatch.setattr(messages, "info", lambda parent, title, text, details=None: shown.append((text, details)))
    shell.show_plugins()
    text, details = shown[0]
    assert text == f"{len(plugins.BUILT_IN) + 1} plugins are loaded, 1 could not be."
    assert "counter: Counter" in details and "no hardware library" in details and folder in details
