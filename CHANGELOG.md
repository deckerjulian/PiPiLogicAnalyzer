# Changelog

All notable changes to this project are documented here. Versions follow
[Semantic Versioning](https://semver.org/). A release is created by pushing a tag `v<version>`
whose version has a section in this file (see [Creating a release](README.md#creating-a-release)).

## [7.2.0] - 2026-10-01

### Application

- **The application requires the firmware it comes with.** Boards report a protocol version
  (`PROTOCOL:<n>` after the identification); a board with another or older firmware, including
  the original 6.5 firmware, is no longer opened: connecting it offers the firmware update. The
  48 byte capture request of V6_0 and the fallbacks for firmware without capabilities were
  removed.
- **Analysis tools in the style of professional logic analyzers:** cursors A and B with Δt and
  1/Δt, statistics of the channels (frequency, duty cycle, pulse widths) and setup/hold times;
  named markers; search for edges, patterns with don't-cares, pulse widths, gaps, bus values and
  decoder output (F3); buses and groups with symbol tables; a listing of every change or of the
  decoder output; charts of bus values and histograms; comparison with a reference capture; state
  analysis on the edges of a clock channel.
- **Trigger sequences:** stages of patterns, edges, pulse widths and gaps, with counts and time
  limits; evaluated by the application on a stream (software trigger, any device that streams)
  or by the device when it reports `TRIGGER_SEQUENCE`. State mode with an external clock for
  devices that report `STATE_MODE`.
- **New main window layout:** rate, length and trigger of the next capture in the toolbar,
  *Start* captures at once (F5), all settings with Ctrl+F5; the decoders, measurements, search,
  markers and capture information as tabs of a panel, the listing as a panel below; both can be
  moved and closed, and the layout is restored. The toolbar groups file, device, capture settings,
  start/repeat/stop and the panels with icons; every menu entry has an icon; zoom and channel
  height moved from the side panel to a bar below the waveform.
- **Start, Repeat and Stop in the middle of the toolbar**; while a device is connected the device
  list makes room for the capture settings.
- **Detailed list of an annotation row:** the other rows of the decoder are columns of their own,
  e.g. the bytes read during each instruction of a disassembly (`D0 F5 EE`) and its memory region;
  the details of the selected entry show every form of its text, those entries one by one with
  their time, and the levels of the channels the decoder read. The filter and *Copy* include them.
- **Hovering an annotation marks what belongs to it** in every row: the bus cycles of an
  instruction, or the instruction of a bus cycle (dashed outline). An entry made of several values
  shows where each was read and lists them next to the marked span, instead of one read point with
  the levels of every channel; a single value keeps its read point, now with the bus values next
  to it rather than in a corner.
- **State analysis reads where the data is stable:** it suggests the clock edge and read offset at
  which the other lines do not change (on a C64: the falling edge of Φ2, one sample before it;
  the rising edge read the VIC phase and gave wrong addresses), keeps the clock channel so the
  decoders still find their channels, and *Analyze → Back to the timing capture* returns to the
  capture it was made of.
- **sigrok sessions** (`.sr`, PulseView) are exported and opened; decoder output is exported as
  CSV or JSON.
- **Command line and Python API** (`pipilogicanalyzer-cli`, `pipilogicanalyzer.api`): list
  devices, capture, decode, convert between formats; see [docs/cli.md](docs/cli.md).
- **New look of the waveform:** a time grid and a ruler in time units (0 at the trigger, steps of
  1, 2 or 5 of ns/µs/ms/s) instead of sample numbers; thinner antialiased lines with the area under
  a high level lightly filled; dense signals as a translucent band with its envelope instead of a
  solid block; a calmer default palette of 16 distinct colours; the trigger as a dashed amber line
  with a flag on the ruler; the overview dims what lies outside the view.
- **Channel list** in one line per channel: colour strip (click to change), visibility, name and
  channel number; rename with a double-click or the context menu.
- **Device information with tabs:** *Overview*, *Capture limits* and *Self-test*; *Device →
  Self-test…* opens it on the self-test (for the PiPiLogicAnalyzer boards and the DSLogic).
- **DreamSourceLab DSLogic Plus, U2Pro16, U3Pro16 and U3Pro32** (experimental, tested on a
  U2Pro16): a USB driver following the DSLogic driver of DSView, including its FPGA security
  handshake. Buffer and stream captures at the rates of the device (up to 400 MHz / 1 GHz), edge,
  level pattern and immediate triggers, adjustable input threshold. The FPGA bitstreams come from
  an installed DSView or are downloaded once from the DSView repository; they are not shipped.
- DSLogic stream captures are shown while they run, following their end; *Stop* keeps the samples
  received so far. *Until stopped* streams endlessly and keeps the latest samples in a ring
  buffer. Reading and unpacking run in separate threads, so a stream at the USB 2 limit
  (20 MHz × 16 channels) keeps up.
- Board self-test for the DSLogic: the internal test counter of the FPGA checks the capture memory,
  buffer and stream transfers and the triggers bit by bit; the inputs are checked to read low.
- Self-test titles keep their acronyms ("RAM", "FPGA").
- **Stream captures with the PiPiLogicAnalyzer boards** (firmware of this project, over USB):
  the samples are sent while capturing and shown live, with a fixed length or until stopped.
  Up to 800 kHz with 8 channels, 400 kHz with 16, 200 kHz with 24; an overflow of the USB link
  ends the stream with a warning and keeps the samples before it. The board self-test streams
  a test counter and checks it sample by sample.
- Capture dialog: the highest rate follows the acquisition mode (a stream is limited by its
  link); devices whose stream starts at once offer only "None" as its trigger.
- **Record to disk** (streams of the DSLogic and the PiPiLogicAnalyzer boards): the samples go
  into memory-mapped files, so a stream is limited by the free disk space instead of about one
  billion samples. The edge index of such a capture is built in blocks and kept on disk too, the
  live index is kept when the stream ends, decoders run on request, and a disk that fills up
  stops the stream. Sample counts beyond 32 bits in the dialog.
- *Max* next to the sample count, and *Until stopped* starts with the most samples that fit
  (it kept a small number from earlier settings, e.g. 30,000).
- The capture overview is drawn from the edge index (two searches per column instead of a sum
  over every sample) and follows a stream live; the display no longer converts the edge index to
  floats on every frame, which made large captures slow. Progress of a stream the display cannot
  keep up with is shown at its newest state instead of queueing.
- Capture dialog: devices with a fixed list of rates get a list instead of the free value; an
  acquisition mode (buffer/stream), a threshold and "no trigger" appear where the device has them.
  The edge trigger offers all channels of devices with more than 24.
- `.lac` files store the acquisition mode and the input threshold of the capture settings.
- Selecting the new analyzer after a firmware installation works again (the device list lookup
  missed its entries).

### Firmware

- The identification ends with `PROTOCOL:<n>`, the protocol version the application requires
  (`FIRMWARE_PROTOCOL`, raised with every protocol change).
- Trigger sequences evaluated on the board (command 10, trigger type 7) and the state mode with an
  external clock, reported as `TRIGGER_SEQUENCE`, `TRIGGER_CONDITIONS`, `SEQUENCE_MAX_RATE`,
  `STATE_MODE` and `STATE_MAX_CLOCK`.
- Stream capture (capture request with trigger type 6, USB only): the DMA channels fill the
  capture buffer without end and the samples are sent in chunks while they arrive, until the host
  stops the stream; an overtaken buffer ends it with an overflow marker. `STREAM=800000` in the
  capabilities; `triggerValue = 1` streams a PIO test counter instead of the inputs.

### Project

- Dependencies `pyusb` and `libusb-package` (the packaged applications include libusb); the smoke
  test of the builds checks that libusb loads. Linux udev rule for the DSLogic.
- **Device drivers are independent of the application:** the main window and the dialogs no longer
  ask which kind of device is connected, the driver describes what its device can do
  (`is_hardware`, `boards()`, `supports_bootloader`, `has_self_test`, `describe()`, ...). The
  device list is filled by one backend per kind of device (`ui/devices/`). The driver of the Pico
  boards (gusmanb's LogicAnalyzer hardware) moved to `driver/pico/`, next to `driver/dslogic/`.
  [docs/drivers.md](docs/drivers.md) explains how to add a device.

## [7.1.1] - 2026-09-24

### Application

- Multi device sets: the boards started through the trigger line were compensated for the
  trigger delay with far too few samples (the delay in clock cycles was divided by the sample
  period in nanoseconds), so their samples were shifted against the board evaluating the trigger.
- Pattern and edge-out captures whose post-trigger samples do not cover the trigger delay are
  rejected by the settings check instead of failing on the device.
- LogicAnalyzer Interceptor: the capture mode is chosen by the bit of every channel in the
  samples (channel 0 = GPIO 6), so channels 4–7 in 8 channel mode and 12–15 in 16 channel mode no
  longer read as 0.
- Dragging a selection in the ruler beyond the left edge produced negative sample numbers; cut
  and delete then removed the wrong samples. Cut no longer deletes when copying was refused.
- Automatic decoder channel assignment missed capture channel 0 when it matched by id.
- Changes made while the decoders run (sample edits, decoder settings) are decoded afterwards
  instead of being ignored; the decoders work on a snapshot of the channels.
- Loading capture settings or a profile in blast mode kept the default post-trigger samples.
- Aborting a WiFi capture closes the connection properly, which also ends the waiting read.
- VCD export: the time stamps no longer drift at sample periods that are not whole nanoseconds
  (e.g. 24 MHz).
- The board self-test dialog can no longer be closed with `Esc` while the test runs.
- Smaller fixes: the first pixel column of dense waveforms was drawn as toggling, the burst
  timestamp wraparound was off by one tick, stale input could be read as device details.
- Unused code removed.

### Firmware

- WiFi: received data is acknowledged to lwIP (`tcp_recved`); without it the receive window shrank
  with every request until the connection hung after about 11 KB of requests.
- WiFi: the error callback no longer closes the PCB lwIP has already freed, a closed connection
  no longer returns `ERR_ABRT`, responses are sent immediately (`tcp_output`), and a client that
  stops reading is dropped after 5 s.
- WiFi: received data waits in lwIP instead of blocking the WiFi core on a full event queue, so
  the two cores can no longer block each other during a large transfer.
- WiFi: an invalid stored IP address keeps the address assigned by DHCP.
- Captures are rejected when a channel does not fit into the samples of the requested mode
  (Interceptor) instead of silently reading 0; unknown trigger types are answered with
  `CAPTURE_ERROR` instead of starting an edge capture.
- Undefined shifts in the burst timestamps and the blast trigger mask fixed; a blast capture
  releases its GPIOs; the power status line cannot overflow its buffer.
- CMake applies a changed `BOARD_TYPE` to an existing build directory; unused code removed.

### Project

- `publish.ps1` works again after the rename (settings file, image name) and names the images
  like `build_all.sh`. The VS Code kit uses the SDK 2.1.1 toolchain.
- Corrected references to gusmanb's original LogicAnalyzer that the rename had changed.
- The release check also compares the firmware version in `CMakeLists.txt` with the tag.

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
- `SECURITY.md` (private vulnerability reporting, where problems are plausible) and a Contributor
  Covenant `CODE_OF_CONDUCT.md`.
- Corrected license statements: a few bundled sigrok decoders are MIT or BSD, not GPL, and the
  AppImage runtime is distributed with the Linux build. The copies of the Raspberry Pi
  `lwipopts.h` and `pico_sdk_import.cmake` carry their BSD-3-Clause notice again.
- Source files carry a copyright and `SPDX-License-Identifier` header; `pyproject.toml` declares
  `GPL-3.0-or-later`.
- Pull requests that only change documentation now run the tests as well, so a required check can
  pass.

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
- *Help → Online documentation* opens the wiki of this project; the wiki of the original software
  by gusmanb, which documents the hardware, has an entry of its own.

### Firmware

- Identifies itself as `V7_1`. A multi device set only accepts boards with the same version, so
  flash every board of a set with this firmware.
- Trigger type 5: edge trigger that also drives the trigger output.
- Pattern triggers accept all channels on consecutive GPIOs; the capabilities report
  `EDGE_TRIGGER_OUT` and `PATTERN_GROUPS`.

## [7.0.0] - 2026-09-14

First release, published under the name *LogicAnalyzer 7*. It is based on version 6.5 of the
[LogicAnalyzer](https://github.com/gusmanb/logicanalyzer) by Agustín Giménez Bernad (gusmanb)
(branch `version/v6_5`, commit `3fa3703`) and extends his firmware and software. Thank you,
Agustín, for creating the LogicAnalyzer and sharing it under the GPL.

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
