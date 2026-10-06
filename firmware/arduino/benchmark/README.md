# openSciLab Arduino benchmark (step 2a)

A small PlatformIO project that measures what a board can do, so the provisional rates of the
firmware (`src/arch/*/board_*.cpp`, `INFO`) and of the simulator profiles (`examples/sim/uno*.json`)
can be replaced by measured ones. It is not part of the firmware build or CI.

## Run

```bash
pio run -d firmware/arduino/benchmark -e uno -t upload          # or nano, megaatmega2560, uno_r4_minima, esp32dev, esp32-s3
python firmware/arduino/benchmark/read_benchmark.py /dev/ttyACM0 115200   # 2000000 for R4 and ESP32
```

For the interrupt test, wire `PIN_ISR_OUT` to `PIN_ISR_IN` (AVR and R4: D3 to D2; ESP32/S3:
GPIO4 to GPIO5). Without the wire `pin_isr.counted` is 0. Nothing else may be connected.

## What to record

Run three times per board and note the median (the profiles `examples/sim/uno*.json` and the
board tables of the firmware take these numbers):

| Key | Meaning | Feeds |
| --- | --- | --- |
| `port_read_hz` | port register reads per second | `buffer_max_rate` (leave a margin: the loop also stores and waits) |
| `port_write_hz` | port register writes per second | `generator_max_rate` together with `timer_isr_hz` |
| `digital_read_hz` | `digitalRead()` per second | bit-banged TX speeds |
| `analog_read_hz` | `analogRead()` of one channel per second | analog rates of stream and monitor |
| `timer_isr_hz` | highest periodic interrupt rate that leaves the loop half its time | `stream_max_rate`, `generator_max_rate` |
| `pin_isr.counted` | of 20000 fast edges, how many the pin interrupt saw | `STATE_MAX_CLOCK` (with a signal generator: raise the rate until edges are lost) |
| `host_bytes_s` | bytes per second the host received | `stream_bytes` (CAPS `STREAM=`) |

Also note the board revision, the USB chip (CH340, 16U2, native) and the host OS: serial
throughput depends on them.
