# Protocols and the capability vocabulary

This file is the one source of the strings devices use to say what they can do. The Python
constants (`CAPABILITY_*` in `openscilab/driver/`, the pin capabilities in
`openscilab/core/instrument.py`) and the C headers of the firmware (`firmware/*/capabilities.h`)
are checked against the tables below by `tests/test_protocol_spec.py`. A new capability is added
here first, then in Python and in the firmware.

## Capabilities

A device reports a list of capabilities (the Pico firmware in the answer `CAPS:` to command 8,
other drivers in `capabilities()`). The application never asks for the type of a device: what a
document, a node or the device card offers follows from these strings and the facets they unlock.

* **Capability**: the string, exactly as in the constant. Strings ending in `=` carry a parameter
  directly behind it; the others are reported alone or, where the *Parameter* column says so,
  with `=<value>`.
* **Facet**: the facet of `openscilab/core/instrument.py` the capability unlocks (`–`: it changes
  what the capture facet offers).
* **Pico**: `yes` – the Pico firmware reports it today (it is in its `capabilities.h`); `8` –
  planned for the Pico firmware 8 (phase 1); `–` – not a Pico capability.

<!-- capabilities -->
| Capability | Parameter | Meaning | Facet | Pico |
| --- | --- | --- | --- | --- |
| `SELFTEST` | – | The board tests itself (command 7, no signal may be connected). | – | yes |
| `SIMULATION` | – | Captures of generated test signals (trigger type `SIMULATION`). | – | yes |
| `DEVICEINFO` | – | Detailed board and build information (command 9). | – | yes |
| `EDGE_TRIGGER_OUT` | – | An edge trigger that drives the trigger output (trigger type `EDGE_OUT`). | – | yes |
| `PATTERN_GROUPS=` | `<first>-<last>/…` | Channel groups a pattern trigger can cover (channels on consecutive GPIOs). | – | yes |
| `IMMEDIATE_TRIGGER` | – | Captures can start without a trigger (trigger type `IMMEDIATE`). | – | – |
| `THRESHOLD` | – | The input threshold voltage can be set. | – | – |
| `CONTINUOUS_STREAM` | – | Stream captures can run until they are stopped. | – | – |
| `STREAM_IMMEDIATE_ONLY` | – | Stream captures start at once, without a trigger. | – | – |
| `STREAM` | `=<bytes per second>` | Stream captures over USB with this throughput. | – | yes |
| `TRIGGER_SEQUENCE` | `=<stages>` | Trigger sequences in the device (trigger type `SEQUENCE`, command 10). | – | yes |
| `TRIGGER_CONDITIONS=` | `pattern/edge/pulse/gap` | Condition kinds of the trigger sequence. | – | yes |
| `SEQUENCE_MAX_RATE=` | `<Hz>` | Highest sample rate of a capture with a trigger sequence. | – | yes |
| `STATE_MODE` | – | State mode: samples on the edges of a clock input. | – | yes |
| `STATE_MAX_CLOCK=` | `<Hz>` | Highest clock of the state mode. | – | yes |
| `STREAM_STATE` | – | State mode on a stream: every clock edge is sent at once (live state). | – | yes |
| `GPIO` | – | Pins as inputs and outputs: modes, levels, pulses. | `GpioFacet` | yes |
| `PWM` | – | Hardware PWM on pins. | `GpioFacet` | yes |
| `MONITOR` | – | Periodic reports of the inputs (a slow capture alongside everything else). | `MonitorFacet` | yes |
| `ANALOG=` | `<count>` | Analog input channels in captures and the monitor. | `AnalogInFacet` | yes |
| `DAC` | – | Analog outputs. | `AnalogOutFacet` | – |
| `PATTERN_GEN=` | `<max rate>,<pins>` | Pattern generator on pins. | `GeneratorFacet` | yes |
| `GEN_SQUARE` | – | Square waves on a pin by PWM or a timer. | `GeneratorFacet` | yes |
| `AFG` | – | Analog function and arbitrary waveform generator outputs. | `GeneratorFacet` | – |
| `TX_UART` | – | Sends UART frames. | `GeneratorFacet` | yes |
| `TX_SPI` | – | Sends SPI transfers. | `GeneratorFacet` | yes |
| `TX_I2C` | – | Sends I²C transfers. | `GeneratorFacet` | yes |
| `PROGRESSIVE` | – | Large captures arrive progressively: an overview first, tiles on demand. | – | – |
| `RESTART` | – | The device restarts on request: outputs are released, it answers again on the same connection. | – | – |
<!-- /capabilities -->

