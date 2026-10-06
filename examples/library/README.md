# Example library

Small projects that show what openSciLab does, sorted by topic like the examples of the Arduino IDE -
and the projects a new one starts from. In the application they are tiles on the start page (by
category, with a search) and in the **Templates** menu; a project opens as a copy (a temporary project),
so the library stays as it is – saving asks where to keep the copy. Every example uses simulated devices
and runs without hardware; with a real device, change the address in `project.yaml` or in the device node.

Each example is a folder with a `project.yaml` (`project`: the title, `description`, `icon`, `devices`,
`open`: the files to open, or `data` for a data view with the connected device; `realtime: true` when it
does not work in virtual time), its flows and panels. Category folders have a `category.yaml` with
`title`, `description` and `icon`; folders sort by their number. The test
`tests/test_examples.py` runs every example and checks its results.

## Start a project

Starting points for a measurement of your own - open one, change the devices and the flow, save it as your project. Every example below is a starting point too.

| Example | What it shows | Devices |
| --- | --- | --- |
| [Empty lab](00-start/01-empty-lab/) | An empty measuring station with a simulated logic analyzer; replace the device with yours in project.yaml (or in the device list) and build the flow. | `sim:free` |
| [Logic Analyzer](00-start/02-logic-analyzer/) | A data view that captures with a connected device - settings, capture, decode protocols, measure and save. With several devices connected it asks which one; without one it shows the device list (or open a capture file). | the connected device |
| [Data acquisition](00-start/03-data-acquisition/) | Records analog inputs of a measuring box until Stop - a strip chart per input, every block into data/recording.lac (opens in a data view) - and sets an analog output from the panel. The box is the simulated USB DAQ (sim:daq, AO0 wired to AI2); put the address of yours into project.yaml or choose it in the device list, and the channels into the stream node. | `sim:daq` |
| [Synchronized instruments](00-start/04-synchronized-instruments/) | Two instruments on one time axis - a logic analyzer and a measuring box whose samples arrive late and uneven over USB and whose clock drifts. The logic analyzer drives a sync signal on GP16; a wire takes it to one of its own channels (GP17) and to a line of the box (P0.7); timing.align matches the edges and puts the box's samples on the logic analyzer's time to microseconds. Both are simulated; with hardware run a wire from the sync pin to a channel of every instrument. | `sim:pico`, `sim:daq` |
| [Remote measuring device](00-start/05-remote-measuring-device/) | A measuring device on another computer (a Raspberry Pi, the PC of a DAQ) sends its samples to openSciLab with the time it took them; openSciLab measures that computer's clock, so they line up with everything else. device/my_device.py is the script for the other computer - fill in your hardware where it says so. Until it runs, a simulated remote DAQ stands in (remote-sim:daq); then set the device in project.yaml to remote:my-device. | `remote-sim:daq` |

## Basics

First steps with the simulated 16 channel logic analyzer (sim:free) - capture, trigger, save, read, stream and capture on a clock of the device (state mode).

| Example | What it shows | Devices |
| --- | --- | --- |
| [First capture](01-basics/01-first-capture/) | Captures the 8 bit counter of the simulator (D0-D3) and its clock (D8) once and shows them in a data view. Run the flow (F5); the scope window opens with the capture. | `sim:free` |
| [Edge trigger](01-basics/02-edge-trigger/) | Waits for a rising edge of the 1 kHz signal on D15 and keeps a quarter of the samples from before the trigger. Change the edge or the source channel in the inspector of 'capture'. | `sim:free` |
| [Save a capture](01-basics/03-save-capture/) | Captures the SPI lines D10-D12 and writes the capture three times - as .lac (opens in the data view), as .vcd (for other tools) and as .csv - into the data folder of the project. | `sim:free` |
| [Read a capture file](01-basics/04-read-capture-file/) | Reads a saved capture (data/demo.lac) when the flow starts, shows it and measures the frequency of its clock channel (CLK) - the capture holds I²C (SCL, SDA), UART (RX) and a clock - a flow can work on recordings as well as on devices. | – |
| [Stream](01-basics/05-stream/) | Streams D15 for 200 ms instead of capturing into the device's memory - the blocks arrive while the device records. Every block is measured; the strip chart shows the frequency over time. | `sim:free` |
| [Repeated captures](01-basics/06-repeated-captures/) | A timer arms the capture five times, 200 ms apart. Each capture is measured; Run data in the toolbar shows all five captures and the measurements on one time axis. | `sim:free` |
| [State mode](01-basics/07-state-mode/) | A capture clocked by the device under test - one sample on each rising edge of the clock on D8 instead of at a fixed rate. The simulated analyzer counts on D0-D7 with that clock, so every state is the next count; the pattern trigger 1111 starts at the count 15. convert.state_bit takes one channel of the states, its edges are counted. | `sim:free` |

