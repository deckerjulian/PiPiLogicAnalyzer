# The lab: flows, nodes, panels, reports

openSciLab is a measurement and control lab: instruments in a hub, flows that capture, process,
check and generate signals, panels that operate them, reports that document them. Everything
works with [simulated instruments](simulator.md) as well; the logic analyzer of the
[README](../README.md#the-logic-analyzer) is one document of the lab.

## The window

* **Activity bar and sidebar** (left): *Project* (open documents, the flows, panels and waveforms
  of the project, its data, recent files), *Devices* (connected instruments with their status,
  the devices to connect, among them every simulator; *Connected hardware…*) and *Search*; at the
  bottom the start page and the settings. The parts of a section open and close with a click on
  their header (kept); a right click on the activity bar shows or hides its entries, orders them
  and puts their names under the icons.
* **Node palette**: beside a flow whose graph is edited (Ctrl+Shift+N hides it).
* **Documents** in tabs, split side by side or below each other, or in windows of their own:
  data view, flow, panel, waveform, device card, charts and values of running flows.
* **Inspector** (right): properties of the selection – a node, a panel widget, a device.
* **Console** (bottom): problems (a double-click shows the node), the execution log of flows,
  the application log and a Python console with access to the shell. The status bar counts the
  problems and says which documents are unsaved and whether the active flow runs in real or
  virtual time.
* **Header**: the command palette right after the logo (Ctrl+K, every command with fuzzy search),
  new documents, open, save, *Templates*, and *Start/Stop* for the active document – run a flow or
  panel, capture in a data view. A right click (or *View → Customize the header…*) arranges it.
* **Start page**: *Try a simulator*, *Connect a device*, *New flow*, *Open file…*, *Open
  project…*, every project of the example library as a tile by category (with a search; the first
  category, *Start a project*, holds *Empty lab*, *Logic Analyzer*, *Data acquisition*, *Synchronized
  instruments* and *Remote measuring device*) and recent files and projects. The application opens the saved files of the
  last session again (*Settings → Startup*; devices only when asked for there); when nothing was
  saved it starts with the start page and an untitled flow beside it. An unsaved flow writes its files into a scratch folder (shown under *Data*). A
  project of the library opens as a temporary project; the first *Save* (or *Project → Save project as…*)
  asks where to keep it, closing its last document asks whether to keep it.
* **Settings** (*Project → Settings…*, Cmd+, on macOS): theme (dark, light, like the system),
  text size, startup, mouse wheel, data folder, devices.

## Instruments

A device is an instrument with facets – capture, GPIO, monitor, analog inputs and outputs,
generator – which follow from the capabilities it reports ([protocols.md](protocols.md)). Devices
are opened from the device list, *Devices → Connect…* or the start page (*Try a simulator*,
*Connect a device*); the **device card** is the place to work with one. Its tabs:
*Pins* – every pin with its channel and
name, and for GPIO a pin strip and table (modes, switches, pulses, PWM, voltages); *Events*
(simulators); *Details* – everything about the device: its functions (capture settings, self-test, network,
restart, bootloader, firmware, reconnect), the time of its samples, capture limits, capabilities
and connection. Its header has *Capture…* (the result appears in a **data view**, which shows,
analyses, decodes and edits data and holds the settings of the next capture), *Profiles* (saved
capture settings) and *Disconnect*. *Devices → Connected hardware* lists every
board with its firmware and installs the image that fits it. On the *Pins* tab: *All outputs safe* (Esc), the monitor (*Record* makes it a slow
capture) and *stimulus and capture* (arm a capture, also on another instrument, then pulse a
pin). Everything set on a device while a capture runs becomes a marker in it. Several
instruments are open at once; trigger routes of the hub tell how their trigger lines are wired.

## Flows

A flow is a graph of nodes and wires, stored as `*.flow.yaml`:

```yaml
flow: Counter
nodes:
  sim: {type: device.instrument, address: sim:free}
  cap: {type: device.capture, channels: [D0, D1, D8], rate: 4 MHz, samples: 20000,
        trigger: {edge: rising, source: D8}}
  scope: {type: view.scope, title: Counter}
  file: {type: data.file, path: counter.lac}
edges:
  - sim.device -> cap.device
  - cap.capture -> scope.in
  - cap.capture -> file.in
```

**Devices are nodes.** A `device.instrument` node stands for an instrument: its `address` is a
simulator (`sim:uno`), a device (`pico:/dev/cu.usbmodem1`, `dslogic`) or the address of an
instrument open in the device list – in real time that instrument is used as it is. Without an
address it is the project's device of the same name. Its `device` output is wired to the
`device` input of every node that uses the instrument (capture, stream, monitor, GPIO,
generator); a device wire fits only device inputs. The palette offers the open instruments, the
project's devices and every simulator under *Devices*, the device list *Add to the flow*; a node
that needs a device is wired at once when the flow has exactly one. A simulator simulates what
the node's `signals` say (`{scenario: uart, text: Hello, baud: 9600}`, `{scenario: c64}`, single
channels); `sim:uno#2` is a second Uno, `sim:pico*2` a multi device set of two boards
([simulator.md](simulator.md)).

