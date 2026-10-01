# Command line and Python API

PiPiLogicAnalyzer can capture, convert and decode without its window: with the command
`pipilogicanalyzer-cli` (installed with the package) or from Python scripts with
`pipilogicanalyzer.api`. Neither loads the Qt user interface, so both work over SSH and in CI.

## Devices

Every device has an identifier:

| Identifier | Device |
| --- | --- |
| `pico:/dev/cu.usbmodem1`, `pico:COM5` | Pico board with the PiPiLogicAnalyzer firmware over USB |
| `pico-net:192.168.1.5:4045` | Pico W board over WiFi |
| `pico-multi:/dev/ttyACM0,/dev/ttyACM1` | Multi device set (2 to 5 boards, the first one triggers) |
| `dslogic:1:4`, `dslogic` | DSLogic at USB bus 1, address 4 / the first DSLogic |
| `emulated` | No hardware: the test signals of the simulation |

A plain serial port, `address:port` and a comma separated list of ports work as well. Without a
device the first one found is used.

```sh
pipilogicanalyzer-cli devices
pipilogicanalyzer-cli info pico:/dev/cu.usbmodem1
```

A DSLogic needs the FPGA bitstream of DSView. If it is missing, install DSView or add
`--download-bitstream` to fetch it once from the DSView repository.

## Capturing

```sh
# 8 channels at 10 MHz for 5 ms, 1000 samples before a rising edge on channel 0
pipilogicanalyzer-cli capture --channels 0-7 --rate 10M --duration 5ms --pre 1000 \
    --trigger edge:0:rising -o capture.sr

# Start at once, stream 2 seconds over USB
pipilogicanalyzer-cli capture -d dslogic -c 0-3 -r 20M -t 2s --mode stream --trigger immediate -o long.lac.gz

# Stream a Pico board and keep 2000 samples around a falling edge on channel 3, found in the stream
pipilogicanalyzer-cli capture -c 0-7 -r 400k -n 4000 --pre 2000 --mode stream --software-trigger \
    --trigger edge:3:falling -o edge.sr
```

| Option | Meaning |
| --- | --- |
| `--device`, `-d` | device identifier |
| `--channels`, `-c` | channels counted from 0: `0-7,9` (default `0-7`) |
| `--names` | channel names, comma separated |
| `--rate`, `-r` | `10M`, `100k`, `1e6`, `24MHz` (default: the highest) |
| `--samples`, `-n` / `--duration`, `-t` | total samples (including `--pre`) / `5ms`, `250us`, `1s` |
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

`convert` reads `.lac`, `.lac.gz` (PiPiLogicAnalyzer and gusmanb's LogicAnalyzer) and `.sr`
(sigrok-cli and PulseView) and writes those plus `.csv` and `.vcd`:

```sh
pipilogicanalyzer-cli convert capture.lac capture.sr      # open it in PulseView
pipilogicanalyzer-cli convert pulseview.sr capture.lac.gz
pipilogicanalyzer-cli convert --time capture.sr capture.csv
```

## Decoding

The sigrok protocol decoders run without PulseView or libsigrokdecode. `<decoder>:` is followed by
the decoder's channels (capture channel number or name) and options; a decoder that works on the
output of another one (`eeprom24xx` on `i2c`) is stacked on the `--decode`/`--decoder` before it.

```sh
pipilogicanalyzer-cli decoders                          # id, name and channels of each decoder
pipilogicanalyzer-cli decode capture.sr --decoder uart:rx=0,baudrate=115200 -o uart.csv
pipilogicanalyzer-cli decode capture.lac -D i2c:scl=SCL,sda=SDA -D eeprom24xx -o eeprom.json
pipilogicanalyzer-cli capture -c 0,1 -r 1M -n 200000 --trigger immediate -o i2c.sr \
    --decode i2c:scl=0,sda=1 --annotations i2c.csv
```

The CSV has the columns *Start time (s)*, *End time (s)*, *Start sample*, *End sample*, *Decoder*,
*Row* and *Value*; times are relative to the trigger. The JSON file holds the same records (plus
every text of an annotation) and the errors of the decoders. Without an output file the
annotations are printed. `--decoders-dir DIR` (before the command) adds a folder of decoders.

## Python API

```python
from pipilogicanalyzer import api

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
from pipilogicanalyzer.driver.models import ConditionKind, EdgeKind, TriggerCondition, TriggerStage

glitch = api.Sequence([
    TriggerStage(TriggerCondition(ConditionKind.PULSE, channel=0, edge=EdgeKind.RISING, max_ns=10_000)),
])
capture = device.capture(channels=[0, 1], rate="400k", samples=20_000, pre_trigger=10_000,
                         trigger=glitch, mode="stream", software_trigger=True, timeout=60)
```
