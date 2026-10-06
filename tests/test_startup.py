"""What the application does (and does not do) when it starts."""

from __future__ import annotations

from PySide6.QtWidgets import QApplication

from openscilab.lab.nodes import decode
from openscilab.lab.nodes.registry import Registry
from openscilab.ui.flow.palette import PENDING_ROLE, NodePalette


def groups(palette) -> dict[str, int]:
    tree = palette.tree
    return {tree.topLevelItem(index).text(0): tree.topLevelItem(index).childCount()
            for index in range(tree.topLevelItemCount())}


def test_the_palette_loads_the_decoders_when_they_are_looked_at(qtbot):
    registry = Registry()
    palette = NodePalette(registry)
    qtbot.addWidget(palette)
    assert registry.pending_groups() == ["decode"]  # not imported for showing the palette
    assert not any(name.startswith("decode.") for name in palette.types() if name)
    pending = [palette.tree.topLevelItem(index) for index in range(palette.tree.topLevelItemCount())
               if palette.tree.topLevelItem(index).data(0, PENDING_ROLE)]
    assert len(pending) == 1

    pending[0].setExpanded(True)
    QApplication.processEvents()
    assert registry.pending_groups() == []
    assert any(name.startswith("decode.") for name in palette.types())
    assert all(count > 0 for count in groups(palette).values())


def test_searching_finds_decoders_that_were_not_loaded(qtbot):
    registry = Registry()
    palette = NodePalette(registry)
    qtbot.addWidget(palette)
    palette.search.setText("uart")
    assert "decode.uart" in palette.types()


def test_flows_use_the_decoders_of_the_application(shell):
    assert decode.decoder_registry() is shell.provider.registry
    assert shell.decoders().registry is shell.provider.registry


# ------------------------------------------------------------ Qt stays in ui/
def test_the_core_the_drivers_and_the_lab_load_without_qt():
    """Scripts, the command line and the flow engine work without the user interface: none of
    their modules imports Qt (a subprocess, because this test run has Qt loaded)."""
    import subprocess
    import sys

    modules = ("openscilab.api", "openscilab.cli", "openscilab.lab", "openscilab.lab.engine",
               "openscilab.lab.nodes.registry", "openscilab.core.capture_io", "openscilab.core.compare",
               "openscilab.core.regions", "openscilab.core.profiles", "openscilab.driver.simulated",
               "openscilab.driver.pico.analyzer", "openscilab.driver.dslogic.driver", "openscilab.sigrok.provider")
    code = (f"import sys\nimport {', '.join(modules)}\n"
            "from openscilab.lab.nodes.registry import default_registry\n"
            "default_registry.specs(lazy=False)\n"  # the built-in node modules
            "loaded = sorted(name for name in sys.modules if name.startswith('PySide6'))\n"
            "print(','.join(loaded))\n")
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


def test_the_api_loads_the_lab_on_demand():
    import subprocess
    import sys

    code = ("import sys\nfrom openscilab import api\n"
            "before = 'openscilab.lab' in sys.modules\n"
            "engine = api.lab.Engine\n"
            "print(before, 'openscilab.lab' in sys.modules, engine.__name__)\n")
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert result.stdout.split() == ["False", "True", "Engine"], result.stderr


def test_regions_hold_their_colour_as_numbers(make_dataview):
    from openscilab.core.regions import DEFAULT_REGION_COLOR, SampleRegion
    from openscilab.ui import colors

    region = SampleRegion(first_sample=1, last_sample=9, region_name="r")
    assert region.region_color == DEFAULT_REGION_COLOR == (255, 255, 255, 64)
    stored = region.to_dict()
    assert (stored["R"], stored["G"], stored["B"], stored["A"]) == (255, 255, 255, 64)
    again = SampleRegion.from_dict({**stored, "R": 10, "A": 200})
    assert again.region_color == (10, 255, 255, 200)
    shown = colors.region_color(again)
    assert (shown.red(), shown.alpha()) == (10, 200)


# --------------------------------------------------------------- the window
def test_the_window_does_not_repeat_the_style_of_the_application(shell):
    from openscilab.ui.shell.main_window import ShellWindow
    from openscilab.ui.theme import SHELL_STYLESHEET

    application = QApplication.instance()
    before = application.styleSheet()
    try:
        application.setStyleSheet("")
        assert len(ShellWindow._window_stylesheet()) > len(SHELL_STYLESHEET)  # a window on its own
        application.setStyleSheet("QWidget { }")
        assert ShellWindow._window_stylesheet() == SHELL_STYLESHEET
    finally:
        application.setStyleSheet(before)


def test_a_restart_keeps_the_session(shell, monkeypatch):
    from PySide6.QtCore import QProcess
    from PySide6.QtGui import QCloseEvent

    saved = []
    monkeypatch.setattr(shell, "save_state", lambda: saved.append(len(shell.documents())))
    monkeypatch.setattr(QProcess, "startDetached", staticmethod(lambda *args: True))
    monkeypatch.setattr(QApplication.instance(), "quit", lambda: None)
    shell.new_flow()
    assert shell.restart()
    shell.closeEvent(QCloseEvent())  # what quitting does to the window
    assert saved == [1]  # once, with the document – not again without it
    shell._restarting = False
