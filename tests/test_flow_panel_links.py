"""Flows and their panels find each other: the flow editor lists the panels that use the flow (open
ones and panel files) and opens them, the panel editor opens its flow."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from openscilab.lab import panel_model
from openscilab.lab.panel_model import Panel
from openscilab.ui.documents.flow import FlowDocument
from openscilab.ui.documents.panel import PanelDocument

FLOW = "flow: Meter\nnodes: {}\n"


def write_files(folder):
    flow = folder / "meter.flow.yaml"
    flow.write_text(FLOW)
    (folder / "other.flow.yaml").write_text("flow: Other\nnodes: {}\n")
    panel_model.save(Panel(name="Meter panel", flow="meter.flow.yaml"), str(folder / "meter.panel.yaml"))
    panel_model.save(Panel(name="Other panel", flow="other.flow.yaml"), str(folder / "other.panel.yaml"))
    return str(flow)


def test_a_new_panel_of_an_unsaved_flow_is_listed_and_opens_its_flow(shell):
    flow = shell.new_flow()
    assert flow.panels() == []
    flow.action_panel.trigger()  # the button still makes a panel
    panel = shell.active_document()
    assert isinstance(panel, PanelDocument)
    assert flow.panels() == [("Panel", panel)]
    assert panel.action_open_flow.isEnabled()
    assert panel.open_flow()
    assert shell.active_document() is flow


def test_the_flow_lists_the_panel_files_that_use_it_and_opens_them(shell, tmp_path):
    path = write_files(tmp_path)
    flow = shell.open_file(path)
    assert isinstance(flow, FlowDocument)
    panels = flow.panels()
    assert panels == [("Meter panel", str(tmp_path / "meter.panel.yaml"))]  # not the panel of the other flow
    flow.panels_menu.aboutToShow.emit()
    entries = [action for action in flow.panels_menu.actions() if action.text()]
    assert [action.text() for action in entries] == ["Meter panel", "New panel for this flow"]
    entries[0].trigger()
    panel = shell.active_document()
    assert isinstance(panel, PanelDocument) and panel.panel.name == "Meter panel"
    assert flow.panels() == [("Meter panel", panel)]  # now the open document, only once
    # the panel opens its flow: the open document is shown
    shell.area.activate(panel)
    panel.action_open_flow.trigger()
    assert shell.active_document() is flow


def test_the_flow_inspector_lists_the_panels(shell, tmp_path):
    path = write_files(tmp_path)
    flow = shell.open_file(path)
    flow._inspector = None
    inspector = flow.inspector_widget()
    assert [button.text() for button in inspector.panel_buttons] == ["Meter panel"]
    inspector.panel_buttons[0].click()
    assert isinstance(shell.active_document(), PanelDocument)


def test_a_panel_opens_its_flow_file(shell, tmp_path):
    write_files(tmp_path)
    panel = shell.open_file(str(tmp_path / "meter.panel.yaml"))
    assert isinstance(panel, PanelDocument) and panel.action_open_flow.isEnabled()
    panel.open_flow()
    flow = shell.active_document()
    assert isinstance(flow, FlowDocument) and os.path.samefile(flow.path, tmp_path / "meter.flow.yaml")
    assert panel.open_flow() and shell.active_document() is flow  # not opened twice


def test_a_panel_without_a_flow_has_nothing_to_open(shell):
    panel = shell.new_panel(panel=Panel(name="Alone"))
    assert not panel.open_flow()