## Pins

Instruments with pins describe them in a pin table (`PinInfo` in `openscilab/core/instrument.py`):
name, capabilities, the capture channel the pin is wired to, a reason when it is reserved, its
logic level and its analog channel. The pin capabilities:

<!-- pin-capabilities -->
| Pin capability | Meaning |
| --- | --- |
| `DIN` | Digital input. |
| `DOUT` | Digital output. |
| `PULLUP` | Input with pull-up. |
| `PULLDOWN` | Input with pull-down. |
| `PWM` | Hardware PWM output. |
| `ADC` | Analog input. |
| `DAC` | Analog output. |
| `CLOCK` | Can clock a state capture in hardware. |
| `CLOCK_SW` | Can clock a state capture in software (interrupt), slower. |
<!-- /pin-capabilities -->

On the wire (Pico firmware 8, Arduino firmware) a device sends one line per pin after `PINS:<n>`:

```
PIN:<name>,<capabilities separated by />,<channel or ->,<logic level in mV>,<analog channel or ->[,<reserved: reason>]
PIN:GP2,DIN/DOUT/PULLUP/PULLDOWN/PWM/CLOCK,0,3300,-
PIN:GP0,DIN/DOUT,-,3300,-,reserved: trigger output of a multi-board set
```

Simulator profiles use the same vocabulary (`pins` in `examples/sim/*.json`, see
[simulator.md](simulator.md)).

## Pico firmware (binary protocol, protocol 8)

The firmware of `firmware/pico/` (USB serial or TCP port 4045 on the W boards).
Requests are framed and escaped; answers are text lines ended by `\n`, captures binary. All
numbers in requests are little endian; `f32` is an IEEE 754 float.

```
request:  0x55 0xAA <payload> 0xAA 0x55
payload:  <command byte> [<data>]
escaping: 0xAA, 0x55 and 0xF0 inside the payload are sent as 0xF0 (byte ^ 0xF0)
```

A frame holds at most 512 bytes on the wire (escaped); data of variable length (pattern blocks,
bytes to send) goes in pieces of at most 200 bytes.

### Commands of protocol 1 (unchanged)

| Command | Request | Answer |
| --- | --- | --- |
| 0 | identification | `OPENSCILAB_PICO_<BOARD>_V<major>_<minor>`, `FREQ:<Hz>`, `BLASTFREQ:<Hz>`, `BUFFER:<bytes>`, `CHANNELS:<n>`, `PROTOCOL:<n>` (one line each) |
| 1 | start a capture: `CAPTURE_REQUEST` (56 bytes, little endian, natural alignment) | `CAPTURE_STARTED` or `CAPTURE_ERROR`; when it completes, see *Capture data* below |
| 2 | WiFi settings: `WIFI_SETTINGS_REQUEST` (116 bytes; W boards) | `SETTINGS_SAVED`, `ERR_UNSUPPORTED` on other boards |
| 3 | supply voltage (W boards) | the voltage, `ERR_UNSUPPORTED` on other boards |
| 4 | enter the bootloader | `RESTARTING_BOOTLOADER` |
| 5 / 6 | blink the LED on / off | `BLINKON` / `BLINKOFF` |
| 7 | self-test | `SELFTEST:<item>:<status>:<detail>` lines, `SELFTEST_END` |
| 8 | capabilities | `CAPS:<capability>,<capability>,…` |
| 9 | device information | `INFO:<key>:<value>` lines, `INFO_END` |
| 10 | trigger sequence and state mode of the next request with trigger type 7 (or of the next stream, trigger type 6): header `<BBBB` (version 1, flags: 1 state mode, 2 falling clock; clock channel; stage count) and stages `<BBBxIIIIII` (kind, channel, edge, mask, value, min, max, count, within) | `SEQUENCE_OK` or `SEQUENCE_ERROR` |

