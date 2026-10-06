# openSciLab Arduino firmware

Turns an Arduino into an openSciLab instrument: GPIO, PWM, DAC, pulses, a monitor, stream,
buffer and state captures, a pattern generator, a square wave, an arbitrary waveform (DAC boards)
and UART/SPI/I²C sending. The protocol (COBS frames with CRC-8, protocol 1) is specified in
[`docs/protocols.md`](../../docs/protocols.md), section *Arduino firmware*; the capability strings
come from [`capabilities.h`](capabilities.h), which `tests/test_protocol_spec.py` checks against it.

## Boards

| Environment | Board | Line | Pins in the table | Capture buffer | Pattern runs | DAC points |
| --- | --- | --- | --- | --- | --- | --- |
| `uno` | Arduino Uno R3 | 115200 baud | D0–D13 (D0/D1 reserved), A0–A5 | 448 B | 32 | – |
| `nano` | Arduino Nano (Optiboot) | 115200 baud | as Uno, plus A6/A7 (analog only) | 448 B | 32 | – |
| `megaatmega2560` | Arduino Mega 2560 | 115200 baud | full ports: D22–D29, D53–D50/D10–D13, D49–D42, A8–A15 | 4 KiB | 256 | – |
| `uno_r4_minima` | Arduino Uno R4 Minima | USB | D0–D13, A0–A5 (DAC on A0) | 12 KiB | 512 | 2048 |
| `esp32dev` | ESP32 DevKitC (WROOM-32) | 2 Mbaud | 26 GPIOs (GPIO1/3 reserved), DAC on GPIO25/26 | 48 KiB | 2048 | 8192 |
| `esp32-s3` | ESP32-S3 DevKitC-1 | native USB | 31 GPIOs (GPIO19/20 reserved) | 64 KiB | 2048 | – |

Pins on the wire are indexes into the pin table (command `PINS`); bit *n* of a level mask is pin
*n*. Capture channels (the `channel` field of a `PIN:` line) count the free digital inputs
without gaps. The levels are 32 bit, so a table has at most 32 pins: the Mega uses whole ports
(fast to sample) instead of D0–D13.

Rates (`INFO` in `src/arch/*/board_*.cpp`) are estimates until the benchmark (step 2a,
[`benchmark/`](benchmark/README.md)) measured them.

## Build and flash

```bash
pip install platformio
pio run -d firmware/arduino                        # every board
pio run -d firmware/arduino -e uno -t upload       # one board, flashed over USB
pio test -d firmware/arduino -e native             # hardware-free tests (needs a host C++ compiler)
firmware/build_all.sh --arduino                    # images to firmware/arduino-images/
```

Upload uses the board's own bootloader (avrdude, bossac/dfu for the R4, esptool). An ESP32 may
need its BOOT button held while the upload starts.

## Layout

| Path | What |
| --- | --- |
| `src/protocol.*` | COBS, CRC-8, the frame decoder (counts damaged frames) and writer |
| `src/app.*` | the command dispatcher, pin checks, monitor, watchdog, SAFE |
| `src/capture*.cpp` | stream (timer interrupt, runs), buffer (counted loop, pre-trigger ring), state (clock interrupt) |
| `src/generator.cpp` | pattern generator, square wave, arbitrary waveform |
| `src/tx.cpp` | UART, SPI, I²C sending, bit-banged on any free pins |
| `src/board.*`, `src/board_config.h` | the pin table interface, memory sizes per board |
| `src/hal.h`, `src/arch/<arch>/` | the hardware layer: `avr`, `renesas` (R4), `esp32`, `native` (the fake board of the tests) |
| `test/` | Unity tests: frames against `tests/fixtures/arduino_frames.json`, the dispatcher on the fake board |
| `benchmark/` | step 2a: measures a board |

## Behaviour worth knowing

* The board starts with every pin an input. When outputs are driven (WRITE, PIN_MODE output/PWM,
  PWM, DAC, PULSE, TX, a running generator or square wave) and no frame came for 1 s, it releases
  them (as SAFE) and sends the event WATCHDOG. SAFE also stops the monitor.
* Analog values of stream and state captures and of the monitor are the latest conversion of each
  channel; the main loop converts the channels in turn (sample and hold), the interrupts never wait
  for the ADC. `ADC_READ` converts at once.
* Stream: one timer interrupt per sample; digital levels go out as runs, a level that holds is sent
  every 50 ms for the live view. A full ring ends the capture with DONE status 1 (overflow).
* Buffer: digital only, 1–4 bytes per sample (by the highest pin of the mask), at most two seconds
  behind the trigger. While it waits for the trigger the board keeps answering (on AVR the GPIO
  commands answer busy); a sample taken late ends with DONE status 1.
* State: a frame per clock edge (`CLOCK`: external interrupt pins, `CLOCK_SW`: pin change or
  other interrupt pins); a full ring loses the edge and counts it in DONE.
* AVR timers: Timer2 serves either the stream sampling or the pattern generator (the other answers
  busy, PWM on D3/D11 stops meanwhile); Timer1 serves either the square wave (D9/D10, Mega D11/D12)
  or the counted clock of buffer captures and UART sending. PWM uses the core's fixed frequencies
  (490/980 Hz); the actual frequency is in the answer.
* SQUARE on a pin without the square timer uses its PWM at 50 %.
