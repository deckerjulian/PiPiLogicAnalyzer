# The time of samples

Every sample openSciLab shows has a time on one clock: the clock of the computer openSciLab runs
on. A logic analyzer behind USB, a DAQ on another computer and a simulator each have clocks of their
own; this page describes how their samples are placed on that clock, how well, and how to do better
with a wire, a measurement or a shared time scale. The code is in `openscilab_device/timing.py`
(the algorithms, standard library only: device scripts use them too), `openscilab/core/timing.py`
(the instruments of openSciLab) and `openscilab/driver/remote/clock.py` (remote devices).

## Methods, best first

| Method | How | Accuracy (guide values until measured) |
| --- | --- | --- |
| **simulated** | a simulator knows when it took its samples | exact |
| **sync signal** | the same irregular signal recorded by both sides, edges matched | a sample period of the slower recording |
| **shared clock** | both sample with one clock; a sync signal gives the offset only | as above, no drift |
| **time scale** | the device stamps in UTC/TAI (PTP, GPS) and this computer's clock follows it too | what PTP/NTP achieve (PTP with hardware time stamps: < 1 µs; software: 10-100 µs) |
| **latency** | the arrival envelope less a latency measured with a loopback | a fraction of a millisecond |
| **bounds** | between the command that started it and the arrival envelope | half the distance (about a millisecond over USB) |
| **arrival** | only the envelope | an upper bound (late by the shortest latency) |
| **network** (remote) | the device's clock measured with pings | ± half the shortest round trip (wired: 0.1-0.5 ms, WiFi: ms) |

The **Timing** tab of the device card has all of it for openSciLab's instruments:

* *Time of the samples*: the method and accuracy of the last capture or stream - of a flow and of
  the data view alike -, the sample clock, whether the device stamps in a time scale, whether this
  computer's clock follows NTP or PTP (*This computer's clock…* opens *Settings → Time*);
* *Sample clock*: own (offset and drift are fitted) or shared/external (only the offset):
  `timing.align` with `drift: auto` (the default) follows it;
* *Latency*: the measured latencies of the device, and *Measure* - the loopback of
  `timing.calibrate` without a flow: choose the output, the channel it is wired to, the rate and
  stream or capture; the result is kept like that of the node. A simulator is wired with *Wire
  them* (see [simulator.md](simulator.md), its USB link is set on its *Signals* tab);
* *Sync output*: the signal of `timing.sync` on a pin until it is stopped (or the card closed),
  e.g. for an input of a DAQ that records it with its data.

Remote devices show *Clock* and *Inputs* on their *Remote* tab.

## Arrival and start

A block of samples cannot arrive before its last sample was taken. The sample clock of a device is
even; the arrivals are not (USB frames of 1 ms or 125 µs, buffers, the operating system). A line
under the arrivals - their *lower envelope*, the line of the fastest ones - removes the jitter;
what remains is the shortest latency, so the envelope is an upper bound of when a sample was
taken. Only the fastest arrival of every stretch of samples is kept (minutes back, little memory);
once the blocks span 10 s the slope of the line gives the drift of the sample clock as well.

No sample is taken before the command that started the acquisition: a lower bound. Between the
two the middle is the estimate and half the distance the uncertainty - the assumption NTP makes
(starting takes about as long as delivering). A capture that waited for a trigger has no lower
bound: only its arrival (less a measured latency) places it.

The estimate improves while blocks arrive. A sync signal is therefore fitted with every edge placed
again with all that is known at that moment (`timing.align` by the sample index, `remote.sync` by
the arrival data a device sends along with its blocks).

## Latency: a loopback

An output of the instrument, wired to one of its inputs, is switched a few times. The edge cannot
come before its command; its sample cannot be later than the envelope. Their distance is the latency
of the output plus that of the input; half of the shortest one is the latency of the input, ± half of
it. The latency is kept per instrument, mode, rate (and length of a capture) in `timing.json` of the
settings directory (*Settings → Time* lists them) and used by every later capture or stream with the
same settings - without the wire.

* openSciLab's instruments: node `timing.calibrate` (pin, channel, rate, stream or capture).
* A device script: `input.calibrate(write, store="latency.json")` while its blocks keep coming;
  `dev.input(..., latency="latency.json")` uses it at the next start.

## Sync signal

`timing.sync` drives a pin of an instrument with edges at irregular intervals (20-60 ms, the same
for the same seed): wherever it is recorded, the edges can be matched without ambiguity - by their
pattern, even when the clocks are seconds apart. Wire the pin to a channel of every instrument (and
to an input of a remote device):

* **Instruments of openSciLab**: `timing.align` gets the signal as the instrument to align recorded
  it (`signal`) and as the reference did (`reference`, or the commanded `edges` of `timing.sync`).
  Offset and drift are fitted (`drift: none` for a shared sample clock; `auto`, the default,
  follows the *Sample clock* of the device on its card); the instrument's later
  samples are placed with them and what arrives at `in` leaves `out` on the reference's time.
* **Remote devices**: the device reports the edges in its clock - a sync output it drives
  (`dev.sync_output`), a sync input it sees (`dev.sync_input`) or an input of blocks that records
  the signal (`dev.input(..., sync=True)`, e.g. a spare channel of a DAQ). `remote.sync` matches
  them with a recording (`signal`) or with the edges of `timing.sync` (`edges`); `drift: auto` fits
  the drift unless the input shares openSciLab's sample clock (`clock="shared"`).

The edges of a recording are as precise as its sample period: a DAQ at 10 kS/s aligns to about
30 µs (the scatter of a uniform error of 100 µs), a logic analyzer at 100 MHz to nanoseconds.

## Shared clock