The same flow in Python (the two convert into each other unchanged):

```python
from openscilab.lab import flow, nodes as n

with flow("Counter") as f:
    sim = f.device("sim", "sim:free")
    cap = n.device.capture(sim, channels=["D0", "D1", "D8"], rate="4 MHz", samples=20000,
                           trigger={"edge": "rising", "source": "D8"})
    cap.capture >> n.view.scope(title="Counter")
    cap.capture >> n.data.file(path="counter.lac")

result = f.run(fast=True)
```

`n.<group>.<name>(...)` adds a node; `a.out >> b.in` wires two ports, `a >> b` the first output
to the first free input. `f.device(name, address)` adds a device node; given to a node (`sim`, or
`uno.pin("D9")` for a pin) it is wired to the node's `device` input.

The **flow document** has three views: *Graph*, *YAML* and *Python* (a flow opened from a script
is saved as `.flow.yaml`; broken YAML is kept until it is fixed or discarded). In the graph:

* nodes come from the palette (drag or double-click), *Add node* or Tab, or by typing a name on
  the canvas; they do not land on top of each other;
* while a wire is dragged the ports it fits light up and it snaps to them (or to the right port
  of the node it is dropped on); dropped on the canvas it offers the nodes it fits; a wire that
  does not fit says why above the graph and offers the conversion node;
* right-click a wire to insert a node or remove it, a port to add a connected node or disconnect
  it; cut, copy, paste, duplicate (Ctrl+D) and select all work with the wires between the nodes;
* required inputs without a wire are marked, nodes show a badge for their problems; groups carry
  the nodes inside them and resize at their corner, as comments do;
* navigation: two fingers move, pinch zooms, the wheel zooms at the pointer, *Flow → Zoom in /
  Zoom out / Fit (Ctrl+0) / Actual size*;
* undo/redo of every edit, subflows. A running flow shows the state of every node and the latest
  values on its wires; breakpoints and single steps work in the graph. Why a flow cannot run, or
  how a run failed, appears above the graph (a missing device offers *Run with simulators*).

The **engine** runs a flow in real time or – with simulators – in virtual time (as fast as
possible, the same result every run). Values wait in mailboxes with back pressure; nodes run when
values arrive, timers and sweeps sleep in the flow's time. A flow ends when nothing can happen any
more, after `duration`, or when it is stopped. On the command line: `openscilab run` (see
[cli.md](cli.md)).

Wire types: `Digital`, `Analog`, `Scalar`, `Bool`, `Event`, `States`, `Capture`, `Table` and
`Any`, with conversions between them (`openscilab.core.signals`).

### Own nodes

A project's `nodes/*.py` add node types:

```python
from openscilab.lab import In, Out, Param, node


@node("example.double", inputs=[In("value", "Scalar", optional=False)], outputs=[Out("out", "Scalar")])
def double(value):
    return value.value * 2


@node("example.blink", outputs=[Out("level", "Bool")], params=[Param("period", "quantity", "1 s", "s")])
async def blink(ctx):
    for _ in range(3):
        ctx.emit("level", True)
        await ctx.sleep(ctx.quantity("period") / 2)
        ctx.emit("level", False)
        await ctx.sleep(ctx.quantity("period") / 2)
```

