# Changelog

All notable changes to this project are documented here. Versions follow
[Semantic Versioning](https://semver.org/) (pre-releases written as Python does: `0.1.0b1`). A
release is created by pushing a tag `v<version>` whose version has a section in this file (see
[Builds and releases](README.md#builds-and-releases)).

The project was called **PiPiLogicAnalyzer** up to version 7.2.0; as **openSciLab** it counts
again from 0.1.

## [Unreleased]

### Application

- **A tab into a window of its own**: drag it off the tab bar or double-click it (as well as
  *Open in a new window* on its right click); closing the window puts it back.
- The close button of a tab sits inside the tab, centred on its title.
- The sidebar starts at its width (250 px) instead of taking the room of the hidden node palette
  as well; the node palette opens beside it at its own width. A layout saved by an earlier version
  is not restored once (it held the wide sidebar).
- *Signals* of a simulator's device card: the button for a channel's own signal says that it acts
  on the channel selected in the table (*Set signal of the selected channel…*) and is off while
  none is selected (before, it did nothing without a selection).
- Loading a profile from a device card opens no data view: its decoders go to the device's data
  view when one is open, else to the next one.
- A panel has an icon of its own - a dial in a frame, the front of an instrument - and the *Panel*
  button says what a panel is.
- **Panels are laid out freely, like in a GUI editor**: widgets are placed and sized in pixels
  on the panel's surface instead of grid cells. Moved or resized (eight handles) they snap to the
  edges and middles of the other widgets and of the panel - red guides show it - or to a raster of
  8 pixels (Alt: freely). Shift/Ctrl and a click or a frame on the free surface select several;
  the arrow keys move the selection; *Arrange* (tool bar, right click) aligns, distributes and
  orders them; the panel's corner resizes it. An operated panel grows and shrinks with its window
  as a whole. The panel file stores `width`/`height` of the panel and `x`, `y`, `width`, `height`
  of each widget (`row`, `column` and `columns` are gone; the example panels are converted).
- **Templates of your own**: *Project → Save project as template…* (also at the top of the
  *Templates* menu) keeps the project as a template - its flows, panels, nodes, waveforms and
  devices, without the captures and results of its `data` folder, with a name and a description.
  Your templates come first, as *My templates*, on the start page and in the *Templates* menu;
  delete one with a right click on its tile or *Templates → My templates → Delete*. They are kept
  in `templates/` of the settings directory.

### Stability

- **Errors nobody caught are kept**: an error in a part of the window or a thread that nobody
  handled lands in `crash.log` in the settings directory with its traceback, and the status bar
  says so; *Help → Open the log folder* opens the folder. A crash of the application itself (or
  of a device process) leaves the stacks of its threads in `faults.log`.
- **A device process that hangs is ended**: a device process sends a sign of life every second;
  after 10 s without one, or when a call to it takes longer than 60 s, it is ended and its device
  shown as disconnected (its capture fails with the reason) instead of freezing what waited for it.
- The device card of a device whose process just ended no longer raises an error: it asked the
  monitor of the device every few hundred milliseconds whether it runs, and a tick between the end
  of the device process and the card's close failed (*An error occurred* in the status bar).
- The full screen window of a panel (F11) is deleted by Qt when it closes. Before, Python's garbage
  collector freed it at a moment of its own, which could crash the application when that moment
  fell into something Qt did with its windows.
- No error while a capture still arrives is drawn: the data view read the state of the transfer
  twice in one paint, and when the transfer ended in between, the paint failed (a flickering view,
  "endPaint called with active painter" in the log).
- A protocol decoder that cannot be loaded is no longer left out silently: *Help → Decoder search
  paths…* names it with the error, and the log says so.
- Nothing fails silently any more: every error openSciLab ignores on purpose (a port that is gone
  while it closes, a device that cannot tell its capabilities, ...) is in the debug log with its
  traceback (`--debug-driver`, *View → Console*).
- For developers: `tests/test_fuzz_parsers.py` throws arbitrary input at every parser (files,
  protocol frames, addresses, quantities) with hypothesis, and `pytest -m soak tests/test_soak.py`
  streams for minutes while it watches the memory; the workflow *Stability* runs the soak test
  on request, and a crash hunt: the tests on several runners at once, a crash with its Python and
  native stacks.

### Devices

- **The built-in devices are plugins too**: the Pico boards, Arduino, DSLogic, Rigol, the
  simulators and the remote devices register their kinds of address the same way a plugin does
  (`openscilab/plugins/`). *Help → Plugins…* and `openscilab-cli plugins` list them as *built in*,
  together with every kind of address. A plugin can now also open its device as an instrument
  with all its facets (`instrument=`), say that no simulator stands in for it
  (`simulation=None`), or that it is a simulator itself (`simulator=True`); see
  [docs/drivers.md](docs/drivers.md#plugins).
- **Plugins add nodes for the flows**: a plugin's functions with `@node` - as a project's own nodes
  - are known to every flow and project, in the palette (in a group of their own, titled with
  `node_group`), in flow files and to `openscilab run`. *Help → Plugins…* and
  `openscilab-cli plugins` list them; a flow with a node of a missing plugin says so; a plugin
  cannot replace a node of openSciLab. Example: `examples/plugins/calibration_nodes.py`
  (two-point calibration, NTC thermistor).
- *Simulate* of a flow runs a simulated Arduino (`arduino-sim:`) on the simulator *uno* and a
  simulated Rigol (`rigol-sim:`) on *dho924s*, as it does for the real devices (before: *free*).

## [0.1.0b1] - 2026-10-06

The first release of **openSciLab**, the open measurement and control lab that PiPiLogicAnalyzer
has become - a beta: every function works with the simulators and is covered by about 2000 tests,
but much of the new firmware has not been checked on real hardware yet.

**Highlights**

- **A lab instead of one instrument**: several instruments at once, each with a device card -
  Pico boards (firmware protocol 8 with GPIO, PWM, ADC, pattern generator and UART/SPI/I²C),
  Arduino boards with a firmware of their own, DSLogic analyzers, the Rigol DHO900 oscilloscope
  (with a bridge app), measuring devices on other computers (`openscilab_device`) and simulators
  of all of them.
- **Flows**: a visual editor (and a Python DSL) for `*.flow.yaml` - devices, GPIO, generators,
  every sigrok decoder, measurements, signal processing, control, data, views and reports; real
  time or deterministic virtual time; every node type has an example.
- **Panels** as the front panel of a measuring station, **reports** in HTML or PDF with checks,
  and `openscilab run` to use a flow as a test.
- **Time**: the samples of different instruments on one time axis (sync signals, latency
  measurement, drift, NTP/PTP).
- **The logic analyzer** of PiPiLogicAnalyzer as the data view, with streams, analog and state
  captures, trigger sequences and large captures arriving progressively.
- **A new window**: start page with every example project as a starting point, projects,
  templates, sidebar, command search, settings.
- **The repository is now `deckerjulian/openSciLab`**, with a new wiki as the user guide.

The details follow.

### Application

- `openscilab run <name>` in a project folder runs the project's flow of that name, as the
  command line documentation says; `openscilab --debug-driver` also logs the drivers that run in a
  device process of their own.
- Only the simulated boards with the openSciLab Arduino firmware are offered with the Arduino
  protocol (the simulated DAQ no longer shows up among them); messages and help texts name the
  current menus.

- **Every node checked, every node in an example.** A review of all node types and simulators
  found and fixed these; a test now fails when a node type has no example and when an example's
  flow has any warning (a misspelt parameter was silently ignored before):
  - *Streams*: `convert.edges` and `dsp.debounce` no longer lose edges and short pulses where two
    blocks meet; `dsp.resample` keeps its sample grid across blocks; `dsp.math` combines the
    signals at their common times (blocks of a stream once, not block k with block k-1);
    `measure.count` keeps each wire apart. FIR filters and moving averages are placed without
    their delay; empty blocks no longer give two samples; impossible cutoffs are refused.
  - *Numbers*: measurements no longer round the sample rate to whole hertz; `measure.setup_hold`
    pairs data and clock by time; `dsp.integral` starts at 0 (trapezoids), `dsp.derivative` is the
    same streamed and whole; the FFT no longer doubles the Nyquist line; `min(a)` and `max(a)` work
    in `dsp.math`, which keeps the unit and makes a comparison of signals digital.
  - *Units*: `control.compare`, `control.limit`, `dsp.threshold` and `control.sweep` take
    quantities in the unit of the signal (`20 mA` against a reading in mA, `3.3 V`, a tolerance of
    `50 mV`); a missing value to compare with is an error.
  - *State machine*: values that came before a state was entered no longer lead out of it, and
    values for other states no longer hold up their sender; a way out without `after` or `input`
    is refused. A wired `start` of a sequence runs it for every value; waiting for an input
    without a wire is refused.
  - *Devices*: unknown pins are named (`uno has no pin 'd7' - D7?`); a refused capture says the
    device's limits (`the rate is at most 100 kHz`); `pre` must be fewer than `samples`; levels
    may be `high`/`low`, a duty cycle `50 %`, an I²C address `0x48`; `remote.set` reads `false`
    as false; channels named CH1 fit digital and analog inputs.
  - *Generators*: `gen.arbitrary` plays a CSV file or a signal at their own rate; `gen.tx_*`
    send the `data` parameter for an event without data and say `done` when the transmission is
    through; `gen.output` plays an `.sdl` file on a pin and rate of your choice; a square-only
    output gets a square over its whole range by default.
  - *Decoders*: `text` takes the first data row that has annotations (only MOSI wired: its data)
    and of I²C only the data bytes.
  - *Data, views, reports*: bundles are written as numbers, not Python text; `data.file` writes
    signals as captures, bundles as CSV, refuses a table as a capture file, and its numbered files
    go on after those already there (`data.file_write`, a duplicate of it, is gone);
    `data.logger` writes only the new rows of a sliding table, one line per column, and keeps
    analog samples too; `data.buffer` keeps when its values came. `view.scope` joins only blocks
    that follow each other; `view.strip_chart` draws every channel of a capture and events at
    their times; `view.number` and `view.led` show signals and texts like `on`; diagrams of a
    report skip values that are no numbers; `structure.unbundle` finds numbered fields.
  - *Flows*: an unknown parameter is named with a suggestion (`has no parameter 'level' - did you
    mean 'threshold'?`); errors no longer repeat the node's name; a conversion inserted on a wire
    gets its parameters (a number to a truth value: at or above 0.5).
- **Captures and streams in virtual time see what the flow does meanwhile**: the flow's time runs
  on while a simulator's capture waits for its trigger, so a stimulus after `armed` - also after a
  wait - is captured, and a stream ends where its `stop` came. Two examples that had to run in real
  time no longer do; captures of USB devices (Pico, DAQ) have the flow's time in virtual time too.
- **State mode in flows**: `device.capture` with a `clock` channel takes one sample per clock edge
  and sends them at `states` (`convert.state_bit` takes a channel of them); a pattern trigger may
  have don't-care bits (`1x11`).
- **The simulators behave like the devices**: the Uno's captures hold 448 samples up to 100 kHz
  (pins only, at most 2 s after the trigger) and it streams 11.5 kB/s, as its firmware; the Uno R4,
  the Pico (800 kB/s) and the DAQ (20 kS/s for all inputs) likewise; the oscilloscope and a
  multi-board set no longer offer a stream. A generator that stops keeps its time in the signal
  (a capture across the stop sees both parts); *Safe*, a restart and the watchdog stop generators
  and analog outputs too; pull resistors no longer stick; an RC low pass follows a generator or a
  DAC; sources swing to the board's logic level (5 V on an Uno); the Arduino board stays free
  after a stopped capture; the state mode waits for its trigger; an edge right at the start of a
  capture counts. The simulated DAQ on the network measures a 50 Hz sine on AI0 as its demo does.
- **New examples**: *State mode*, *Signal processing* (derivative, integral, resampling, averaging,
  logic to volts), *Send SPI*, *Waveform file*, *Replay a capture*; *Bundle* takes its bundle apart
  again.
- Fixed: questions and messages had a large empty space left of their text (the icon took the
  width meant for the text); the question when closing names a temporary project properly.
- **Edit a node in a window of its own**: a double-click on a node opens its parameters in a
  window (a double-click on the data shown in it still opens them in a view). Mappings and long
  lists - the states of a state machine, the signals of a simulator - get a big text field in YAML,
  one entry a line, in the inspector too.
- Fixed: YAML read `on:` and `off` as true and false (YAML 1.1), so the transitions of a state
  machine were lost and the *Traffic light* example stopped in its first state. Flows, panels and
  projects are read as YAML 1.2 now (only `true` and `false` are truth values); a state machine
  names keys it does not know and states it cannot go to.
- **A tidier window.** The command search sits right after the logo; *Examples* is called
  *Templates* (menu and header). Tabs have their close button on the left. The node palette is no
  sidebar section any more: it shows beside a flow whose graph is edited (*View → Node palette*,
  Ctrl+Shift+N). The activity bar has the start page and the settings at the bottom and is
  arranged with a right click (show, hide, order, names under the icons); the parts of the
  sidebar sections (open documents, project, data, recent files, connected and available devices)
  open and close with a click on their header and show how many entries they hold. The overview
  of a flow draws its nodes as blocks in the colours of their kinds, with the visible part outlined.
- The rows of a data view start at the top with their height instead of stretching to fill the
  view; the overview below the waveform shows analog channels too (as an envelope) and fast
  digital signals clearly.
- **Nodes of a flow look the part**: a title bar in the colour of their kind (devices, measurements,
  views, data, timing, ...) with an icon, a soft shadow, port names brighter when wired, parameters
  as a small table. Wires back (feedback, e.g. to the next step of a sweep) run round below the
  nodes instead of through them.
- **Better arranging, and Auto-arrange**: *Arrange* (now in the tool bar of a flow) lines wires up
  port to port, routes long wires past the columns between and orders the nodes of a column with
  as few crossings as it finds; *Auto* arranges the flow again whenever nodes or wires are added or
  removed. Every example and template is arranged anew (none overlaps).
- Fixed: the capture settings opened from the tool bar of a data view showed on a white background.
- **Every example is a starting point; the start page shows them all.** The start page lists every
  project of the example library as a tile, by category, scrollable and with a search; a click opens
  it as a temporary project. The first category, *Start a project*, holds the plain starting points:
  *Empty lab*, *Logic Analyzer* (now an ordinary project that opens a data view with the connected
  device: `open: [data]`), *Data acquisition* (analog inputs of a measuring box recorded until Stop,
  strip charts, a file, an output set from the panel), *Synchronized instruments* (a logic analyzer
  and a DAQ with a drifting clock aligned by a sync signal) and *Remote measuring device* (a device on
  another computer with the script to run there). *Characteristic curve*, *Test bench* and
  *Long-term logger* are examples of *Measurement* and *Data and reports*. *Project → New project…*
  shows the start page; there are no separate templates any more.
- **Streams of analog inputs in flows**: `device.stream` hands on analog channels as blocks of
  volts (`channels: [AI0, AI1]`), also without digital channels; analog-only streams of the
  simulators no longer fail. Inputs named `AI0` or `AIN0` are analog ports.
- **A local DAQ simulator** (`sim:daq`, *Simulation: USB DAQ*): a measuring box on USB in the class
  of an NI USB-6001 - eight analog inputs, two analog outputs, twelve digital lines - whose blocks
  arrive as over USB and whose sample clock runs 30 ppm fast, with a digital loopback (P0.0 → P0.1)
  for the latency, AO0 wired to AI2 and P0.7 free for a sync signal. Simulator profiles take a
  `clock_drift` (ppm); *Signals → USB link and clock* changes it.
- **Smaller text and tighter controls by default**: 10 px text; padding, icons and the rows of the
  waveform follow the text size (*Settings → Appearance*; 12 px gives the earlier spacing).
- **Simulated remote devices with the timings to try.** The device list has the remote DAQ with its
  own drifting clock, with a clock that follows UTC (as with PTP) and with a shared sample clock;
  the *Simulation: timing* box on its *Remote* tab changes the network's latency and jitter and the
  drift of its clock while it runs, and starts it again with another clock. The address keeps the
  settings (`remote-sim:daq?latency=20&jitter=2&drift=300`).
- **A higher priority for reading devices on macOS and Linux** (*Settings → Devices*, off by
  default): openSciLab asks when it starts and raises itself and its device processes with the
  password dialog of the system; on Linux it can be allowed for good. Windows needs no rights.
- Fixed: a black box in the top left corner of some device cards (an empty event list).
- **A Timing tab on the device card.** How the samples of the device are placed and how well (now
  also for captures of the data view, not only of flows), its **sample clock** - own, or shared /
  external, which `timing.align` (new default `drift: auto`) follows -, **Measure** the latency
  with a loopback right there (an output wired to a channel, rate, stream or capture; kept like
  that of `timing.calibrate`), the measured latencies, a **sync output** on a pin (the signal of
  `timing.sync` until stopped) and this computer's NTP/PTP clock.
- **Wires and the USB link of a simulator** on its *Signals* tab: *Add wire…* connects a pin to a
  channel (*Wire them* on the Timing tab adds the loopback at once), *USB link* emulates latency,
  jitter and frames (or not); both are kept per simulator. In real time a simulated capture
  records what the outputs do until it ends, as the device does.
- **The device card has everything about a device in one place.** The extra *Device information*
  window is gone: the *Details* tab shows the device's functions (capture settings, self-test,
  network, bootloader, firmware, *Reconnect*, *Copy the details*), the capture limits, the
  capabilities, board, firmware and connection. New there: *Restart device* for devices that report
  `RESTART` (the simulators; the Pico firmware has no restart command yet - *Reconnect* restarts an
  Arduino, *Restart into the bootloader* a Pico). **Profiles** are in the card's header (load,
  save, standard profiles), which also names the settings of the next capture.
- **Disconnect all devices** at once (*Devices* menu, the button above the connected devices):
  one question names every device and what disconnecting it interrupts.
- **A fresh start when nothing was saved.** The session brings back saved files and the project;
  device cards, data views of devices and the connected hardware no longer come back, and devices
  are only connected again when *Settings → Startup* says so (off by default).
- **Views of a flow's data do not offer to capture.** The data view of *Run data* and of a scope
  node shows search, measure, panels and save - no capture bar, no device, no *Open*.
- **The simulators are one group of the device list again**: also the simulated Arduinos and
  oscilloscopes that speak the real protocols. They are no longer listed as connected hardware.
- **Connected hardware says how firmware gets onto a board**: three steps above the list (plug in,
  with BOOTSEL for a board without firmware; find it; *Install firmware* in its row).
- **Tabs look alike everywhere** (console, device card, dialogs, analysis panels): flat, with an
  accent line under the current one; the white line under the tabs is gone.
- **Plugins add devices without changing openSciLab.** A `.py` file or package in the `plugins`
  folder of the settings directory (or a folder of `OPENSCILAB_PLUGINS`, or an installed package
  with an `openscilab.plugins` entry point) registers a kind of device with its driver:
  `kinds.register("mydevice", open_mydevice, detect=..., process=True)`. The device list then shows
  its devices, flows open it with `address: mydevice:...`, and so do `openscilab-cli` and
  `api.open()`; with `process=True` it is read in a device process of its own, *Simulate* uses the
  simulator profile it names. A plugin with user interface of its own (a backend that asks for an
  address) brings it in `setup_ui()`. A plugin that fails is reported (status bar, *Help →
  Plugins…*, `openscilab-cli plugins`), the others load. `examples/plugins/counter_device.py` is a
  complete example; see `docs/drivers.md`. An address of an unknown kind (`foo:...`) now says so
  instead of being tried as a Pico.
- **Tool bars that fit the window and can be arranged.** The header, flow, panel and data view bars
  (including the view bar above the tracks) give way when the window is narrow: the less important
  entries first show only their icon, then move into a *More* menu (`⋯`) at the end of the bar,
  where they work as in the bar (check boxes, device and other choices, settings menus). Capture and
  Run always stay. A right click on a bar opens *Customize toolbar…* (show, hide and order entries,
  some extra ones such as the align tools of a flow), the display (*Automatic*, *Icons only*, *Icons
  and text*) and *Reset toolbar*; the arrangement is kept per bar. The data view's two bars became
  one, its view bar is a tool bar as well, so a data view fits narrower windows. Buttons have a flat
  tool look with clear hover and checked states.
- **Devices on USB are read in a process of their own**, so nothing the application does - drawing,
  flows, scripts, its garbage collection - can make a stream overflow (the Pico firmware's ring lasts
  22 ms at 6 MB/s). A Pico, Arduino or DSLogic runs in a *device process*: its driver and facets live
  there, its samples come through shared memory, progress and completion come back in order (a busy
  application lags behind, the reading never waits). The device process stamps the start command and
  every block, which places the samples more closely. A device process that dies fails its capture
  with a message and disconnects the instrument; one whose application went away stops the device.
  On by default (*Settings → Devices*); simulators, remote devices and the Rigol stay in the
  application, the Python API opens in a device process with `process=True`. Also: drivers hand
  their events to a notifier thread of their own, the startup heap is frozen for the garbage
  collector, serial ports get a large receive buffer (Windows). The simulated Pico emulates the
  firmware's ring: `tools/benchmark.py stream` shows its stream overflowing under load in the
  application and not in a device process.
- **Decoding never stalls recording and synchronisation.** The protocol decoders run in a process
  of their own (started once, the samples through shared memory, the annotations back in small
  parts); a long decode in the application used to make the threads that receive a stream or a
  remote device wait up to about 100 ms, now as long as when nothing else runs. Cancelling ends it
  at once; a decoder process that died is started again. Annotation segments are tuples the
  garbage collector stops looking at (no long pauses after a large decode). The sync path is
  vectorized: pairing edges (15x), finding the sample of a time (500x), analog sync inputs (5x,
  and noise no longer makes edges before the signal shows its levels). `tools/benchmark.py`
  measures all of it.
- **The time of samples, for every device** ([docs/timing.md](docs/timing.md)). Captures and
  streams of openSciLab's own instruments are placed between the command that started them and the
  arrival of their data (the lower envelope of the arrivals removes the jitter of USB) instead of at
  the moment the flow asked; the device card shows the method and the accuracy (*Timing*). Better:
  - **a sync signal from openSciLab**: `timing.sync` drives a pin with edges at irregular intervals;
    `timing.align` places an instrument on a reference's time by it (offset and drift, or only the
    offset for a shared sample clock), `remote.sync` a remote device - also one that records the
    signal on an input of its own (a spare channel of a DAQ), with the edges placed again with all
    its arrivals. Accuracy: a sample period of the slower recording.
  - **a measured latency**: `timing.calibrate` switches an output wired back to an input and keeps
    the latency per instrument and rate; later captures and streams use it without the wire.
    Device scripts do the same (`input.calibrate()`, `latency=`) and stamp their blocks from when
    they arrive (`input.start()`).
  - **a time scale (PTP, GPS, NTP)**: a device whose clock follows UTC or TAI is converted without
    measuring when this computer's clock follows it too (*Settings → Time*); drivers of local
    instruments that stamp their samples report it the same way.
  - The simulated Pico emulates USB (frames, latency, jitter), so all of it works without hardware;
    `remote-sim:daq` is a DAQ behind USB on another computer (`?shared_clock=1`, `?timescale=utc`);
    `examples/remote/ni_daq.py` is a template for a real NI DAQ; four examples in the category
    *Time*.
- **Standard profiles for the common buses**, loaded without importing: every profile file in the
  new profiles folder (`profiles/` in the settings directory, *Profiles → Open the profiles
  folder*) is there at once. openSciLab puts its standard profiles into it – UART, MIDI, DMX512,
  LIN, I²C, SPI, 1-Wire, CAN, PWM, WS2812, I²S, parallel, SWD, JTAG, IR (NEC), USB and the C64
  expansion port (moved from `examples/c64-expansion-port-profile.json` to
  `examples/profiles/c64-expansion-port.json`) – each with its channels named, a trigger, the
  decoders and the wiring in its notes. *Profiles → Standard profiles* loads one with a click; a
  profile needing more channels than the device has is off (with the reason), and rate and length
  are fitted to the device when a profile is loaded. Profiles from the folder are edited and
  deleted in their own file; a deleted standard profile stays deleted.
- **Flows and their panels find each other.** The flow editor's *Panel* button has a menu of the
  panels that use the flow (open ones and the panel files of the project or the flow's folder) and
  *New panel for this flow*; the flow's inspector lists them too. A panel opens its flow with
  *Open flow* (tool bar and inspector).
- **The simulators of the device list are one group** that opens and closes with a click (it
  remembers how it was left).
- The example library is part of the packaged application (the *Examples* menu was empty there).
- **Capturing happens in the data view.** Its capture tool bar chooses the device (connected
  instruments, or *Connect a device…*) and holds *Capture* (`F5`, also the header's button),
  *Again*, *Stop*, the quick settings, *Settings…* and *Profiles*; the *Capture* tab shows the
  next capture's settings and channels. The template *Logic Analyzer* asks which connected device
  to use. The device card no longer captures: *Capture…* opens its data view, and the functions of
  the device (information, self-test, test signals, network, bootloader, firmware) are buttons on
  its *Details* tab instead of a *Device* menu.
- **A tool bar instead of the device names.** The top bar starts with the new logo of openSciLab
  (a lab flask holding a digital signal; also the application icon) and a button for each basic
  element – *Flow*, *Panel*, *Data view*, *Waveform*, *Project* (from a template) – then *Open*,
  *Save* and *Examples*; the connected devices are in the device list and the status bar.
- **The selection on the time axis shows over every track** (digital, analog, pinned, buses), and
  *Fit selection* (view bar, the ruler's menu, `Ctrl+E`) shows exactly the selected samples.
- **Time axis and tracks line up again:** with annotation rows (or many channels) the ticks of the
  time axis drifted away from the lines of the tracks by the width of the tracks' scroll bar.
- **Fixes of the window:** menus that open from a full tool bar (or from its buttons) and the scroll
  buttons of the document tabs were transparent; the tabs drew stray white lines and cut names to
  a few letters; on macOS the menus of the application (Examples, Devices, …) were replaced by those
  of a data view once it had the focus.
- **Remote devices over IP.** A Python script on another computer becomes a device: with the
  package `openscilab_device` (standard library only, Python 3.8+) it describes its inputs (numbers,
  truth values, sampled signals, events), outputs, commands and sync signals and connects to
  openSciLab (*Settings → Remote devices*, with a token; a beacon lets devices find openSciLab).
  The device appears in the device list with a *Remote* tab (latest values, outputs, commands, how
  well its clock is known) and in flows as `remote:<name>`: `remote.receive`, `remote.set`,
  `remote.call`, `remote.sync`; `gpio.write`, `gpio.read` and `device.monitor` work with it too.
  **Time stays right by itself:** values are stamped on the device; openSciLab measures offset and
  drift of its clock all the time (a fraction of a millisecond on a wired network), outputs are sent
  ahead with the time they should happen, values of several devices are handed on in the order of
  their time. **A sync signal** (an output of the device that a logic analyzer records too) aligns
  it to microseconds. Demo devices (`python -m openscilab_device.demo climate`), simulated remote
  devices (`remote-sim:climate`, `audio`, `echo`) behind an emulated network, four examples, a
  Raspberry Pi template and `docs/remote.md` (with the protocol).
- **Examples menu with a library of 63 examples**, sorted by topic like the examples of the Arduino
  IDE: Basics, Digital I/O, Analog, Protocols, Measurement, Signal generation, Control, Data and
  reports, Panels, Python and Advanced. Each one is a small project with simulated devices (logic
  analyzer, Arduino Uno, Pico, oscilloscope with function generator) that runs without hardware
  and explains itself; it opens as a copy, so the library stays as it is. The examples are also in
  the command palette; `examples/library/README.md` lists them.
- **Fixes found with the examples:** a counter missed the edges between the blocks of a stream
  (200 ms of 1 kHz counted 180); a buffer joins the captures of a stream (it kept only single
  channels); channels with a dot in their name (`B2.GP5` of a multi device) can be wired; a
  simulated capture in virtual time sees outputs the flow sets after it was armed (a pulse train);
  values read better on wires and in logs (the value of a single event, bytes in hex, the fields of
  a bundle, numbers without a unit without an SI prefix).
- **The data of a run in the graph and in data views.** While and after a flow runs, its nodes
  show their data below their parameters: a trace of the latest capture, the latest value with its
  course, the rows of a table; a view node shows what arrives at it. Double-click a node (or *Show
  data* in its menu, or the corner of its data) to open its data in a data view – a table opens as
  a table. *Run data* in the flow's toolbar shows all data of the run in one data view on one time
  axis: the channels of every capture, sampled signals, measurements as lines that step from value
  to value, checks as digital lines. Open data views follow a running flow and the next run.
  *Show data in the nodes* and *Clear run data* are in the Flow menu. Nodes keep room for their
  data, so an arranged flow stays in order when data arrives.
- **Flows are arranged along their wires.** Nodes without a place are laid out from left to right
  in columns, with their real height (no more nodes on top of each other) and with feedback wires
  (a sweep waiting for its measurement) not turning the flow around. *Arrange* (Flow menu,
  `Ctrl+Shift+L`) does it for the selected nodes or the whole flow, as one undo step.
- **The inspector offers what the device has.** Pins come as a list of the pins that can do what
  the node needs (PWM pins for a PWM node, outputs for *Write pin*, reserved pins left out),
  channels and analog inputs are ticked in a menu, generator outputs are chosen from the device's
  outputs. A decoder's channels are chosen one by one from the channels of the capture wired to
  it; a report image offers the columns of its table. The values come from the instruments open in
  the device list and from the simulator profiles.
- **Completions while typing YAML and Python.** The YAML view of a flow offers the sections, node
  types after `type:`, the parameters of a node's type that are not set yet, the values a
  parameter can have (choices, the pins and channels of the device – a node typed just now uses the
  flow's only device) and the ports in the edges (outputs before `->`, inputs after it). The code
  of a Python node is highlighted and completes `ctx.`, `np.`, `signals.`, `units.`, the names of
  the code and its port names in `ctx.emit("…")`, `ctx.receive("…")` and `port == "…"`. The Python
  console completes the attributes of its names. The list opens by itself or with `Ctrl+Space`;
  `Enter` or `Tab` takes a completion.
- **Values that do not fit are found while editing.** A word where a number belongs, a frequency
  given in seconds, a choice that does not exist, a pin the device does not have or that cannot do
  PWM, a channel the capture does not record: each is a warning at its node, in the inspector and
  in *Problems* (the flow still runs – a project may use another device).
- **A panel from the graph.** *Panel* in the flow's toolbar (and the Flow menu) makes a front
  panel for the flow – also for the unsaved flow the application starts with, which the panel then
  uses as it is edited; saving the panel saves the flow first. A new panel starts with suggested
  widgets: a start button of a sweep, a slider for the duty cycle of a PWM, inputs the flow needs,
  numbers and LEDs for measurements and checks, scopes and charts for what the flow shows.
- **The template flows are laid out and the test bench passes.** Every node of the templates has
  its place; the test bench captures long enough to find the UART text wherever the capture
  starts (it failed in real time) and writes its report when the flow ends, with both checks. The
  empty template shows its device as a node.
- **Pico boards with firmware 8 are full instruments.** The device card, flows, scripts and the
  command line get their pins (the board's pin table, reserved pins with the reason), outputs,
  pulses and pulse trains timed by the board, PWM with the frequency the board makes, the monitor,
  analog inputs A0–A2 in captures and streams, the pattern generator, the square wave of GP22,
  UART/SPI/I²C sent by the board and the live state stream. A pulse can be triggered while a
  capture waits for its trigger on the same board. A loaded pattern and analog channels share the
  capture memory, so the capture settings offer fewer samples then.
- **Arduino boards** (`arduino:<port>`) with the openSciLab Arduino firmware: Uno, Nano, Mega 2560,
  Uno R4 Minima, ESP32 and ESP32-S3. They are found by their USB identifiers and offer stream,
  buffer and live state captures with analog channels, GPIO, the monitor, the pattern generator,
  square and arbitrary waveforms (DAC on the R4 and the ESP32) and UART/SPI/I²C. A simulated board
  speaks the same protocol (`arduino-sim:uno`, *Simulation: Arduino Uno (firmware protocol)*).
- **Rigol DHO900 oscilloscopes** (`rigol:<address>`): D0–D15 and CH1–CH4 in one capture with edge,
  pattern or no trigger and adjustable logic thresholds, and the built-in generator from the
  waveform document (standard waveforms, arbitrary points, sweep, burst). With the openSciLab
  bridge app on the instrument a large capture is shown at once and its samples follow tile by
  tile, the visible range first; an interrupted transfer resumes; the *Cache* tab of the device card
  lists the captures the app keeps. Oscilloscopes with the app appear in the device list by
  themselves. Without the app the driver reads the instrument directly, more slowly.
  `rigol-sim:bridge` and `rigol-sim:direct` simulate one. The SCPI commands are provisional until
  checked on an instrument.
- `tools/dho_la_probe.py` records what a DHO900 answers to the commands the driver needs.
- Pulses take a count and a period (device card, flow node `gpio.pulse`, simulators).

- **Outputs stay as they were set.** The driver keeps a device's watchdog satisfied while it is
  connected – not the device card: an output a flow or a script sets no longer falls back to an
  input after a second (it did on the simulated Arduino). Disconnecting asks while any pin is an
  output, whoever set it.
- **Sending UART, SPI and I²C with the device's own hardware.** Devices that can (capabilities
  `TX_UART`, `TX_SPI`, `TX_I2C`) send through the generator facet and give back what a target
  answers: the bytes on MISO, or the bytes an I²C device returns after a write. The nodes
  *Send UART/SPI/I²C* use it (new output *received*, I²C parameter *read*, SPI parameter *miso*)
  and play a pattern on devices without it; the device card has a new tab *Send*. The simulated
  Pico, Uno and Uno R4 can, with a temperature sensor at I²C address 0x48; the Uno got a pattern
  output (D2–D7) and a square wave output (D9).
- Devices get their facets (GPIO, monitor, analog inputs, generator) from what their driver
  offers, the same way in the device list, in flows, on the command line and in scripts.
- **A second check of the review.** Saving a capture file keeps the form it has (a file of
  the original software was silently packed by *Save*). The dialog shown while a file is
  written can no longer be closed (closing it ended in an error and left the document
  "unsaved"); data edited or captured while a file is being written stays marked as unsaved.
  Files are written through links, keep their permissions, respect write protection and may
  have long names again. A flow gives the Ctrl+C handler of the program around it back. A
  decoder channel assigned to a capture channel that a profile or the capture does not have is
  kept instead of being dropped by saving the profile or pressing *OK* in the decoder options.
- **Command line and smaller corrections.**
  - `capture` checks the ending of the output file and its options before it captures (a
    capture was thrown away for `-o x.txt`). Errors of damaged files, flows that cannot be
    read and devices are one line instead of a traceback (`OPENSCILAB_TRACEBACK=1` shows it).
  - **`capture --duration 5m` is 5 milliseconds now**, as in flows and `run --duration`
    (it was 5 minutes there and 5 ms here); minutes are `5 min`.
  - Analog to digital with hysteresis is computed without a loop (2 million noisy samples:
    0.6 s → a few milliseconds).
  - Patterns (SDL): `//` inside a string is no comment, `\\n` is a backslash and an n, and a
    repeat count that would make a pattern longer than 64 million samples is refused instead
    of hanging.
  - Removed what could not be reached or was not used: the help menu of a data view (the
    window has it), a few helper functions.
- **Scripts and the command line without the user interface.** No module of the core, the
  drivers or the lab loads Qt any more (the colours of the waveform moved to the user interface,
  a region holds its colour as four numbers): `import openscilab.api` takes 110 ms instead of
  195 ms, and the lab is loaded when a script first uses it.
- **Start-up.** The theme *Follow the system* asks the application for the colour scheme (it
  was always dark on Linux and started a helper program on macOS); the style sheet is built
  and applied once instead of three times. Restarting after a change of the theme opens the
  documents again (the session was saved a second time, empty).
- **Drivers: what happens when two things happen at once** (checked against simulated
  transports, not on hardware):
  - Pico: asking a board for its capabilities or details while a capture starts no longer
    writes the question into the capture and reads samples as the answer; a stopped capture
    leaves no reader behind that could take the first bytes of the next one. A set of boards
    sends the bootloader command to every board (it stopped at the first that did not answer)
    and reports the result of a capture without holding its lock.
  - DSLogic: every capture has its own stop flag, so a capture started right after one was
    stopped is neither read nor ended by the reader of the old one. A stream the computer
    cannot keep up with ends with a message instead of filling the memory.
  - Software trigger: a second capture started while one waits for its trigger is refused
    without taking away the watch of the first (its endless stream would never have stopped);
    analog channels with a rate of their own are cut at the right place.
  - Simulator: stopping a capture no longer interrupts the transfer of the capture before it.
  - Scripts: `Device.capture` waits until all samples of a capture that arrives piece by piece
    (an oscilloscope over the network) are there; saving or decoding it at once saw zeros.
  - The window no longer stutters every few seconds while the serial ports are listed (read in
    a thread now), and writing a firmware with avrdude, bossac, esptool or adb no longer
    freezes it for minutes.
- **The data view stays quick with large captures** (measured with 20 million samples on 24
  channels and 47,000 annotations):
  - A change of the data – an edit, an undo, a renamed channel – blocked the window for 2.1 s;
    now 20 ms. Moving a channel: 2.2 s → 60 ms. The index of the edges and the values of the
    buses are kept for the channels whose samples did not change, the rows of the channel
    column are updated instead of built again, and the statistics of the *Measure* tab are
    read from the edge index, only while the tab is shown.
  - Annotation rows with more entries than pixels are drawn as blocks: 640 ms → 5 ms per
    picture, moving a cursor over them 65 ms → 3 ms. An entry that began far before the view
    (a frame around many bytes) is drawn; it could be missing.
  - Bus rows zoomed out: 245 ms → 7 ms per picture, pixel for pixel the same.
  - Dragging a cursor no longer computes minimum, maximum and RMS of the analog channels over
    the whole range at every step.
  - The search runs in the background and can be cancelled; the filter of a long annotation
    list waits for a pause in typing, and an entry that spans the whole row no longer makes
    every lookup search the row from its start.
  - Undo keeps as many steps as fit into 2 GB of replaced samples (4 for that capture, 50 for
    small ones) instead of 5 steps of any size.
  - The preview of *Play as signal* is drawn from the runs of the pattern, not from every
    sample.
  - States shown on their real time: the regions and markers of the states wait for the way
    back instead of marking wrong times, and saving writes the states, not the picture of
    them. A state analysis no longer carries the regions of the timing capture.
- **Capture files: small, quick, any size.**
  - `.lac` files hold the samples packed (8 per byte, compressed) instead of a number per
    sample: a capture of 5 million samples on 24 channels is a file of 0.05 to 20 MB instead of
    360 MB, saved in 0.1 s instead of 5 s and opened in 0.1 s instead of 7 s with a gigabyte
    less memory. Captures recorded to disk that did not fit into memory can be saved and
    opened now. Every file written so far still opens.
    **The original LogicAnalyzer software cannot open the packed files**: *Save as → Captures
    for the original LogicAnalyzer (\*.lac)* (in scripts `Capture.save(path, compatible=True)`)
    writes its form, a number per sample. A file that has that form keeps it when it is opened
    and saved again; only *Save as* changes the form.
  - Opening, saving and exporting run in the background with a small dialog; the window stays
    usable and a second save cannot start meanwhile. A file that is damaged or cut off is
    reported as such in every case (some errors ended silently).
  - CSV and VCD exports are written block by block: 2 million samples on 16 channels in 0.06 s
    instead of 3.2 s (CSV) and 0.75 s instead of 1.9 s (VCD), without a copy of the capture in
    memory. A channel name with a comma is quoted in the CSV; a VCD never contains `nan`, and a
    capture without samples is refused with a message.
  - `.lac.gz` is compressed at level 6 (level 9 took much longer for a few percent).
- **A running flow no longer floods the window.**
  - What the engine reports is collected and shown 25 times a second: the last state of every
    node, the values of every output as a batch (charts of panels and the sparklines of the
    wires still get every value), the log, the views. A flow in virtual time sends hundreds of
    thousands of values a second; each one used to be a signal, a search through all wires and
    a repaint of the scene and the minimap.
  - A value in a chart, number or table no longer rebuilds the menus, the inspector and the
    project lists of the window (it did so for every value); neither does an undo step or a
    keystroke in a document. The data list of the project is read when the folder changes and
    when a run ends.
  - The scope of a flow keeps the zoom and position while more of the signal arrives.
  - Charts: thousands of points are drawn as their envelope (two points per pixel column);
    a value that is no number (a measurement without a result) no longer blanks the chart of a
    panel; zooming in very far no longer freezes the window; numbers, LEDs and charts of a
    panel show the last sample of a block of samples (an LED was always on for one); tables
    add their new rows instead of being filled again.
  - Flow editor: the inspector is replaced, not collected (every click left one behind), and
    typing the name or description of the flow keeps the cursor; the ports of the nodes are
    only built again when they change; search popups, the command palette and detached
    windows are freed; closing a document whose flow still runs no longer prints errors.
  - A control of a panel bound to a node the flow no longer has is skipped and named in the log
    instead of stopping the start of the run.
- **Flows that run for days.**
  - Memory no longer grows with the run: the engine keeps what every view shows now instead of
    everything it ever showed (the *Long-term logger* template took 750 MB after 60,000 values,
    now a constant 80 MB, and runs twice as fast), and the last 10,000 lines of the log.
  - Timers and the monitor keep their period (the n-th tick at n × interval; the time a tick
    took no longer adds up), a flow in real time wakes a hundred instead of a thousand times a
    second, and `duration` also ends a flow whose stream is still open.
  - The logger names the source of every row (the pin, e.g. `A0`, instead of its own name),
    writes to disk after `flush` rows or five seconds, logs the samples of analog and digital
    blocks, and the rows of a growing table once each.
  - Reports render each section, table and diagram once when they are written instead of on
    every value; diagrams of long captures show their envelope; **PDF reports contain the
    diagrams** (they were missing).
  - Two flows could not be stopped: a sequence that only sets, emits or logs with `repeat: 0`,
    and an endless timer with interval 0 (now refused). Nodes that wait for each other to take
    their values end the flow with the error *deadlock* instead of "finished".
  - Pins, PWM, voltages and the monitor of a real device are called in a thread, one call at a
    time per device: a device that is slow or does not answer no longer holds the other
    nodes, *Pause* and *Stop*.
  - Smaller: text parameters of several lines keep their line breaks exactly when saved;
    parameters may be called `position` or `node_id`; a function node with a `params`
    argument validates; a capture rate below 1 Hz is refused with a message; *Browse…* stores
    the path the flow resolves to the chosen file, also in subfolders of the project.
- **Decoders: about eight times faster, and safe to close.**
  - Waiting for an edge or a level is looked up in an index of the channel's edges instead of
    searching 65,536 samples each time: a UART capture of 2 million samples decodes in about
    3 s instead of 26 s, with identical results.
  - Closing a data view (or the application) while it decodes no longer risks a crash: the
    decoders are told to end and the view lets go of them. A new decode requested while one
    runs ends the old one instead of waiting for it.
  - An edit of the samples or an undo decodes once, not twice; the options dialog can be used
    while a decode runs without mixing old and new settings.
  - Flows and data views share one set of decoders (the decoder folders of the application
    apply to flows as well), loaded once. The node palette imports the decoders only when its
    *Decoders* group is opened or searched, so the window starts faster.
- **Review of the code base: nothing is lost or silently wrong.**
  - Decoders keep reading their channels when the channels are reordered (dragging a channel
    made them decode another one), also after the next capture and with profiles that capture
    only some channels. A decoder assigned to a channel the capture does not have says so.
  - A flow that ends with an error, with Ctrl+C or by *Stop* still writes what it measured: the
    rows of a logger, the recorded samples and the report (which then says what failed). *Stop*
    pressed right after *Run* is no longer lost.
  - Subflows with a required input run (*Make subflow* produced flows that could not start),
    and a subflow may use the other subflows of its flow.
  - Captures, profiles, waveforms, projects, panels, flows and reports are written to a
    temporary file that replaces the old one only when it is complete: a crash or a full disk
    no longer destroys the file that was there. A settings file that cannot be read is kept as
    `<name>.bad` instead of being overwritten, and one unreadable profile no longer takes the
    others with it.
  - Markers and pinned channels are saved in the capture file and come back when it is opened.
  - A selection in the ruler is cleared when other data is shown (cut or delete acted on the
    old positions); shifting channels of different length, inserting samples with an empty
    channel and search hits after an edit are right.
- **Simulators: several at once, multi device sets, and what they simulate.** Every simulator
  can be connected again and again (`sim:uno`, `sim:uno#2`, …), each an instrument of its own.
  *Simulated multi device…* combines 2 to 5 simulated boards into one device captured together
  (`sim:pico*2`: 48 channels), like a multi device set of real boards. The new *Signals* tab of
  a simulator's device card says what it simulates: a UART, SPI, I²C, all three, a counter, the
  **C64 bus** (a Commodore 64 at its expansion port, on the channels of the profile *C64
  expansion port*; needs 48 channels), the channels of a capture file, or nothing – and any
  source on a single channel (double-click it). It applies at once, names the channels for the
  next capture, is kept per device and comes back with the session. In a flow the device node
  takes it as `signals`. A pin of a simulator shows its signal again after it was an output.
- **Device handling: more reliable and faster.**
  - Opening a device no longer freezes the window: boards, network devices and the DSLogic are
    opened in the background with a small dialog (*Cancel* stops waiting), also when the session
    reconnects its devices at startup; the power of a network device is asked in the background.
  - Stopping a capture that waits for its trigger ends it everywhere (card, data view, header);
    stopping never blocks the window.
  - A device that stops answering (a failed capture or no answer over the network) is marked
    with an error and its card offers *Reconnect*; a network board that vanished is noticed
    through TCP keep-alive instead of a capture waiting forever.
  - Pico driver: a board still streaming from a program that crashed is resynchronised on open;
    stopping and starting again at once no longer lets the old capture end the new one; a board
    that does not answer the capability question is not asked again for every property (each
    time 5 s); device details are read once; serial ports are opened exclusively; the timeouts
    of the identification follow the connect timeout.
  - Flows leave the devices of the device list tidy: a capture or stream they started is
    stopped when the flow ends early, generators stop also after an error, and simulators wired
    for the run are wired as before.
  - The simulator: a failing source ends the capture with an error instead of leaving the
    device "capturing" for good; wiring loops are refused; stopping a capture that waits for a
    trigger works at once (it could take 10 s) and no longer spins a core; endless streams with
    analog channels use constant memory; state captures sample thousands of times faster;
    sources are read consistently while outputs change in another thread.
  - Less work in the background: firmware images and simulator profiles are read once, serial
    ports are listed once per look, the overview of the connected hardware only scans while it
    is shown, and the pin values of a fast monitor update 30 times a second.
  - A listener of the device hub that fails no longer breaks adding or removing devices; a
    removed device takes its capture controller, handlers and timers with it; closing a device
    card leaves the monitor of a running recording alone; closing the hardware overview while
    a version is being read no longer risks a crash.
- **Polish.** Shortcuts are written the way the platform does (⌘K on macOS); keys without
  Ctrl/Cmd only act in a data view that has the focus; *Measurements* moved to Ctrl+Shift+M
  (Cmd+M minimises on macOS); Esc means *All outputs safe* on the device card's *Pins* tab only;
  the shell's shortcuts and the document's menus work in documents opened in a window of their
  own; device card and panel commands are in the command palette; *Help → Keyboard shortcuts*
  is a searchable list with the keys and gestures of the views. The data view's view menu is
  called *Display*; file dialogs say *Captures*; saving with *Compressed captures* adds
  `.lac.gz`; code and logs use the system's fixed-width font. The device card, the data view
  and the side panels need less width, so a card and its data view fit side by side.
- **Capture settings in pages; accessibility.** The capture settings are pages – *Sampling*,
  *Channels*, *Trigger* – with a line below that says what the capture will be; a problem opens
  the page where it is fixed and marks it. Every button that shows only an icon has a name for
  screen readers (from its tooltip), the flow graph and the waveform describe what they show,
  and the status dots are sharp on high resolution screens.
- **Panels arranged with the mouse, charts that zoom.** The panel editor draws its grid with free
  rows below the widgets: drag a widget from the palette onto a cell, drag widgets to move them
  and their corner to resize them (a taken place is shown red and refused); every step can be
  undone. The Port list offers the ports of a type the widget shows, Tab and Group are chosen
  from the existing ones (or typed new), a widget whose port does not exist is marked, and Edit
  and Operate are toggles in the toolbar. Charts and the plots of panels zoom with the wheel or a
  pinch (Shift: values only), move with two fingers or by dragging, and go back to the automatic
  range on a double-click or smart zoom.
- **Undo in the data view, channels in any order.** Deleting, inserting, pasting and shifting
  samples, derived channels, renaming, colours, hiding, the order of channels, markers, regions
  and buses can be undone and redone (Edit → Undo/Redo, Ctrl+Z); large captures keep fewer
  steps. Cut, copy, paste and delete of the selected samples are in the *Data* menu with their
  usual shortcuts, as are *Insert samples…* (now also for files and test signals) and *Shift
  channels…*. Channels are reordered by dragging their name or with *Move up/down*; the order
  is saved with the capture. The button above the channel names says how many channels are
  hidden and shows them again one by one or all at once. Regions can be renamed and recoloured
  (*Markers → Edit…*). *Data → Use these channel names for the next capture* hands names given
  in the data view to the device.
- **The last session comes back.** openSciLab opens the documents of the last session again –
  files, device cards, data views of a device, the start page, the connected hardware – in their
  groups and side by side, with the active one in front, flows and captures zoomed as they were,
  and the console as it was; the devices are connected again. Files that are gone are left out
  (noted in *Log*), devices that are not there are named in the device list. Unsaved documents
  are not kept. *Settings → Startup* turns this off and chooses whether the start page shows. A
  window saved on a screen that is no longer there opens on one that is.
- **Settings and a light theme.** *Project → Settings…* (Cmd+, on macOS) sets the theme (dark,
  light, like the system; applied after a restart the dialog offers), the text size, what happens
  at startup, whether the mouse wheel zooms or moves and in which direction, the folder for data
  and new projects, the number of recent entries, how often devices are looked for and whether
  simulators are listed. The light theme covers the shell, the documents, the flow graph,
  comments, charts, LEDs and the pin strip; the waveform display stays dark like an instrument
  screen. Text keeps a contrast of at least 4.5:1 in both themes.
- **A friendlier flow editor.** While a wire is dragged, the ports it fits light up and the others
  fade; it snaps to a fitting port nearby or to the right port of the node it is dropped on. A
  wire dropped on the empty canvas offers the nodes it fits (placed there and wired), a wire that
  does not fit says why above the graph and offers the conversion node. Right-click a wire to
  insert a node into it or remove it, a port to add a connected node or disconnect it. Copy,
  cut, paste, duplicate (Ctrl+D) and select all work on nodes with their wires; new nodes no
  longer land on top of each other; Tab or *Add node* in the toolbar opens the node search, and
  an empty flow says how to begin. Required inputs without a wire are marked, nodes show a badge
  for their problems and their type next to their name, the breakpoint dot shows on hover.
  Adding the first device wires the nodes waiting for one (the status bar says what was wired).
  Group frames carry the nodes inside them, are grabbed by their title and edge and resize at
  their corner (comments too). The inspector marks required parameters, lists the node's
  problems, offers the pins of the wired device and *…* to choose a file, says when changes
  apply only to the next run, keeps multi-line descriptions and checks seed and duration. Less
  used tools in the flow toolbar show their icon only; the inspector wraps instead of cutting
  text off.
- **Clearer ways through the application.** The start page comes first with *Try a simulator*
  (a simulated device, its card open; its first capture shows data at once) and *Connect a
  device*; with every document closed the area offers a new flow, connecting, opening a file or
  the start page. *Devices → Connect…* lists detected boards, network and multi devices and the
  simulators in one dialog; the Devices menu reaches the card, the data and *Disconnect* of every
  open device, *Install or update firmware…* and outdated firmware lead to *Connected hardware*,
  and a device whose firmware was installed there opens again by itself. Disconnecting asks while
  a capture runs, outputs are driven or a flow uses the device; an unplugged or disconnected
  device card offers *Reconnect*. The first capture opens its data view beside the device card,
  the header's *Capture* / *Capture again* / *Stop* work for device cards and data views, and the
  card shows the state of the capture. Connection errors, failed captures and flows that cannot
  run appear as a banner where they happened (a run without its device offers *Run with
  simulators*); *Check* and running a flow open the console, the status bar counts the problems
  and a double-click on a problem shows its node. *Use settings* (formerly *Apply*) keeps the
  capture settings, Enter in a channel name moves to the next name, and the card's *Profiles* is
  the only profile menu. The Project menu has *New project from template…*, *Open project…* and
  recent projects; the Project sidebar lists the flows, panels and waveforms of the project.
  Untitled documents are numbered (*Untitled flow 2*), a data view names its device, Ctrl+Tab
  switches tabs, and *Split* needs a second document. The Devices menu's mnemonic is Alt+V
  (Alt+D is the data view's *Data*).
- **Data, device and project fixes.** Every data view has its own decoders (adding one no longer
  changes the others). The data view of the *Logic Analyzer* template receives the first capture.
  Opening a file or test signals over unsaved data asks first; renaming channels, colours,
  markers, regions and buses count as unsaved changes; a capture never replaces data the user
  changed (it opens in another data view); *Capture again* uses the settings of the last capture,
  not samples edited since; a capture whose data view was closed opens in a new one. The device
  card shows the settings of the *next* capture, its capture bar works for simulators, and
  loading a profile sets the next capture of this device only (and updates the bar). A status
  change keeps switches and values in the pin table, a declined switch goes back, and the
  *Monitor* box follows the monitor (also when a recording starts or stops it); a new rate
  applies at once. Devices found by autodetect, over the network or as a multi device carry
  their real address, and unplugged devices are shown as disconnected. *Choose an image…* no
  longer restarts the port of an open device behind the application's back.
- **Projects and closing.** Closing the last document of a temporary project asks whether to
  keep it; its files no longer fill the recent files. *Save as* in a temporary project saves the
  project first, and *Save project as* lets you name the project folder. Closing the window asks
  once for everything unsaved, and *Cancel* closes nothing. Opening a project without flows or
  panels says so instead of doing nothing. The status bar shows real or virtual time of the
  active flow or panel and which documents are unsaved; the window title marks unsaved changes.
  The sidebar stays as you left it at startup. Messages point to the right menus (*Data → Align
  boards*, *Devices → Connected hardware*).
- **Flow and panel editor fixes.** A double-click in the node palette adds one node (not two);
  a new device node starts with the default device instead of failing when the flow runs; a click
  on a node no longer marks the flow as changed, and two moves are two undo steps; saving a flow
  opened from a Python script saves it as `.flow.yaml` instead of overwriting the script; YAML
  with an error is kept when switching views (keep editing or discard), and the edits of one visit
  of the YAML view are one undo step; an error tooltip goes when the node is idle again; Run is
  available again as soon as a run has ended; renaming a node keeps the ports of a subflow; the
  palette shows the node types of the active flow's project. The panel inspector keeps the
  cursor while typing, steps of one setting are one undo step, Redo is enabled after Undo, and a
  panel runs the current flow (the open flow document with its changes, or the file read again
  when it changed). Save dialogs without a project start in `~/Documents/openSciLab`.
- **Trackpad and pinch in the flow graph.** Two fingers move the canvas instead of zooming it,
  pinch zooms at the fingers, a two-finger double tap fits the flow, Cmd + swipe zooms; the mouse
  wheel zooms at the pointer (Cmd + wheel sideways, Shift + wheel up and down). The zoom stops at
  its limits instead of ignoring the step, far nodes stay reachable, *Flow → Zoom in / Zoom out /
  Fit (Ctrl+0) / Actual size* are new, and switching to YAML and back keeps zoom and position.
  The overview can be dragged and passes wheel and pinch on to the canvas.
- **The same navigation in every part of the data view.** Analog tracks and buses follow the
  trackpad instead of zooming on every swipe, pinch works over the ruler, buses and analog tracks,
  Cmd + swipe zooms the waveform, and the overview below the waveform follows the trackpad. The
  panel editor scrolls also with the pointer over a widget.
- **Clearer device card.** The card has tabs: *Capture* (settings of the next capture, Start,
  the channels with their names), *Pins* (pin, channel, name, mode, value, actions; PWM and DAC
  controls on lines of their own), *Events* (simulators) and *Details*. Device information,
  self-test, network, bootloader and firmware moved into the *Device* menu of its header. A
  declined or failed switch goes back to *Off*.
- **Apply in the capture settings.** *All settings… → Apply* keeps the settings without capturing;
  the channel names appear in the card's channel and pin tables at once.
- **Connected hardware.** *Devices → Connected hardware* lists every board and device on the
  computer with its firmware and whether it is current. *Install/Update firmware* picks the image
  that fits the board, restarts it into its bootloader, writes the image and waits until it is
  back; *Choose an image…* keeps the manual dialog.
- **PiPiLogicAnalyzer is now openSciLab.** The Python package is `openscilab`, the commands are
  `openscilab`, `openscilab-cli` and `openscilab-gui`, the settings live in the folder
  `openSciLab` and the environment variables are `OPENSCILAB_SETTINGS_DIR` and
  `OPENSCILAB_DECODERS`. The old names are gone (no aliases, settings are not taken over). The
  packaged applications are called `openSciLab-<version>-<platform>`. The Pico firmware keeps
  its name until the next firmware release.
- **New lab shell.** The application opens in the openSciLab shell: an activity bar and sidebar
  (project, devices, search), documents in tabs that can be split side by side or below each
  other and opened in a window of their own, an inspector on the right, a console below
  (problems, execution, log and a Python console with access to the shell) and a command palette
  (Ctrl+K) that reaches every menu entry, with fuzzy search and the recently used commands first.
  A start page lists recent files and the *Logic Analyzer* template. Layouts can be saved and
  restored (View → Layout).
- **The logic analyzer is a document.** Everything the main window did is unchanged inside an
  analyzer document: its *Capture, Device, Analyze* and *Profiles* menus appear while it is
  active, its view menu is called *Waveform*. Several captures can be open at once. A capture
  with unsaved changes is marked in its tab and closing it asks first. Files given on the command
  line open in analyzer documents.
- **Several devices at once.** Opened devices are instruments in the device hub: the sidebar
  section *Devices* lists the open ones with their status and the ones that can be opened, the
  header shows a chip per device, and each device has a device card (capabilities, pins,
  connection). An analyzer document captures with a device of the hub; closing the document
  keeps the device open for others, *Disconnect* closes it.
- For scripts and later steps: typed signals (`openscilab.core.signals`: digital, analog,
  scalar, bool, event, states, capture, table, with time bases and conversions), instruments
  with facets (`openscilab.core.instrument`) and the hub with trigger routes and time base
  offsets (`openscilab.core.hub`).
- **Flows.** A measurement program is a flow: nodes (devices, timers, sweeps, tables, files,
  views) and the wires between them, stored as `*.flow.yaml` or written in Python with the DSL
  of `openscilab.lab` (`with flow(...) as f: n.device.capture(...) >> n.data.file(...)`); both
  convert into each other unchanged. The engine runs flows in real time or, with simulators, in
  virtual time (fast and deterministic), with back pressure, breakpoints and single steps.
  Projects are folders with `project.yaml`, `flows/`, `data/` and own nodes in `nodes/`.
- **`openscilab run <flow> [--sim] [--fast] [--device name=address] [--report out.html]`** runs a
  flow without the window, `openscilab sim` lists the simulator profiles; `openscilab capture`
  and the other commands of `openscilab-cli` work as `openscilab <command>` too.
  `api.run_flow()` does the same from scripts.
- **Simulated instruments** (`sim:free`): a 16 channel logic analyzer with test signals
  (counter with clock, UART, SPI, I²C, square wave), buffer captures with edge and pattern
  triggers and streams, with the limits of its profile. `api.open("sim:free")` opens it.
- **Visual flow editor.** A flow document has three views: *Graph* (nodes and wires on a canvas,
  wires coloured by type, minimap, zoom and pan), *YAML* (the file itself, editable) and *Python*
  (the same flow as a DSL script). Nodes come from the palette in the sidebar (search, drag and
  drop) or by typing on the canvas; the inspector edits their parameters. Wires whose types do
  not fit are refused, with an offer to insert a conversion node. Undo/redo for every edit,
  comments, groups, alignment, subflows (opened as documents of their own). Running a flow shows
  the state of every node and the latest values on the wires (a sparkline for numbers);
  breakpoints and single steps work in the graph, scope nodes open analyzer documents.
- **Node library:** measurements (frequency, period, pulse width, duty cycle, min/max/mean/RMS,
  peak to peak, counts, setup/hold), signal processing (threshold with hysteresis, debounce,
  FIR/IIR filters, FFT, derivative, integral, averaging, resampling, expressions), every sigrok
  decoder as a node (`decode.uart`, `decode.i2c`, ...), control (sequences without code, state
  machines, Python nodes, compare, limit, counter), data (tables, buffers, reading and writing
  captures and CSV, long-term logger on disk), views (scope, strip chart, XY chart, spectrum,
  table, number, LED, log – each opens a document while the flow runs), bundles. Projects add
  their own nodes as Python functions in `nodes/`.
- **Analog channels.** Devices with analog inputs capture them together with the logic channels;
  the analyzer shows them as tracks below the digital channels (envelope zoomed out, line and
  points zoomed in), with their value at the pointer and at the cursors, min/max/mean/RMS/peak to
  peak in the measurements, and a digital channel from a threshold with hysteresis. `.lac` files
  keep them (older files load unchanged), sigrok sessions for PulseView carry them as analog
  traces, CSV exports them in volts and VCD as `real`.
- **Large captures arrive progressively:** an overview of the whole capture at once, the samples
  in full resolution after it, the part on screen first; saving, export and decoders wait for the
  rest; an interrupted transfer can be resumed.
- **Live:** roll mode (the view follows the newest samples or holds still), analog channels and
  losses while streaming.
- **State captures with times:** the time of every state is kept; the view shows the states one
  by one (numbered axis, cursors with state and time) or on their real time. State mode on a
  stream (live state) and software triggers on states; the clock input is chosen from the pins
  that can clock.
- Simulator: analog sources (sine, triangle, ramp, noise, sums), state captures, the provisional
  profile `sim:dho924s` (4 analog and 16 logic channels, progressive transfer).
- **Controlling pins from the device card.** Devices with GPIO show their pins as a strip and a
  table: mode per pin (input, pull-up, output, PWM, analog), switches, pulses of a set width, PWM
  and voltages of analog outputs; reserved pins are greyed out with the reason. The first output
  on a pin asks once about its level. *All outputs safe* (Esc) turns every output back into an
  input. While outputs are driven the card keeps the device's watchdog fed. The event log lists
  what the device did.
- **Monitor:** the card reads the inputs periodically (levels and voltages with a short history);
  *Record* records the monitor as a slow capture in a new analyzer document.
- **Markers for actions:** everything set on a device while a capture runs is a marker in that
  capture (at once in a live display, placed when it completes otherwise, marked with ≈ when the
  position is estimated).
- **Stimulus and capture:** the card arms a capture with any connected instrument (also another
  device) and then pulses the selected pin.
- GPIO nodes for flows: `gpio.write`, `gpio.pulse`, `gpio.pwm`, `gpio.read`, `gpio.dac` and
  `device.monitor`.
- Simulator `sim:uno`: an Arduino Uno with GPIO latency, exact pulses, a watchdog, D7 wired to D3,
  the PWM of D9 through an RC low pass to A0 and a bouncing push button on D2; faults can be
  injected (disconnect, delay, restart). Buffer captures of the simulator wait for their trigger
  in real time, so a stimulus after arming is captured.
- **Signal generation.** A waveform document builds what a generator output plays: standard
  shapes (sine, square with duty cycle, triangle, ramp, pulse, DC, noise) with sweep and burst,
  arbitrary waveforms from a formula or a CSV file, and digital patterns as SDL per pin with a
  preview of the tracks. It lists the generator outputs of all connected instruments, explains
  what does not fit an output (frequency, voltage, points, rate – a too fast pattern can be
  limited to the output's rate) and starts and stops it. Waveforms are saved as `*.wave.yaml`;
  `*.sdl` files open too.
- **Play as signal** (Analyze menu of the analyzer): digital channels of a capture as a pattern,
  or an analog channel as an arbitrary waveform, between the cursors if they are set.
- Generator nodes for flows: `gen.waveform`, `gen.arbitrary`, `gen.pattern`, `gen.tx_uart`,
  `gen.tx_spi`, `gen.tx_i2c`, `gen.replay` and `gen.output`; their `sync` output marks the start.
  The capture node reports `armed` once it waits for its trigger.
- Simulator: `sim:free` has a pattern generator on D0-D7 (looped back to the inputs) and a
  trigger input on D15, `sim:dho924s` a 25 MHz generator output on CH1 with its sync on D15;
  trigger routes of the hub between simulated instruments are wired (a sync output triggers a
  capture on another instrument).
- **Panels.** A panel document (`*.panel.yaml`) is the front panel of a measuring station:
  switches, push buttons, sliders, inputs, choices, numbers, LEDs, charts, an embedded scope and
  labels on a grid with tabs and groups, each bound to a port of a flow (inspector). In
  *Operate* the controls send their values into the running flow and the displays show its
  outputs; F11 shows the panel full screen as an application of its own. Undo/redo for every
  change of the layout.
- **Project data:** the project section of the sidebar lists the captures, tables, logs and
  reports of the project's `data/` folder; captures open in the analyzer, two of them can be
  compared, one exported as CSV; tables and reports open in their system application.
- **Reports:** nodes `report.section`, `report.table`, `report.image` (XY diagrams and traces as
  SVG), `report.check` (limits or an expected value, passed or failed) and `report.write` (HTML,
  or PDF). `openscilab run --report` puts the same parts into its report, and its exit code is 1
  when a check failed.
- New template *Characteristic curve* (start page): a PWM duty sweep on the simulated Uno
  measured behind an RC low pass, with a panel, a report and a check of the slope. Templates are
  projects copied to a folder you choose; recent projects open from the start page.
- `data.table` keeps the values of each column in order, so a column that runs ahead no longer
  overwrites values before their row is complete; `control.sweep` can be started by a value at
  `trigger` (a start button).
- **Simulators complete:** profiles `sim:uno`, `sim:uno_r4` (DAC on A0 wired to A1), `sim:pico`
  (24 channels, depth by the number of channels, ADC, pattern generator), `sim:dho924s` and
  `sim:free`, as JSON files in `examples/sim/` (own profiles in the settings folder `sim`). The
  device list of the sidebar offers every profile (*Simulation: …*); simulators opened there run
  on the clock of the hub. *Devices → Simulation* injects faults (disconnect, delay, overflow,
  restart), scripts use `device.inject()`; `api.instrument("sim:uno")` gives an instrument with
  all its facets. A project wires simulated circuits further (`simulation.wiring` in
  `project.yaml`), the source type `pulse` is new, and the same seed gives the same samples in
  virtual time. The profile keys, sources, wires and faults are described in `docs/simulator.md`.
- **Templates** on the start page: *Empty lab*, *Logic Analyzer*, *Characteristic curve* (now with
  the simulated Uno and DHO924S: the scope measures the RC voltage), *Test bench* (checks with a
  report, a test on the command line) and *Long-term logger* (CSV and strip chart).
- Panels run in virtual time if wanted (*Virtual time*); a project wires two simulated devices to
  each other (`simulation.wiring`: `{from: "uno:A0", to: CH1}`).
- Documentation: the README starts with the lab, the analyzer is a chapter; `docs/lab.md`
  (window, instruments, flows, all nodes, panels, generation, reports, projects),
  `docs/simulator.md`, `docs/protocols.md`, facets in `docs/drivers.md`.
- **Devices are nodes of a flow.** A `device.instrument` node stands for an instrument (a
  simulator, a device address, or an instrument already open in the device list, which is used as
  it is); its `device` output is wired to the nodes that use it (capture, stream, monitor, GPIO,
  generator). The palette offers the open instruments, the project's devices and the simulators,
  the device list *Add to the flow*; a new node is wired to the flow's only device at once. The
  `devices:` section and the `device:` parameter of flow files are gone.
- **The analyzer document is a data view.** It shows, analyses, decodes and edits captures (also
  files and test signals) and no longer connects devices or holds their settings. Capturing is the
  **device card's** business: its capture bar (rate, length, trigger, *Settings…*, *Start* F5,
  *Capture again*, *Stop*), *Profiles*, *Test signals of the board*, *Device information*,
  *Self-test*, *Network*, *Bootloader* and *Update firmware*; the result appears in the device's
  data view, whose toolbar names the source (a click opens its card) and can capture again or
  stop. Devices are opened only in the device list, which also notices boards without firmware
  and offers the update of outdated ones. *Forget multi device sets* moved to the *Devices* menu.
- **A flow from the start:** the application opens with an untitled flow beside the start page; it
  writes into a scratch folder until it is saved. Templates open as temporary projects without
  asking for a folder; the first *Save* (or *Project → Save project as…*) asks where to keep the
  project and moves its open documents along.
- New dependency: PyYAML.

### Firmware

- **openSciLab Pico, protocol 8.** The Pico firmware is now *openSciLab Pico* in `firmware/pico/`
  (source files without the old prefix), released with the application (0.1.0b1) and speaking
  **protocol 8**. It identifies itself as
  `OPENSCILAB_PICO_<BOARD>_V8_0`, its USB product name is *openSciLab Pico* (VID/PID unchanged),
  the images are called `openSciLab-pico_<BOARD>[_Turbo].uf2`. Boards with PiPiLogicAnalyzer 7
  are recognised and offered the update; the application only works with protocol 8.
- The capability strings of the Pico firmware come from `capabilities.h`; `docs/protocols.md`
  specifies the whole vocabulary (capabilities, the facets they unlock, pin capabilities, the
  wire protocols) and a test checks the firmware header and the application against it. The
  firmware reports the same capabilities as before.
- `firmware/build_all.sh` builds by firmware (`--pico [boards]`, `--arduino`, `--bridge`); a dev
  container (`.devcontainer/`) has the Pico SDK, the ARM toolchain, picotool, PlatformIO and the
  Android tools. The release file with all Pico images is called
  `openSciLab-firmware-pico-uf2.zip`.
- *Devices → Install or update firmware…* and the device card update the firmware of a Pico. For
  the other boards the update tools (avrdude, bossac, esptool, adb) are prepared in the
  application, but no driver uses them yet: Arduino boards are flashed with PlatformIO or
  avrdude, the bridge app with adb.
- **openSciLab Pico 8 does more** (protocol 8): pin table, pin modes, outputs, PWM, pulses
  exact to a clock cycle, the monitor, the watchdog (outputs are released 1 s after the host is
  gone), analog channels by DMA, the pattern generator on up to 24 pins, UART/SPI/I²C transmitters
  and the live state stream. Host tests of the frame parser, pattern data and pin tables run in CI.
- **openSciLab Arduino firmware** (`firmware/arduino/`, PlatformIO) for Uno, Nano, Mega 2560,
  Uno R4 Minima, ESP32 and ESP32-S3, with native tests and a benchmark project.
- **Bridge app** for the Rigol DHO900 (`firmware/bridge-android/`): passes SCPI through, keeps
  captures in a cache, sends overviews and compressed tiles and announces itself on the network.
- None of the three has been tried on hardware yet.

## [7.2.0] - 2026-10-01

### Application

- **The application requires the firmware it comes with.** Boards report a protocol version
  (`PROTOCOL:<n>` after the identification); a board with another or older firmware, including
  the original 6.5 firmware, is no longer opened: connecting it offers the firmware update. The
  48 byte capture request of V6_0 and the fallbacks for firmware without capabilities were
  removed.
- **Analysis tools in the style of professional logic analyzers:** cursors A and B with Δt and
  1/Δt, statistics of the channels (frequency, duty cycle, pulse widths) and setup/hold times;
  named markers; search for edges, patterns with don't-cares, pulse widths, gaps, bus values and
  decoder output (F3); buses and groups with symbol tables; a listing of every change or of the
  decoder output; charts of bus values and histograms; comparison with a reference capture; state
  analysis on the edges of a clock channel.
- **Trigger sequences:** stages of patterns, edges, pulse widths and gaps, with counts and time
  limits; evaluated by the application on a stream (software trigger, any device that streams)
  or by the device when it reports `TRIGGER_SEQUENCE`. State mode with an external clock for
  devices that report `STATE_MODE`.
- **New main window layout:** rate, length and trigger of the next capture in the toolbar,
  *Start* captures at once (F5), all settings with Ctrl+F5; the decoders, measurements, search,
  markers and capture information as tabs of a panel, the listing as a panel below; both can be
  moved and closed, and the layout is restored. The toolbar groups file, device, capture settings,
  start/repeat/stop and the panels with icons; every menu entry has an icon; zoom and channel
  height moved from the side panel to a bar below the waveform.
- **Start, Repeat and Stop in the middle of the toolbar**; while a device is connected the device
  list makes room for the capture settings.
- **Detailed list of an annotation row:** the other rows of the decoder are columns of their own,
  e.g. the bytes read during each instruction of a disassembly (`D0 F5 EE`) and its memory region;
  the details of the selected entry show every form of its text, those entries one by one with
  their time, and the levels of the channels the decoder read. The filter and *Copy* include them.
- **Hovering an annotation marks what belongs to it** in every row: the bus cycles of an
  instruction, or the instruction of a bus cycle (dashed outline). An entry made of several values
  shows where each was read and lists them next to the marked span, instead of one read point with
  the levels of every channel; a single value keeps its read point, now with the bus values next
  to it rather than in a corner.
- **State analysis reads where the data is stable:** it suggests the clock edge and read offset at
  which the other lines do not change (on a C64: the falling edge of Φ2, one sample before it;
  the rising edge read the VIC phase and gave wrong addresses), keeps the clock channel so the
  decoders still find their channels, and *Analyze → Back to the timing capture* returns to the
  capture it was made of.
- **sigrok sessions** (`.sr`, PulseView) are exported and opened; decoder output is exported as
  CSV or JSON.
- **Command line and Python API** (`pipilogicanalyzer-cli`, `pipilogicanalyzer.api`): list
  devices, capture, decode, convert between formats; see [docs/cli.md](docs/cli.md).
- **New look of the waveform:** a time grid and a ruler in time units (0 at the trigger, steps of
  1, 2 or 5 of ns/µs/ms/s) instead of sample numbers; thinner antialiased lines with the area under
  a high level lightly filled; dense signals as a translucent band with its envelope instead of a
  solid block; a calmer default palette of 16 distinct colours; the trigger as a dashed amber line
  with a flag on the ruler; the overview dims what lies outside the view.
- **Channel list** in one line per channel: colour strip (click to change), visibility, name and
  channel number; rename with a double-click or the context menu.
- **Device information with tabs:** *Overview*, *Capture limits* and *Self-test*; *Device →
  Self-test…* opens it on the self-test (for the PiPiLogicAnalyzer boards and the DSLogic).
- **DreamSourceLab DSLogic Plus, U2Pro16, U3Pro16 and U3Pro32** (experimental, tested on a
  U2Pro16): a USB driver following the DSLogic driver of DSView, including its FPGA security
  handshake. Buffer and stream captures at the rates of the device (up to 400 MHz / 1 GHz), edge,
  level pattern and immediate triggers, adjustable input threshold. The FPGA bitstreams come from
  an installed DSView or are downloaded once from the DSView repository; they are not shipped.
- DSLogic stream captures are shown while they run, following their end; *Stop* keeps the samples
  received so far. *Until stopped* streams endlessly and keeps the latest samples in a ring
  buffer. Reading and unpacking run in separate threads, so a stream at the USB 2 limit
  (20 MHz × 16 channels) keeps up.
- Board self-test for the DSLogic: the internal test counter of the FPGA checks the capture memory,
  buffer and stream transfers and the triggers bit by bit; the inputs are checked to read low.
- Self-test titles keep their acronyms ("RAM", "FPGA").
- **Stream captures with the PiPiLogicAnalyzer boards** (firmware of this project, over USB):
  the samples are sent while capturing and shown live, with a fixed length or until stopped.
  Up to 800 kHz with 8 channels, 400 kHz with 16, 200 kHz with 24; an overflow of the USB link
  ends the stream with a warning and keeps the samples before it. The board self-test streams
  a test counter and checks it sample by sample.
- Capture dialog: the highest rate follows the acquisition mode (a stream is limited by its
  link); devices whose stream starts at once offer only "None" as its trigger.
- **Record to disk** (streams of the DSLogic and the PiPiLogicAnalyzer boards): the samples go
  into memory-mapped files, so a stream is limited by the free disk space instead of about one
  billion samples. The edge index of such a capture is built in blocks and kept on disk too, the
  live index is kept when the stream ends, decoders run on request, and a disk that fills up
  stops the stream. Sample counts beyond 32 bits in the dialog.
- *Max* next to the sample count, and *Until stopped* starts with the most samples that fit
  (it kept a small number from earlier settings, e.g. 30,000).
- The capture overview is drawn from the edge index (two searches per column instead of a sum
  over every sample) and follows a stream live; the display no longer converts the edge index to
  floats on every frame, which made large captures slow. Progress of a stream the display cannot
  keep up with is shown at its newest state instead of queueing.
- Capture dialog: devices with a fixed list of rates get a list instead of the free value; an
  acquisition mode (buffer/stream), a threshold and "no trigger" appear where the device has them.
  The edge trigger offers all channels of devices with more than 24.
- `.lac` files store the acquisition mode and the input threshold of the capture settings.
- Selecting the new analyzer after a firmware installation works again (the device list lookup
  missed its entries).

### Firmware

- The identification ends with `PROTOCOL:<n>`, the protocol version the application requires
  (`FIRMWARE_PROTOCOL`, raised with every protocol change).
- Trigger sequences evaluated on the board (command 10, trigger type 7) and the state mode with an
  external clock, reported as `TRIGGER_SEQUENCE`, `TRIGGER_CONDITIONS`, `SEQUENCE_MAX_RATE`,
  `STATE_MODE` and `STATE_MAX_CLOCK`.
- Stream capture (capture request with trigger type 6, USB only): the DMA channels fill the
  capture buffer without end and the samples are sent in chunks while they arrive, until the host
  stops the stream; an overtaken buffer ends it with an overflow marker. `STREAM=800000` in the
  capabilities; `triggerValue = 1` streams a PIO test counter instead of the inputs.

### Project

- Dependencies `pyusb` and `libusb-package` (the packaged applications include libusb); the smoke
  test of the builds checks that libusb loads. Linux udev rule for the DSLogic.
- **Device drivers are independent of the application:** the main window and the dialogs no longer
  ask which kind of device is connected, the driver describes what its device can do
  (`is_hardware`, `boards()`, `supports_bootloader`, `has_self_test`, `describe()`, ...). The
  device list is filled by one backend per kind of device (`ui/devices/`). The driver of the Pico
  boards (gusmanb's LogicAnalyzer hardware) moved to `driver/pico/`, next to `driver/dslogic/`.
  [docs/drivers.md](docs/drivers.md) explains how to add a device.

## [7.1.1] - 2026-09-24

### Application

- Multi device sets: the boards started through the trigger line were compensated for the
  trigger delay with far too few samples (the delay in clock cycles was divided by the sample
  period in nanoseconds), so their samples were shifted against the board evaluating the trigger.
- Pattern and edge-out captures whose post-trigger samples do not cover the trigger delay are
  rejected by the settings check instead of failing on the device.
- LogicAnalyzer Interceptor: the capture mode is chosen by the bit of every channel in the
  samples (channel 0 = GPIO 6), so channels 4–7 in 8 channel mode and 12–15 in 16 channel mode no
  longer read as 0.
- Dragging a selection in the ruler beyond the left edge produced negative sample numbers; cut
  and delete then removed the wrong samples. Cut no longer deletes when copying was refused.
- Automatic decoder channel assignment missed capture channel 0 when it matched by id.
- Changes made while the decoders run (sample edits, decoder settings) are decoded afterwards
  instead of being ignored; the decoders work on a snapshot of the channels.
- Loading capture settings or a profile in blast mode kept the default post-trigger samples.
- Aborting a WiFi capture closes the connection properly, which also ends the waiting read.
- VCD export: the time stamps no longer drift at sample periods that are not whole nanoseconds
  (e.g. 24 MHz).
- The board self-test dialog can no longer be closed with `Esc` while the test runs.
- Smaller fixes: the first pixel column of dense waveforms was drawn as toggling, the burst
  timestamp wraparound was off by one tick, stale input could be read as device details.
- Unused code removed.

### Firmware

- WiFi: received data is acknowledged to lwIP (`tcp_recved`); without it the receive window shrank
  with every request until the connection hung after about 11 KB of requests.
- WiFi: the error callback no longer closes the PCB lwIP has already freed, a closed connection
  no longer returns `ERR_ABRT`, responses are sent immediately (`tcp_output`), and a client that
  stops reading is dropped after 5 s.
- WiFi: received data waits in lwIP instead of blocking the WiFi core on a full event queue, so
  the two cores can no longer block each other during a large transfer.
- WiFi: an invalid stored IP address keeps the address assigned by DHCP.
- Captures are rejected when a channel does not fit into the samples of the requested mode
  (Interceptor) instead of silently reading 0; unknown trigger types are answered with
  `CAPTURE_ERROR` instead of starting an edge capture.
- Undefined shifts in the burst timestamps and the blast trigger mask fixed; a blast capture
  releases its GPIOs; the power status line cannot overflow its buffer.
- CMake applies a changed `BOARD_TYPE` to an existing build directory; unused code removed.

### Project

- `publish.ps1` works again after the rename (settings file, image name) and names the images
  like `build_all.sh`. The VS Code kit uses the SDK 2.1.1 toolchain.
- Corrected references to gusmanb's original LogicAnalyzer that the rename had changed.
- The release check also compares the firmware version in `CMakeLists.txt` with the tag.

## [7.1.0] - 2026-09-15

### Project

- **The project is now called PiPiLogicAnalyzer.** The Python package is `pipilogicanalyzer`, the
  command `pipilogicanalyzer`, the settings live in a directory of that name (the settings of the
  previous name are taken over on the first start) and the environment variables are
  `PIPILOGICANALYZER_SETTINGS_DIR` and `PIPILOGICANALYZER_DECODERS`; the previous names still work.
- The firmware is called PiPiLogicAnalyzer as well: the sources are in `firmware/PiPiLogicAnalyzer`,
  the images are named `PiPiLogicAnalyzer_<BOARD>[_Turbo].uf2` and a board identifies itself as
  `PIPI_LOGIC_ANALYZER_<BOARD>_V<major>_<minor>`. The application also accepts the previous
  identification, so boards with an older firmware keep working. USB VID/PID (0x1209/0x3020) and
  the hardware of Agustín Giménez Bernad are unchanged.
- `SECURITY.md` (private vulnerability reporting, where problems are plausible) and a Contributor
  Covenant `CODE_OF_CONDUCT.md`.
- Corrected license statements: a few bundled sigrok decoders are MIT or BSD, not GPL, and the
  AppImage runtime is distributed with the Linux build. The copies of the Raspberry Pi
  `lwipopts.h` and `pico_sdk_import.cmake` carry their BSD-3-Clause notice again.
- Source files carry a copyright and `SPDX-License-Identifier` header; `pyproject.toml` declares
  `GPL-3.0-or-later`.
- Pull requests that only change documentation now run the tests as well, so a required check can
  pass.

### Application

- The device list and the multi device dialog only offer detected analyzers; other serial ports
  are no longer listed.
- Clicking the name of an annotation row (or double-clicking an annotation) opens the row as a
  list in a window of its own: filter, copy (e.g. a disassembly listing), and selecting an entry
  shows it in the waveform.
- Multi device sets are no longer triggered by the master only: the board of the trigger channel
  evaluates a pattern or an edge (firmware of this project) and starts the other boards, or every
  board waits for an edge on its external trigger input.
- Pattern triggers use every group of consecutive trigger inputs the firmware reports (Pico:
  channels 1–21 and 22–24 instead of 1–16), fast matching included.
- C64 bus decoder: the *Bus cycles* row keeps the value read or written visible when zoomed out
  (`R $FFFC=$E2`, `FFFC=E2`, `E2`) instead of showing only the address.
- *Device → Install or update firmware*: every image shows the firmware version it contains, in
  the list and in its description.
- Channels can be made lower (or taller) to fit more of them on the screen: `Alt` + mouse wheel,
  *View → Taller/Shorter channels* (`Ctrl+Shift+Up/Down`) or the *Channel height* slider; the
  height is remembered.
- *Help → Online documentation* opens the wiki of this project; the wiki of the original software
  by gusmanb, which documents the hardware, has an entry of its own.

### Firmware

- Identifies itself as `V7_1`. A multi device set only accepts boards with the same version, so
  flash every board of a set with this firmware.
- Trigger type 5: edge trigger that also drives the trigger output.
- Pattern triggers accept all channels on consecutive GPIOs; the capabilities report
  `EDGE_TRIGGER_OUT` and `PATTERN_GROUPS`.

## [7.0.0] - 2026-09-14

First release, published under the name *LogicAnalyzer 7*. It is based on version 6.5 of the
[LogicAnalyzer](https://github.com/gusmanb/logicanalyzer) by Agustín Giménez Bernad (gusmanb)
(branch `version/v6_5`, commit `3fa3703`) and extends his firmware and software. Thank you,
Agustín, for creating the LogicAnalyzer and sharing it under the GPL.

### Application

- Cross-platform Python/Qt application with the feature set of the original 6.5 software:
  serial, network and multi device (2 to 5 boards) capture, `.lac` files, sigrok protocol
  decoders, profiles, sample editing, measurements and the Signal Description Language.
- Ready-to-run builds for Windows, macOS (Apple Silicon and Intel) and Linux (AppImage) that
  include the protocol decoders and the firmware images.
- Reworked user interface: toolbar with connection status, start page, capture menu, dialogs
  with named actions and inline validation, consistent dark theme and icons.
- *Install or update firmware*: flashes boards in bootloader mode, restarts connected boards
  into the bootloader and shows the installed firmware and Pico model of every board.
- *Board self-test*, *simulated capture* (on the board or computed locally) and *device
  information* for boards running this firmware.
- Much faster drawing of large captures, decoding on a worker thread, VCD/CSV export and
  compressed captures.
- Profiles can be edited: name, notes, capture settings and decoders.
- Navigation: the mouse wheel and `+`/`-` zoom, the arrow keys scroll horizontally, a trackpad
  scrolls horizontally through the samples and vertically through the channels, pinching zooms.
- Channels can be pinned; pinned channels stay at the top while the waveform scrolls.
- `examples/c64-demo.lac`: a generated C64 capture (reset, screen output, IRQs) for the C64 bus
  decoder.
- Multi device captures are aligned after capturing: exactly through a reference line (a slave
  signal also connected to the master) or estimated from a clock channel; *Capture → Align
  boards* does the same for captures opened from files and lets you choose the method when several
  are possible; the capture information names the method used. The C64 profile captures at 20 MHz and
  uses A0 as reference line.
- Hovering a decoder annotation marks its samples in the waveform and shows how the value is
  composed: the level of every channel at the sample the decoder read it, bit weights and bus
  values such as `A15…A0 = 1111 1111 1111 1100 = $FFFC`.
- C64 system bus decoder (`c64bus`) and an example profile for the C64 expansion port. It reads
  the bus at the last sample before the falling Φ2 edge (configurable, with an offset), flags
  cycles whose lines change next to the read point, and disassembles the 6510 code including
  undocumented opcodes, subroutine calls, branches and IRQ/NMI sequences, although the 6510 has
  no SYNC signal.
- Numerous bug fixes compared with the original software, see
  [docs/improvements.md](docs/improvements.md).

### Firmware

- Based on the 6.5 firmware; identifies itself as `V7_0`. The original 6.5 software accepts
  this version and keeps working with it.
- New commands: capabilities, board self-test, simulated capture and device information.
- Bug fixes in the receive buffer, request validation, blast and burst captures, pattern
  trigger, WiFi handling and the event machine, see [firmware/README.md](firmware/README.md).
- Turbo mode (overclocking and overvoltage) is off by default.

### Build

- GitHub Actions workflow that builds the firmware for all boards and the applications,
  publishes the pre-release *latest-build* from `main` and creates releases from tags.
