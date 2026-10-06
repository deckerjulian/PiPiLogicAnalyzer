"""Simulated remote devices whose timings can be set (network, drift, clock, sample clock), no stray
widgets on device cards, and a higher priority for the processes that read devices (asking for the
rights on macOS and Linux)."""

from __future__ import annotations

import os
import subprocess
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QWidget

from openscilab.core import preferences, priority
from openscilab.driver.remote import simulated as remote_sim
from openscilab.ui import messages


def settle(times: int = 20) -> None:
    for _ in range(times):
        QApplication.processEvents()


# ------------------------------------------------------------ device cards
@pytest.mark.parametrize("address", ["sim:pico", "rigol-sim:bridge", "remote-sim:echo", "arduino-sim:uno"])
def test_a_device_card_has_no_widget_outside_its_layout(shell, address):
    """A child of the card left out of its layout showed as a black box in its corner."""
    from openscilab.lab.engine.devices import open_instrument

    instrument = open_instrument(address)
    shell.hub.add(instrument)
    card = shell.open_device_card(instrument)
    settle()
    managed: set[int] = set()

    def walk(layout) -> None:
        for index in range(layout.count()):
            item = layout.itemAt(index)
            if item.widget() is not None:
                managed.add(id(item.widget()))
            if item.layout() is not None:
                walk(item.layout())

    walk(card.layout())
    stray = [child for child in card.findChildren(QWidget) if child.parentWidget() is card and child.isVisible()
             and not child.isWindow() and id(child) not in managed]
    assert stray == []


# ------------------------------------------------------- remote simulators
def test_the_timings_of_a_simulated_remote_device_are_in_its_address():
    assert remote_sim.parse("remote-sim:daq#2?latency=20&jitter=1.5&drift=-200&timescale=utc") == (
        "daq", 2, {"latency": 20.0, "jitter": 1.5, "drift": -200.0, "timescale": "utc"})
    assert remote_sim.address("daq", 2, {"latency": 20.0, "shared_clock": "1"}) == \
        "remote-sim:daq#2?latency=20&shared_clock=1"
    with pytest.raises(ValueError, match="latency is a number of ms"):
        remote_sim.parse("remote-sim:daq?latency=fast")
    with pytest.raises(ValueError, match="drift is a number of ppm"):
        remote_sim.parse("remote-sim:daq?drift=1e9")


def test_a_simulated_remote_device_changes_its_network_and_drift_while_it_runs():
    instrument = remote_sim.open_simulated_remote("remote-sim:echo?latency=10&jitter=0&drift=100")
    try:
        simulated = instrument.remote.simulated
        assert simulated.settings() == pytest.approx({"latency": 0.01, "jitter": 0.0, "drift": 100e-6})
        before = simulated.clock()
        simulated.configure(latency=0.03, jitter=0.001, drift=-50e-6)
        assert abs(simulated.clock() - before) < 0.05  # no jump of the device's clock
        assert simulated.settings() == pytest.approx({"latency": 0.03, "jitter": 0.001, "drift": -50e-6})
        assert simulated.network.latency == 0.03  # (the emulated network delays by it at once)
        with pytest.raises(ValueError):
            simulated.configure(latency=-1)
    finally:
        instrument.close()
    utc = remote_sim.open_simulated_remote("remote-sim:echo?timescale=utc")
    try:
        assert utc.remote.simulated.settings()["drift"] is None  # it follows UTC: no drift of its own
    finally:
        utc.close()


def test_the_device_list_offers_the_timings_of_a_remote_daq(shell):
    from openscilab.ui.devices.remote import RemoteSimulatorBackend

    entries = {entry.value: entry for entry in RemoteSimulatorBackend().manual_entries()}
    assert {"daq", "daq?timescale=utc", "daq?shared_clock=1"} <= set(entries)
    assert all(entry.simulated for entry in entries.values())
    first = shell.connect_entry(entries["daq?timescale=utc"])
    second = shell.connect_entry(entries["daq?timescale=utc"])
    try:
        assert first.name == "Remote DAQ (UTC)" and first.uri == "remote-sim:daq?timescale=utc"
        assert second.name == "Remote DAQ (UTC) (2)" and second.uri == "remote-sim:daq#2?timescale=utc"
    finally:
        shell.disconnect_all(ask=False)