A plain function is called with the latest values of its inputs whenever one changes; an
`async def` taking `ctx` runs with the flow (`ctx.emit`, `await ctx.receive(port)`,
`await ctx.sleep(seconds)`, `ctx.param`, `ctx.quantity`, `ctx.device(name)`, `ctx.log`). For
periodic work `await ctx.sleep_until(start + n * period)` keeps the period (sleeping `period`
after each round adds the time the round took); `await ctx.device_call(device, function, ...)`
calls a device without holding the other nodes while a real one answers. The
example project (`examples/project/`) has both kinds; the node `control.python` holds such code
inside the flow.

## Nodes

### Devices

| Node | What it does |
| --- | --- |
| `device.capture` | Captures with an instrument: channels, rate, length and trigger. Without a wire at 'arm' it captures once when the flow starts, otherwise on every value at 'arm'. With a 'clock' channel it takes one sample on each edge of the clock instead (state mode, 'samples' states; 'rate' stamps their times) and sends them at 'states'. |
| `device.instrument` | An instrument of the flow: its address (sim:uno, pico:/dev/cu.usbmodem1, or the address of an instrument open in the device list, which is then used as it is). Without an address it is the project's device of the same name. Wire 'device' to the nodes that use it. A simulator (sim:uno, sim:pico*2 for a multi device of two boards, sim:uno#2 for a second one) simulates what 'signals' says: {scenario: uart, text: Hello, baud: 9600}, {scenario: c64}, {scenario: file, path: capture.lac}, or single channels: {channels: {D5: {type: square, frequency: 1 kHz}}}. |
| `device.monitor` | Reads pins periodically at 'rate' for 'duration' (until the flow ends with 0): a value per pin and report (1 s at 10 Hz: 10 reports, the first at once). |
| `device.stream` | Streams the channels of an instrument; blocks of samples flow while it runs. Starts with the flow (or on 'start'), ends after its duration or on 'stop'. |

### GPIO

| Node | What it does |
| --- | --- |
| `gpio.dac` | Sets an analog output to the voltage arriving at 'volts' (or the parameter once). |
| `gpio.pulse` | 'count' pulses of 'width', one every 'period', on a pin for every value at 'trigger' (once at the start without a wire). The device times the pulses exactly. |
| `gpio.pwm` | PWM on a pin: 'freq' and the duty cycle (0..1) from 'duty' or the parameter; a duty of 0 stops it. |
| `gpio.read` | Reads a pin for every value at 'trigger' (once at the start without a wire): the level of a digital pin, the voltage of an analog input ('analog'). |
| `gpio.write` | Drives an output pin to the value arriving at 'value' (≥ 0.5 is 1). Without a wire it sets 'level' once when the flow starts. |

### Generator

| Node | What it does |
| --- | --- |
| `gen.arbitrary` | Arbitrary points on an analog output: a formula over one pass (x from 0 to 1, numpy functions), a CSV file (one column of volts, or time and volts), or the analog signal arriving at 'signal'. 'frequency' sets the passes per second; without it a CSV file with times and a signal play at their own rate. |
| `gen.output` | Plays the waveform arriving at 'waveform' (from a *.wave.yaml 'file' without a wire); 'sync' marks every start, e.g. to arm a capture. |
| `gen.pattern` | A digital pattern on a pattern output: SDL text per pin ('tracks', e.g. {D0: 'l5;h5;'}) at 'rate', or the capture arriving at 'capture'. 'repeat': passes (0: until stopped). |
| `gen.replay` | Plays a capture: its digital channels on a pattern output under their own names, or only the channels of 'pins' under new names (e.g. {CH1: D0}); or one analog channel ('analog') on an analog output. |
| `gen.tx_i2c` | Writes 'data' to the I²C 'address' on the pins 'pins' (SCL, SDA) and reads 'read' bytes back (devices with their own I²C, TX_I2C; else the write as a pattern). |
| `gen.tx_spi` | Sends 'data' as SPI mode 0 (MSB first) on the pins 'pins' (CS, SCK, MOSI); with the device's own SPI (TX_SPI) the bytes on 'miso' come back at 'received'. |
| `gen.tx_uart` | Sends the bytes of 'data' (or of each value at 'data') as 8N1 UART frames on 'pin': with the device's own UART where it has one (TX_UART), else as a pattern on a pattern output. |
| `gen.waveform` | A standard waveform (sine, square, triangle, ramp, pulse with 'duty', DC, noise with 'amplitude' as its standard deviation) on a generator output. Values at 'frequency' and 'amplitude' change it while it plays; 'start' starts it (without a wire it starts with the flow). On a square-only output (PWM) a square or pulse without 'amplitude' and 'offset' swings over the output's whole range. |