When the sample clock of a device comes from an instrument of openSciLab (a DAQ's external sample
clock from the Pico, a 10 MHz reference) the two never drift apart: only the offset has to be found,
by one edge of a sync signal or a shared start trigger. Mark such an input with `clock="shared"`
(remote) or use `drift: none` (`timing.align`). Hardware that makes the clock (a divided clock
output of the Pico for a DAQ) is a firmware topic and not part of openSciLab yet.

## Time scales (PTP, GPS, NTP)

A device whose clock follows a shared time scale stamps in it: `Device(clock=timescale_clock("tai"),
timescale="tai", timescale_accuracy=...)` (Linux with `ptp4l` + `phc2sys`; `utc` with NTP or a GPS
receiver). When this computer's clock follows it too (*Settings → Time*: NTP or PTP, with its
accuracy), openSciLab converts the device's times without measuring anything; the network
measurement goes on in the background as a check. A driver of a local
instrument that stamps its first sample in a time scale sets `session.device_start_time` and
`session.device_timescale` ([docs/drivers.md](drivers.md)).

PTP aligns system clocks, not samples: an instrument behind USB still needs its latency (or a sync
signal) to place its samples on that clock. macOS has no PTP client of its own; Windows has one with
software time stamps.

## Simulators

A simulator knows its time - unless its profile emulates USB (`usb`: `frame`, `latency`, `jitter`;
the Pico's does): then its blocks arrive in frames, late and uneven, and the methods above find the
time as with the real device. `remote-sim:daq` is a DAQ behind USB on another computer: a sample
clock that drifts (25 ppm), an analog input, a sync line (`SYNC_IN`, wire a simulator's pin to it),
a loopback DO0 -> LOOP and the command `calibrate_latency`; `?shared_clock=1` shares the sample
clock, `?timescale=utc` makes its clock follow UTC as with PTP; `latency`, `jitter` (ms) and `drift`
(ppm) set its network and clock. It is in the device list with each of these clocks, and its device
card changes network and drift while it runs (*Remote → Simulation: timing*): the *Clock of the
device* above it shows how the measurement follows. The examples of the category *Time* show all of
it; `examples/remote/ni_daq.py` is a template for a real NI DAQ.

## Staying responsive

The threads that receive samples (a stream, a remote device) must never wait long: their arrival
times place the samples, and the device's buffer fills while they wait. The Pico firmware streams
from a ring of 128 KiB and ends the stream with an overflow when the computer reads too late - at
6 MB/s that is 22 ms. Python lets one thread run at a time (the GIL): drawing, flows, scripts and
the garbage collector of the application can hold a reading thread for longer than that.

**Devices in a process of their own.** A Pico, Arduino or DSLogic is read in a *device process*
(`openscilab/driver/process`, *Settings → Devices*, on by default): its driver and facets live
there, with nothing of the application beside them. The application gets an instrument that passes
every call on; captures start there with the application's own session objects; the samples go
through shared memory (`core/shared_arrays`), never through the pipe; progress, completion and the
calls of the application's handlers come back in order - a newer progress of a stream replaces one
not sent yet, so a busy application lags behind but never slows the reading down. The device
process stamps the start command right before the device gets it and every block when it arrives:
the samples are placed with those stamps. If the device process dies, its capture fails with a
message and the instrument is disconnected; a device process that hangs is ended the same way - it
sends a sign of life every second (`host.BEAT_INTERVAL`), and after `proxy.BEAT_TIMEOUT` (10 s)
without one, or when a call takes longer than `proxy.CALL_TIMEOUT` (60 s), the application kills
it; if the application goes away, the device process stops the device and ends. Remote devices (TCP
loses nothing) and the Rigol stay in the application; the Python API keeps its devices in the
script's process (`api.open(..., process=True)` opens them in one).

**Simulators of USB devices, too.** A simulator of the device list that emulates a USB link
(`sim:pico`, `sim:daq`; the link its *Signals* tab set counts) runs in a device process as the
device it simulates (*Settings → Devices: Run simulators of USB devices in a process of their own*,
on by default): the same proxy, shared memory and stamps, its processor load in the device list. It
opens with what it simulated last time (`driver/simulated/stored.py`); the application reaches its
signals, wires, USB link, drift, faults and events through its simulation facet
(`core.instrument.SimulationFacet`), here or there alike. A simulator that knows the time of its
samples, the simulators of flows (the engine's clock, *Fast*, `simulation.wiring`) and simulators
wired to each other stay in the application; a flow that wires a simulator in a device process to
another one says to switch the setting off.

**In the application, too:** drivers raise their events into a notifier thread of their own (the
reading thread hands them over and goes on), the protocol decoders run in a process of their own
(`openscilab/sigrok/worker.py`; data views and the flow nodes `decode.*` use it, the Python API
decodes in its own process), the sync path is vectorized, annotations are tuples the garbage
collector does not walk again and again, and what the application made while starting is frozen
(`gc.freeze`): a full collection holds every thread for well under a millisecond.

**A higher priority** (*Settings → Devices: Read devices with a higher priority*, off by default,
`core/priority.py`). Windows lets the device processes run above normal without rights (they do).
macOS and Linux let a process lower its priority but not raise it: with the setting on, openSciLab
asks when it starts and raises itself and its device processes to nice -10 with the password
dialog of the system (`osascript … with administrator privileges`, polkit's `pkexec` on Linux);
device processes started later take it over, the decoder process gives it back. On Linux *Allow it
for good* writes `/etc/security/limits.d/90-openscilab.conf`: from the next login openSciLab raises
itself without asking. The card of a device in a device process shows its priority (*Details →
Process*).

`python tools/benchmark.py stream` shows when a stream overflows while the application works (the
simulated Pico emulates USB and the ring of the firmware: in the application it overflows under
load, in a device process it does not); `stall` how late a thread that wants to run every
millisecond runs while a large capture is decoded.