## Digital I/O

Outputs, inputs, PWM, pulses and the monitor of a simulated Arduino Uno (sim:uno) - the classic Arduino examples as flows.

| Example | What it shows | Devices |
| --- | --- | --- |
| [Blink](02-digital/01-blink/) | The "Blink" of the Arduino IDE as a flow - a timer toggles D13 eight times; the monitor reads the pin back and the LED view shows it. | `sim:uno` |
| [Button](02-digital/02-button/) | A push button on D2 (simulated, pressed twice, with contact bounce). The stream records it; one counter counts every edge of the bouncing contact, the other one counts after the debounce filter - two presses. | `sim:uno` |
| [PWM](02-digital/03-pwm/) | PWM of 1 kHz with a duty cycle of 25 % on D9. A capture of D9 measures frequency and duty cycle; two checks compare them with what was set. | `sim:uno` |
| [Pulse train](02-digital/04-pulse-train/) | Three pulses of 0.5 ms, one every 1 ms, timed by the board on D7. D7 is wired to D3 on the simulated Uno; a capture of D3 (448 samples at 100 kHz, what the Uno holds) waits for the first edge and measures count and width. A sequence starts the pulses 1 ms after the capture is armed - also in virtual time, where the capture lets the flow's time run on while it waits. | `sim:uno` |
| [Monitor](02-digital/05-monitor/) | The monitor reads pins and analog inputs again and again (here 20 times a second) - the way to watch slow signals. PWM on D9 charges the RC low pass at A0; the strip chart shows the voltage rising, the LED shows D3. | `sim:uno` |

## Analog

Voltages - analog inputs of the Arduino and the Pico, measurements, spectrum, filters and signal processing with a simulated oscilloscope (DHO924S).

| Example | What it shows | Devices |
| --- | --- | --- |
| [Analog read](03-analog/01-analog-read/) | Like "AnalogReadSerial" - a timer reads A0 ten times a second. PWM on D9 charges the RC low pass in front of A0, so the voltage rises towards 3 V (60 % of 5 V). | `sim:uno` |
| [Voltage limits](03-analog/02-voltage-limits/) | Watches A0 against a window of 2.5 V to 3.5 V - control.limit tells for every reading whether it is inside; the LED shows it, the log lists the readings outside. | `sim:uno` |
| [Scope measurements](03-analog/03-scope-measurements/) | The four channels of a simulated oscilloscope (1 kHz sine, 10 kHz square, 500 Hz triangle, 100 Hz ramp) - mean, RMS, peak to peak and minimum/maximum of each, as the measurement menu of a scope shows them. | `sim:dho924s` |
| [Spectrum](03-analog/04-spectrum/) | The FFT of the 10 kHz square wave on CH2 - its odd harmonics (30, 50, 70 kHz ...) stand out in the spectrum view. Compare with the sine on CH1 by changing the wire. | `sim:dho924s` |
| [Low pass filter](03-analog/05-low-pass-filter/) | The 1 kHz sine on CH1 is noisy. A low pass filter (FIR, 3 kHz) removes the noise - the scopes show the signal before and after, the numbers its RMS. | `sim:dho924s` |
| [Analog to digital](03-analog/06-analog-to-digital/) | A threshold with hysteresis turns the triangle on CH3 into a logic signal; its frequency and duty cycle are measured like those of a digital channel. | `sim:dho924s` |
| [Pico ADC](03-analog/07-pico-adc/) | The three ADC inputs of a simulated Raspberry Pi Pico (a 100 Hz sine on GP26, a 50 Hz triangle on GP27, 1.2 V on GP28), read by the monitor 200 times a second. | `sim:pico` |
| [Signal processing](03-analog/08-signal-processing/) | Slope, area, a lower rate and an average on the channels of a simulated oscilloscope - the triangle on CH3 rises 3 V in 1 ms (3000 V/s), the 10 kHz square on CH2 stays at 1.65 V on average, the logic channel D15 becomes volts, and eight captures of the noisy sine on CH1, each started by the rising edge of D15, are averaged into a clean one. | `sim:dho924s` |

