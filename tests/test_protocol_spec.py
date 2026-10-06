"""docs/protocols.md is the one source of the capability strings: the Python constants, the pin
capabilities, the facets they unlock and the C headers of the firmware agree with its tables;
the CAPS: line of the Pico firmware, compiled on the host, is what the driver understands."""

from __future__ import annotations

import ast
import glob
import os
import re
import shutil
import subprocess

import pytest

from openscilab.core import instrument
from openscilab.driver import base
from openscilab.driver.pico import protocol

ROOT = os.path.join(os.path.dirname(__file__), "..")
SPEC = os.path.join(ROOT, "docs", "protocols.md")
FIRMWARE = os.path.join(ROOT, "firmware", "pico")


def table(name: str) -> list[list[str]]:
    """The rows of the table between ``<!-- name -->`` and ``<!-- /name -->`` (cells without backticks)."""
    with open(SPEC, encoding="utf-8") as handle:
        text = handle.read()
    match = re.search(rf"<!-- {name} -->\n(.*?)<!-- /{name} -->", text, re.S)
    assert match, f"docs/protocols.md has no table '{name}'"
    rows = [line for line in match.group(1).strip().splitlines() if line.startswith("|")][2:]  # header, rule
    return [[cell.strip().strip("`") for cell in row.strip("|").split("|")] for row in rows]


def spec() -> dict[str, dict[str, str]]:
    return {row[0]: {"parameter": row[1], "meaning": row[2], "facet": row[3], "pico": row[4]}
            for row in table("capabilities")}


def python_capabilities() -> dict[str, str]:
    """``CAPABILITY_*`` constants of every module of the driver package: value -> where."""
    found: dict[str, str] = {}
    for path in glob.glob(os.path.join(ROOT, "openscilab", "driver", "**", "*.py"), recursive=True):
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        for node in tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                    and isinstance(node.value.value, str):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id.startswith("CAPABILITY_"):
                        found[node.value.value] = f"{os.path.relpath(path, ROOT)}:{target.id}"
    return found


def c_capabilities() -> dict[str, dict[str, str]]:
    """``#define CAP_<name> "<string>"`` of every ``firmware/*/capabilities.h``."""
    headers = {}
    for path in glob.glob(os.path.join(ROOT, "firmware", "*", "capabilities.h")):
        with open(path, encoding="utf-8") as handle:
            defines = dict(re.findall(r'^#define (CAP_\w+) "([^"]*)"', handle.read(), re.M))
        headers[os.path.basename(os.path.dirname(path))] = defines
    return headers


def test_the_table_is_well_formed():
    rows = table("capabilities")
    assert len(rows) == len(spec())  # no capability twice
    for name, row in spec().items():
        assert re.fullmatch(r"[A-Z0-9_]+=?", name), name
        assert row["pico"] in ("yes", "8", "–"), name
        assert row["facet"] == "–" or row["facet"].endswith("Facet"), name


def test_python_and_the_spec_agree():
    python = python_capabilities()
    specified = spec()
    assert set(python) == set(specified), (
        f"only in Python: {sorted(set(python) - set(specified))}, "
        f"only in docs/protocols.md: {sorted(set(specified) - set(python))}")


def test_the_facets_agree():
    specified = {name: row["facet"] for name, row in spec().items() if row["facet"] != "–"}
    assert base.FACET_CAPABILITIES == specified
    for facet in specified.values():
        assert hasattr(instrument, facet), facet


def test_the_pin_capabilities_agree():
    assert [row[0] for row in table("pin-capabilities")] == list(instrument.PIN_CAPABILITIES)


def test_the_c_headers_agree():
    headers = c_capabilities()
    assert "pico" in headers  # the Pico firmware
    specified = spec()
    for firmware, defines in headers.items():
        for macro, value in defines.items():
            assert value in specified, f"{firmware}/capabilities.h: {macro} {value!r} is not in docs/protocols.md"
            assert macro == "CAP_" + value.rstrip("="), f"{firmware}/capabilities.h: {macro} should be named after {value!r}"
    pico = set(headers["pico"].values())
    reported = {name for name, row in specified.items() if row["pico"] == "yes"}
    assert pico == reported, f"Pico: header {sorted(pico)} != spec {sorted(reported)}"


