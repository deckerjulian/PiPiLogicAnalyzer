"""Button icons are rendered from inline SVG and follow the button variant."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QPushButton

from pipilogicanalyzer.ui import icons
from pipilogicanalyzer.ui.theme import set_variant


@pytest.fixture(scope="module")
def application():
    return QApplication.instance() or QApplication([])


def test_every_icon_renders(application):
    for name in icons.names():
        image = icons.icon(name).pixmap(16, 16).toImage()
        assert not image.isNull(), name
        visible = any(
            image.pixelColor(x, y).alpha() > 0 for x in range(image.width()) for y in range(image.height())
        )
        assert visible, f"icon {name} is empty"


def test_icon_follows_the_variant(application):
    button = QPushButton("Connect")
    icons.set_icon(button, "plug")
    plain = button.icon().cacheKey()

    set_variant(button, "primary")
    assert button.icon().cacheKey() != plain

    icons.set_icon(button, None)
    assert button.icon().isNull()