Older identifications (`PIPI_LOGIC_ANALYZER_…`, `LOGIC_ANALYZER_…`) are recognised by the
application only to offer the update. It opens boards whose `PROTOCOL:<n>` equals
`FIRMWARE_PROTOCOL` in `openscilab/driver/pico/protocol.py` (8).

### Commands of protocol 8

Pins are GPIO numbers (`GP<n>` in the pin table). Answers are `OK`, the answer named below, or
`ERR:<reason>` (one line; the reason is for people). `ERR:BUSY` – the pin belongs to a running capture
(a channel, a trigger or clock input; pins already driven before the capture stay usable) or the
command is not allowed while one runs; `ERR:RESERVED` – the pin is reserved (pin table);
`ERR:PIN` – the pin cannot do it; `ERR:ARG` – a request of the wrong length or an impossible
value; `ERR:FULL` – pattern data beyond the buffer; `ERR:NACK`, `ERR:TIMEOUT` – I²C. Hexadecimal
answers (`LEVELS:`, `STATE:`, `RX:`) are upper case without `0x`; frequencies and rates
(`PWM:`, `GEN_STARTED:`, `ANALOG_OK:`) have three decimals.

| Command | Request data | Answer |
| --- | --- | --- |
| 11 PINS | – | `PINS:<n>` and `n` lines `PIN:…` (see *Pins* above), one for every GPIO 0–29 |
| 12 PIN_MODE | `<BB` pin, mode (0 input, 1 input with pull-up, 2 input with pull-down, 3 output, 4 PWM, 5 analog) | `OK` |
| 13 WRITE | `<II` mask, levels (bit n: GPIO n; the pins of the mask become outputs) | `OK` |
| 14 READ | `<I` mask | `LEVELS:<hex>` (the levels of all GPIOs, masked) |
| 15 PWM | `<BfH` pin, frequency Hz, duty (0–65535; 0 stops: output low) | `PWM:<actual frequency, Hz>` |
| 16 PULSE | `<BBIHI` pin, level, width ns, count, period ns (count 1: one pulse; timed by a PIO state machine, exact to a cycle) | `OK` when it started (the command returns at once) |
| 17 MONITOR | `<IIB` rate in mHz (0 stops, at most 1 kHz), GPIO mask, ADC mask (bit n: ADC n) | `OK`; then reports `STATE:<time µs>,<levels hex>[,<adc raw>…]` (raw 12 bit values in the order of the mask bits) |
| 18 HEARTBEAT | – | no answer |
| 19 ADC_READ | `<B` ADC mask | `ADC:<raw>,…` (12 bit, in the order of the mask bits) |
| 20 SAFE | – | `OK`: every output an input, PWM, pulses, the pattern generator and the monitor stopped |
| 21 GEN_LOAD | `<IB` offset (samples), flags (bit 0: run lengths; bits 1–2: bytes per sample 0 = 1, 1 = 2, 2 = 4), then the data; offset 0 without data empties the buffer | `GEN_LOADED:<samples in the buffer>` |
| 22 GEN_START | `<fBBIIBB` rate Hz, first pin, pin count (1–24, consecutive GPIOs), length (samples), passes (0: until stopped), flags (1: wait for a rising edge on the trigger input), sync pin (0xFF: none) | `GEN_STARTED:<actual rate>` |
| 23 GEN_STOP | – | `OK`; the pattern pins become inputs (a finished run holds its last sample until then) |
| 24 GEN_STATUS | – | `GEN:<running 0/1>,<passes done>` |
| 25 CAPTURE_ANALOG | `<BI` ADC mask, rate per channel in Hz (0: no analog channels) for the next capture request or stream | `ANALOG_OK:<actual rate per channel>` |
| 26 TX_UART | `<BI` TX pin, baud, then the bytes | `OK` when sent |
| 27 TX_SPI | `<BBBBIB` SCK, MOSI, MISO (0xFF: none), CS (0xFF: none), frequency Hz, mode (0–3), then the bytes | `RX:<hex>` (the bytes read on MISO; empty without it) |
| 28 TX_I2C | `<BBBIH` SDA, SCL, 7 bit address, frequency Hz, bytes to read, then the bytes to write | `RX:<hex>` (the bytes read; written first, then read with a repeated start), `ERR:NACK` when nothing acknowledges |

