# Remote devices

A script on another computer – a Raspberry Pi with sensors, a PC with a measuring card, a laptop
with a sound card – becomes a device of openSciLab. The script uses the package
`openscilab_device` (Python 3.8 or newer, standard library only; numpy is used when it is there),
describes its inputs, outputs, commands and sync signals, and connects to openSciLab. openSciLab
uses it like any other instrument: in the device list, in flows, panels and data views.

```
other computer                                  openSciLab
┌──────────────────────────────┐   TCP (port    ┌──────────────────────────────┐
│ your script                  │   24050)       │ server (Settings → Remote    │
│   import openscilab_device   │ ─────────────► │ devices)                     │
│   inputs / outputs / sync    │ ◄───────────── │ device list, flows, panels   │
│   stamps every value with    │   outputs with │ measures the device's clock  │
│   its own clock              │   their time   │ all the time                 │
└──────────────────────────────┘                └──────────────────────────────┘
```

## Getting started

1. In openSciLab: **Settings → Remote devices → Accept remote devices**. The page shows the port
   (24050), the token a device needs and a command that starts a demo device.
2. On the other computer: copy the folder `openscilab_device` of the repository there (or install
   openSciLab, which contains it). Try a demo device:

   ```bash
   python -m openscilab_device.demo climate --server 192.168.1.20 --token <token>
   ```

   Without `--server` the device listens for openSciLab's beacon in the local network.
3. The device appears in the device list as *climate (remote, …)*. Its card shows the latest
   value of every input, fields for its outputs, buttons for its commands and how well its clock is
   known. In a flow, a device node with the address `remote:climate` uses it.

Without a second computer: the simulator `remote-sim:climate` (also `audio`, `echo`, `daq`) runs the
same demo devices inside openSciLab, with a clock of their own (minutes apart, 50 ppm drift) and an
emulated network (4 ms ± 2 ms) in between. `remote-sim:daq` is a measuring box behind USB (see
[docs/timing.md](timing.md)); options `?timescale=utc` and `?shared_clock=1`, and for every one
`latency` and `jitter` of the network (ms, one way) and `drift` of its clock (ppm), e.g.
`remote-sim:daq?latency=20&drift=200`. The device list has the DAQ three times - with its own
drifting clock, with a clock that follows UTC (PTP) and with a shared sample clock - and the
*Simulation: timing* box on the *Remote* tab of a simulated device changes network and drift while
it runs and starts it again with another clock. The example library has the categories *Remote
devices* and *Time*.

## Writing a device

```python
from openscilab_device import Device

dev = Device("climate-pi", server="192.168.1.20", token="...")
temperature = dev.input("temperature", kind="scalar", unit="°C")
microphone = dev.input("mic", kind="analog", rate=48000, unit="V")
relay = dev.output("relay", kind="bool", default=False)
heater = dev.output("heater", kind="scalar", unit="W", range=(0, 100))
dev.sync_output("SYNC", lambda level: gpio.output(22, level))

@relay.on_set
def switch(value, at):           # called at the time openSciLab asked for ('at': this device's clock)
    gpio.output(17, value)

@dev.command()
def calibrate(reference=21.0):   # keyword arguments from the flow, the result goes back
    return sensor.calibrate(reference)

dev.start()                      # threads: connect (again), answer the clock measurements, send
while True:
    temperature.send(read_sensor())              # stamped with this computer's clock when called
    microphone.send_block(samples, t0=adc_time)  # samples at the rate, t0: time of the first one
```

| Inputs (`dev.input`) | arrive in openSciLab as |
| --- | --- |
| `scalar` (with `unit`) | numbers with their time (`Scalar`) |
| `bool` | truth values (`Bool`), also a pin for `gpio.read` |
| `analog`, `digital` (with `rate`) | blocks of samples (`Analog`, `Digital`) with their time base |
| `event`, `text` | events with data |

Outputs (`dev.output`): `scalar`, `bool` (also a pin for `gpio.write`), `text`. Values sent while
the connection is down are kept (up to `buffer` messages) and follow with their original times.
Blocks of `analog`/`digital` inputs are only sent while a flow listens to them.
`examples/remote/raspberry_pi.py` is a template for a Raspberry Pi, `examples/remote/ni_daq.py` for
a NI DAQ.

**Blocks from hardware behind USB.** `send_block(samples)` without `t0` stamps the block from when
it arrived: the envelope of the arrivals, bounded by `input.start()` (call it right before the
command that starts the hardware) and less a latency - measured with `input.calibrate(write,
store="latency.json")` over a loopback (an output of the hardware wired to this input), or given
(`dev.input(..., latency=0.0012)` or the file). Give `t0` when the hardware stamps its samples
itself. `dev.input(..., sync=True)` marks an input that records openSciLab's sync signal (a spare
channel), `clock="shared"` one whose sample clock comes from openSciLab's instrument;
`Device(clock=timescale_clock("tai"), timescale="tai")` a device whose clock follows PTP or GPS.
All of it: [docs/timing.md](timing.md).

## In flows

