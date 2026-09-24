# Differences from the original software

The application reproduces the feature set of the C#/Avalonia software of the
[LogicAnalyzer](https://github.com/gusmanb/logicanalyzer) by Agustín Giménez Bernad (gusmanb),
version 6.5, which it is based on. Along the way the bugs below were fixed and the display was
rebuilt. Every fix is commented in the source code where it applies; most are covered by tests.

## Fixed bugs

| Area | Behaviour in the original | Here |
| --- | --- | --- |
| `LogicAnalyzerDriver.ReadCapture` | After the length byte always `LoopCount + 2` timestamps were read, although the firmware only sends the block when it reports more than one → the application hung while reading. | Exactly the number reported by the device is read (`tests/test_driver.py::test_timestamp_block_is_only_read_when_the_device_sends_one`). |
| `MultiAnalyzerDriver.Dev_CaptureCompleted` | The device was identified through the mutable `Tag` property; with devices finishing at the same time channels could be swapped. | Every completion callback is bound to its device index. |
| `EmulatedAnalyzerDriver.BlastFrequency` | Threw `NotImplementedException`; the capture dialog crashed for loaded files. | Returns 0, blast mode is disabled in the dialog. |
| `CaptureDialog` (pattern trigger) | Checked `(trigger - 1) + bits > 16` and thereby accepted patterns reaching beyond the last usable channel. | Checks the groups of consecutive trigger inputs reported by the firmware (channels 1–16 for other firmware), at most 16 bits or 5 in fast mode. |
| `CaptureDialog` | Also wrote the settings with `File.WriteAllText` into the working directory; only the copy in the AppData folder was read. | Stored once in the settings directory, written atomically. |
| `MainWindow.MnuNew_Click` | `scrSamplePos.Maximum = samples.Length - 1`, with `samples` indexed by channel → the scroll range matched the channel count. | The scroll range follows the number of samples. |
| `MainWindow.Driver_CaptureCompleted` | `updateSamplesInDisplay(PreTriggerSamples - 2, PreTriggerSamples - 10)` passed a position as zoom level. | A defined window around the trigger is shown after a capture. |
| `MainWindow.DeleteSamples` | Deleted `SampleCount + 1` samples but moved regions by `SampleCount`; the pre-trigger count was reduced even when deleting behind the trigger. | Selections are inclusive throughout; trigger and regions are updated consistently (`tests/test_ui.py`). |
| `MainWindow` (sample counters) | After inserting/deleting, `PreTriggerSamples + PostTriggerSamples` no longer matched the actual number of samples. | The counters are recomputed from the channel data and tested. |
| `ChannelViewer` | The eye could only hide channels; they came back only through the collective button. | The dot of every channel toggles it; the collective button remains. |
| Power display (network) | `tmrPower.Change(30000, Timeout.Infinite)` fired exactly once. | Periodic timer, stopped on disconnect. |
| `DeviceDetector` | Separate implementations for the Windows registry and Linux sysfs; macOS was not supported. | Detection through the USB descriptors of pyserial (all platforms), sysfs only as a fallback. |
| `SendNetworkConfig` | Copied the strings into fixed buffers without checks. | Fields are truncated to their maximum length, leaving room for the NUL byte. |
| SDL parser | A group referencing itself caused a stack overflow; the trailing comma before `}` was required or forbidden depending on the position. | Recursion is detected and reported, both notations are accepted. |
| Measure dialog | The "predicted" pulse duration was the average of the last 5 % of a list sorted by frequency – the result depended on the number of distinct pulse lengths. | Most frequent value (mode), the median on ties; additionally the duty cycle per channel. |
| Time/frequency format | Output as `us` and `Mhz`. | Correct units `µs`, `MHz`, `kHz`, `ns`. |

## Display and performance

The original drew a line per visible sample **and** channel (`SampleViewer.Render`) and built
the overview with a loop over all samples of all channels (`SamplePreviewer.UpdateSamples`). Both
grow linearly with the capture size; with the buffer sizes the devices support the interface
became unusable.

Here:

* **Transition index:** `core/analysis.ChannelTransitions` stores the start of every level run of
  a channel once (`numpy`). Tooltips, measurements and the display access it with
  `searchsorted`.
* **Two drawing paths:** if a sample is at least one pixel wide, the exact waveform is drawn as
  one `QPainterPath` per channel. If more samples than pixels are visible, two `searchsorted`
  calls decide for every pixel column whether it is "low", "high" or "edge"; equal columns are
  merged. The cost depends on the window width, not on the number of samples.
* **Overview:** built as a `numpy` image (one reduction per channel) instead of line by line.
* **Annotations:** the visible range is found by binary search instead of iterating over all
  segments.
* **Decoders:** run on a worker thread; the original decoded on the UI thread.

## Protocol decoders without Python.NET

The original embedded CPython through Python.NET, generated a C# class for every decoder at run
time with Roslyn and mapped the `sigrokdecode` API to C#. Wait conditions were evaluated sample by
sample through `Dictionary<int,int>`.

`pipilogicanalyzer/sigrok/runtime.py` provides the same API directly in Python (`Decoder`, `wait`,
`put`, `register`, `has_channel`, the `OUTPUT_*` constants). Conditions are evaluated in blocks
with `numpy`, `wait()`/`wait({'skip': n})` additionally through an arithmetic fast path. Unmodified
`pd.py` files of libsigrokdecode run as they do in PulseView.

## Taken over from LogicAnalyzer 6.5

| Area | 6.5 | Here |
| --- | --- | --- |
| Profiles (`ProfilesSet.cs`) | Menu *Profiles*: save, load and delete settings and decoder tree under a name. | The same, plus export/import as JSON, profiles directly in the capture dialog, import of the original `profiles.json` (`$type` wrappers, decoder tree through option/channel indexes). |
| `CAPTURE_REQUEST` | 32 channels, `loopCount` as `uint16`; the application requires firmware V6_5. | Layout by reported firmware version: V6_0 devices keep the 48 byte format, V6_5 and newer devices get the 56 byte format. |
| Burst mode | Up to 65,534 bursts; measurement only up to 254 bursts and from 100 post-trigger samples. | Limits in the dialog and the driver; the maximum follows the device (`max_loop_count`). |
| Burst timing | Tick length from the blast frequency instead of a fixed 5 ns (RP2350). | The same, 5 ns only without a reported blast frequency. |
| Multi device | External trigger fixed at channel 24; only the master evaluates a pattern. | External trigger = channel count of the respective device (28 channel boards). Any board evaluates a pattern or an edge (firmware of this project) and starts the others, or every board waits for the external trigger. |
| Tooltip | Channel name and derived frequency `1 / (2 · length)`. | The same. |
| Channel names | Empty/whitespace only → channel number. | The same. |
| Preview | The pin state is saved. | The visibility of the preview is saved with the window geometry. |
| Exit | A running capture is aborted before closing. | `dispose()` sends the abort command. |
| Driver debug log | `DEBUG_MODE` (compile switch) → `driver_debug.log`. | `logging` (`pipilogicanalyzer.driver`), written to the file with `--debug-driver`. |
| Decoders | Updated decoder set (including `mos6502`, `mcp230xx`, `max72xx`). | Run unmodified; `./decoders` in the project is loaded first. |

Not taken over: the changed mouse wheel assignment (here wheel = zoom, `Ctrl` = scroll,
`Shift` = fine zoom), disconnecting the device when opening a file, and querying the battery
level by click instead of by timer.

## Additional functions

* VCD export, CSV export with an optional time column, gzip-compressed captures.
* Zoom at the mouse position, dragging the waveform, clickable overview, "Zoom to fit" and
  "Go to trigger".
* Tooltips with sample number, time relative to the trigger and length of the level run.
* Decoder browser with categories and search, automatic channel assignment by channel name,
  missing required channels are shown instead of silently empty results.
* Measure dialog as a table over all channels instead of a list of single fields.
* Firmware installation, board self-test, simulated captures and device information.
* Command line: open a file directly, add decoder paths.