### Time

How the samples of several instruments get one time axis: [docs/timing.md](timing.md).

| Node | What it does |
| --- | --- |
| `timing.align` | Aligns an instrument (at 'device') with a reference by a sync signal both recorded: 'signal' is the signal as this instrument recorded it, 'reference' as the reference instrument did (or 'edges' of timing.sync: as precise as its output). The matched edges give offset and drift (drift none: their sample clocks are shared, only the offset); the instrument's later samples are placed with them, and what arrives at 'in' leaves 'out' moved onto the reference's time (a capture of the instrument, its other channels). |
| `timing.calibrate` | Measures how late the samples of an instrument arrive: an output pin, wired to one of its channels, is switched 'repeats' times while the instrument streams (or captures) that channel. No edge is earlier than its command and no sample later than its arrival: half of the shortest loop is the latency. It is kept for the instrument at this rate (and length of a capture) and used by its captures and streams from then on. Real time only. |
| `timing.sync` | Drives a pin with a sync signal: edges at irregular intervals (the same for the same 'seed') that recordings of it can be matched by without ambiguity. Wire the pin to a channel of every instrument to align (timing.align) and to the sync input of a remote device (remote.sync). 'edges' are the times the edges were commanded (as precise as the output). Runs for 'duration' (0: until 'stop' or the end of the flow). |

### Remote devices

Measuring devices on other computers ([docs/remote.md](remote.md)); they run in real time.

| Node | What it does |
| --- | --- |
| `remote.call` | Calls a command of a remote device with 'args' for every value at 'trigger' (once at the start without a wire); 'result' is what it answers. |
| `remote.receive` | The values of an input of a remote device, with the time they were measured on the device (converted with its measured clock): numbers and truth values one by one, samples as blocks of an analog or digital signal. 'playout' waits that long for late values so the values of several devices come in the order of their time (auto: what the network needs; 0: at once). Ends after 'duration' (0: with the flow). |
| `remote.set` | Sets an output of a remote device to the value arriving at 'value' (or to the parameter 'value' once at the start). The value is sent 'lead' ahead with the time it should take effect, and the device applies it at that time (auto: what the network needs, 0: as soon as it arrives). 'done' tells the time it happened. |
| `remote.sync` | Aligns the clock of a remote device with a sync signal: the device reports the times of the signal's edges in its clock (a sync output it drives, a sync input it sees, or an input whose blocks record it). 'signal' is the same signal recorded by a channel of another instrument (a capture or stream); 'edges' instead takes the edge times of timing.sync (as precise as its output). The matched edges give offset and drift with the precision of the recording (drift: auto fits it unless the device's sample clock is shared; none: only the offset); from then on the device's values are converted with them. 'offset' is the device time minus the local time, 'uncertainty' its scatter. |

### Decoders

Every sigrok protocol decoder is a node (133 of them, e.g. `decode.uart`, `decode.i2c`, `decode.spi`, `decode.can`, `decode.onewire_link`, `decode.usb_packet`, `decode.jtag`): a capture at `in`, the annotations as `events`, a `table` and `text` at the outputs. A stacked decoder (e.g. `decode.eeprom24xx` on I²C) takes the capture itself, runs the decoder below it and needs that decoder's `channels`.

### Measurement