## Protocols

Decoding UART, SPI and I²C with the protocol decoders, checking what a device sends, and sending - with the device's own hardware or as a pattern.

| Example | What it shows | Devices |
| --- | --- | --- |
| [UART](04-protocols/01-uart/) | Decodes the UART on D9 of the simulator (115200 baud, it repeats "openSciLab"). The log shows the text, the table every byte with its time. | `sim:free` |
| [SPI](04-protocols/02-spi/) | Decodes SPI on D10 (clock), D11 (MOSI) and D12 (chip select, active low) - every word the master sends appears in the table. | `sim:free` |
| [I²C](04-protocols/03-i2c/) | Decodes I²C on D13 (SCL) and D14 (SDA) - start and stop conditions, the address, reads and writes and every data byte with its acknowledge. | `sim:free` |
| [Check a message](04-protocols/04-check-a-message/) | Does the device send what it should? The Pico sends "openSciLab Pico" on GP11; the flow decodes it and compares the text - a test that passes or fails, also on the command line (openscilab run flows/check.flow.yaml ends with 1 when it fails). | `sim:pico` |
| [Send UART](04-protocols/05-send-uart/) | The Uno sends "Hi" with its own UART on D7 (9600 baud). D7 is wired to D3; a capture of D3 waits for the start bit (448 samples at 100 kHz, what the Uno holds) and the decoder reads the text back. | `sim:uno` |
| [I²C sensor](04-protocols/06-i2c-sensor/) | Reads two bytes from the temperature sensor at address 0x48 on the I²C bus of the Pico (GP3 = SCL, GP2 = SDA) - the Pico is the master; the answer appears in the log. | `sim:pico` |
| [Send SPI](04-protocols/07-send-spi/) | gen.tx_spi sends three bytes as SPI (mode 0, MSB first, 100 kHz). The simulated analyzer has no SPI of its own, so the frames play as a pattern on its pattern generator (D0-D7, looped back to the inputs) - CS on D0, SCK on D1, MOSI on D2. A capture of the same pins waits for CS to go low, the SPI decoder reads the bytes back. (A device with its own SPI, TX_SPI, sends them itself.) | `sim:free` |

## Measurement

Timing measurements on digital signals - frequency, period, duty cycle, pulse width, edges, setup and hold -, statistics over many captures, a characteristic curve and a test bench with a report.

| Example | What it shows | Devices |
| --- | --- | --- |
| [Frequency and duty cycle](05-measurement/01-frequency-and-duty/) | Frequency, period, duty cycle and pulse width of the 1 kHz signal on D15 and the 1 MHz clock on D8, each with a check against its nominal value. | `sim:free` |
| [Count edges](05-measurement/02-count-edges/) | Counts the rising edges of D15 in a 100 ms capture (100 at 1 kHz) and the edges of the UART line D9 - measure.count works on signals, events and tables. | `sim:free` |
| [Setup and hold](05-measurement/03-setup-and-hold/) | How long is the SPI data line (MOSI, D11) stable before and after the rising clock edges (D10)? measure.setup_hold finds the shortest setup and hold time - the margin of the bus. | `sim:free` |
| [Statistics](05-measurement/04-statistics/) | Ten captures, one every 100 ms; the frequency of each goes into a table. The table view lists them, the report adds the table, a diagram and a check of every value. | `sim:free` |
| [Test report](05-measurement/05-test-report/) | A complete test of a device - checks of the clock, the 1 kHz signal and the UART text with limits, sections with explanations, and the report as HTML (or PDF with a .pdf path). | `sim:free` |
| [Characteristic curve](05-measurement/06-characteristic-curve/) | Sweeps the PWM duty cycle of D9 of an Arduino Uno; an oscilloscope measures the voltage behind the RC low pass at A0, its mean per step makes the curve. A panel starts it, a report shows the curve and checks its slope. Both instruments are simulated. | `sim:uno`, `sim:dho924s` |
| [Test bench](05-measurement/07-test-bench/) | Tests a device under test and writes a report with passed or failed. On the command line it is a test - "openscilab run flows/test.flow.yaml" ends with 1 when a check fails -, the panel runs it with a button. The device under test is simulated (sim:free). | `sim:free` |