Pattern data (`GEN_LOAD`): one sample is 1 byte for up to 8 pins, 2 bytes for up to 16, 4 bytes
for more (bit n: pin `first + n`). With flag 1 the data are run lengths: pairs of a 16 bit count
(1–65535) and one sample; the board unpacks them into its buffer from `offset` on. The pattern
lives at the start of the capture memory (at most 64 KiB on the RP2040, 256 KiB on the RP2350;
`GEN_LOAD` answers `ERR:FULL` beyond it), and the samples of analog channels go behind the digital
ones: a loaded pattern and analog channels make the capture depth smaller (`BUFFER:` reports the
whole memory; a capture that no longer fits answers `CAPTURE_ERROR`). `GEN_START` answers
`ERR:ARG` when the pin count does not fit the bytes per sample of the loaded data, or when length
× passes is beyond 2³². The sync pin is high from the first sample to the end of the run.

The square wave output (`GEN_SQUARE`) is the PWM of GP22 (command 15).

### While a capture runs

* A buffer capture (types 0–5, 7) accepts the commands 13–20, 23 and 24 while it waits for its
  trigger and while it records; commands that would use a pin of the capture answer `ERR:BUSY`.
  Monitor reports keep coming. When the capture completes the board sends the line
  `CAPTURE_DATA` and the binary capture data (below); answers and reports never come in between.
* The single byte `0xFF` outside a frame stops a capture (protocol 1 stopped on any byte).
* A stream (type 6) takes no commands: any byte stops it, and there are no monitor reports.
* The watchdog does not run during a capture or a stream.

### Capture data

After `CAPTURE_DATA` (buffer captures): the sample count (`<I`), the samples (1, 2 or 4 bytes each
for 8, 16 or 24 channels), the count of burst timestamps (`<B`) and the timestamps (`<I` each,
when the count is more than 1). With analog channels (command 25) then: the sample count per
channel (`<I`), the actual rate in mHz (`<I`) and the samples, 12 bit in `<H`, interleaved in the
order of the mask bits. The analog samples cover the same time as the digital ones, aligned to
within one analog sample.

A stream answers `STREAM_STARTED:<bits>` (the bit of every channel in the samples) and then sends
chunks: a header `<I` and the data. The header's top two bits say what follows, the rest is the
byte count:

| Header | Chunk |
| --- | --- |
| `0x00000000` | end: stopped by the host |
| `0xFFFFFFFF` | end: overflow (USB too slow; the chunk before may hold overwritten samples) |
| `00` + count | digital samples |
| `10` + count | analog samples (12 bit, `<H`, interleaved in mask order) |

A stream with the state mode (command 10 with flag 1 before the request) samples on the edges
of the clock channel (`STREAM_STATE`); its samples have no times.

### Watchdog

When outputs are driven (pins set by 12, 13, 15, 16, a pattern running) and no frame came for
1 s, the board releases them (command 20) and sends the line `WATCHDOG`. The application keeps
it satisfied with command 18 while it is connected (`core.instrument.KeepAlive`).

## Arduino firmware (protocol 1)

The firmware of `firmware/arduino/` on Arduino Uno, Nano, Mega 2560, Uno R4 Minima, ESP32 and
ESP32-S3 (USB serial, 115200 baud on the AVR boards, 2 Mbaud elsewhere). Frames carry COBS-encoded
payloads ended by `0x00`, so a lost byte costs one frame:

```
frame:  COBS(<type> <sequence> <payload> <crc8>) 0x00
crc8:   polynomial 0x07, initial value 0x00, not reflected, over type, sequence and payload
type:   0x01 request (host), 0x02 answer, 0x03 event, 0x04 error answer
```