| Node | What it does |
| --- | --- |
| `measure.count` | Counts: the edges of a digital signal, the events of an event stream, the rows of a table – or, for other values, the values that arrived. 'total' adds up over the flow. |
| `measure.duty` | Share of the period the signal is high (0..1). |
| `measure.frequency` | Frequency of a digital signal (from its periods). |
| `measure.max` | The highest value of an analog signal (of each block of a stream). |
| `measure.mean` | The mean of an analog signal (of each block of a stream). |
| `measure.min` | The lowest value of an analog signal (of each block of a stream). |
| `measure.peak_to_peak` | Highest minus lowest value of an analog signal (of each block of a stream). |
| `measure.period` | Period (rising edge to rising edge) of a digital signal. |
| `measure.pulse_width` | Width of the high (or low) pulses. |
| `measure.rms` | The root mean square of an analog signal (of each block of a stream). |
| `measure.setup_hold` | Shortest setup and hold time of 'data' around the edges of 'clock'. |

### Signal processing

| Node | What it does |
| --- | --- |
| `dsp.average` | 'captures': the sample-by-sample mean of the last 'count' signals of the same length and unit (scope averaging); 'moving': a moving average over 'count' samples (without delay: placed half its length earlier). |
| `dsp.debounce` | Removes pulses shorter than 'time' (a bouncing switch becomes one edge). In a stream a change that goes on into the next block counts with its whole length (it shows from that block on). |
| `dsp.derivative` | Change per second (unit/s): each sample minus the one before, by the time between them (the first sample of a signal takes the change to the second). |
| `dsp.fft` | Spectrum of an analog signal: a table of frequency and amplitude (dB or linear). The mean (DC) is taken off first, so the 0 Hz line shows only what the window leaves of it. |
| `dsp.filter` | Low-pass, high-pass, band-pass, band-stop or moving average. 'fir' (windowed sinc, numpy; without delay: its output is placed half its length earlier) or 'iir' (Butterworth, needs scipy). 'cutoff' is one frequency, or [low, high]. |
| `dsp.integral` | Running integral over time (unit·s, trapezoids), across the blocks of a stream; it starts at 0 with the first sample, 'reset' starts again at 0. |
| `dsp.math` | An expression of the inputs a, b, c, d with numpy (e.g. 'a * 2 - b', 'sqrt(a**2 + b**2)', 'where(a > 1.5, 1, 0)', 'max(a)'). Signals are arrays, scalars numbers. Several signals are taken at the times of the first one where they all have samples (blocks of a stream once, when they have arrived); a comparison of signals gives a digital signal. |
| `dsp.resample` | A signal at another rate: linear interpolation for analog, nearest sample for digital. |
| `dsp.threshold` | 1 above the threshold, 0 below; with hysteresis the level changes only beyond threshold ± hysteresis/2. |

### Control

| Node | What it does |
| --- | --- |
| `control.compare` | Compares 'a' with 'b' (or with 'value' when 'b' has no wire): ==, !=, <, <=, >, >=, within (\|a - b\| <= tolerance), contains, startswith. Sends the result and 'passed' or 'failed'. |
| `control.counter` | Counts the values arriving at 'in' (events count each event); 'reset' starts again. |
| `control.limit` | Checks a value against 'low' and 'high': 'ok' and the value clipped to the limits. The limits are numbers in the unit of the value, or quantities (3.3 V, 20 mA). |
| `control.python` | Your own code: 'async def run(ctx)' runs with the flow (ctx.emit, await ctx.sleep, await ctx.receive(port), ctx.device(name), ctx.log); 'def on_input(ctx, port, value)' is called for values on the inputs. 'inputs' and 'outputs' name the ports. |
| `control.sequence` | Steps without code, one after the other: {set: out, value: 1} sends a value, {emit: out} an event, {wait: 100 ms} waits, {wait_for: in, timeout: 1 s} waits for a value on an input. 'repeat' runs it several times (0: for ever). |
| `control.state_machine` | States with what they set and when they go on. It starts in 'initial'; entering a state sends its 'enter' values on outputs of those names (enter: {red: 1, green: 0} makes the outputs red and green) and 'state' says which state it is. 'on' lists the ways out: {after: 1 s, to: green} after a time, {input: button, when: 1, to: green} when a value arrives at an input of that name; the first that happens wins. A state without 'on' ends the machine ('done'). |
| `control.sweep` | Steps a value from 'start' to 'stop' (or through 'values'): sends 'value', waits 'dwell', then sends 'step'. With a wire at 'next' it waits for it before the next value; with one at 'trigger' it sweeps for every value there (a start button). |
| `control.timer` | Ticks every 'interval' (after 'delay'); 'count' ticks, or endless with 0. |

