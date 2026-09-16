"""The packaged application: PyInstaller spec, application icon and start-up check."""

from __future__ import annotations

import ast
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DECODERS = os.path.join(ROOT, "decoders")


@pytest.fixture(scope="module")
def application():
    return QApplication.instance() or QApplication([])


def spec_decoder_modules() -> set[str]:
    with open(os.path.join(ROOT, "packaging", "pipilogicanalyzer.spec"), encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "DECODER_MODULES" for target in node.targets
        ):
            return set(ast.literal_eval(node.value))
    raise AssertionError("DECODER_MODULES not found in the spec")


def test_packaged_build_includes_every_module_the_decoders_import():
    if not os.path.isdir(DECODERS):
        pytest.skip("no decoders folder in the project")

    local = {"sigrokdecode"} | {
        entry for entry in os.listdir(DECODERS) if os.path.isdir(os.path.join(DECODERS, entry))
    }
    imported: set[str] = set()
    for directory, _folders, files in os.walk(DECODERS):
        for name in files:
            if not name.endswith(".py"):
                continue
            with open(os.path.join(directory, name), encoding="utf-8") as handle:
                tree = ast.parse(handle.read())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported |= {alias.name.split(".")[0] for alias in node.names}
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    imported.add(node.module.split(".")[0])

    missing = imported - local - spec_decoder_modules() - set(sys.builtin_module_names)
    assert not missing, f"add to DECODER_MODULES in packaging/pipilogicanalyzer.spec: {sorted(missing)}"


def test_application_icon_renders(application):
    from pipilogicanalyzer.ui.icons import app_icon, render_app_icon

    image = render_app_icon(64)
    assert image.width() == 64 and image.pixelColor(32, 32).alpha() == 255
    assert not app_icon().isNull()


def test_smoke_test_reports_decoders_and_firmware(application, tmp_path):
    from pipilogicanalyzer import app
    from pipilogicanalyzer.ui.main_window import MainWindow

    report = tmp_path / "report.txt"
    status = app.smoke_test(MainWindow(), str(report))

    text = report.read_text()
    assert "window created" in text and "firmware images" in text
    if os.path.isdir(DECODERS):
        assert status == 0