An answer and an error answer repeat the sequence number of their request; events count on
their own. The payload of a request is the command byte and its data; of an answer the command
byte and the result; of an error answer the command byte, an error code and a text. Numbers are
little endian, `f32` IEEE 754. A frame payload holds at most 250 bytes.
`tests/fixtures/arduino_frames.json` has test vectors (COBS, CRC, whole frames) for the firmware
and the driver.

Error codes: 1 unknown command, 2 bad arguments, 3 reserved pin, 4 busy (a capture runs or the pin
belongs to it), 5 not supported by this board, 6 no acknowledge (I²C), 7 overflow.

Pins are indexes into the board's pin table (command 0x02). Pin masks: bit n is pin n of the table; the capture channel of a pin is the
`channel` field of its `PIN` line. `CAPS` lists `STREAM=<bytes per second>` on boards that stream.

| Command | Request data | Answer data |
| --- | --- | --- |
| 0x01 HELLO | – | text `board=<name>;version=<x.y.z>;protocol=1;rate=<highest sample rate, Hz>;clock=<Hz>;buffer=<bytes>;adc_bits=<n>;dac_bits=<n>;vref_mv=<n>` |
| 0x02 PINS | `<B` index | text `PIN:…` (see *Pins*), empty after the last pin |
| 0x03 CAPS | – | text `<capability>,…` |
| 0x10 PIN_MODE | `<BB` pin, mode (as Pico command 12) | – |
| 0x11 WRITE | `<II` mask, levels (bit n: pin n) | – |
| 0x12 READ | `<I` mask | `<I` levels |
| 0x13 PWM | `<BfH` pin, frequency, duty 0–65535 | `<f` actual frequency |
| 0x14 DAC | `<BH` pin, raw value | – |
| 0x15 PULSE | `<BBI` pin, level, width µs | – (returns when the pulse is over) |
| 0x16 ADC_READ | `<H` ADC mask | `<H` raw value per mask bit |
| 0x17 MONITOR | `<IIH` rate mHz (0 stops), pin mask, ADC mask | –; then events STATE |
| 0x18 HEARTBEAT | – | – |
| 0x19 SAFE | – | – |
| 0x20 CAPTURE_SETUP | `<BIIHIIBIIBB` mode (0 stream, 1 buffer, 2 state), rate Hz, pin mask, ADC mask, samples, pre-trigger samples, trigger (0 none, 1 rising, 2 falling, 3 pattern), trigger pin mask, trigger levels, clock pin, clock edge (0 rising, 1 falling) | `<I` actual rate |
| 0x21 CAPTURE_START | – | –; then events DATA and DONE |
| 0x22 CAPTURE_ABORT | – | – (then the event DONE) |
| 0x30 GEN_LOAD | `<H` offset (runs), then runs: `<HI` count, levels | `<H` runs in the buffer |
| 0x31 GEN_START | `<fIHB` rate, pin mask, passes (0: until stopped), flags (1: wait for the trigger pin) | `<f` actual rate |
| 0x32 GEN_STOP | – | – |
| 0x33 GEN_STATUS | – | `<BH` running, passes done |
| 0x34 SQUARE | `<Bf` pin, frequency (0 stops) | `<f` actual frequency |
| 0x35 ARB_LOAD | `<H` offset, then `<H` DAC values | `<H` points in the buffer |
| 0x36 ARB_START | `<fHB` rate (points per second), points, passes | `<f` actual rate |
| 0x40 TX_UART | `<BI` pin, baud, then the bytes | – |
| 0x41 TX_SPI | `<BBBBI` SCK, MOSI, MISO (0xFF none), CS (0xFF none), frequency, then the bytes | the bytes read |
| 0x42 TX_I2C | `<BBBIB` SDA, SCL, address, frequency, bytes to read, then the bytes to write | the bytes read |

Events (type 0x03), the first byte says which:

| Event | Data |
| --- | --- |
| 0x01 STATE | `<II` time µs, pin levels, then `<H` per ADC mask bit |
| 0x02 DATA | `<BI` kind, index of the first sample, then: kind 0 digital runs `<HI` (count, levels), kind 1 analog `<H` values interleaved in mask order, kind 2 state frames `<II` (time µs, levels) and `<H` per ADC |
| 0x03 DONE | `<BII` status (0 complete, 1 overflow, 2 stopped), samples, edges lost (state mode) |
| 0x04 WATCHDOG | – (the outputs were released) |