## Signal generation

Making signals - waveforms on the function generator of an oscilloscope, digital patterns, square waves, arbitrary curves - and measuring them again.

| Example | What it shows | Devices |
| --- | --- | --- |
| [Function generator](06-generators/01-function-generator/) | A 2 kHz sine of 1 V amplitude on the generator output of the simulated oscilloscope (it is connected to CH1). The capture starts with the generator's sync; frequency and RMS are checked. | `sim:dho924s` |
| [Frequency sweep](06-generators/02-frequency-sweep/) | The generator steps through 1, 2, 5 and 10 kHz; for every step the scope captures CH1 and measures the frequency. The table and the XY chart compare set and measured frequency. | `sim:dho924s` |
| [Arbitrary waveform](06-generators/03-arbitrary-waveform/) | A curve of your own - a formula over one period (x from 0 to 1) with numpy functions, here a sine with its third harmonic - played by the generator and captured on CH1. | `sim:dho924s` |
| [Pattern generator](06-generators/04-pattern-generator/) | A digital pattern on D0-D3 written in SDL (h = samples high, l = low, {...}n = a group repeated n times, b = a byte) and played by the simulator's pattern generator; its outputs are looped back to the inputs, so the capture shows the pattern. | `sim:free` |
| [Square wave](06-generators/05-square-wave/) | The square wave output of the Arduino Uno (a timer on D9) at 10 kHz, measured by a capture of the same pin. | `sim:uno` |
| [Waveform file](06-generators/06-waveform-file/) | A waveform kept as a file (waveforms/triangle.wave.yaml, made and edited in the waveform document) played by gen.output on the generator of the oscilloscope; its sync starts the capture of CH1, which measures the frequency and the peak-to-peak voltage of the triangle. | `sim:dho924s` |
| [Replay a capture](06-generators/07-replay-a-capture/) | A capture played back - the UART line D9 of the simulator is captured, gen.replay plays it on the pattern generator at D3 (pins picks the channel and renames it), a second capture of D3 records the replay, and the UART decoder reads the same text from both. | `sim:free` |

## Control

Flows that decide - timers, sweeps, sequences, state machines, comparisons and subflows that are used like one node.

| Example | What it shows | Devices |
| --- | --- | --- |
| [Timer and counter](07-control/01-timer-and-counter/) | The simplest flow without a device - a timer ticks ten times, a counter counts the ticks and math squares the count. Good to try single steps and breakpoints (F10 while paused). | – |
| [PWM sweep](07-control/02-pwm-sweep/) | Steps the duty cycle of D9 from 0 to 100 % in five steps; after the RC low pass at A0 has settled, A0 is read. The table and the XY chart show the voltage over the duty cycle - a straight line up to 5 V. | `sim:uno` |
| [Sequence](07-control/03-sequence/) | Steps without code - a sequence switches D13 on and off three times (on for 200 ms, off for 100 ms), then sends 'done'. The monitor shows the pin. | `sim:uno` |
| [Traffic light](07-control/04-state-machine/) | A state machine drives a traffic light on D11 (red), D12 (yellow) and D13 (green) - red, red and yellow, green, yellow, red, then off. Every state says what its outputs are and when it goes on. | `sim:uno` |
| [Threshold switch](07-control/05-threshold-switch/) | Switches an LED on D13 when A0 rises above 2.5 V - control.limit tells for every reading whether it is above the threshold. (control.compare does the same for any comparison, and its result counts as a check in the report.) | `sim:uno` |
| [Subflow](07-control/06-subflow/) | A part of a flow used like one node - the subflow 'duty_check' measures the duty cycle of a signal and checks it; the flow uses it twice, for D8 and for D15. Double-click a subflow node to open it. Select nodes and choose Make subflow to make your own. | `sim:free` |

