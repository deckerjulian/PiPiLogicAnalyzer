"""The example library (examples/library, lab/examples.py) and its menu: every example is complete,
valid, runs with its simulators and passes its own checks."""

from __future__ import annotations

import os

import pytest

from openscilab.lab import examples, hints, panel_model, templates
from openscilab.lab.engine import Engine
from openscilab.lab.flow_files import load_flow
from openscilab.lab.project import Project

DECODERS = os.path.isdir(os.path.join(os.path.dirname(__file__), "..", "decoders", "uart"))
EXAMPLES = examples.examples()


def test_the_library_has_categories_with_examples():
    catalog = examples.catalog()
    assert len(catalog) >= 10 and len(EXAMPLES) >= 50
    assert catalog[0].title == "Start a project" and catalog[0].examples[1].title == "Logic Analyzer"
    assert catalog[1].title == "Basics" and catalog[1].examples[0].title == "First capture"
    assert all(example.icon for example in EXAMPLES)  # (its own, or its category's)
    keys = [example.key for example in EXAMPLES]
    assert keys == sorted(keys) and len(set(keys)) == len(keys)  # in the order of their folders
    for category in catalog:
        assert category.description, category.key
    for example in EXAMPLES:
        assert example.title and len(example.description) > 40, example.key
        assert examples.find(example.key) == example


@pytest.mark.parametrize("example", EXAMPLES, ids=[example.key for example in EXAMPLES])
def test_an_example_runs_and_passes_its_checks(example, tmp_path):
    if not DECODERS and "decode." in open_texts(example.path):
        pytest.skip("the sigrok decoders are not installed in ./decoders")
    root = templates.copy_project(example.path, str(tmp_path / "copy"))
    project = Project.open(root)
    assert templates.files_to_open(root) or templates.views_to_open(root), "the example opens nothing"
    assert project.flows() or templates.views_to_open(root) == ["data"]  # (the Logic Analyzer: a data view)
    for path in project.flows():
        flow = project.complete(load_flow(path, registry=project.registry()))
        assert [str(p) for p in flow.validate(project.registry())] == [], path  # (no warnings either)
        devices = hints.flow_devices(flow, project.devices)
        assert [str(p) for p in hints.check(flow, project.registry(), devices)] == [], path  # no warnings
        if path.endswith(".flow.yaml"):  # (a script places nothing)
            assert all(node.position is not None for node in flow.nodes.values()), f"{path}: arrange it"
        engine = Engine(flow, registry=project.registry(), mode="real" if example.realtime else "virtual",
                        project=project,
                        duration=None if flow.settings.get("duration") else (2.0 if example.realtime else 5.0))
        result = engine.run(timeout=60)
        assert result.ok, f"{path}: {result.error}"
        failed = [item for item in engine.views.items if item[0] == "check" and not item[2]]
        assert failed == [], failed
    for path in project.panels():
        panel = panel_model.load(path)
        flow = load_flow(panel_model.flow_path(panel, path), registry=project.registry())
        assert panel.problems(flow, project.registry()) == [], path


def open_texts(folder: str) -> str:
    text = ""
    for directory, _folders, files in os.walk(folder):
        for name in files:
            if name.endswith((".yaml", ".py")):
                with open(os.path.join(directory, name), encoding="utf-8") as handle:
                    text += handle.read()
    return text


# ----------------------------------------------------------------------- menu
def test_the_examples_menu_lists_the_categories(shell):
    menu = shell.examples_menu
    titles = [action.text() for action in menu.actions()]
    assert titles[:6] == ["Save project as &template...", "All templates (start page)", "", "Start a project",
                          "Basics", "Digital I/O"]
    basics = menu.actions()[4].menu()
    assert [action.text() for action in basics.actions()][:2] == ["First capture", "Edge trigger"]
    assert basics.actions()[0].toolTip().startswith("Captures the 8 bit counter")
    assert len(shell.example_actions) == len(EXAMPLES)
    commands = [command.title for command in shell.commands()]
    assert any("Templates" in title and "Blink" in title for title in commands)  # also in the palette


def test_an_example_opens_as_a_temporary_copy(shell):
    from openscilab.ui.documents.flow import FlowDocument
    from openscilab.ui.documents.panel import PanelDocument

    example = examples.find("09-panels/01-control-panel")
    before = open_texts(example.path)
    shell.example_actions[example.key].trigger()
    documents = shell.documents()
    flows = [document for document in documents if isinstance(document, FlowDocument)]
    panels = [document for document in documents if isinstance(document, PanelDocument)]
    assert flows and panels and isinstance(shell.active_document(), PanelDocument)
    flow = flows[-1]
    assert not flow.path.startswith(os.path.abspath(example.path)) and shell.is_temporary(flow)
    flow.set_param("pwm", "duty", 0.9)  # edits the copy (saving asks where to keep it)
    assert flow.flow.nodes["pwm"].params["duty"] == 0.9 and open_texts(example.path) == before


# ------------------------------------------------------------------- coverage
def _node_types_of_examples() -> set[str]:
    """Every node type the flows of the examples use (files and scripts, also inside subflows)."""
    found: set[str] = set()
    for example in EXAMPLES:
        project = Project.open(example.path)
        for path in project.flows():
            flow = load_flow(path, registry=project.registry())
            stack = [flow]
            while stack:
                current = stack.pop()
                found |= {node.type for node in current.nodes.values()}
                stack.extend(current.subflows.values())
    return found


def test_every_node_type_is_used_by_an_example():
    """Every node of the palette (but the sigrok decoders, which are all alike) shows up in an example
    that runs and checks its result - so every node is known to work and to make sense."""
    from openscilab.lab.nodes.registry import default_registry

    types = {name for name in default_registry.types() if not name.startswith("decode.")}
    missing = sorted(types - _node_types_of_examples())
    assert missing == [], f"node types without an example: {missing}"
