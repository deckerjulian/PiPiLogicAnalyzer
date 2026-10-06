# Command line and Python API

openSciLab can capture, convert, decode and run flows without its window: with the command
`openscilab-cli` (installed with the package; `openscilab <command>` does the same) or from Python
scripts with `openscilab.api` and `openscilab.lab`. Neither loads the Qt user interface, so both work over SSH and in CI.

## Devices

Every device has an identifier:

| Identifier | Device |
| --- | --- |
| `pico:/dev/cu.usbmodem1`, `pico:COM5` | Pico board with the openSciLab Pico firmware over USB |
| `pico-net:192.168.1.5:24000` | Pico W board over WiFi |
| `pico-multi:/dev/ttyACM0,/dev/ttyACM1` | Multi device set (2 to 5 boards, the first one triggers) |
| `dslogic:1:4`, `dslogic` | DSLogic at USB bus 1, address 4 / the first DSLogic |
| `emulated` | No hardware: the test signals of the simulation |
| `sim:free`, `sim:<profile>` | A simulated instrument (`openscilab-cli sim` lists the profiles) |
| `arduino:/dev/cu.usbserial-1410` | Arduino board with the openSciLab Arduino firmware |
| `arduino-sim:uno`, `arduino-sim:uno_r4` | A simulated Arduino that speaks the firmware protocol |
| `rigol:192.168.1.20`, `rigol:192.168.1.20:5555` | Rigol DHO900 oscilloscope (through its bridge app when it runs) |
| `rigol-sim:bridge`, `rigol-sim:direct` | A simulated DHO924S with or without the bridge app |
| `<kind>:...`, e.g. `counter:8` | A device of a plugin ([drivers.md](drivers.md#plugins); `openscilab-cli plugins` lists them) |

A plain serial port, `address:port` and a comma separated list of ports work as well. Without a
device the first one found is used.

```sh
openscilab-cli devices
openscilab-cli info pico:/dev/cu.usbmodem1
```

A DSLogic needs the FPGA bitstream of DSView. If it is missing, install DSView or add
`--download-bitstream` to fetch it once from the DSView repository.

## Capturing

```sh
# 8 channels at 10 MHz for 5 ms, 1000 samples before a rising edge on channel 0
openscilab-cli capture --channels 0-7 --rate 10M --duration 5ms --pre 1000 \
    --trigger edge:0:rising -o capture.sr

# Start at once, stream 2 seconds over USB
openscilab-cli capture -d dslogic -c 0-3 -r 20M -t 2s --mode stream --trigger immediate -o long.lac.gz

# Stream a Pico board and keep 2000 samples around a falling edge on channel 3, found in the stream
openscilab-cli capture -c 0-7 -r 400k -n 4000 --pre 2000 --mode stream --software-trigger \
    --trigger edge:3:falling -o edge.sr
```

| Option | Meaning |
| --- | --- |
| `--device`, `-d` | device identifier |
| `--channels`, `-c` | channels counted from 0: `0-7,9` (default `0-7`) |
| `--names` | channel names, comma separated |
| `--rate`, `-r` | `10M`, `100k`, `1e6`, `24MHz` (default: the highest) |
| `--samples`, `-n` / `--duration`, `-t` | total samples (including `--pre`) / `5ms`, `250us`, `1s`, `2 min` (a quantity as everywhere: `5m` is 5 milliseconds) |
| `--pre` | samples before the trigger |
| `--trigger` | `edge:<ch>:rising\|falling`, `pattern:0=1,1=0`, `fast:0=1,1=0`, `immediate`, `simulation[:<n>]` |
| `--mode` | `buffer` or `stream` |
| `--software-trigger` | look for the trigger in the stream (with `--mode stream`): any channel, also on a Pico board, whose streams have no trigger of their own |
| `--threshold` | input threshold in volts (DSLogic) |
| `--timeout` | seconds to wait for the trigger, then the capture is stopped |
| `--output`, `-o` | `.lac`, `.lac.gz`, `.sr`, `.csv` (add `--time` for a time column) or `.vcd` |
| `--decode`, `--annotations` | decode right after the capture (see below) |

Settings the device cannot capture (rate, length, trigger channels) are rejected before the
capture starts. Every error ends the program with exit code 1 and one line on stderr.

## File formats

`convert` reads `.lac`, `.lac.gz` (openSciLab, PiPiLogicAnalyzer and gusmanb's LogicAnalyzer) and `.sr`
(sigrok-cli and PulseView) and writes those plus `.csv` and `.vcd`. The `.lac` files it writes
hold the samples packed (small and quick); a file for gusmanb's LogicAnalyzer is written by the
application (*Save as → Captures for the original LogicAnalyzer*) or in a script with
`Capture.save(path, compatible=True)`. A message without details ends with the line of the error;
`OPENSCILAB_TRACEBACK=1` shows where it came from.

```sh
openscilab-cli convert capture.lac capture.sr      # open it in PulseView
openscilab-cli convert pulseview.sr capture.lac.gz
openscilab-cli convert --time capture.sr capture.csv
```

## Decoding

The sigrok protocol decoders run without PulseView or libsigrokdecode. `<decoder>:` is followed by
the decoder's channels (capture channel number or name) and options; a decoder that works on the
output of another one (`eeprom24xx` on `i2c`) is stacked on the `--decode`/`--decoder` before it.

```sh
openscilab-cli decoders                          # id, name and channels of each decoder
openscilab-cli decode capture.sr --decoder uart:rx=0,baudrate=115200 -o uart.csv
openscilab-cli decode capture.lac -D i2c:scl=SCL,sda=SDA -D eeprom24xx -o eeprom.json
openscilab-cli capture -c 0,1 -r 1M -n 200000 --trigger immediate -o i2c.sr \
    --decode i2c:scl=0,sda=1 --annotations i2c.csv
```

The CSV has the columns *Start time (s)*, *End time (s)*, *Start sample*, *End sample*, *Decoder*,
*Row* and *Value*; times are relative to the trigger. The JSON file holds the same records (plus
every text of an annotation) and the errors of the decoders. Without an output file the
annotations are printed. `--decoders-dir DIR` (before the command) adds a folder of decoders.

## Python API

```python
from openscilab import api

print(api.devices())

with api.open() as device:                    # or api.open("pico:/dev/ttyACM0")
    capture = device.capture(
        channels=[0, 1, 2, 3],
        rate="10M",                           # or 10_000_000
        duration="5ms",                       # or samples=50_000
        pre_trigger=1000,
        trigger=api.Edge(0, rising=True),     # Pattern({0: 1, 1: 0}), Immediate(), Sequence([...])
        mode="buffer",
        timeout=10,
    )

capture.save("capture.sr")                    # .lac, .lac.gz, .sr, .csv, .vcd
clock = capture.samples(0)                    # numpy uint8 array of 0/1, or samples("CLK")
print(capture.frequency, capture.sample_count, capture.trigger_sample)

capture = api.load("capture.lac")
for item in capture.decode("i2c", channels={"scl": 0, "sda": 1}):
    print(f"{item.start_time:.6f} {item.row}: {item.value}")

groups = capture.decode_groups("uart", channels={"rx": 2}, options={"baudrate": 9600})
capture.export_annotations("uart.json", groups)
```

`capture()` blocks until the samples arrived; it raises `ValueError` for settings the device
cannot capture, `api.CaptureFailed` when the device reports an error and `TimeoutError` after
`timeout` seconds without a trigger. `device.build_session(...)` returns the validated
`CaptureSession` without starting it, `device.driver` is the driver itself.

With `mode="stream", software_trigger=True` the trigger is looked for in the stream instead of in
the device: any trigger on any captured channel, also a `Sequence` of patterns, edges, pulse
widths and gaps on devices that have none:

```python
from openscilab.driver.models import ConditionKind, EdgeKind, TriggerCondition, TriggerStage

glitch = api.Sequence([
    TriggerStage(TriggerCondition(ConditionKind.PULSE, channel=0, edge=EdgeKind.RISING, max_ns=10_000)),
])
capture = device.capture(channels=[0, 1], rate="400k", samples=20_000, pre_trigger=10_000,
                         trigger=glitch, mode="stream", software_trigger=True, timeout=60)
```

## Flows

A flow (`*.flow.yaml`, or a Python script with the DSL of `openscilab.lab`) runs without the
window as well:

```sh
openscilab run examples/flows/counter.flow.yaml --sim --fast     # simulators, virtual time
openscilab run flows/kennlinie.flow.yaml --device dho=sim:dho924s --report out.html
openscilab run examples/flows/counter.py --sim --fast            # the same flow as a script
openscilab sim                                                    # simulator profiles
openscilab sim free                                               # one profile: channels, signals
```

| Option | Meaning |
| --- | --- |
| `--sim` | every device node of the flow is a simulator (`sim:<profile>` devices are anyway) |
| `--fast` | virtual time: the flow runs as fast as possible and gives the same result every time |
| `--device NAME=ADDRESS` | another address for the device node `NAME` (repeatable) |
| `--duration 10s` | end the flow after this (virtual or real) time |
| `--seed N` | seed of the random numbers (noise of simulators, random nodes) |
| `--data-dir DIR` | where files are written (default: `data/` of the project, or the flow's folder) |
| `--report FILE.html` | a report of the run: the sections, tables, diagrams and checks of the `report.*` nodes, then the run itself |
| `-v`, `-q` | the log of the nodes while running / no summary |

The exit code is 0 when the flow finished and every check passed (`report.check`,
`control.compare`), 1 otherwise – a flow is a test on a test bench.

Inside a project folder (with `project.yaml`) a flow can be named without its path
(`openscilab run kennlinie`), the project's devices and own nodes (`nodes/*.py`) are used.

```python
from openscilab.lab import flow, nodes as n

with flow("Counter") as f:
    sim = f.device("sim", "sim:free")
    cap = n.device.capture(sim, channels=["D0", "D8"], rate="4 MHz", samples=20000, trigger=n.edge("D8"))
    cap.capture >> n.data.file(path="counter.lac")

result = f.run(fast=True)
print(result.state, result.time)
print(f.to_yaml())        # the same flow as YAML
```
