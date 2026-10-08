# openSciLab Sim: simulated instruments

Every function of openSciLab works without hardware: a simulated instrument behaves like the real
one, including its limits (rates, memory depth, stream throughput, latency, reserved pins). Open
one from the device list of the sidebar (*Simulation: Arduino Uno*, ...), in a flow as a device
`sim:<profile>`, on the command line (`openscilab capture --device sim:free ...`) or in scripts:

```python
from openscilab import api

uno = api.instrument("sim:uno")          # all facets: GPIO, monitor, analog inputs, ...
uno.gpio.write("D7", 1)                  # D7 is wired to D3
print(uno.monitor.sample(["D3"]).digital)

with api.open("sim:free") as device:     # the capture driver, as for a real device
    capture = device.capture(channels=[0, 8], rate="4 MHz", samples=20_000)
```

`openscilab sim` lists the profiles, `openscilab sim <profile>` shows one.

## Several simulators, multi device sets

A simulator has an address: `sim:<profile>[*<boards>][#<number>]`.

* `sim:uno`, `sim:uno#2`, `sim:uno#3`: every simulator can be connected again and again; each is
  an instrument of its own (its own pins, signals and state). Choosing a simulator in the device
  list that is already connected connects the next one.
* `sim:pico*2`: a **multi device set** of two (up to five) boards captured together as one
  device, as *Multiple devices…* does for real boards: their channels one board after the other
  (channels 1–24 are board 1, 25–48 board 2; the nets are `B1.GP2`, `B2.GP2`, …). A set only
  captures: it has no GPIO, analog inputs or generator. In the device list: *Simulated multi
  device…*.

## What a simulator simulates

By default the channels carry the test signals of the profile. The *Signals* tab of the device
card puts something else on them, at once and also while a capture runs:

| Scenario | Signals |
| --- | --- |
| *Test signals of the device* (`default`) | what the profile connects |
| *UART* (`uart`) | 8N1 frames of `text` at `baud` on `channel`, repeated |
| *SPI* (`spi`) | CLK, MOSI and CS on three channels from `channel`, `frequency` steps a second |
| *I²C* (`i2c`) | SCL and SDA on two channels from `channel` |
| *UART, SPI and I²C* (`protocols`) | UART on channel 1, SPI on 2–4, I²C on 5–6 |
| *Counter* (`counter`) | an 8 bit counter on the first channels, its clock on the next |
| *C64 bus* (`c64`) | a Commodore 64 at its expansion port running a small program (reset, a loop, two interrupts) on the channels of the profile *C64 expansion port*; needs 48 channels, i.e. `sim:pico*2` |
| *Capture file* (`file`) | the channels of a capture (`path`), played at its rate and repeated |
| *Nothing connected* (`idle`) | every channel low |

A double-click on a channel gives that channel a signal of its own (any source of the table
below, e.g. `square` with `frequency: 2 kHz`); *Name the channels after their signals* names them
for the next capture (TX, SCL, A0, …). What the device drives itself – its outputs, a running
generator, the wiring of its profile – stays; a pin shows its signal again once it is an input
again. The signals are kept per device address and come back with the next connection.

In a flow the device node says it, in scripts `open_simulated`:

```yaml
nodes:
  uart: {type: device.instrument, address: "sim:free",
         signals: {scenario: uart, text: "Hello\n", baud: 9600, channel: 1}}
  c64:  {type: device.instrument, address: "sim:pico*2", signals: {scenario: c64}}
  own:  {type: device.instrument, address: "sim:uno#2",
         signals: {channels: {D5: {type: square, frequency: 1 kHz}}}}
```

```python
from openscilab.driver.simulated import open_simulated, scenarios

c64 = open_simulated("pico*2", signals={"scenario": "c64"})
scenarios.apply(c64.simulated_driver, {"scenario": "idle"})      # change it later
```

Without `signals`, a flow that opens the simulator itself (virtual time) uses what the simulator
of the same address in the device list was told.

To see the C64 at work: connect *Simulated multi device…* (Pico, 2 boards), choose *C64 bus* on
its *Signals* tab, load the profile *C64 expansion port* (*Profiles* in the header of the device card) and
capture – the result is the capture `examples/c64-demo.lac`, decoded by the C64 bus decoder.

## Profiles