### Data

| Node | What it does |
| --- | --- |
| `data.buffer` | Keeps the latest values: the last 'length' seconds of a signal (blocks are joined) or the last 'count' other values. Sends the buffer on every value, or only on 'read'. |
| `data.file` | Writes what arrives: captures as .lac/.sr/.csv/.vcd (by the extension), tables, bundles and events as CSV, every value replacing the file; other values (numbers, text) are lines of the file, one per value of the run. 'numbered' writes name-001, name-002, ... instead, after the numbers already there. |
| `data.file_read` | Reads a capture (.lac, .sr) or a table (.csv) when the flow starts, or on every value at 'read'. |
| `data.logger` | Records for a long time on disk. A .csv path: values, events and the new rows of tables as lines time, source, value (a table with several columns: a line per column), written at once and appended to the file of earlier runs. A .lac or .sr path: the samples of streams and signals (digital and analog), in a capture on disk saved when the flow ends. |
| `data.table` | Collects values into a table. With 'columns', each column has an input and a row is complete when every column got a value (values wait in the order they came, so a column may run ahead); otherwise every value on 'in' adds rows. |

### Views

| Node | What it does |
| --- | --- |
| `view.led` | On for true, a number of 0.5 or more, or a text like on/yes/true; off otherwise. |
| `view.log` | Every value that arrives as a line of text. |
| `view.number` | Shows the latest value as a number with its unit (of a signal: its last sample). |
| `view.scope` | Shows captures in a data view (with decoders, cursors and measurements). Blocks of a stream are joined. |
| `view.spectrum` | The spectrum of an analog signal (or a table of frequency and magnitude from dsp.fft). |
| `view.strip_chart` | Values over time, like a chart recorder: numbers, events, the samples of analog and digital signals, the channels of a capture or stream (a trace each). More traces: name them in 'inputs'. |
| `view.table` | Shows a table (or the rows of other values). |
| `view.xy` | Points (x, y): a point for every value at 'y', with the x it belongs to – the x values in the order they came, the latest one again for more values at 'y' (a characteristic curve: several measurements per step are fine). |

### Report

| Node | What it does |
| --- | --- |
| `report.check` | Checks the values at 'in' against limits ('low', 'high') or an 'expected' value with a 'tolerance'; 'pass' tells the result, the report lists it. |
| `report.image` | A diagram in the report: an XY chart of two table columns ('x', 'y') or of (x, y) values, or the traces of a capture or signal. |
| `report.section` | A heading and text in the report; values at 'in' are added below it. |
| `report.table` | The last table (or the values) arriving at 'in' as a table in the report. |
| `report.write` | Writes the report (sections, tables, images, checks with passed or failed) to 'path' when the flow ends, or on every value at 'write'. HTML, or PDF for a .pdf path. |

### Conversions

| Node | What it does |
| --- | --- |
| `convert.channel` | One channel of a capture: 'channel' by its name, else the first one of 'kind' (any, digital, analog). |
| `convert.edges` | The edges of a digital signal as events. |
| `convert.state_bit` | One channel of the states (values taken on the edges of a clock) as a digital signal, one sample per state. |
| `convert.to_analog` | Logic levels as voltages. |
| `convert.to_scalar` | 1 for true, 0 for false. |

### Structure

| Node | What it does |
| --- | --- |
| `structure.bundle` | Several values on one wire: a mapping of the 'fields', sent when every field has a value (or on every value with 'partial'). |
| `structure.comment` | A note on the canvas. |
| `structure.group` | A frame around nodes that belong together. |
| `structure.unbundle` | The fields of a bundle on wires of their own. |

## Panels

A panel (`*.panel.yaml`) is the front panel of a measuring station: widgets placed freely on a
surface (tabs, groups), each bound to a port of a flow.

