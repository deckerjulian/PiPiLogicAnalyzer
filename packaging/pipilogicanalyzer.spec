# -*- mode: python -*-
"""PyInstaller build of the PiPiLogicAnalyzer application.

    python -m pip install -r requirements.txt pyinstaller pillow
    python packaging/make_icons.py build/icons
    python -m PyInstaller packaging/pipilogicanalyzer.spec --noconfirm

Creates dist/PiPiLogicAnalyzer/ (Windows: PiPiLogicAnalyzer.exe, Linux: PiPiLogicAnalyzer) or
dist/PiPiLogicAnalyzer.app (macOS). The protocol decoders and the firmware images in
firmware/uf2 (built with firmware/build_all.sh) are included.
"""

import glob
import os
import sys
import tomllib

ROOT = os.path.abspath(os.path.join(SPECPATH, os.pardir))
ICONS = os.path.join(ROOT, "build", "icons")

with open(os.path.join(ROOT, "pyproject.toml"), "rb") as handle:
    VERSION = tomllib.load(handle)["project"]["version"]

#: Modules imported by the protocol decoders. The decoders are loaded at run time, so
#: PyInstaller cannot see these imports (tests/test_packaging.py keeps the list complete).
DECODER_MODULES = [
    "binascii",
    "calendar",
    "collections",
    "copy",
    "ctypes",
    "dataclasses",
    "decimal",
    "enum",
    "functools",
    "itertools",
    "json",
    "math",
    "operator",
    "os",
    "platform",
    "re",
    "string",
    "struct",
    "subprocess",
    "zlib",
]


def icon(name):
    path = os.path.join(ICONS, name)
    return path if os.path.exists(path) else None


a = Analysis(
    [os.path.join(SPECPATH, "launch.py")],
    pathex=[ROOT],
    hiddenimports=DECODER_MODULES,
    excludes=["tkinter"],
)

# The application looks for decoders/ and firmware/uf2/ next to the pipilogicanalyzer package.
decoders = Tree(os.path.join(ROOT, "decoders"), prefix="decoders", excludes=["__pycache__", "*.pyc"])
firmware_images = [
    (os.path.join("firmware", "uf2", os.path.basename(path)), path, "DATA")
    for path in sorted(glob.glob(os.path.join(ROOT, "firmware", "uf2", "*.uf2")))
]

# License texts and credits travel with every binary (GPL section 4 and 6).
notices = [
    (name, os.path.join(ROOT, name), "DATA")
    for name in ("LICENSE", "THIRD_PARTY_NOTICES.md", "README.md", "CHANGELOG.md")
    if os.path.exists(os.path.join(ROOT, name))
]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PiPiLogicAnalyzer",
    console=False,
    icon=icon("pipilogicanalyzer.ico" if sys.platform.startswith("win") else "pipilogicanalyzer.icns"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas + decoders + firmware_images + notices,
    name="PiPiLogicAnalyzer",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="PiPiLogicAnalyzer.app",
        icon=icon("pipilogicanalyzer.icns"),
        bundle_identifier="io.github.deckerjulian.pipilogicanalyzer",
        version=VERSION,
        info_plist={
            "CFBundleName": "PiPiLogicAnalyzer",
            "CFBundleDisplayName": "PiPiLogicAnalyzer",
            "CFBundleShortVersionString": VERSION,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
            "CFBundleDocumentTypes": [
                {
                    "CFBundleTypeName": "PiPiLogicAnalyzer capture",
                    "CFBundleTypeRole": "Editor",
                    "LSItemContentTypes": ["public.data"],
                    "CFBundleTypeExtensions": ["lac"],
                }
            ],
        },
    )
