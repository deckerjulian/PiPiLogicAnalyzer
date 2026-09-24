# Credits and third-party notices

PiPiLogicAnalyzer is licensed under the GNU General Public License v3 (see [LICENSE](LICENSE)).
It contains, is based on, or is distributed together with the following works. Their copyright
notices and licenses are kept; the complete license texts are part of the respective projects.

## LogicAnalyzer by Agustín Giménez Bernad (gusmanb)

<https://github.com/gusmanb/logicanalyzer> — GNU General Public License v3

PiPiLogicAnalyzer is a modified and extended version of this project, based on version 6.5
(branch `version/v6_5`, commit `3fa3703`):

* the firmware in `firmware/PiPiLogicAnalyzer` is his firmware with the bug fixes and additions
  described in [firmware/README.md](firmware/README.md);
* the application is a Python/Qt port of his C#/Avalonia software (`LogicAnalyzer`,
  `SharedDriver`, `SignalDescriptionLanguage`, the sigrok decoder bridge) with the changes
  described in [docs/improvements.md](docs/improvements.md) and [CHANGELOG.md](CHANGELOG.md);
* the device protocol, the `.lac` file format, the profile format and the USB identifiers
  (VID 0x1209, PID 0x3020, manufacturer "Dr. Gusman") are his.

Thank you, Agustín, for the LogicAnalyzer hardware, firmware and software.

## DSView by DreamSourceLab (DSLogic driver)

<https://github.com/DreamSourceLab/DSView> — GNU General Public License v3 or later

The DSLogic driver in `pipilogicanalyzer/driver/dslogic` implements the USB protocol of the
DSLogic analyzers after the driver of DSView (`libsigrok4DSL/hardware/DSL`, `trigger.c`): device
profiles, commands, capture settings and data format. The FPGA bitstreams and firmware images of
the DSLogic boards are **not** included; they are loaded from a DSView installation or
downloaded from the DSView repository at the user's request. DSLogic and DSView are trademarks
of DreamSourceLab; PiPiLogicAnalyzer is not affiliated with DreamSourceLab.

## Protocol decoders (`decoders/`)

* Decoders of the **sigrok** project (libsigrokdecode), <https://sigrok.org>, copyright by their
  respective authors. Most are licensed under the GNU General Public License v2 or later or v3
  or later; a few are MIT (`ade77xx`, `caliper`, `enc28j60`, `nrf905`) or BSD-2/3-Clause (`cfp`,
  `mdio`). The license is stated in each file; all of them are GPL-compatible.
* Decoders added by the LogicAnalyzer 6.5 decoder set (for example `mos6502`, `mcp230xx`,
  `max72xx`), licensed as stated in each file.
* `c64bus`, part of this project, GNU General Public License v2 or later.

The decoders are shipped as source files, including in the packaged applications.

## Application dependencies (included in the packaged applications)

| Component | License | Project |
| --- | --- | --- |
| Python | Python Software Foundation License | <https://www.python.org> |
| Qt for Python (PySide6) and Qt 6 | GNU Lesser General Public License v3 | <https://www.qt.io/qt-for-python> |
| NumPy | BSD-3-Clause (with bundled OpenBLAS/LAPACK: BSD-3-Clause, license files in `numpy.libs`) | <https://numpy.org> |
| AppImage runtime (Linux build) | MIT, with squashfuse and zstd (BSD) | <https://github.com/AppImage/AppImageKit> |
| pyserial | BSD-3-Clause | <https://github.com/pyserial/pyserial> |
| pyusb | BSD-3-Clause | <https://github.com/pyusb/pyusb> |
| libusb (via libusb-package, shared library) | GNU Lesser General Public License v2.1 | <https://libusb.info>, <https://github.com/pyocd/libusb-package> |
| PyInstaller bootloader | GPL-2.0-or-later with the PyInstaller bootloader exception | <https://pyinstaller.org> |

Qt and PySide6 are included as separate shared libraries, so they can be replaced by other
versions as the LGPL requires. Their source code is available from the Qt project.

## Firmware components (included in the firmware images)

| Component | License | Project |
| --- | --- | --- |
| Raspberry Pi Pico SDK | BSD-3-Clause | <https://github.com/raspberrypi/pico-sdk> |
| TinyUSB | MIT | <https://github.com/hathach/tinyusb> |
| lwIP (WiFi builds) | BSD-3-Clause | <https://savannah.nongnu.org/projects/lwip/> |
| cyw43-driver (Pico W / Pico 2 W builds) | Raspberry Pi specific license, see below | <https://github.com/georgerobotics/cyw43-driver> |

**cyw43-driver:** Copyright (C) 2019-2022 George Robotics Pty Ltd. For use with Raspberry Pi
semiconductor devices it is licensed by Raspberry Pi Ltd under the terms of `LICENSE.RP` in the
driver: use and redistribution are permitted only in conjunction with the RP2040 or other
semiconductor devices produced by Raspberry Pi Ltd, and binary redistributions must reproduce
the copyright notice, the conditions and the disclaimer. The firmware images for the Pico W and
Pico 2 W contain this driver and may only be used on these boards.

## Build tools (not distributed)

The ARM GNU Toolchain, CMake, Ninja, picotool, PyInstaller, Pillow (icons) and appimagetool are
used to build the firmware and the applications; of appimagetool only its runtime travels with
the Linux build (see the table above). Runtime parts of the ARM GNU Toolchain linked into the firmware
(newlib, libgcc) are covered by their respective licenses and exceptions permitting this use.
