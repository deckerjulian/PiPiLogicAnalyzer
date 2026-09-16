# Firmware (PiPiLogicAnalyzer)

Firmware for the RP2040/RP2350 logic analyzer. It is based on the firmware of the
**[LogicAnalyzer](https://github.com/gusmanb/logicanalyzer) by Agustín Giménez Bernad
(gusmanb)**, version 6.5 (branch `version/v6_5`, commit `3fa3703`, folder
`Firmware/LogicAnalyzer_V2`). The capture engine, the PIO programs, the board support and the
protocol are his work; this project fixes the bugs listed below and adds a self-test, simulated
captures and device information. Thank you, Agustín, for this excellent firmware!

License: GNU General Public License v3, like the original. Not included are
`Firmware/LogicAnalyzer` (the old V5_2 firmware) and `Firmware/PiMoroni Plus 2 files` (archives,
schematic and PSRAM helpers the build does not use).

All changes compared with the original are part of the source code in
[`PiPiLogicAnalyzer/`](PiPiLogicAnalyzer/); they are described in
[Fixed bugs](#fixed-bugs) and [Self-test and simulation](#self-test-and-simulation).

## Building

Requirements: Raspberry Pi Pico SDK 2.1.1, ARM GCC 14.2 and CMake/Ninja. The easiest way is the
*Raspberry Pi Pico* extension for VS Code, which installs everything in `~/.pico-sdk`. Editor
settings are not part of the repository (`.vscode/` is ignored); point the extension at
`firmware/PiPiLogicAnalyzer` as the CMake source directory.

1. Set `BOARD_TYPE` and, if wanted, `TURBO_MODE` in
   [`PiPiLogicAnalyzer/PiPiLogicAnalyzer_Build_Settings.cmake`](PiPiLogicAnalyzer/PiPiLogicAnalyzer_Build_Settings.cmake).
2. Build:

   ```bash
   cd firmware/PiPiLogicAnalyzer
   export PICO_SDK_PATH=~/.pico-sdk/sdk/2.1.1   # not needed with the VS Code extension
   cmake -S . -B build -G Ninja
   cmake --build build
   ```

3. Copy `build/PiPiLogicAnalyzer.uf2` to the board (hold *BOOTSEL* while plugging it in, or use
   *Device → Enter bootloader mode* in the application).

### All variants at once

[`build_all.sh`](build_all.sh) (macOS/Linux) builds every board and turbo variant in turn. It
uses the tools the VS Code extension installed in `~/.pico-sdk` and writes the images to
`firmware/uf2/`. It builds in `firmware/uf2/.build`, so the VS Code build folder
(`PiPiLogicAnalyzer/build`) is left alone:

```bash
firmware/build_all.sh                          # all boards
firmware/build_all.sh BOARD_PICO BOARD_PICO_2  # selected boards
```

The files are named `LogicAnalyzer_<BOARD_TYPE>[_Turbo].uf2`; the application recognises the
board of an image by this name when flashing. The build settings are restored afterwards; error
logs are kept as `.log` files next to the images. On Windows, `publish.ps1` does the same
(PowerShell).

### Automatic builds (GitHub Actions)

The workflow [`.github/workflows/build.yml`](../.github/workflows/build.yml) builds the firmware
for all boards in parallel with the same versions as the VS Code extension (Pico SDK 2.1.1,
picotool 2.1.1, ARM GNU Toolchain 14.2) using `build_all.sh`. SDK and toolchain are cached.

* Push to `main`: only if `firmware/` (or the workflow) changed since the pre-release
  [*latest-build*](https://github.com/deckerjulian/PiPiLogicAnalyzer/releases/tag/latest-build). The
  images are published in that release together with the applications, which include them.
* Tag `v*` and manual runs (*Actions → Build → Run workflow*): always; a tag creates a release.
* Every firmware run creates the artifact `firmware-uf2` with all images (*Actions* → run →
  *Artifacts*); if a board fails, its build log is in `firmware-log-<BOARD>`.
* Pull requests do not build the firmware.

### Flashing from the application

*Device → Install or update firmware…* lists all boards in bootloader mode, Raspberry Pi boards
running other firmware (e.g. MicroPython) and connected analyzers with their installed firmware.
The dialog restarts a board into the bootloader (by command or 1200 baud reset) and copies the
selected image to the drive. It checks the chip family in the UF2 (RP2040 or RP2350) against the
bootloader first and then waits until the analyzer appears as a serial port.

| `BOARD_TYPE` | Board | Channels | Buffer | Turbo |
| --- | --- | --- | --- | --- |
| `BOARD_PICO` | Raspberry Pi Pico | 24 | 128 KB | ✓ |
| `BOARD_PICO_2` | Raspberry Pi Pico 2 | 24 | 384 KB | ✓ |
| `BOARD_PICO_W` | Pico W, data over USB | 24 | 128 KB | – |
| `BOARD_PICO_W_WIFI` | Pico W with WiFi | 24 | 128 KB | – |
| `BOARD_PICO_2_W` | Pico 2 W, data over USB | 24 | 384 KB | – |
| `BOARD_PICO_2_W_WIFI` | Pico 2 W with WiFi | 24 | 384 KB | – |
| `BOARD_ZERO` | RP2040-Zero | 24 | 128 KB | ✓ |
| `BOARD_INTERCEPTOR` | LogicAnalyzer Interceptor | 28 | 128 KB | ✓ |

Turbo mode overclocks to 400 MHz with raised core voltage (200 Msps instead of 100 Msps). Unlike
the original, `TURBO_MODE` is **off** by default and has to be enabled deliberately.

## Working with the application

The firmware identifies itself as `PIPI_LOGIC_ANALYZER_<BOARD>_V7_1` (before the rename: `LOGIC_ANALYZER_<BOARD>_V7_0`, which the application still accepts). Hosts compare the version with
`V<major>_<minor>`: the application of this project and the original LogicAnalyzer 6.5 software
both accept every version from V6_5 on and send the 56 byte capture request (32 channel entries,
16 bit loop count). Requests of a different length are answered with `CAPTURE_ERROR`; the
original firmware misinterpreted them instead. Software for V6_0 (48 byte request) therefore does
not work with this firmware, just as with the original 6.5 firmware, which only lacked a clear
error.

## Self-test and simulation

Additions of this project. The original software never sends the new commands, so the firmware
stays compatible with it. The application queries the capabilities first and only uses the
functions the firmware reports.

| Request | Response |
| --- | --- |
| Command 8 (capabilities) | `CAPS:SELFTEST,SIMULATION,DEVICEINFO,EDGE_TRIGGER_OUT,PATTERN_GROUPS=0-20/21-23` (the last two on boards with pattern trigger); the original firmware answers `ERR_UNKNOWN_MSG` |
| Command 7 (self-test) | one line `SELFTEST:<test>:<status>:<details>` per test, finally `SELFTEST_END` |
| Command 9 (device information) | lines `INFO:<key>:<value>`, finally `INFO_END`. Keys: `BOARD`, `FIRMWARE`, `BUILD_DATE`, `SDK`, `CHIP`, `CHIP_REVISION` and `ROM_VERSION` (RP2040 only), `UNIQUE_ID`, `FLASH_SIZE`, `CLOCK`, `TURBO`, `WIFI`, `PATTERN_TRIGGER` |
| Capture request with `triggerType = 4` | simulated capture, pattern in `triggerValue`, `loopCount` must be 0 |
| Capture request with `triggerType = 5` | edge trigger on channel `trigger` (falling edge with `inverted`) that also drives the trigger output, like the pattern triggers: the board that triggers a multi device set |

### Triggers

* **Pattern trigger channels:** the complex and the fast trigger read their channels with one
  `MOV` from the first trigger pin, so a pattern has to lie on consecutive GPIOs. The original only
  accepted channels 1 to 16; this firmware accepts every run of consecutive GPIOs (up to 16 bits,
  5 for the fast trigger) and reports the runs as `PATTERN_GROUPS`, channels counted from 0: Pico
  boards `0-20/21-23` (channels 1–21 and 22–24), RP2040-Zero `0-15/16-19/20-23`, Interceptor
  `0-23/24-27`.
* **Edge trigger with trigger output (type 5):** a second state machine waits for the opposite
  level and then for the trigger level on the trigger channel (`jmp pin`) and sets the trigger
  output, which starts the capture through the trigger input exactly like the complex trigger. A
  multi device set can therefore be triggered by an edge on any channel of any board.

### Self-test

No signals may be connected. Nothing is ever driven onto the channel inputs; they are only
checked with the internal pull-up and pull-down resistors.

| Test | Check | Status |
| --- | --- | --- |
| `BOARD` | board, firmware version, channels, buffer size, system clock | `INFO` |
| `RAM` | write the capture buffer with two bit patterns and read it back | `OK` / `FAIL` |
| `TRIGGER_LINK` | trigger output drives trigger input (Pico: GPIO 0 → GPIO 1), against the opposite pull | `OK` / `FAIL` / `SKIPPED` |
| `CH1` … `CHn` | input follows pull-up and pull-down | `OK`, `STUCK_HIGH`, `STUCK_LOW`, `INVERTED` |
| `CAPTURE` | capture the pull pattern (odd channel numbers high, even low) through PIO and DMA, 1000 samples at 1 MHz, channel 1 as trigger | `OK` / `FAIL` / `SKIPPED` |
| `BLAST_CAPTURE` | the same in blast mode at maximum frequency | `OK` / `FAIL` / `SKIPPED` |

The capture tests only compare channels that follow the pull resistors.

**Limits:**
* Shorts between two channels cannot be detected reliably with equally strong pull resistors.
* Boards with input buffers or level shifters report their channels as `STUCK_*`, because the
  buffer drives the input.

### Simulated capture

Instead of sampling pins, the firmware writes test signals directly into the capture buffer and
transfers them like a real capture. This tests the USB/WiFi transfer, the display and the
decoders without a single signal. The generator is
[`PiPiLogicAnalyzer_Simulation.c`](PiPiLogicAnalyzer/PiPiLogicAnalyzer_Simulation.c). It is identical to
`pipilogicanalyzer/core/simulation.py`; `tests/test_simulation.py` compiles the C file on the
computer and compares both generators sample by sample.

| `triggerValue` | Pattern |
| --- | --- |
| 0 | counter: channel n toggles every 2^(n mod 16) samples |
| 1 | walking bit: one channel is high for 16 samples each |
| 2 | protocols carrying the text `PiPiLogicAnalyzer\n`, at 1 MHz sampling rate:<br>channel 1: UART 8N1 at f/10 baud (100,000 baud)<br>channels 2–4: SPI CLK, MOSI, CS (mode 0, f/10 clock)<br>channels 5–6: I2C SCL, SDA (write to address 0x50, f/20 bit/s)<br>further channels: counter |

## Fixed bugs

| File | Problem in the original | Fix |
| --- | --- | --- |
| `PiPiLogicAnalyzer.c` | 128 byte receive buffer with an 8 bit index; the overflow check `>= 256` could never trigger. Longer frames overwrote the memory behind it, e.g. WiFi settings with escaped characters (`U` = 0x55 in the password) or garbage after `0x55 0xAA`. | 256 byte buffer, 16 bit index, check before writing. |
| `PiPiLogicAnalyzer.c` | Capture and WiFi requests were interpreted as structures without a length check, directly in the unaligned buffer. | The length must equal `sizeof` the structure; copied with `memcpy`. |
| `PiPiLogicAnalyzer.c` | SSID, password and IP were stored without a terminating zero although the WiFi core uses them as C strings. | The last byte of every field is set to 0. |
| `PiPiLogicAnalyzer.c` | WiFi responses were copied into a 32 byte event with `memcpy` without a length limit. | Responses go through `wifi_transfer` in 32 byte chunks; `printf` with the format string `"%s"`. |
| `LogicAnalyzer_Capture.c` | Blast mode: the buffer end was computed as `lastPostSize` instead of `lastPostSize - 1`. The first sample was missing and a zeroed one was appended (with a full buffer sample 0 came last). | Correct buffer end. |
| `LogicAnalyzer_Capture.c` | The NMI and SysTick handlers of the burst measurement were only restored for `timestampIndex != 0`. After an abort before the first timestamp, the next measurement panicked in `exception_set_exclusive_handler`. | Separate flag `lastBurstMeasure`; the previous SysTick state is restored. |
| `LogicAnalyzer_Capture.c` | A burst measurement without loops loaded the measuring PIO program but installed no NMI handler. The `irq wait 1` blocked the capture forever. | Measuring only with `loopCount > 0`; the program is selected accordingly. |
| `LogicAnalyzer_Capture.c` | `GetBuffer` computed with `lastTail = 0xFFFFFFFF` when `find_capture_tail` could not determine the DMA position and then wrote far behind the buffer. | The buffer end is limited to the valid range. |
| `LogicAnalyzer_Capture.c` | Missing parameter checks: unknown capture mode (undefined buffer size), channel numbers outside the pin map (reading outside the array, arbitrary GPIOs), 0 post-trigger samples (`postLength - 1` gave an endless capture), frequency 0 (division by 0) or too low for the PIO divider, 32 bit overflow of `pre + post × (loops + 1)`. | All values are checked before starting. |
| `LogicAnalyzer_Capture.c` | Pattern trigger: bits above the pattern length in the trigger value prevented any trigger. | The trigger value is masked to the pattern length. |
| `LogicAnalyzer_Capture.c` | `StopCapture` could finish a capture a second time if it ended right before interrupts were disabled. `captureFinished` was not `volatile`. | Checked again with interrupts disabled; `volatile`. Blast captures no longer touch the unused second DMA channel. |
| `LogicAnalyzer_Capture.c` | After a simple capture the PIO program of the other edge was removed, never the measuring one. | The program actually loaded is removed. |
| `LogicAnalyzer_Capture.c` | `1 << pin` with pin 31 is undefined in C. | `1u << pin`. |
| `LogicAnalyzer_Capture.h` | `RP2350.h` was only included for `BUILD_PICO_2`, not for the Pico 2 W. | Included through `CORE_TYPE_2`. |
| `LogicAnalyzer_WiFi.c` | `tryStartServer` had no `return true` on the success path (undefined return value). On errors every retry leaked a PCB. | Return value added, PCBs are closed on errors. |
| `LogicAnalyzer_WiFi.c` | `sendData` called `tcp_write` with `NULL` if the client disconnected while waiting for send buffers. | Aborts when no client is connected. |
| `LogicAnalyzer_WiFi.c` | `sleep_ms(100)` with interrupts disabled during the battery measurement. | `busy_wait_ms(100)`. |
| `Event_Machine.c` | `event_has_events` compared the addresses of two structure fields and always returned `true`. | `!queue_is_empty(...)`. |
| `PiPiLogicAnalyzer_Build_Settings.cmake` | Turbo mode (overclocking and overvoltage) was the default; the board list was outdated. | Turbo off, complete list. |
| `publish.ps1` | Turbo builds were attempted for the Pico 2 W although CMake refuses them. | They are skipped. |

## Known, unfixed issues

* **Interceptor in 8 and 16 channel mode:** the channels do not start at the first input GPIO
  there (channel 0 = GPIO 6). Channels whose bit position `GPIO − INPUT_PIN_BASE` exceeds the
  width of the mode chosen by the host arrive as 0, e.g. channels 4–7 in 8 channel mode. A fix
  needs a wider internal capture and copying within the ring buffer; it cannot be tested without
  the hardware. Workaround: also capture a channel from 16 on, then the host uses 32 bit mode.
* **WiFi:** both event queues (depth 8) block when adding. If both are full at the same time,
  the two cores could theoretically block each other.
* `find_capture_tail` evaluates `transfer_count` of the DMA channel the way the author marked
  with "TODO: CHECK". This project only prevents an invalid value from corrupting memory.

## Status

All twelve variants (eight boards, turbo where possible) compile with Pico SDK 2.1.1 and ARM GCC
14.2 via `build_all.sh`, without warnings from the source files of this project. The signal
generator `PiPiLogicAnalyzer_Simulation.c` is also built and checked on the computer in every test
run. **The changes have not been tested on hardware yet.**

Before relying on it, run the self-test and a simulated capture once and check at least a
simple, a burst and a blast capture with real signals.