| Profile | Instrument |
| --- | --- |
| `free` | 16 logic channels with test signals (counter with clock, UART, SPI, I²C, 1 kHz); pattern generator on D0-D7 (looped back); trigger input `TRIG IN` on D15. Built in. |
| `uno` | Arduino Uno with the openSciLab firmware: GPIO with latency and watchdog, PWM, A0-A5 (10 bit, 5 V). The limits of its firmware: captures of 448 samples (8 channels) up to 100 kHz, pins only, at most 2 s after the trigger; streams of 11.5 kB/s (115200 baud) up to 20 kHz. Wired: D7 → D3, PWM of D9 → RC low pass (10 kΩ, 10 µF) → A0, a bouncing push button on D2. |
| `uno_r4` | Arduino Uno R4: like the Uno, D0/D1 free (native USB), 14 bit ADC, a 12 bit DAC on A0 (also a generator output). Captures of 12288 samples up to 1 MHz, streams of 500 kB/s up to 100 kHz. Wired: DAC (A0) → A1, D7 → D3, PWM of D9 → RC → A2. |
| `pico` | Raspberry Pi Pico with the firmware 8: 24 channels (GP2-GP22, GP26-GP28), 100 MHz, depth by the number of channels, GPIO/PWM on every pin, ADC on GP26-GP28, pattern generator on consecutive pins from GP2 (up to GP22), stream 800 kB/s as the firmware reports. |
| `daq` | A USB DAQ in the class of an NI USB-6001: AI0-AI7 (±10 V, 14 bit, one multiplexed ADC: 20 kS/s for all inputs together), AO0/AO1 (±10 V, 12 bit, AO0 also a generator output), P0.0-P0.7 and P1.0-P1.3; blocks arrive as over USB, its sample clock runs 30 ppm fast. Wired: P0.0 → P0.1 (loopback for the latency), AO0 → AI2; P0.7 free for a sync signal. |
| `dho924s` | Rigol DHO924S: 4 analog channels (12 bit) and 16 logic channels, large captures arrive progressively over a simulated network; generator output GI (25 MHz) on CH1 with its sync on D15. |

The values of `uno`, `uno_r4`, `pico` and `dho924s` are provisional (`"provisional"` in the file)
until they are measured on the real devices.

Profiles are JSON files in `examples/sim/`. Your own go into the folder `sim` of the settings
directory (`OPENSCILAB_SETTINGS_DIR`); a file named like a profile of `examples/sim/` is not used,
give it a name of its own.

