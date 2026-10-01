# Firmware (PiPiLogicAnalyzer)

Firmware for the RP2040/RP2350 logic analyzer. It is based on the firmware of the
**[LogicAnalyzer](https://github.com/gusmanb/logicanalyzer) by Agustín Giménez Bernad
(gusmanb)**, version 6.5 (branch `version/v6_5`, commit `3fa3703`, folder
`Firmware/LogicAnalyzer_V2`). The capture engine, the PIO programs, the board support and the
protocol are his work; this project fixes the bugs listed below and adds a self-test, simulated
captures, device information, stream captures, trigger sequences and a state mode (external
clock). Thank you, Agustín, for this excellent firmware!

License: GNU General Public License v3, like the original. Not included are
`Firmware/LogicAnalyzer` (the old V5_2 firmware) and `Firmware/PiMoroni Plus 2 files` (archives,
schematic and PSRAM helpers the build does not use).

All changes compared with the original are part of the source code in
[`PiPiLogicAnalyzer/`](PiPiLogicAnalyzer/); they are described in
[Fixed bugs](#fixed-bugs), [Self-test and simulation](#self-test-and-simulation) and
[Trigger sequences and state mode](#trigger-sequences-and-state-mode).

## Building

Requirements: Raspberry Pi Pico SDK 2.1.1, ARM GCC 14.2 and CMake/Ninja. The easiest way is the
*Raspberry Pi Pico* extension for VS Code, which installs everything in `~/.pico-sdk`. The VS Code
setup of the extension is part of the repository in `firmware/PiPiLogicAnalyzer/.vscode`; open
`firmware/PiPiLogicAnalyzer` as the folder.

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

The files are named `PiPiLogicAnalyzer_<BOARD_TYPE>[_Turbo].uf2`; the application recognises the
board of an image by this name when flashing. The build settings are restored afterwards; error
logs are kept as `.log` files next to the images. On Windows,
`firmware/PiPiLogicAnalyzer/publish.ps1` does the same (PowerShell) and writes the images to
`firmware/PiPiLogicAnalyzer/publish`.

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

The firmware identifies itself as `PIPI_LOGIC_ANALYZER_<BOARD>_V7_2`, followed by `FREQ:`,
`BLASTFREQ:`, `BUFFER:`, `CHANNELS:` and `PROTOCOL:<n>`. The application works only with the
firmware it comes with: it opens a board only when `PROTOCOL` equals its own protocol version
(`FIRMWARE_PROTOCOL` in `CMakeLists.txt` and in `pipilogicanalyzer/driver/pico/protocol.py`) and
offers the firmware update otherwise. **Raise `FIRMWARE_PROTOCOL` with every change of a command,
request or response**, in both places; compatibility with older applications or firmware is not
kept. The capture request is the 56 byte request of V6_5 (32 channel entries, 16 bit loop count);
requests of a different length are answered with `CAPTURE_ERROR`.

## Self-test and simulation

Additions of this project. The capabilities describe the functions of the board and the
connection (e.g. streams only over USB, pattern triggers only on boards with them); the
application only offers what they list.

| Request | Response |
| --- | --- |
| Command 8 (capabilities) | `CAPS:SELFTEST,SIMULATION,DEVICEINFO,STREAM=800000,EDGE_TRIGGER_OUT,PATTERN_GROUPS=0-20/21-23,TRIGGER_SEQUENCE=8,TRIGGER_CONDITIONS=pattern/edge/pulse/gap,SEQUENCE_MAX_RATE=<Hz>,STATE_MODE,STATE_MAX_CLOCK=<Hz>` (`EDGE_TRIGGER_OUT` and `PATTERN_GROUPS` on boards with pattern trigger; `STREAM=<bytes per second>` is the rate a stream capture can send; the last five: see [Trigger sequences and state mode](#trigger-sequences-and-state-mode)) |
| Command 7 (self-test) | one line `SELFTEST:<test>:<status>:<details>` per test, finally `SELFTEST_END` |
| Command 9 (device information) | lines `INFO:<key>:<value>`, finally `INFO_END`. Keys: `BOARD`, `FIRMWARE`, `BUILD_DATE`, `SDK`, `CHIP`, `CHIP_REVISION` and `ROM_VERSION` (RP2040 only), `UNIQUE_ID`, `FLASH_SIZE`, `CLOCK`, `TURBO`, `WIFI`, `PATTERN_TRIGGER` |
| Capture request with `triggerType = 4` | simulated capture, pattern in `triggerValue`, `loopCount` must be 0 |
| Capture request with `triggerType = 5` | edge trigger on channel `trigger` (falling edge with `inverted`) that also drives the trigger output, like the pattern triggers: the board that triggers a multi device set |
| Capture request with `triggerType = 6` | stream capture over USB, see [Stream capture](#stream-capture); `triggerValue = 1` streams a test counter instead of the inputs |
| Command 10 (trigger sequence) | configures the trigger sequence and the state mode of the next capture request with `triggerType = 7`; answered `SEQUENCE_OK` or `SEQUENCE_ERROR` |
| Capture request with `triggerType = 7` | capture with the trigger sequence and/or state mode of command 10, see [Trigger sequences and state mode](#trigger-sequences-and-state-mode) |

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
| `SEQUENCE` | speed of the trigger sequence evaluation measured at start-up, `SEQUENCE_MAX_RATE` and `STATE_MAX_CLOCK` | `INFO` |
| `CAPTURE` | capture the pull pattern (odd channel numbers high, even low) through PIO and DMA, 1000 samples at 1 MHz, channel 1 as trigger | `OK` / `FAIL` / `SKIPPED` |
| `BLAST_CAPTURE` | the same in blast mode at maximum frequency | `OK` / `FAIL` / `SKIPPED` |

The capture tests only compare channels that follow the pull resistors.

**Limits:**
* Shorts between two channels cannot be detected reliably with equally strong pull resistors.
* Boards with input buffers or level shifters report their channels as `STUCK_*`, because the
  buffer drives the input.

### Stream capture

The capture runs without end and the samples are sent while they arrive, until the host sends
any byte. The two DMA channels fill the whole capture buffer alternately and count their passes;
the main loop sends what they wrote since the last chunk. Only over USB (not WiFi), without
trigger (it starts at once), without pre-trigger samples.

* **Answer:** `STREAM_STARTED:<bits>`, the bit of every requested channel in the samples, e.g.
  `0,1,2,24`. The samples are the raw input words of the capture mode (1, 2 or 4 bytes, bit n =
  GPIO `INPUT_PIN_BASE + n`): sorting the bits of every sample, as after a normal capture, would
  take longer than the transfer leaves.
* **Data:** chunks of a 32 bit little-endian byte count and that many sample bytes (at most
  4096, sent at the latest after 20 ms).
* **End:** a count of `0` after a stop, or `0xFFFFFFFF` when USB was too slow and the DMA overtook
  the samples not sent yet; the chunk before it may then hold overwritten samples and is dropped
  by the application.
* **Rate:** USB full speed carries about 890 kB/s of CDC data (measured on a Pico 2); the firmware
  reports 800 kB/s, which is 800 kHz with 8 channels, 400 kHz with 16 and 200 kHz with 24.
* **Test counter** (`triggerValue = 1`): a PIO program counts down by one per sample instead of
  reading the pins, so the transfer can be checked sample by sample; the application's self-test
  streams it for half a second (`STREAM` result).

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

## Trigger sequences and state mode

Additions of this project, reported as capabilities; the application only offers them when the
firmware reports them, and older applications never send command 10 or trigger type 7. The
capture request keeps its 56 byte layout: the sequence and the clock are configured by
command 10 right before the capture request.

### Design

Both use the ring buffer of the stream capture. A PIO program samples without end into the
capture buffer through the two ping-pong DMA channels (`SEQUENCE_CAPTURE` at the requested rate,
5 PIO cycles per sample; `STATE_CAPTURE` on the edges of the clock input). The main loop
evaluates the trigger sequence on the samples behind the DMA write position
([`PiPiLogicAnalyzer_Sequence.c`](PiPiLogicAnalyzer/PiPiLogicAnalyzer_Sequence.c), plain C,
compared with a reference implementation by `tests/test_pico_triggers.py`). It skips samples in
which no watched channel changed with a load, a mask and a compare per sample and only looks at
the others in detail. Once the last stage completes at sample *T*, the firmware sends the stop
value *T + post* to the PIO program: it counts its samples in Y and stops exactly after that
sample (`irq 0`). The capture then ends like every other capture: the last sample is the tail of
the ring buffer, the pre + post samples before it are sorted into channel order and sent with the
usual length, data and timestamp byte. The trigger sample is the first post-trigger sample
(index *pre*).

Why not PIO trigger programs per stage: a PIO block has 32 instructions and 4 state machines, and
pulse widths, gaps, counts and time limits need counters that a PIO program cannot combine for 8
stages. The software evaluation handles every condition alike and keeps the PIO capture as
simple as the stream capture; its price is the sample rate (below).

If the evaluation falls so far behind that the DMA overwrites samples it still needs (the
evaluated samples or the pre-trigger samples), the capture ends **without samples** (length 0
followed by the timestamp byte 0); the application reports that the evaluation could not keep up.
If the stop value reaches the PIO program too late (only when the evaluation lags), the firmware
stops the capture itself as soon as the post-trigger samples are written and checks that the
pre-trigger samples are still intact.

### Command 10

Payload (little endian, packed):

| Offset | Size | Field |
| --- | --- | --- |
| 0 | 1 | format version, `1` |
| 1 | 1 | flags: bit 0 state mode, bit 1 falling clock edge (others 0) |
| 2 | 1 | clock channel (state mode, else 0) |
| 3 | 1 | number of stages, 0 to 8 |
| 4 + 28·n | 28 | stage *n*, below |

Stage:

| Offset | Size | Field |
| --- | --- | --- |
| 0 | 1 | kind: 0 pattern, 1 edge, 2 pulse, 3 gap |
| 1 | 1 | channel (edge, pulse, gap) |
| 2 | 1 | edge: 0 rising / high pulse, 1 falling / low pulse, 2 any |
| 3 | 1 | 0 |
| 4 | 4 | pattern: channel mask (bit n = channel n) |
| 8 | 4 | pattern: levels of the masked channels |
| 12 | 4 | pulse: shortest width, gap: length, in samples |
| 16 | 4 | pulse: longest width in samples, `0xFFFFFFFF` = no limit |
| 20 | 4 | occurrences that complete the stage, at least 1 |
| 24 | 4 | samples after the previous stage within which the stage has to complete, `0xFFFFFFFF` = no limit (ignored for the first stage) |

Times are at most 2^31 − 1 samples. The application converts nanoseconds with the sample rate:
the shortest pulse width is rounded down, the longest pulse width, the gap and the time limit
up, so the timing accuracy is ±1 sample period. A configuration is used by the next capture
request only; a capture request with trigger type 7 without a configuration is answered
`CAPTURE_ERROR`. The longest command (8 stages, every byte escaped) takes 462 bytes, so the
receive buffer was enlarged to 512 bytes.

### Capture request with trigger type 7

Channels, capture mode, pre- and post-trigger samples as usual; `loopCount` and `measure` must
be 0, `trigger`, `inverted/count` and `triggerValue` are ignored. Every channel of the stages
must be part of the samples of the capture mode (the application chooses the mode by the
captured *and* the stage channels). Timing mode: the frequency must not exceed
`SEQUENCE_MAX_RATE` when there are stages (and the system clock / 5). State mode: the frequency
is ignored.

### Conditions

A stage is evaluated from the sample after the one that completed the previous stage.

* **Pattern:** the masked channels have the given levels. An occurrence is a sample where the
  pattern starts to match, or the first sample of the stage if it matches already.
* **Edge:** a rising, falling or any edge of the channel.
* **Pulse:** a pulse of the channel whose width (samples between its two edges) lies within the
  limits, recognised at its closing edge; high pulse: rising then falling edge, low pulse:
  falling then rising, any: both. The opening edge may lie before the stage began (the edges of
  every pulse channel are recorded in every stage); a pulse whose opening edge was not captured
  does not count.
* **Gap:** the channel does not change for the given number of samples, measured from the start
  of the stage, its last edge or the previous occurrence, whichever is latest; recognised when
  the time is over.
* **Count / time limit:** a stage completes after `count` occurrences; if a stage after the first
  does not complete within its time limit after the previous stage, the sequence starts again
  with the first stage at that sample.

### Capabilities and limits

| Capability | Meaning |
| --- | --- |
| `TRIGGER_SEQUENCE=8` | up to 8 stages |
| `TRIGGER_CONDITIONS=pattern/edge/pulse/gap` | condition kinds |
| `SEQUENCE_MAX_RATE=<Hz>` | highest sample rate of a capture with stages, measured at start-up |
| `STATE_MODE` | state mode (external clock) |
| `STATE_MAX_CLOCK=<Hz>` | highest clock of the state mode, system clock / 8 |

`SEQUENCE_MAX_RATE` is measured by the firmware at start-up on the board itself: the evaluation
of 16384 samples in which *every* sample is an event of the current stage (a pulse stage whose
channel toggles on every sample, the most expensive sample with one pulse channel), minus 20 %
for the DMA, the USB interrupts and further pulse channels. Signals that change less often are
evaluated much faster (a sample without a change of a watched channel costs about 8–12 cycles);
bursts above the rate are absorbed by the ring buffer. Only long stretches of an event on every
sample above the rate can end a capture without samples. The self-test reports the measured
speed (`SEQUENCE`). Estimates from the generated code (instruction counts of the emulated
Cortex-M0+ / M33 code, before the 20 % margin):

| Board | System clock | Event on every sample | Edge stage, quiet watched channel | Pattern stage, a change every 8th sample |
| --- | --- | --- | --- | --- |
| RP2040 (Pico, Pico W, Zero, Interceptor) | 200 MHz | ≈ 1.3 Msps | ≈ 17 Msps | ≈ 7 Msps |
| RP2040 turbo | 400 MHz | ≈ 2.5 Msps | ≈ 35 Msps | ≈ 14 Msps |
| RP2350 (Pico 2, Pico 2 W) | 200 MHz | ≈ 2 Msps | ≈ 23 Msps | ≈ 11 Msps |
| RP2350 turbo | 400 MHz | ≈ 4 Msps | ≈ 47 Msps | ≈ 22 Msps |

So `SEQUENCE_MAX_RATE` should be about 1 MHz on RP2040 boards and 1.6 MHz on RP2350 boards (twice
that with turbo); the value the board reports is what counts. The Pico W and Pico 2 W builds are
debug builds, but the evaluation is always compiled with `-O3` and runs from RAM.

### State mode

The PIO program waits for the active edge of the clock input (`wait 1 pin` / `wait 0 pin`; the
firmware patches the input index and the polarity into a copy of `STATE_CAPTURE`) and samples all
inputs one PIO cycle (5 ns at 200 MHz) after the edge as seen by the input synchronisers, which
delay clock and data alike. The data therefore has to stay valid for at least ≈ 10 ns after the
active edge (one cycle plus synchroniser jitter).

* **Clock input:** any channel of the board (channels 1–24, Interceptor 1–28), given by its
  channel number; its GPIO comes from the pin map. Not the external trigger input. The clock
  channel may also be captured (it then shows its level after the edge).
* **Edge:** rising or falling (flag bit 1); one edge only.
* **Clock frequency:** at most `STATE_MAX_CLOCK` = system clock / 8 (25 MHz, 50 MHz with turbo):
  the clock has to stay at least 4 cycles at the active and 3 cycles at the inactive level. The
  first sample needs a complete edge (the program waits for the inactive level first).
* **Triggers:** the application sends the immediate trigger as a sequence without stages (the
  trigger follows the pre-trigger samples), the edge and the pattern trigger as a sequence of one
  stage and a trigger sequence with pattern and edge stages and counts. Pulse, gap and time
  limits need a time base and are refused. A clock faster than the evaluation can follow works as
  long as the ring buffer absorbs the difference, otherwise the capture ends without samples
  (immediate captures are never affected: the PIO program stops by itself).
* The frequency of the request is ignored; the application shows the samples as states.
* Without a clock no sample is taken: the capture waits until the application cancels it.

## Fixed bugs

| File | Problem in the original | Fix |
| --- | --- | --- |
| `PiPiLogicAnalyzer.c` | 128 byte receive buffer with an 8 bit index; the overflow check `>= 256` could never trigger. Longer frames overwrote the memory behind it, e.g. WiFi settings with escaped characters (`U` = 0x55 in the password) or garbage after `0x55 0xAA`. | 256 byte buffer (512 since the trigger sequences), 16 bit index, check before writing. |
| `PiPiLogicAnalyzer.c` | Capture and WiFi requests were interpreted as structures without a length check, directly in the unaligned buffer. | The length must equal `sizeof` the structure; copied with `memcpy`. |
| `PiPiLogicAnalyzer.c` | SSID, password and IP were stored without a terminating zero although the WiFi core uses them as C strings. | The last byte of every field is set to 0. |
| `PiPiLogicAnalyzer.c` | WiFi responses were copied into a 32 byte event with `memcpy` without a length limit. | Responses go through `wifi_transfer` in 32 byte chunks; `printf` with the format string `"%s"`. |
| `PiPiLogicAnalyzer_Capture.c` | Blast mode: the buffer end was computed as `lastPostSize` instead of `lastPostSize - 1`. The first sample was missing and a zeroed one was appended (with a full buffer sample 0 came last). | Correct buffer end. |
| `PiPiLogicAnalyzer_Capture.c` | The NMI and SysTick handlers of the burst measurement were only restored for `timestampIndex != 0`. After an abort before the first timestamp, the next measurement panicked in `exception_set_exclusive_handler`. | Separate flag `lastBurstMeasure`; the previous SysTick state is restored. |
| `PiPiLogicAnalyzer_Capture.c` | A burst measurement without loops loaded the measuring PIO program but installed no NMI handler. The `irq wait 1` blocked the capture forever. | Measuring only with `loopCount > 0`; the program is selected accordingly. |
| `PiPiLogicAnalyzer_Capture.c` | `GetBuffer` computed with `lastTail = 0xFFFFFFFF` when `find_capture_tail` could not determine the DMA position and then wrote far behind the buffer. | The buffer end is limited to the valid range. |
| `PiPiLogicAnalyzer_Capture.c` | Missing parameter checks: unknown capture mode (undefined buffer size), channel numbers outside the pin map (reading outside the array, arbitrary GPIOs), 0 post-trigger samples (`postLength - 1` gave an endless capture), frequency 0 (division by 0) or too low for the PIO divider, 32 bit overflow of `pre + post × (loops + 1)`. | All values are checked before starting. |
| `PiPiLogicAnalyzer_Capture.c` | Pattern trigger: bits above the pattern length in the trigger value prevented any trigger. | The trigger value is masked to the pattern length. |
| `PiPiLogicAnalyzer_Capture.c` | `StopCapture` could finish a capture a second time if it ended right before interrupts were disabled. `captureFinished` was not `volatile`. | Checked again with interrupts disabled; `volatile`. Blast captures no longer touch the unused second DMA channel. |
| `PiPiLogicAnalyzer_Capture.c` | After a simple capture the PIO program of the other edge was removed, never the measuring one. | The program actually loaded is removed. |
| `PiPiLogicAnalyzer_Capture.c` | `1 << pin` with pin 31 is undefined in C. | `1u << pin`. |
| `PiPiLogicAnalyzer_Capture.h` | `RP2350.h` was only included for `BUILD_PICO_2`, not for the Pico 2 W. | Included through `CORE_TYPE_2`. |
| `PiPiLogicAnalyzer_WiFi.c` | `tryStartServer` had no `return true` on the success path (undefined return value). On errors every retry leaked a PCB. | Return value added, PCBs are closed on errors. |
| `PiPiLogicAnalyzer_WiFi.c` | `sendData` called `tcp_write` with `NULL` if the client disconnected while waiting for send buffers. | Aborts when no client is connected. |
| `PiPiLogicAnalyzer_WiFi.c` | `sleep_ms(100)` with interrupts disabled during the battery measurement. | `busy_wait_ms(100)`. |
| `Event_Machine.c` | `event_has_events` compared the addresses of two structure fields and always returned `true`. | `!queue_is_empty(...)`. |
| `PiPiLogicAnalyzer_Build_Settings.cmake` | Turbo mode (overclocking and overvoltage) was the default; the board list was outdated. | Turbo off, complete list. |
| `publish.ps1` | Turbo builds were attempted for the Pico 2 W although CMake refuses them. | They are skipped. |
| `PiPiLogicAnalyzer_WiFi.c` | Received data was never acknowledged with `tcp_recved`: every request shrank the TCP receive window until the connection hung. | Acknowledged as it is handed to the capture core. |
| `PiPiLogicAnalyzer_WiFi.c` | The error callback closed the PCB that lwIP had already freed; a remote close returned `ERR_ABRT` without aborting the PCB. | Only the reference is dropped; `ERR_ABRT` only after `tcp_abort`. |
| `PiPiLogicAnalyzer_WiFi.c` | Received data was pushed into the event queue with a blocking call. With both queues full the two cores could block each other; a client that stopped reading stalled both cores. | The data waits in lwIP (closed window) until the queue has room; a client is dropped after 5 s without progress. Responses are sent with `tcp_output`. |
| `PiPiLogicAnalyzer_Capture.c` | Channels whose GPIO bit did not fit into the samples of the mode read as 0 (Interceptor in 8 and 16 channel mode). | Rejected by the firmware; the application chooses the mode by the GPIO bits. |
| `PiPiLogicAnalyzer.c` | Unknown trigger types started an edge capture. | `CAPTURE_ERROR`. |
| `PiPiLogicAnalyzer_Capture.c` | Undefined shifts: `systickLoops << 24` (signed) and the blast mask for the external trigger (negative shift). A blast capture kept the trigger input inverted. | Unsigned shift, mask only for sampled GPIOs, GPIOs released. |
| `CMakeLists.txt` | A changed `BOARD_TYPE` was ignored in an existing build directory (cached `PICO_BOARD`). | `PICO_BOARD` is forced. |

## Known, unfixed issues

* `find_capture_tail` evaluates `transfer_count` of the DMA channel the way the author marked
  with "TODO: CHECK". This project only prevents an invalid value from corrupting memory.

## Status

All twelve variants (eight boards, turbo where possible) compile with Pico SDK 2.1.1 and ARM GCC
14.2 via `build_all.sh`, without warnings from the source files of this project. The signal
generator `PiPiLogicAnalyzer_Simulation.c` is also built and checked on the computer in every test
run. **The changes have not been tested on hardware yet.**

Before relying on it, run the self-test and a simulated capture once and check at least a
simple, a burst and a blast capture with real signals.

The trigger sequences and the state mode have only been tested without hardware: the evaluation
on the computer against a reference implementation (`tests/test_pico_triggers.py`), the
application side against a simulated device. To check on a board:

* The self-test line `SEQUENCE` and `SEQUENCE_MAX_RATE` in the capabilities (about 1 MHz on an
  RP2040, 1.6 MHz on an RP2350, twice that with turbo).
* A sequence with every condition kind on a known signal (e.g. a UART or a PWM from a second
  board): trigger position, pre- and post-trigger samples, pulse widths at the limits (±1 sample).
* The cancel of a sequence that never completes, and a capture right after it.
* A sequence at the maximum rate on a signal that toggles on every sample: it has to complete or
  end with the "could not keep up" message, never with wrong samples.
* State mode on a clock from a second board (SPI clock): rising and falling edge, immediate,
  edge, pattern and sequence trigger, up to `STATE_MAX_CLOCK`; check that each sample shows the
  data of its edge (hold time).
* The existing captures (edge, pattern, blast, stream) once more, the receive buffer and the
  capability line changed.