def test_the_pico_firmware_builds_its_caps_line_from_the_header():
    """No capability string is written into the firmware code itself."""
    with open(os.path.join(FIRMWARE, "main.c"), encoding="utf-8") as handle:
        source = handle.read()
    block = caps_block(source)
    literals = re.findall(r'"([^"]*)"', block)
    for name in spec():
        assert not any(name.rstrip("=") in literal for literal in literals), f"{name} is written literally"


def caps_block(source: str) -> str:
    start = source.index("case 8:")
    start = source.index("{", start)
    depth = 0
    for index in range(start, len(source)):
        depth += {"{": 1, "}": -1}.get(source[index], 0)
        if depth == 0:
            return source[start:index + 1]
    raise AssertionError("no end of the case 8 block")


@pytest.mark.parametrize("complex_trigger", [True, False])
def test_the_compiled_caps_line_is_understood_by_the_driver(tmp_path, complex_trigger):
    compiler = shutil.which("cc") or shutil.which("clang") or shutil.which("gcc")
    if compiler is None:
        pytest.skip("no C compiler available")
    with open(os.path.join(FIRMWARE, "main.c"), encoding="utf-8") as handle:
        block = caps_block(handle.read())
    program = tmp_path / "caps.c"
    program.write_text(
        '#include <stdio.h>\n#include <string.h>\n#include <stdbool.h>\n#include "capabilities.h"\n'
        "#define SEQ_MAX_STAGES 4\n"
        "static void sendResponse(const char* text, bool wifi) { (void)wifi; fputs(text, stdout); }\n"
        "__attribute__((unused)) static void GetPatternTriggerGroups(char* out, size_t size) { snprintf(out, size, \"0-20/21-23\"); }\n"
        "static unsigned long GetSequenceMaxRate(void* unused) { (void)unused; return 25000000; }\n"
        "static unsigned long GetStateMaxClock(void) { return 50000000; }\n"
        "static unsigned pins_adc_count(void) { return 3; }\n"
        "static unsigned long pattern_max_rate(void) { return 100000000; }\n"
        "#define PATTERN_MAX_PINS 24\n"
        "int main(void) { bool fromWiFi = false; switch (8) { case 8: " + block + " } return 0; }\n")
    binary = tmp_path / "caps"
    defines = ["-DSUPPORTS_COMPLEX_TRIGGER"] if complex_trigger else []
    built = subprocess.run([compiler, "-std=c11", "-Wall", "-Werror", "-I", FIRMWARE, *defines, str(program), "-o",
                    str(binary)], check=False, capture_output=True, text=True)
    assert built.returncode == 0, built.stderr
    line = subprocess.run([str(binary)], check=True, capture_output=True, text=True).stdout.strip()
    assert line.startswith("CAPS:")
    capabilities = frozenset(line[5:].split(","))
    expected = {"SELFTEST", "SIMULATION", "DEVICEINFO", "STREAM=800000", "STATE_MODE", "TRIGGER_SEQUENCE=4",
                "TRIGGER_CONDITIONS=pattern/edge/pulse/gap", "SEQUENCE_MAX_RATE=25000000", "STATE_MAX_CLOCK=50000000",
                # protocol 8 (the comma inside PATTERN_GEN splits it on the line; the driver joins it)
                "STREAM_STATE", "GPIO", "PWM", "MONITOR", "ANALOG=3", "PATTERN_GEN=100000000", "24",
                "GEN_SQUARE", "TX_UART", "TX_SPI", "TX_I2C"}
    if complex_trigger:
        expected |= {"EDGE_TRIGGER_OUT", "PATTERN_GROUPS=0-20/21-23"}
    assert capabilities == expected
    # what the Pico driver reads from it
    assert "PATTERN_GEN=100000000,24" in protocol.split_capabilities(line[5:])
    assert protocol.parse_trigger_sequence(capabilities) == (4, frozenset({"pattern", "edge", "pulse", "gap"}),
                                                             25_000_000)
    assert protocol.parse_state_max_clock(capabilities) == 50_000_000
    assert base.CAPABILITY_STATE_MODE in capabilities and base.CAPABILITY_SELF_TEST in capabilities