| Node | does |
| --- | --- |
| `remote.receive` | every value of an input, with the time it was measured on the device |
| `remote.set` | sets an output at a time a little ahead (`lead`), so the device applies it on time |
| `remote.call` | calls a command, `result` is the answer |
| `remote.sync` | aligns the device's clock with a sync signal recorded by another instrument (or the edges of `timing.sync`) |

`gpio.write`, `gpio.read` and `device.monitor` work with the `bool` and `scalar` channels too.
The inspector offers the inputs, outputs, commands and sync signals the device describes. Remote
devices run in real time (not with *Fast (virtual time)*).

## Synchronisation

Every value is stamped **on the device** when it is measured, with the device's own clock. The
network only delays it; it does not change its time. openSciLab converts the times:

**Over the network (always, without anything to do).** openSciLab pings the device (16 times right
after it connected, then once a second). From the four time stamps of a ping it computes offset and
round trip; the measurements with the shortest round trips (queues make long ones unsymmetric) give
a line through offset and drift (fitted once the measurements span 10 s). A jump of the device's
clock (it slept, its clock was set) shows as offsets far from the line; after three of them the
model starts anew. The card shows the method, the accuracy (at most ± half the shortest round trip
plus the scatter), round trip, jitter and drift. On a wired network the error is a fraction of a
millisecond; over WiFi a few milliseconds.

**With a sync signal (microseconds).** The device drives a sync output that toggles at irregular
intervals (20–60 ms) while openSciLab asks for it, and reports the time of every edge in its clock.
Wire it to a channel of a logic analyzer as well; stream (or capture) that channel and give it to
`remote.sync`. The node matches the edges and fits offset and drift with the precision of the
recording; the device's values are then converted on the time axis of that recording. The other way
round works too: a sync *input* of the device (`dev.sync_input("PPS")`, `sync.edge()` for every
edge it sees) for a pulse openSciLab (`timing.sync`) or a GPS receiver makes, or an input of blocks
that records it (`dev.input("sync", kind="digital", rate=..., sync=True)`): openSciLab finds the
edges in the blocks and aligns the device to the precision of its sample rate.

**With a time scale (PTP, GPS).** A device whose clock follows UTC or TAI says so in its hello; when
this computer's clock follows it too (*Settings → Time*), its times are converted without measuring;
the pings go on as a check.

**Outputs.** `remote.set` sends a value with the time it should happen (converted into the device's
clock) and a lead time (by default what the network needs); the device applies it at that time and
reports when it did.

**Several devices.** Values arrive late by different amounts. `remote.receive` waits a moment
(`playout`, by default about twice the typical delay) and hands values on in the order of their
time, so values of two devices meet in the right order.

## Settings and security

The server listens only while remote devices are switched on. A device needs the token: openSciLab
sends a random challenge, the device answers with an HMAC of it – the token itself never travels.
The beacon (UDP port 24051) tells devices in the local network where openSciLab is; switch it off
if they all know the address. The connection is not encrypted: use it in your own network (or
through a VPN/SSH tunnel).

`openscilab run` (command line) starts the server by itself when a flow uses `remote:` devices,
with the port and token of the settings.

## Protocol 1

One TCP connection, opened by the device. Every message is a frame:
`uint32 header length | uint32 payload length | header (UTF-8 JSON) | payload` (big endian). The
header holds the type in `"t"`; the payload carries the samples of a block (little endian). Times
are seconds (floats) of the device's monotonic clock. `openscilab_device/protocol.py` is the
reference; devices in other languages (MicroPython, C++) can speak it.

| Direction | Message | Fields |
| --- | --- | --- |
| openSciLab → device | `challenge` | `protocol`, `nonce`, `token` (true: a proof is needed), `server` |
| device → openSciLab | `hello` | `protocol`, `description`, `clock_id` (new for every start), `proof` = HMAC-SHA256(token, nonce); `timescale` (`utc`, `tai`) and `timescale_accuracy` when its clock follows one |
| openSciLab → device | `welcome` / `refused` | `name` / `reason` |
| openSciLab → device | `ping` | `id` |
| device → openSciLab | `pong` | `id`, `t1` (arrival), `t2` (answer), device clock |
| openSciLab → device | `subscribe` | `channels`: inputs and sync signals to send |
| device → openSciLab | `value` | `ch`, `at`, `v` |
| device → openSciLab | `block` | `ch`, `t0`, `rate`, `dtype` (`f4`, `u1`, …), `n` + payload; `i` (index of the first sample since the start); when stamped from the arrival: `m` (method), `u` (± seconds), `ta` (arrival), `ts` (start), `l` ([latency, uncertainty]) |
| device → openSciLab | `sync` | `name`, `edges`: `[[time, level], …]` |
| openSciLab → device | `set` | `id`, `ch`, `v`, `at` (device clock, `null`: at once) |
| device → openSciLab | `done` | `id`, `at` (when it happened), `error` |
| openSciLab → device | `call` | `id`, `name`, `args` |
| device → openSciLab | `result` | `id`, `value` or `error` |
| both | `bye` | |

The description: `{name, description, inputs: [{name, kind, unit, rate, range, description, sync, clock}],
outputs: [{name, kind, unit, range, default, description}], commands: [{name, description}],
sync: [{name, kind: output|input, description}]}`.