## Data and reports

Keeping results - CSV logs, numbered capture files, buffers, bundles of values, reading tables, writing reports with diagrams and logging for hours.

| Example | What it shows | Devices |
| --- | --- | --- |
| [CSV logger](08-data/01-csv-logger/) | Writes every reading of A0 and D2 with its time into data/log.csv - one line per value, for a spreadsheet or a script. Set a duration in the inspector of the flow for a long recording. | `sim:uno` |
| [Numbered files](08-data/02-numbered-files/) | Three captures, one per second, each in a file of its own - capture-001.lac, capture-002.lac, capture-003.lac in the data folder. Open them from the Project sidebar. | `sim:free` |
| [Buffer](08-data/03-buffer/) | A stream of the counter keeps arriving; the buffer keeps only the last 5 ms. A timer reads the buffer three times - each time the scope shows the latest 5 ms, as a scope in roll mode. | `sim:free` |
| [Bundle](08-data/04-bundle/) | Frequency and duty cycle of a capture travel together on one wire - structure.bundle makes one value of both; the log shows them side by side, the CSV file gets one line, and structure.unbundle takes them apart again (the frequency is checked). A group frame marks the measurements. | `sim:free` |
| [Read a table](08-data/05-read-a-table/) | Reads measured values from a CSV file (data/diode.csv, the curve of a diode) and makes a report with the table and its diagram - results of other tools fit into the same reports. | – |
| [Report with a diagram](08-data/06-report-with-diagram/) | The voltage at A0 over the duty cycle of D9 as a table and a diagram in an HTML report - measured point by point and written when the flow ends. | `sim:uno` |
| [Long-term logger](08-data/07-long-term-logger/) | Records inputs of a device for a long time into a CSV file and shows them as a strip chart. On the command line give the time, e.g. "openscilab run flows/logger.flow.yaml --duration 1h". The device is a simulated Arduino Uno (PWM of D9 through an RC low pass at A0, a push button on D2). | `sim:uno` |

## Panels

Front panels for flows - sliders, switches and buttons send values into the running flow, numbers, LEDs, charts and scopes show its results. Operate shows the panel as it runs, F11 full screen.

| Example | What it shows | Devices |
| --- | --- | --- |
| [Control panel](09-panels/01-control-panel/) | Operates an Arduino Uno from a panel - a slider sets the PWM of D9, a switch the LED on D13; the panel shows the voltage at A0 (behind the RC low pass) as a number and a chart, and the pins D2 and D13 as LEDs. Press Run in the panel (it runs until Stop). | `sim:uno` |
| [Measurement panel](09-panels/02-measurement-panel/) | A button captures D15 and D8; the panel shows frequency and duty cycle, an LED tells whether the frequency is within 1 kHz ± 1 Hz, the scope shows the capture. | `sim:free` |
| [Generator panel](09-panels/03-generator-panel/) | A function generator with a panel - sliders set frequency and amplitude of the sine on the oscilloscope's generator output while it plays; the button captures CH1 and the panel shows it with its RMS and frequency. | `sim:dho924s` |

## Python

Your own code - Python nodes inside a flow, a whole flow written as a Python script, and node types of your own in the project's nodes folder.

| Example | What it shows | Devices |
| --- | --- | --- |
| [Python node](10-python/01-python-node/) | Two Python nodes - 'source' runs with the flow and sends a damped oscillation every 20 ms, 'smooth' is called for every value and sends the moving average of the last five. In the code, ctx.emit sends, await ctx.sleep waits; the editor completes ctx., np. and the port names. | – |
| [Flow as a Python script](10-python/02-flow-as-script/) | The same kind of flow written in Python with the flow DSL - devices, nodes and wires as code (flows/counter.py). The application opens it as a graph; on the command line it runs with "python flows/counter.py" or "openscilab run flows/counter.py --fast". | `sim:free` |
| [Own nodes](10-python/03-own-nodes/) | Node types of your own - every function with @node in the project's nodes folder is a node of the palette (here example.bit_rate and example.blink in nodes/example_nodes.py). A plain function is computed from its inputs, an async one runs with the flow. | `sim:free` |