The board starts with every pin an input. When outputs are driven and no frame came for 1 s it
releases them and sends WATCHDOG. While a buffer capture runs on an AVR board, the GPIO commands
answer busy.

Further rules of the firmware:

* Capture channels are numbered without gaps over the free digital inputs (Uno: D2 is channel 0);
  the `channel` field of a `PIN` line says which. On the Mega 2560 the table holds whole ports
  (D22–D29, D50–D53 with D10–D13, D42–D49, A8–A15), so the levels fit 32 bits.
* `samples` 0 in stream or state mode runs until `CAPTURE_ABORT`. Pre-trigger samples and analog
  channels in a buffer capture answer error 5; a buffer capture waits at most 2 s after its trigger
  (error 2 beyond), more samples than the buffer holds answer error 7. DONE status 1 in a buffer
  capture means a sample was taken late.
* Triggers: rising or falling – any pin of the trigger mask changes so; pattern – the pins of the
  mask have the trigger levels.
* Analog values of streams, state frames and the monitor are the latest conversion of each input
  (converted in turn by the main loop).
* `GEN_LOAD` and `ARB_LOAD` refuse an offset beyond what is loaded (error 2); `GEN_STOP` also stops
  the arbitrary waveform; `GEN_START` flag 1 (wait for a trigger) answers error 5 (no trigger input
  is defined yet). `SQUARE` on a PWM pin without a timer of its own gives PWM at 50 %.

## Bridge (Rigol DHO900, protocol 1)

The Android app `firmware/bridge-android/` runs on the oscilloscope. It listens on TCP port 5560
for SCPI lines (ended by `\n`) and passes every command it does not know to the instrument's own
SCPI server (127.0.0.1:5555), answers included. Every two seconds it sends a UDP beacon to port
5561: `OPENSCILAB_BRIDGE <version> <tcp port> <model> <serial>`.

| Command | Answer |
| --- | --- |
| `:BRIDge:VERSion?` | `OPENSCILAB_BRIDGE,<version>,1` (the last number is this protocol) |
| `:BRIDge:BENCH? <bytes>` | `<bytes per second>` – how fast the app reads waveform data from the instrument |
| `:BRIDge:SNAPshot <channels>` | `<id>` once the channels (`D0-D15`, `CH1`…`CH4`, comma separated) of the stopped acquisition are read into the cache |
| `:BRIDge:CACHe:LIST?` | `<id>,<unix time>,<points>;…` (newest first, empty without any) |
| `:BRIDge:META? <id>` | `key=value;…`: `points`, `rate` (Sa/s), `trigger` (sample of the trigger), `channels` (as in SNAPshot), per analog channel `CH1.yinc`, `CH1.yorig`, `CH1.yref` (volts = (raw − yorig − yref) · yinc) |
| `:BRIDge:OVERview? <id>,<channel>,<level>` | IEEE block `#<n><length>`: for buckets of 2^level samples, analog: `<BB` min, max raw; digital channel `D`: `<HH` AND and OR of the 16 bits |
| `:BRIDge:TILE? <id>,<channel>,<start>,<count>` | IEEE block: the samples `start`…`start + count`, compressed: digital `D`: runs of varint count and `<H` levels; analog: the first raw sample, then zigzag varint differences |
| `:BRIDge:CACHe:DELete <id>\|ALL` | `OK` |
| `:BRIDge:CACHe:LIMit <bytes>` | `OK` (the oldest snapshots go when the cache grows beyond it) |

A command the app cannot carry out is answered with one line `ERR <reason>` (also when the
instrument's server cannot be reached for a query). The cache keeps the newest snapshot even when
it alone is beyond the limit.

Varints are LEB128 (7 bits per byte, low bits first); zigzag maps 0, −1, 1, −2 … to 0, 1, 2, 3 ….
`tests/fixtures/bridge_codec.json` has test vectors for the app and `openscilab/driver/rigoldho/codec.py`.
Without the app the driver talks to port 5555 directly and reads `:WAVeform:DATA?` block by block.