```yaml
panel: Characteristic curve
flow: ../flows/curve.flow.yaml
width: 960
height: 400
widgets:
  start: {kind: button, bind: sweep.trigger, title: Start, x: 16, y: 16, width: 224, height: 80}
  volts: {kind: chart, bind: mean.out, title: Mean voltage, x: 16, y: 112, width: 456, height: 272, unit: V}
  slope: {kind: number, bind: fit.slope, title: Slope, x: 488, y: 112, width: 224, height: 80, unit: V}
```

`width` and `height` are the size of the panel, `x`, `y`, `width` and `height` of a widget its
place and size on it, in pixels from the top left corner; `tab` and `group` name its tab and the
framed group it belongs to.

| Widget | Binding | Options |
| --- | --- | --- |
| `switch` | sends true/false | `value` |
| `button` | sends an event per press | – |
| `slider` | sends the value | `min`, `max`, `step`, `unit`, `value` |
| `input` | sends the number | `unit`, `value` |
| `choice` | sends the chosen option | `options`, `value` |
| `number` | shows a value | `unit`, `digits` |
| `led` | on for true or above `threshold` | `color`, `threshold` |
| `chart` | values over time (or x/y pairs) | `points`, `unit` |
| `scope` | a capture or signal as traces | – |
| `label` | text | `text` |

Controls send into an input of a node (as if a wire brought the value) or out of an output to the
inputs wired to it; displays show outputs. Controlled inputs count as wired. *Edit* shows the
surface of the panel like a GUI editor: drag widgets from the palette onto it, drag them to move
them and their eight handles to resize them. Edges and middles snap to those of the other widgets
and of the panel within 6 pixels - red guides show it - and otherwise to a raster of 8 pixels; Alt
places freely. Shift or Ctrl and a click add to the selection, a frame drawn on the free surface
selects what it touches, `Ctrl+A` all; the arrow keys move the selection by a pixel (Shift: by
the raster). *Arrange* (tool bar, right click) aligns edges or middles, distributes three or more
evenly and brings widgets to the front or the back; the corner of the panel resizes it. The
inspector places a widget exactly and binds it (ports of a fitting type, tabs and groups to
choose); a widget whose port does not exist is marked. *Operate* runs the flow (the open flow
document with its unsaved changes, else the file) until it is stopped; the panel grows or shrinks
with its window as a whole (to half its size at least, then it scrolls), F11 shows it full screen.
Charts zoom and move like the waveform; a double-click goes back to the automatic range.

## Signal generation

A waveform (`*.wave.yaml`, `openscilab.core.waveform`) is a standard shape (sine, square, triangle,
ramp, pulse, DC, noise) with sweep and burst, arbitrary points (formula over one pass with `x`
from 0 to 1, CSV file, a captured channel), or a digital pattern (SDL per pin, a capture, UART/SPI/
I²C blocks). The **waveform document** builds it with a preview, lists the generator outputs of all
instruments, explains what does not fit an output and starts it. *Play as signal* in the
analyzer turns a capture into a waveform. In flows: the `gen.*` nodes.

## Reports

`report.section`, `report.table`, `report.image` (diagrams as SVG) and `report.check` collect a
report, `report.write` writes it as HTML or PDF with passed or failed. `openscilab run --report`
puts the same parts into its report and ends with 1 when a check failed.

## Projects

```
my-lab/
├── project.yaml     devices, settings, simulation wiring, files to open
├── flows/           *.flow.yaml, Python flows
├── panels/          *.panel.yaml
├── nodes/           own nodes
├── waveforms/       *.wave.yaml, *.sdl
├── data/            captures, tables, logs, reports (listed in the sidebar)
└── tests/           flow tests against the simulator
```

Devices named in `project.yaml` are available to every flow of the project; `simulation.wiring`
adds wires to simulated circuits, also between two simulated devices
(`scope: [{from: "uno:A0", to: CH1}]`). A new project starts as a copy of one of the library
(`examples/library`); `open` lists the files it opens, and `data` a data view that captures with the
connected device (the *Logic Analyzer*).