def test_the_remote_tab_sets_the_timings_of_a_simulated_device(shell):
    from openscilab.ui.devices.remote import RemoteSimulatorBackend

    entry = next(entry for entry in RemoteSimulatorBackend().manual_entries() if entry.value == "daq")
    instrument = shell.connect_entry(entry)
    card = shell.device_card(instrument)
    panel = card.remote_panel
    assert panel.sim_spins["latency"].value() == pytest.approx(4.0) and not panel.sim_shared_box.isHidden()
    panel.sim_spins["latency"].setValue(25.0)
    panel.sim_spins["drift"].setValue(300.0)
    assert panel.apply_simulation()
    assert instrument.remote.simulated.settings()["latency"] == pytest.approx(0.025)
    assert instrument.uri == "remote-sim:daq?latency=25&jitter=2&drift=300"  # kept (reconnect, session)
    # another clock: the device starts again with it, its card takes the place of the old one
    panel.sim_clock_box.setCurrentIndex(panel.sim_clock_box.findData("utc"))
    panel.sim_shared_box.setChecked(True)
    panel.restart_simulation()
    settle()
    new = next(item for item in shell.hub.instruments())
    assert new is not instrument and new.uri == "remote-sim:daq?latency=25&jitter=2&timescale=utc&shared_clock=1"
    assert new.remote.simulated.settings()["drift"] is None and new.name == instrument.name
    new_card = shell.device_card(new)
    assert new_card.remote_panel.sim_clock_box.currentData() == "utc" and new_card.remote_panel.sim_shared_box.isChecked()
    shell.disconnect_all(ask=False)


# ---------------------------------------------------------------- priority
def test_the_priority_is_raised_without_asking_where_it_may(monkeypatch):
    if sys.platform == "win32":
        pytest.skip("Windows raises without rights")

    def denied(*_args):
        raise PermissionError("not allowed")

    monkeypatch.setattr(os, "setpriority", denied)
    assert not priority.raise_own()
    calls = []
    monkeypatch.setattr(os, "setpriority", lambda which, who, value: calls.append(value))
    monkeypatch.setattr(priority, "current", lambda pid=0: -10 if calls else 0)
    assert priority.raise_own() and calls == [priority.NICE]
    assert priority.describe() == "raised (nice -10)"


def test_the_rights_are_asked_for_with_the_password_dialog_of_the_system(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    command = priority.rights_command([12, 34])
    assert command[0] == "/usr/bin/osascript" and "renice -n -10 -p 12 34" in command[2]
    assert "with administrator privileges" in command[2]
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(priority.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert priority.rights_command([12]) == ["/usr/bin/pkexec", "/usr/bin/renice", "-n", "-10", "-p", "12"]
    monkeypatch.setattr(priority.shutil, "which", lambda name: None)
    assert priority.rights_command([12]) is None
    with pytest.raises(priority.PriorityError, match="pkexec"):
        priority.raise_with_rights([12])
    # granted / cancelled
    monkeypatch.setattr(sys, "platform", "darwin")
    raised: set[int] = set()

    def run(command, **_kwargs):
        if "deny" in os.environ.get("TEST_PRIORITY", ""):
            return subprocess.CompletedProcess(command, 1, "", "User canceled. (-128)")
        raised.update({12, 34})
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(priority.subprocess, "run", run)
    monkeypatch.setattr(priority, "is_raised", lambda pid=0: pid in raised)
    assert priority.raise_with_rights([12, 34]) == [12, 34]
    raised.clear()
    monkeypatch.setenv("TEST_PRIORITY", "deny")
    with pytest.raises(priority.PriorityError, match="cancelled"):
        priority.raise_with_rights([12, 34])


def test_the_application_asks_once_and_the_decoder_process_lowers_itself(shell, monkeypatch):
    if sys.platform == "win32":
        pytest.skip("Windows raises without rights")
    monkeypatch.setattr(priority, "raise_own", lambda nice=priority.NICE: False)
    monkeypatch.setattr(priority, "is_raised", lambda pid=0: False)
    asked, granted = [], []
    monkeypatch.setattr(messages, "choose", lambda parent, title, text, options, details=None:
                        asked.append(options) or 1)
    assert not shell.raise_priority()  # "Not now"
    assert asked[0][:2] == ["Raise the priority", "Not now"]
    monkeypatch.setattr(messages, "choose", lambda *args, **kwargs: 0)
    monkeypatch.setattr(priority, "raise_with_rights", lambda: granted.append(True) or [os.getpid()])
    assert shell.raise_priority() and granted
    assert "higher priority" in shell.statusBar().currentMessage()
    # switched on in the settings: asked at once
    granted.clear()
    shell.apply_preferences({"devices.high_priority": True})
    assert granted
    assert preferences.DEFAULTS["devices.high_priority"] is False  # (off unless the user wants it)
    # the decoder process takes over the raised priority and gives it back
    lowered = []
    monkeypatch.setattr(priority, "current", lambda pid=0: -10)
    monkeypatch.setattr(os, "setpriority", lambda which, who, value: lowered.append(value))
    priority.lower_own()
    assert lowered == [0]


def test_a_device_process_shows_its_priority():
    from openscilab.driver.process import open_instrument

    instrument = open_instrument("sim:pico")
    try:
        rows = dict(next(rows for title, rows in instrument.details() if title == "Process"))
        assert "nice" in rows["Priority"] or sys.platform == "win32"
    finally:
        instrument.close()
    time.sleep(0.1)
