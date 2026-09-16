# Changelog

All notable changes to this project are documented here. Versions follow
[Semantic Versioning](https://semver.org/). A release is created by pushing a tag `v<version>`
whose version has a section in this file (see [Creating a release](README.md#creating-a-release)).

## [Unreleased]

### Application

- *Help → Online documentation* opens the wiki of this project; the wiki of the original software
  by gusmanb, which documents the hardware, has an entry of its own.

## [7.1.0] - 2026-09-15

### Project

- **The project is now called PiPiLogicAnalyzer.** The Python package is `pipilogicanalyzer`, the
  command `pipilogicanalyzer`, the settings live in a directory of that name (the settings of the
  previous name are taken over on the first start) and the environment variables are
  `PIPILOGICANALYZER_SETTINGS_DIR` and `PIPILOGICANALYZER_DECODERS`; the previous names still work.
- The firmware is called PiPiLogicAnalyzer as well: the sources are in `firmware/PiPiLogicAnalyzer`,
  the images are named `PiPiLogicAnalyzer_<BOARD>[_Turbo].uf2` and a board identifies itself as
  `PIPI_LOGIC_ANALYZER_<BOARD>_V<major>_<minor>`. The application also accepts the previous
  identification, so boards with an older firmware keep working. USB VID/PID (0x1209/0x3020) and
  the hardware of Agustín Giménez Bernad are unchanged.

### Application

- The device list and the multi device dialog only offer detected analyzers; other serial ports
  are no longer listed.
- Clicking the name of an annotation row (or double-clicking an annotation) opens the row as a
  list in a window of its own: filter, copy (e.g. a disassembly listing), and selecting an entry
  shows it in the waveform.
- Multi device sets are no longer triggered by the master only: the board of the trigger channel
  evaluates a pattern or an edge (firmware of this project) and starts the other boards, or every
  board waits for an edge on its external trigger input.
- Pattern triggers use every group of consecutive trigger inputs the firmware reports (Pico:
  channels 1–21 and 22–24 instead of 1–16), fast matching included.
- C64 bus decoder: the *Bus cycles* row keeps the value read or written visible when zoomed out
  (`R $FFFC=$E2`, `FFFC=E2`, `E2`) instead of showing only the address.
- *Device → Install or update firmware*: every image shows the firmware version it contains, in
  the list and in its description.
- Channels can be made lower (or taller) to fit more of them on the screen: `Alt` + mouse wheel,
  *View → Taller/Shorter channels* (`Ctrl+Shift+Up/Down`) or the *Channel height* slider; the
  height is remembered.

### Project

- `SECURITY.md` (private vulnerability reporting, where problems are plausible) and a Contributor
  Covenant `CODE_OF_CONDUCT.md`.
- Corrected license statements: a few bundled sigrok decoders are MIT or BSD, not GPL, and the
  AppImage runtime is distributed with the Linux build. The copies of the Raspberry Pi
  `lwipopts.h` and `pico_sdk_import.cmake` carry their BSD-3-Clause notice again.
- Source files carry a copyright and `SPDX-License-Identifier` header; `pyproject.toml` declares
  `GPL-3.0-or-later`.
- Pull requests that only change documentation now run the tests as well, so a required check can
  pass.

### Firmware

- Identifies itself as `V7_1`. A multi device set only accepts boards with the same version, so
  flash every board of a set with this firmware.
- Trigger type 5: edge trigger that also drives the trigger output.
- Pattern triggers accept all channels on consecutive GPIOs; the capabilities report
  `EDGE_TRIGGER_OUT` and `PATTERN_GROUPS`.

## [7.0.0] - 2026-09-14

First release of PiPiLogicAnalyzer 7. It is based on version 6.5 of the
[LogicAnalyzer](https://github.com/gusmanb/logicanalyzer) by Agustín Giménez Bernad (gusmanb)
(branch `version/v6_5`, commit `3fa3703`) and extends his firmware and software. Thank you,
Agustín, for creating the PiPiLogicAnalyzer and sharing it under the GPL.

### Application

- Cross-platform Python/Qt application with the feature set of the original 6.5 software:
  serial, network and multi device (2 to 5 boards) capture, `.lac` files, sigrok protocol
  decoders, profiles, sample editing, measurements and the Signal Description Language.
- Ready-to-run builds for Windows, macOS (Apple Silicon and Intel) and Linux (AppImage) that
  include the protocol decoders and the firmware images.
- Reworked user interface: toolbar with connection status, start page, capture menu, dialogs
  with named actions and inline validation, consistent dark theme and icons.
- *Install or update firmware*: flashes boards in bootloader mode, restarts connected boards
  into the bootloader and shows the installed firmware and Pico model of every board.
- *Board self-test*, *simulated capture* (on the board or computed locally) and *device
  information* for boards running this firmware.
- Much faster drawing of large captures, decoding on a worker thread, VCD/CSV export and
  compressed captures.
- Profiles can be edited: name, notes, capture settings and decoders.
- Navigation: the mouse wheel and `+`/`-` zoom, the arrow keys scroll horizontally, a trackpad
  scrolls horizontally through the samples and vertically through the channels, pinching zooms.
- Channels can be pinned; pinned channels stay at the top while the waveform scrolls.
- `examples/c64-demo.lac`: a generated C64 capture (reset, screen output, IRQs) for the C64 bus
  decoder.
- Multi device captures are aligned after capturing: exactly through a reference line (a slave
  signal also connected to the master) or estimated from a clock channel; *Capture → Align
  boards* does the same for captures opened from files and lets you choose the method when several
  are possible; the capture information names the method used. The C64 profile captures at 20 MHz and
  uses A0 as reference line.
- Hovering a decoder annotation marks its samples in the waveform and shows how the value is
  composed: the level of every channel at the sample the decoder read it, bit weights and bus
  values such as `A15…A0 = 1111 1111 1111 1100 = $FFFC`.
- C64 system bus decoder (`c64bus`) and an example profile for the C64 expansion port. It reads
  the bus at the last sample before the falling Φ2 edge (configurable, with an offset), flags
  cycles whose lines change next to the read point, and disassembles the 6510 code including
  undocumented opcodes, subroutine calls, branches and IRQ/NMI sequences, although the 6510 has
  no SYNC signal.
- Numerous bug fixes compared with the original software, see
  [docs/improvements.md](docs/improvements.md).

### Firmware

- Based on the 6.5 firmware; identifies itself as `V7_0`. The original 6.5 software accepts
  this version and keeps working with it.
- New commands: capabilities, board self-test, simulated capture and device information.
- Bug fixes in the receive buffer, request validation, blast and burst captures, pattern
  trigger, WiFi handling and the event machine, see [firmware/README.md](firmware/README.md).
- Turbo mode (overclocking and overvoltage) is off by default.

### Build

- GitHub Actions workflow that builds the firmware for all boards and the applications,
  publishes the pre-release *latest-build* from `main` and creates releases from tags.
