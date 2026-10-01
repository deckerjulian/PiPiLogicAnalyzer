# Device drivers

The application works with any logic analyzer that has a driver. It never asks which kind of
device is connected: a driver describes what its device can do, and the main window and the
dialogs show what that allows. Adding a device takes two pieces:

1. a **driver** in `pipilogicanalyzer/driver/<device>/`, a subclass of
   `AnalyzerDriverBase` (`driver/base.py`), free of Qt;
2. a **device backend** in `pipilogicanalyzer/ui/devices/<device>.py`, which lists the connected
   devices in the device list and opens the one the user picks.

```
pipilogicanalyzer/
├── driver/
│   ├── base.py        AnalyzerDriverBase, capture limits, capabilities, events
│   ├── models.py      CaptureSession, AnalyzerChannel, TriggerType
│   ├── emulated.py    stands in for a device while captures are loaded or computed
│   ├── pico/          Pico boards with the PiPiLogicAnalyzer firmware (gusmanb's hardware):
│   │                  protocol, serial/network transport, detection, multi device sets
│   └── dslogic/       DreamSourceLab DSLogic over USB (libusb)
└── ui/devices/
    ├── __init__.py    DeviceBackend, DeviceEntry, backends(), register_backend()
    ├── pico.py        autodetect, USB boards, network device, multi device sets
    └── dslogic.py     USB devices, asks for a missing FPGA bitstream
```

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
| `describe()` | the identification | board, firmware and connection for *Device information* |
| `device_details()` | none | the firmware reports details |
| `supports_bootloader`, `enter_bootloader()` | no | the firmware can be updated from the application |
| `supports_network_config`, `send_network_config(...)`, `is_network`, `get_voltage_status()` | no | the device has WiFi |
| `boards()`, `board_count`, `channels_per_device` | the driver itself | the driver combines several devices |
| `dispose()` | clears the handlers | close the connection (call `super().dispose()`) |

`driver_type` (`AnalyzerDriverType`) only names the built-in drivers; a new driver leaves it at
`OTHER` and names itself with `driver_id`.

Errors while opening a device are raised as `DeviceConnectionError` (or `OSError`/`ValueError`);
the main window shows their message.

## The device backend

```python
from pipilogicanalyzer.ui.devices import DeviceBackend, DeviceEntry


class MyDeviceBackend(DeviceBackend):
    id = "mydevice"

    def detected(self) -> list[DeviceEntry]:
        return [DeviceEntry(self.id, "usb", port, f"My analyzer on {port}") for port in find_ports()]

    def connect(self, entry: DeviceEntry, parent) -> MyDeviceDriver | None:
        return MyDeviceDriver(entry.value)  # may ask the user first; None cancels
```

`manual_entries()` adds entries below the detected devices that ask for the device when chosen
(like *Network device…*), and `idle_notice()` reports a device that cannot be used yet while
none is connected (like a Pico board in bootloader mode).

A built-in device is added to the list in `backends()` (`ui/devices/__init__.py`); a device
provided from outside the project calls `register_backend(MyDeviceBackend())` before the main
window is created.

## Tests

`tests/test_device_backends.py` connects a minimal driver through a backend and opens the
dialogs with it; a new driver should at least pass the same steps. Tests never touch real
devices: fake the transport (see `tests/test_driver.py` for the Pico boards and
`tests/test_dslogic_driver.py` for a USB device).