## Advanced

Several instruments in one flow, boards captured together as one analyzer, and simulated circuits of your own.

| Example | What it shows | Devices |
| --- | --- | --- |
| [Two instruments](11-advanced/01-two-instruments/) | An Arduino makes a PWM signal, an oscilloscope measures it - two devices in one flow. In the simulation the probe of CH1 sits on D9 of the Uno (simulation/wiring in project.yaml); with real devices, connect them the same way. | `sim:uno`, `sim:dho924s` |
| [Multi board](11-advanced/02-multi-board/) | Two Picos captured together as one analyzer with 48 channels (sim:pico*2) - the channels of board 2 are called B2.GP2 and so on. Both boards see a 1 kHz signal on GP12; the flow measures both and the time between their edges. | `sim:pico*2` |
| [Simulated scenario](11-advanced/03-simulated-scenario/) | The simulator plays a scenario chosen in the device node - here UART, SPI and I²C on D0 to D5 with a text of your own (signals of the device node). All three decoders read their bus; change the text and run again. | `sim:free` |

## Remote devices

Scripts on other computers as devices (package openscilab_device) - receiving values with the device's time, setting outputs at a time, calling commands, aligning clocks with a sync signal. Simulated here; a real script connects the same way (docs/remote.md).

| Example | What it shows | Devices |
| --- | --- | --- |
| [Climate station](12-remote/01-climate-station/) | A remote climate station (simulated - a script with openscilab_device on another computer works the same way) sends temperature, humidity and a door contact. The values carry the time they were measured on the station; openSciLab measures the station's clock, so they line up with everything else. Runs until Stop; data/climate.csv gets every value. | `remote-sim:climate` |
| [Remote control panel](12-remote/02-remote-control-panel/) | A panel for the remote climate station - a slider sets the heating power, a switch the fan, a button calibrates the sensor; the panel shows temperature and humidity. Outputs are sent a little ahead with the time they should take effect, so the station applies them on time. | `remote-sim:climate` |
| [Remote microphone](12-remote/03-remote-microphone/) | A remote microphone sends 8000 samples a second in blocks; every block carries the time of its first sample. The scope shows the signal, the spectrum its 440 Hz tone, the number the level the microphone computes itself. | `remote-sim:audio` |
| [Sync signal](12-remote/04-sync-signal/) | For microsecond alignment the station drives a sync output (in the simulation it is wired to D5 of a simulated logic analyzer - with hardware, a wire from a GPIO of the station to a channel of the analyzer). The analyzer streams D5; remote.sync matches the edges and from then on the station's values are converted on the time axis of that recording. | `remote-sim:climate`, `sim:free` |

## Time

When samples were taken - a sync signal from openSciLab that aligns instruments (and remote devices) to a few microseconds, a loopback that measures the latency of an instrument behind USB, a remote DAQ that records the sync signal in its blocks (docs/timing.md).

| Example | What it shows | Devices |
| --- | --- | --- |
| [Align instruments](13-time/01-align-instruments/) | timing.sync drives a sync signal on D7 of an Uno, wired to a simulator that knows its time and to a Pico behind (emulated) USB; timing.align places the Pico's samples on the reference's time to a few microseconds. | `sim:uno`, `sim:free`, `sim:pico` |
| [Calibrate latency](13-time/02-calibrate-latency/) | GP16 of the Pico wired to GP17: timing.calibrate measures how late its samples arrive and keeps it; the stream that follows is placed with it. | `sim:pico` |
| [Remote DAQ](13-time/03-remote-daq/) | A DAQ on another computer records openSciLab's sync signal on a line of its own; remote.sync aligns its samples with a logic analyzer to the precision of its sample rate. | `sim:uno`, `sim:free`, `remote-sim:daq` |
| [DAQ latency](13-time/04-daq-latency/) | Without a sync signal: the DAQ measures the latency of its inputs with a loopback (its command calibrate_latency) and stamps its blocks with it. | `remote-sim:daq` |
