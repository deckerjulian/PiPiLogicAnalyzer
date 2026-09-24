"""Adding stacked decoders: browser listing, provider chains and the manager flow."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from pipilogicanalyzer.sigrok.engine import PROJECT_DIRECTORY
from pipilogicanalyzer.sigrok.provider import DecoderInstance, SigrokProvider
from pipilogicanalyzer.ui.dialogs.decoder_browser import DecoderBrowserDialog
from pipilogicanalyzer.ui.view_model import CaptureViewModel
from pipilogicanalyzer.ui.widgets import decoder_manager as manager_module
from pipilogicanalyzer.ui.widgets.decoder_manager import DecoderManager

from test_ui import make_session


@pytest.fixture(scope="module")
def application():
    return QApplication.instance() or QApplication([])


def tree_entries(dialog: DecoderBrowserDialog) -> dict[str, str]:
    entries = {}
    for top in range(dialog.tree.topLevelItemCount()):
        category = dialog.tree.topLevelItem(top)
        for index in range(category.childCount()):
            child = category.child(index)
            entries[child.text(0)] = child.text(1)
    return entries


# -------------------------------------------------------------------- browser
def test_add_lists_stacked_decoders_with_their_input(application, decoder_registry):
    entries = tree_entries(DecoderBrowserDialog(decoder_registry))
    assert "TestDec" in entries
    assert entries["TestStack"] == "Stacked test decoder  (decodes testdec)"


def test_stack_only_lists_matching_decoders(application, decoder_registry):
    entries = tree_entries(DecoderBrowserDialog(decoder_registry, only_inputs={"testdec"}))
    assert entries == {"TestStack": "Stacked test decoder"}


# --------------------------------------------------------------------- chains
def test_provider_chain_of_the_fixture(decoder_registry):
    chain = decoder_registry.provider_chain(decoder_registry.get("teststack"))
    assert [info.id for info in chain] == ["testdec"]
    assert decoder_registry.provider_chain(decoder_registry.get("testdec")) == []


@pytest.mark.skipif(
    not os.path.isfile(os.path.join(PROJECT_DIRECTORY, "decoders", "usb_request", "pd.py")),
    reason="the sigrok decoders are not installed in ./decoders",
)
@pytest.mark.parametrize(
    "decoder, expected",
    [
        ("eeprom24xx", ["i2c"]),
        ("spiflash", ["spi"]),
        ("usb_request", ["usb_signalling", "usb_packet"]),
    ],
)
def test_provider_chain_of_real_decoders(decoder_registry, decoder, expected):
    chain = decoder_registry.provider_chain(decoder_registry.get(decoder))
    assert [info.id for info in chain] == expected


# -------------------------------------------------------------------- manager
@pytest.fixture
def manager(application, decoder_registry, monkeypatch):
    model = CaptureViewModel()
    model.set_session(make_session())
    widget = DecoderManager(model, SigrokProvider(decoder_registry))
    widget.auto_decode.setChecked(False)

    class AcceptingOptions:
        def __init__(self, *args, **kwargs):
            pass

        def exec(self):
            return True

    monkeypatch.setattr(manager_module, "DecoderOptionsDialog", AcceptingOptions)
    yield widget
    widget.close()


def choose_in_browser(monkeypatch, info) -> None:
    class Browser:
        def __init__(self, *args, **kwargs):
            self.selected = info

        def exec(self):
            return True

    monkeypatch.setattr(manager_module, "DecoderBrowserDialog", Browser)


def test_adding_a_stacked_decoder_creates_its_parent_first(manager, decoder_registry, monkeypatch):
    questions = []
    monkeypatch.setattr(
        "pipilogicanalyzer.ui.messages.confirm", lambda *args, **kwargs: questions.append(args[2]) or True
    )
    choose_in_browser(monkeypatch, decoder_registry.get("teststack"))

    manager.add_decoder()

    assert [instance.decoder_id for instance in manager.provider.instances] == ["testdec", "teststack"]
    assert manager.provider.instances[1].parent is manager.provider.instances[0]
    assert "Add TestDec first" in questions[0]


def test_adding_a_stacked_decoder_reuses_an_existing_parent(manager, decoder_registry, monkeypatch):
    parent = manager.provider.add_instance(DecoderInstance(decoder_id="testdec", channel_map={0: 0}))
    monkeypatch.setattr("pipilogicanalyzer.ui.messages.confirm", lambda *args, **kwargs: pytest.fail("asked"))
    choose_in_browser(monkeypatch, decoder_registry.get("teststack"))

    manager.add_decoder()

    assert [instance.decoder_id for instance in manager.provider.instances] == ["testdec", "teststack"]
    assert manager.provider.instances[1].parent is parent


def test_declining_the_parent_adds_nothing(manager, decoder_registry, monkeypatch):
    monkeypatch.setattr("pipilogicanalyzer.ui.messages.confirm", lambda *args, **kwargs: False)
    choose_in_browser(monkeypatch, decoder_registry.get("teststack"))

    manager.add_decoder()

    assert manager.provider.instances == []