| Key | Meaning |
| --- | --- |
| `name`, `title`, `description` | profile name (`sim:<name>`), name in the device list, text |
| `firmware` | `arduino`: the board runs the openSciLab Arduino firmware, so it is also offered as `arduino-sim:<name>` (the simulated board behind its real protocol) |
| `digital` | names of the logic channels, in channel order |
| `analog` | names of the analog inputs |
| `pins` | `{name, caps, reserved?, analog_channel?, level?}`: pin capabilities `DIN DOUT PULLUP PULLDOWN PWM ADC DAC CLOCK CLOCK_SW`; a pin with `reserved` cannot be used and shows why |
| `max_rate` | highest sample rate (Hz) |
| `memory_depth` | samples per channel of a buffer capture; `memory_depth_digital` / `memory_depth_analog`: depth by the number of channels in use (`{"8": 131072, "16": 65536}`) |
| `stream_bandwidth` | bytes per second of a stream (a digital sample: 1 byte per 8 channels, an analog one 2 bytes); more is refused, a host that reads too late overflows. 0: the device does not stream |
| `stream_max_rate` | highest rate of a stream, whatever the bandwidth (the firmware's) |
| `buffer_analog` | `false`: buffer captures read the pins only (analog inputs only in streams) |
| `max_post_seconds` | buffer captures record at most this long after the trigger |
| `analog_aggregate_rate` | samples per second of all analog inputs together (one multiplexed ADC) |
| `clock_hz` | the clock the board reports |
| `latency`, `gpio_latency` | seconds until a capture starts / a pin changes |
| `usb` | `{frame, latency, jitter, ring}`: blocks arrive as over USB (seconds; `ring`: bytes of the device's buffer, a host that reads later overflows it) |
| `clock_drift` | ppm the sample clock of the device runs fast (its blocks arrive by it too); *Signals → USB link and clock* changes it |
| `adc_bits`, `analog_range`, `adc_noise` | ADC resolution, volts of the lowest and highest code, noise (V rms) |
| `logic_level` | volts of a logic 1: the sources of the circuit swing to it, and an analog net reads 1 above half of it |
| `watchdog` | seconds without contact until the outputs are released (0: none) |
| `monitor_rate` | highest rate of the monitor |
| `dac_range`, `dac_bits` | the DAC of `AnalogOut` |
| `capabilities` | the capabilities the device reports (`GPIO`, `PWM`, `MONITOR`, `DAC`, `AFG`, `PATTERN_GEN=<rate>,<pins>`, `STATE_MODE`, ...) |
| `clock_pins` | pins that can clock the state mode |
| `outputs` | generator outputs: `{name, kind (analog/pattern/square), net or pins, voltage_range, resolution, max_rate, max_points, max_frequency, capabilities (SWEEP BURST ARB SYNC_OUT), sync}` |
| `trigger_inputs` | `{"TRIG IN": "D15"}`: inputs a trigger route of the hub can drive |
| `progressive` | `{lan_rate, tile}`: large captures arrive as overview and tiles |
| `circuit` | `sources` per net and `wiring` (below) |

## The circuit

Each net (a pin, a channel) is driven by a source; the device model samples them. Sources are
functions of time, so the same interval always gives the same samples.

| `type` | Parameters |
| --- | --- |
| `constant` | `level` |
| `square`, `clock` | `frequency`, `duty`, `phase` |
| `pulse` | `period`, `width` (a pulse train) |
| `counter` | `frequency`, `bit` (one bit of a counter clocked at `frequency`) |
| `uart` | `text`, `baud`, `gap` |
| `spi`, `i2c` | `line` (`clk`/`mosi`/`cs`, `scl`/`sda`), `frequency` |
| `noise` | `frequency`, `seed` (random levels) |
| `sine`, `triangle` | `frequency`, `amplitude`, `offset`, `phase` |
| `ramp` | `frequency`, `low`, `high_level` |
| `analog_noise` | `rms`, `offset`, `bandwidth`, `seed` |
| `sum` | `parts`: sources added (a sine with noise) |
| `button` | `presses` (`[[down, up], ...]` seconds), `bounce`, `bounces`, `active_low` |
| `c64` | `line`: a line of the C64 expansion port (`A0`…`A15`, `D0`…`D7`, `Φ2`, `R/W`, `/RESET`, `/IRQ`, …) |
| `file` | `path`, `channel`: a channel of a capture file, played at its rate and repeated |

Wires connect nets: `{"from": "D7", "to": "D3"}`, through an RC low pass
`{"from": "D9", "to": "A0", "rc": {"r": "10k", "c": "10 uF"}}`, or an analog net read as logic
`{"from": "A0", "to": "D2", "threshold": "2.5 V"}`. Outputs of the device (GPIO, DAC, generator)
drive their nets while they are on, so whatever is wired to them follows. Wires that lead back
to themselves (D0 → D1 → D0) are refused.

A project adds wires per device node (by its name) without touching the profile, also from
another simulated device of the flow (`{from: "uno:A0", to: CH1}`):

```yaml
# project.yaml
devices: {sim: "sim:free"}
simulation:
  wiring:
    sim: [{from: D15, to: D3}]
```

On the device card, *Signals → Wires* adds and removes wires of a connected simulator (*Add
wire…*: from a pin to a channel), e.g. an output to a channel to measure the latency on the
*Timing* tab (whose *Wire them* adds that wire at once). The wires are kept per device address and
come back with the next connection, as the signals do.

Trigger routes of the hub between simulated instruments are wired as well: the sync output of
one instrument triggers a capture of another (`hub.add_route("scope", "SYNC", "logic", "TRIG IN")`).

Wires between simulators work wherever they run - in the application, or each in a device process
of its own (simulators of USB devices of the device list, see [timing.md](timing.md), *Staying
responsive*): the input reads the other net through the net server of its process. A flow that uses
simulators of the device list gives them their wires for the run - those between devices and those
within one device - and takes them away afterwards.

## Faults

*Devices → Simulate faults* injects faults into an open simulated instrument; scripts call
`device.inject(...)` (`api.open`) or `instrument.simulated_driver.inject(...)`:

| Fault | Effect |
| --- | --- |
| `disconnect` | the device does not answer (captures and GPIO fail, the status is *disconnected*) |
| `reconnect` | it answers again |
| `delay` | answers come later by the given seconds |
| `overflow` | the next stream overflows and ends early |
| `restart` | the device restarted: outputs released (pins inputs again, generators stopped, analog outputs at 0 V), no delay, no fault |

## Time

In a flow in virtual time (`--fast`) the simulators run on the clock of the engine, and with the
same `seed` (flow setting `seed`, `--seed`) a run gives the same samples every time. A capture lets
the flow's time run on while it waits for its trigger and records, and takes its samples when that
time has passed: a stimulus the flow starts after `armed` - also after a wait - is in it. A stream
sends each block when the flow's time reached its end, so what the flow does meanwhile is in it and
a `stop` ends it there. Outputs keep their changes with their times (a generator that stopped shows
until its stop), so a capture across a change sees both sides. In real time the instruments opened
in the window share the clock of the hub.

A simulator whose profile has `usb` (the Pico's: frames of 1 ms, 1.2 ms latency, 0.4 ms jitter, a
128 KiB ring) delivers its blocks as over USB - late, in frames, uneven - and does not tell the
flows when it took its samples: they find it as with the real device (start and arrival, a
measured latency, a sync signal). *Signals → USB link* switches that on or off for any simulator
and sets latency, jitter and frame (kept per device address). In real time a capture records what
the signals and outputs do until it ends, as the device does - a loopback measured in *capture*
mode sees the switches that happen while it runs.
