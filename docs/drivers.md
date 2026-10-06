# Device drivers

The application works with any logic analyzer that has a driver. It never asks which kind of
device is connected: a driver describes what its device can do, and the main window and the
dialogs show what that allows. Every kind of device comes from a **plugin** ([Plugins](#plugins)):
a **driver** (a subclass of `AnalyzerDriverBase`, `driver/base.py`, free of Qt) and one line that
registers its **kind** of address (`mydevice:...`); the device list, flows, the command line, the
Python API and device processes open it by that address. The devices of openSciLab come the same
way, from the plugins it brings along. A device is added in one of two ways:

* **As a plugin of your own**: a Python file or package outside openSciLab. Nothing of openSciLab
  changes. `examples/plugins/counter_device.py` is a complete example.
* **Built in**, in the project:
  1. a **driver** in `openscilab/driver/<device>/`;
  2. a **device backend** in `openscilab/ui/devices/<device>.py`, which lists the connected
     devices in the device list and opens the one the user picks;
  3. a **plugin** `openscilab/plugins/<device>.py` that registers its kinds of address and, in
     `setup_ui()`, its backend; its name goes into `BUILT_IN` of `openscilab/plugins/__init__.py`
     (which also gives the order of the device list).

```
openscilab/
├── driver/
│   ├── base.py        AnalyzerDriverBase, capture limits, capabilities, events
│   ├── models.py      CaptureSession, AnalyzerChannel, TriggerType
│   ├── emulated.py    stands in for a device while captures are loaded or computed
│   ├── ports.py       the serial ports of the computer (cached), Arduino detection by USB IDs
│   ├── pico/          Pico boards with the openSciLab Pico firmware (gusmanb's hardware):
│   │                  protocol, serial/network transport, detection, multi device sets,
│   │                  instrument.py: the facets of firmware 8
│   ├── arduino/       Arduino boards: COBS protocol, connection with a reader thread, driver, facets
│   ├── rigoldho/      Rigol DHO900: SCPI, driver, bridge transfer (fetcher, codec), generator, beacon
│   ├── simulated/     simulator profiles; arduino_shell.py and scpi_shell.py play the Arduino
│   │                  firmware and the DHO with the bridge app over their real protocols
│   ├── dslogic/       DreamSourceLab DSLogic over USB (libusb)
│   ├── kinds.py       the registry of the kinds of devices
│   └── discovery.py   list_devices(), open_device(address): through the registry
├── plugins/           the plugin loader; the plugins of openSciLab's own devices:
│                      pico.py, arduino.py, dslogic.py, rigol.py, simulation.py, remote.py
└── ui/devices/
    ├── __init__.py    DeviceBackend, DeviceEntry, backends(), register_backend()
    ├── pico.py        autodetect, USB boards, network device, multi device sets
    ├── arduino.py     boards by USB ID, simulated boards with the firmware protocol
    ├── rigoldho.py    oscilloscopes by beacon or address, simulated DHO924S
    └── dslogic.py     USB devices, asks for a missing FPGA bitstream
```

## Instruments, facets and the hub

In openSciLab a device is an **instrument** (`core/instrument.py`) with **facets**: a
`CaptureFacet` (captures through an `AnalyzerDriverBase`), `GpioFacet`, `MonitorFacet`,
`AnalogInFacet`, `AnalogOutFacet` and `GeneratorFacet`. The **hub** (`core/hub.py`) keeps every
open instrument, their trigger routes and the time base offsets of their streams. A backend
returns an instrument from `DeviceBackend.open_instrument()`; by default that is
`Instrument.from_driver(connect(...))`, a capture facet around the driver (devices that stream
get the software trigger wrapper). A driver with more than captures returns its further facets
from `instrument_facets()`, which `Instrument.from_driver()` adds – so the device list, flows,
scripts and the command line all get them; it also keeps the device's watchdog satisfied
(`core.instrument.KeepAlive`). The capabilities it reports the capabilities it reports (`CAPABILITY_GPIO`,
`CAPABILITY_ANALOG`, ... in `driver/base.py`, see `facets_of()`) say which facets it has.
Nothing in the application asks for the kind of device: documents, nodes and the device card
ask for facets.

The capability strings and the facets they unlock are specified in
[protocols.md](protocols.md); a new one is added there first (a test compares it with
`driver/base.py` and the firmware headers). The simulated instruments
(`driver/simulated/facets.py`, [simulator.md](simulator.md)) implement every facet and are the
reference for a new device.

### Facets

| Facet | A device implements | Used by |
| --- | --- | --- |
| `CaptureFacet` | the driver (below) | analyzer documents, `device.capture`/`device.stream` nodes |
| `GpioFacet` | `pins()`, `set_mode(pin, mode)`, `mode(pin)`, `write(pin, value)`, `write_many(values)`, `read(pin)`, `pulse(pin, width, level, count, period)`, `pwm(pin, frequency, duty)` → actual frequency, `safe_all()`; raise `InstrumentError` for reserved pins | device card, `gpio.*` nodes |
| `MonitorFacet` | `start(rate, pins, analog)`, `stop()`, `running`; reports go to `_emit(MonitorState)` | device card (monitor, *Record*), `device.monitor` |
| `AnalogInFacet` | `read(pins)` → volts | `gpio.read` (analog) |
| `AnalogOutFacet` | `set_voltage(pin, volts)`, `voltage_range(pin)` | device card, `gpio.dac` |
| `GeneratorFacet` | `outputs()` → `OutputInfo`, `start(output, waveform)`, `stop(output)`, `running(output)`, `sync_out()`; `transmits(protocol)`, `transmit(protocol, data, pins, **settings)` for UART/SPI/I²C sent by the device | waveform document, `gen.*` nodes, *Send* tab |
| `CacheFacet` | `entries()` → `CacheEntry`, `delete(id)`, `set_limit(bytes)` | *Cache* tab of the device card |

Pins are described by `PinInfo` (name, pin capabilities, capture channel, reason when
reserved, logic level, analog channel); the GPIO keeps a device alive with `heartbeat()` when it
has a watchdog. A waveform is a `core.waveform.Waveform`; check it against the output with
`core.waveform.problems()` before playing it.

## The driver

A driver has to implement:

| Member | Meaning |
| --- | --- |
| `driver_id` | short, stable name (`"mydevice"`); names the stored capture settings |
| `device_version` | name shown in the status bar and the device information |
| `max_frequency`, `channel_count`, `buffer_size` | what the capture dialog offers |
| `is_capturing` | a capture is running |
| `start_capture(session, handler)` | starts the capture in a thread of its own and returns a `CaptureError`; when it ends, call `_raise_capture_completed(CaptureCompletedArgs(...), handler)` with the samples in `session.capture_channels[i].samples` (one `uint8` array per channel) |
| `stop_capture()` | stops a running capture |

Everything else has a default for a simple device and is overridden when the device can do more:

| Member | Default | Override when |
| --- | --- | --- |
| `get_limits(channels, acquisition_mode, ...)` | from `buffer_size` | the depth depends on the channels or the mode |
| `min_frequency`, `sample_rates(...)`, `max_frequency_for(...)` | any rate up to the maximum | the device has fixed rates |
| `acquisition_modes()` | buffer only | the device can stream (`ACQUISITION_STREAM`); report progress with `_raise_capture_progress` |
| `capabilities()` | none | the device offers optional functions (`CAPABILITY_*` in `base.py`: immediate trigger, threshold, endless stream, ...) |
| `edge_trigger_channels()`, `pattern_trigger_groups()`, `has_external_trigger()` | every channel, channels 1–16, yes | the trigger inputs are limited |
| `blast_frequency` | 0: no blast mode | the device has a faster single-shot mode |
| `max_loop_count` | 254 (255 bursts) | 0 hides the burst options |
| `has_self_test`, `run_self_test()`, `self_test_description` | no self-test | the device can test itself |
| `describe()` | the identification | board, firmware and connection for the *Details* tab of the device card |
| `device_details()` | none | the firmware reports details |
| `supports_bootloader`, `enter_bootloader()` | no | the firmware can be updated from the application |
| `restart()` (capability `RESTART`) | no | the device can restart on request and answers again on the same connection (*Restart device* on its card) |
| `supports_network_config`, `send_network_config(...)`, `is_network`, `get_voltage_status()` | no | the device has WiFi |
| `boards()`, `board_count`, `channels_per_device` | the driver itself | the driver combines several devices |
| `dispose()` | clears the handlers | close the connection (call `super().dispose()`) |

`driver_type` (`AnalyzerDriverType`) only names the built-in drivers; a new driver leaves it at
`OTHER` and names itself with `driver_id`.

**In a device process** ([docs/timing.md](timing.md), *Staying responsive*). A device on USB is
read in a process of its own: its plugin registers its kind with `process=True` (it is opened there
with `discovery.open_device`; the device process loads the plugins itself). What its
driver and facets take and return travels between the processes: plain values (numbers, text,
dataclasses, numpy arrays) by value, drivers, facets and functions by reference (handlers are called
back). Allocate the samples of a stream with the allocators of `core/sample_store` - in a device
process they are shared memory, nothing is copied - and raise progress and completion with
`_raise_capture_progress`/`_raise_capture_completed` (they are stamped and handed to the driver's
notifier thread, the reading thread goes on at once).

**Time of the samples** ([docs/timing.md](timing.md)). The flows place a capture between the
command that started it and its arrival, a stream by the arrival of its blocks: report the progress
of a stream as soon as a block came (not batched), so its arrival time means something. A device
that stamps its first sample itself in a shared time scale (PTP, GPS) sets
`session.device_start_time` (seconds of the time scale), `session.device_timescale` (`"utc"` or
`"tai"`) and, when known, `session.device_time_accuracy` before it completes; the flows use it when
the computer's clock follows that time scale too.

Errors while opening a device are raised as `DeviceConnectionError` (or `OSError`/`ValueError`);
the main window shows their message.

## The device backend

```python
from openscilab.ui.devices import DeviceBackend, DeviceEntry


class MyDeviceBackend(DeviceBackend):
    id = "mydevice"

    def detected(self) -> list[DeviceEntry]:
        return [DeviceEntry(self.id, "usb", port, f"My analyzer on {port}") for port in find_ports()]

    def connect(self, entry: DeviceEntry, parent) -> MyDeviceDriver | None:
        return MyDeviceDriver(entry.value)  # may ask the user first; None cancels
```

`manual_entries()` adds entries below the detected devices that ask for the device when chosen
(like *Network device…*; entries with `simulated=True` are listed in the group *Simulators*), and `idle_notice()` reports a device that cannot be used yet while
none is connected (like a Pico board in bootloader mode).

A backend comes to the device list from its plugin, which calls
`register_backend(MyDeviceBackend())` in its `setup_ui()` ([Plugins](#plugins)); `backends()`
(`ui/devices/__init__.py`) lists them in the order the plugins load, openSciLab's own first. A kind
that finds its devices (`detect`) needs no backend at all. Devices are opened from the device list of the shell; the device card then
captures with them (`ui/devices/capture.py`), the data view shows the captures.

A driver whose firmware the application can update sets `supports_bootloader` (UF2 boards) or
`firmware_update_method` (`"avrdude"`, `"bossac"`, `"esptool"`, `"adb"`, see
`core/firmware.py`); *Devices → Install or update firmware…* and the device card offer it then
(today only UF2 boards are wired up).

## Plugins

A plugin is a `.py` file or a package folder in the `plugins` folder of the settings directory
(`~/Library/Application Support/openSciLab/plugins` on macOS, `%APPDATA%\openSciLab\plugins` on
Windows, `~/.config/openSciLab/plugins` on Linux), in a folder of the environment variable
`OPENSCILAB_PLUGINS` (handy while developing one), or an installed Python package with an entry
point in the group `openscilab.plugins`:

```toml
# pyproject.toml of the package
[project.entry-points."openscilab.plugins"]
mydevice = "openscilab_mydevice"          # a module, or "openscilab_mydevice:setup" (a function)
```

Importing the plugin registers its kinds of devices (`openscilab/driver/kinds.py`):

```python
from openscilab.driver import kinds
from openscilab.driver.base import AnalyzerDriverBase, DeviceConnectionError


class MyDeviceDriver(AnalyzerDriverBase):
    ...  # see "The driver" above


def open_mydevice(rest: str) -> MyDeviceDriver:
    """mydevice:<rest>, e.g. mydevice:/dev/ttyUSB0"""
    return MyDeviceDriver(rest)


def find_mydevices() -> list[tuple[str, str]]:
    """The connected devices: (rest of the address, label)."""
    return [(port, f"My analyzer on {port}") for port in my_ports()]


kinds.register("mydevice", open_mydevice, title="My analyzer", detect=find_mydevices,
               process=True,        # a device on USB that streams: read in a device process
               simulation="free")   # the simulator profile standing in for it (--sim)
```

| `register(...)` | Meaning |
| --- | --- |
| `kind` | the part of the address before the colon: letters, digits, `-`, `_`, `.`; not one of openSciLab's own (`pico`, `sim`, ...) |
| `open(rest)` | opens the device and returns its driver; raises `DeviceConnectionError` (or `OSError`/`ValueError`) with a message for people. Keyword options of `open_device` are passed on when it takes them (`download_bitstream` of the DSLogic) |
| `detect()` | the connected devices for the device list and `openscilab-cli devices`; leave it out for devices that cannot be found |
| `process` | read the device in a device process of its own (USB, serial ports) |
| `simulation` | the simulator profile of flows run with *Simulate* or `--sim`; `None` when no simulator can stand in |
| `instrument(rest)` | instead of or besides `open`: opens the device as an instrument with all its facets (remote devices, simulators); it gets what a flow passes on and it takes: `clock`, `fast`, `seed`, `wiring`, `signals` (`lab/engine/devices.py`) |
| `simulator` | the device is a simulator itself: a simulated flow opens it as it is |

That is all: the device list shows what `detect()` finds and opens it by its address, a flow uses
`{type: device.instrument, address: "mydevice:/dev/ttyUSB0"}`, the command line
`openscilab-cli capture -d mydevice:/dev/ttyUSB0 ...` and scripts `api.open("mydevice:...")`. Facets
beyond captures come from the driver's `instrument_facets()` (see above).

A plugin that needs user interface of its own - a backend whose `manual_entries()` ask for an
address, or dialogs - registers it in a function `setup_ui()`, which only the application calls
(the command line and device processes never import Qt for a plugin). A backend with the `id` of
the kind replaces the generic one:

```python
def setup_ui():
    from openscilab.ui.devices import register_backend

    register_backend(MyDeviceBackend())  # see "The device backend"
```

The plugins of openSciLab (`openscilab/plugins/`) load first, when a kind of device is looked up;
the others once, when the application starts, or when an address of a kind not known yet is opened
(command line, scripts, device processes). An address without a kind (`COM5`, `/dev/ttyACM0`,
`192.168.1.5:4045`, a comma separated list) is one of Pico boards. A plugin that fails does not stop the others:
the application says so in its status bar, *Help → Plugins…* shows the error with its traceback,
`openscilab-cli plugins` lists every plugin (openSciLab's own as *built in*) and kind (and the
commands warn). An address whose kind
nobody registered fails with the list of known kinds. A plugin runs with the rights of openSciLab:
only install plugins you trust.

In the packaged applications a plugin can import what openSciLab brings (numpy, pyserial, libusb
through pyusb, ...); a plugin that needs other Python packages wants openSciLab installed with
`pip` next to them.

## Tests

`tests/test_device_backends.py` connects a minimal driver through a backend and opens the
dialogs with it; a new driver should at least pass the same steps. `tests/test_plugins.py` loads
the example plugin and opens its device everywhere (device list, flow, command line, API, device
process). Tests never touch real
devices: fake the transport (see `tests/test_driver.py` for the Pico boards and
`tests/test_dslogic_driver.py` for a USB device).
