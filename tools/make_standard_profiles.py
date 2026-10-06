#!/usr/bin/env python3
# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Generate the standard profiles in ``examples/profiles/``: capture settings and decoders for the
common buses and applications, one JSON file each.

Every profile starts at channel 1 and uses as few channels as the bus needs, so it fits every
device with enough channels; rate and length are clamped to the device when a profile is loaded.
The decoder channels are looked up by their id in the decoder registry, the options start from
the decoder's defaults. ``c64-expansion-port.json`` is written by hand and kept as it is::

    python tools/make_standard_profiles.py
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from openscilab.core.profiles import Profile, write_profiles_file
from openscilab.driver.models import AnalyzerChannel, CaptureSession, TriggerType
from openscilab.sigrok.provider import DecoderRegistry

OUTPUT = os.path.join(ROOT, "examples", "profiles")

#: file, name, rate, samples, channel names, trigger (channel name, falling), decoders, notes;
#: a decoder: (id, label, {decoder channel id: channel name}, options, index of its parent)
PROFILES = [
    ("uart-9600", "UART 9600 baud", 1_000_000, 1_000_000, ["RX", "TX"], ("RX", True),
     [("uart", "UART", {"rx": "RX", "tx": "TX"}, {"baudrate": 9600, "format": "ascii"}, None)],
     ("RX to CH1, TX to CH2, GND to GND. 8N1, shown as text. The capture starts with the first start "
     "bit on RX.")),
    ("uart-115200", "UART 115200 baud (Arduino serial)", 2_000_000, 1_000_000, ["RX", "TX"], ("RX", True),
     [("uart", "UART", {"rx": "RX", "tx": "TX"}, {"baudrate": 115200, "format": "ascii"}, None)],
     "RX to CH1, TX to CH2, GND to GND. 8N1 as Serial.begin(115200) sends it, shown as text."),
    ("midi", "MIDI (31250 baud)", 1_000_000, 1_000_000, ["MIDI"], ("MIDI", True),
     [("uart", "UART", {"rx": "MIDI"}, {"baudrate": 31250}, None),
      ("midi", "MIDI", {}, {}, 0)],
     "The output of the optocoupler of a MIDI input to CH1 (not the DIN socket directly)."),
    ("dmx512", "DMX512 (250 kbit/s)", 4_000_000, 1_000_000, ["DMX"], ("DMX", True),
     [("uart", "UART", {"rx": "DMX"}, {"baudrate": 250000, "stop_bits": 2.0}, None),
      ("dmx512", "DMX512", {}, {}, 0)],
     "The receiver output (RO) of an RS-485 transceiver to CH1, not the bus lines."),
    ("lin", "LIN bus 19200 baud", 1_000_000, 1_000_000, ["LIN"], ("LIN", True),
     [("uart", "UART", {"rx": "LIN"}, {"baudrate": 19200}, None),
      ("lin", "LIN", {}, {}, 0)],
     "The RXD pin of the LIN transceiver to CH1 (the bus itself runs at 12 V)."),
    ("i2c-100k", "I²C 100 kHz (standard mode)", 2_000_000, 1_000_000, ["SCL", "SDA"], ("SDA", True),
     [("i2c", "I²C", {"scl": "SCL", "sda": "SDA"}, {}, None)],
     ("SCL to CH1, SDA to CH2, GND to GND. The capture starts with the first falling edge of SDA "
     "(a start condition).")),
    ("i2c-400k", "I²C 400 kHz (fast mode)", 8_000_000, 1_000_000, ["SCL", "SDA"], ("SDA", True),
     [("i2c", "I²C", {"scl": "SCL", "sda": "SDA"}, {}, None)],
     "SCL to CH1, SDA to CH2, GND to GND."),
    ("spi-mode0", "SPI mode 0", 20_000_000, 1_000_000, ["CLK", "MOSI", "MISO", "CS"], ("CS", True),
     [("spi", "SPI", {"clk": "CLK", "mosi": "MOSI", "miso": "MISO", "cs": "CS"},
       {"cpol": 0, "cpha": 0, "cs_polarity": "active-low"}, None)],
     ("CLK to CH1, MOSI to CH2, MISO to CH3, CS to CH4. For clocks up to about 4 MHz; other modes: edit "
     "CPOL/CPHA of the decoder. The capture starts when CS goes low.")),
    ("onewire", "1-Wire (DS18B20 and others)", 1_000_000, 1_000_000, ["1-Wire"], ("1-Wire", True),
     [("onewire_link", "1-Wire link", {"owr": "1-Wire"}, {}, None),
      ("onewire_network", "1-Wire network", {}, {}, 0)],
     "The data line to CH1 (with its pull-up), GND to GND."),
    ("can-500k", "CAN 500 kbit/s", 4_000_000, 1_000_000, ["CAN RX"], ("CAN RX", True),
     [("can", "CAN", {"can_rx": "CAN RX"}, {"nominal_bitrate": 500000}, None)],
     ("The RXD pin of the CAN transceiver to CH1, not CAN-H/CAN-L. Other rates: edit the bitrate of the "
     "decoder.")),
    ("pwm", "PWM and servo signals", 1_000_000, 1_000_000, ["PWM"], ("PWM", False),
     [("pwm", "PWM", {"data": "PWM"}, {}, None)],
     "The PWM output to CH1. One second: 50 periods of a servo signal, duty cycle and period of each."),
    ("ws2812", "WS2812 / NeoPixel LEDs", 10_000_000, 500_000, ["DIN"], ("DIN", False),
     [("rgb_led_ws281x", "WS2812", {"din": "DIN"}, {"wireorder": "GRB"}, None)],
     "The data input of the first LED to CH1. The colour of every LED, in the order of the strip."),
    ("i2s", "I²S audio", 20_000_000, 1_000_000, ["SCK", "WS", "SD"], ("WS", True),
     [("i2s", "I²S", {"sck": "SCK", "ws": "WS", "sd": "SD"}, {}, None)],
     "Bit clock (SCK/BCLK) to CH1, word select (WS/LRCLK) to CH2, data (SD) to CH3."),
    ("parallel-8bit", "Parallel bus, 8 bit with clock", 10_000_000, 1_000_000,
     ["CLK"] + [f"D{bit}" for bit in range(8)], ("CLK", False),
     [("parallel", "Parallel", {"clk": "CLK", **{f"d{bit}": f"D{bit}" for bit in range(8)}}, {}, None)],
     "The clock (or strobe) to CH1, D0-D7 to CH2-CH9. Every rising clock edge gives a word."),
    ("swd", "SWD (ARM debug)", 20_000_000, 1_000_000, ["SWCLK", "SWDIO"], ("SWDIO", True),
     [("swd", "SWD", {"swclk": "SWCLK", "swdio": "SWDIO"}, {}, None)],
     "SWCLK to CH1, SWDIO to CH2, GND to GND, while a debugger talks to the target."),
    ("jtag", "JTAG", 20_000_000, 1_000_000, ["TCK", "TMS", "TDI", "TDO"], ("TMS", True),
     [("jtag", "JTAG", {"tck": "TCK", "tms": "TMS", "tdi": "TDI", "tdo": "TDO"}, {}, None)],
     "TCK to CH1, TMS to CH2, TDI to CH3, TDO to CH4."),
    ("ir-nec", "IR remote control (NEC)", 200_000, 200_000, ["IR"], ("IR", True),
     [("ir_nec", "IR NEC", {"ir": "IR"}, {"polarity": "active-low"}, None)],
     ("The output of an IR receiver (TSOP38238 and similar) to CH1. One second: press a button of the "
     "remote.")),
    ("usb-low-speed", "USB low speed (1.5 Mbit/s)", 12_000_000, 1_000_000, ["D+", "D-"], ("D-", True),
     [("usb_signalling", "USB signalling", {"dp": "D+", "dm": "D-"}, {"signalling": "low-speed"}, None),
      ("usb_packet", "USB packets", {}, {"signalling": "low-speed"}, 0)],
     "D+ to CH1, D- to CH2, GND to GND (keyboards, mice). Only listen: do not load the lines."),
    ("usb-full-speed", "USB full speed (12 Mbit/s)", 100_000_000, 1_000_000, ["D+", "D-"], ("D+", True),
     [("usb_signalling", "USB signalling", {"dp": "D+", "dm": "D-"}, {"signalling": "full-speed"}, None),
      ("usb_packet", "USB packets", {}, {"signalling": "full-speed"}, 0)],
     "D+ to CH1, D- to CH2, GND to GND. Needs 100 MHz: with less the decoder misses bits."),
]


def make(registry: DecoderRegistry, entry) -> Profile:
    _file, name, rate, samples, channels, trigger, decoders, notes = entry
    numbers = {channel: number for number, channel in enumerate(channels)}
    session = CaptureSession(frequency=rate, pre_trigger_samples=samples // 10,
                             post_trigger_samples=samples - samples // 10)
    session.capture_channels = [AnalyzerChannel(channel_number=number, channel_name=channel)
                                for channel, number in numbers.items()]
    if trigger is None:
        session.trigger_type = TriggerType.IMMEDIATE
    else:
        session.trigger_type = TriggerType.EDGE
        session.trigger_channel = numbers[trigger[0]]
        session.trigger_inverted = trigger[1]
    configuration = []
    for colour, (decoder_id, label, channel_map, options, parent) in enumerate(decoders):
        info = registry.get(decoder_id)
        if info is None:
            raise SystemExit(f"{name}: the decoder {decoder_id} is not installed")
        indexes = {channel.id: channel.index for channel in info.channels}
        unknown = set(options) - {option.id for option in info.options}
        if set(channel_map) - set(indexes) or unknown:
            raise SystemExit(f"{name}: {decoder_id} has no {sorted(set(channel_map) - set(indexes)) or sorted(unknown)}")
        configuration.append({
            "decoder_id": decoder_id,
            "label": label,
            "channel_map": {str(indexes[key]): numbers[channel] for key, channel in channel_map.items()},
            "options": {**info.default_options(), **options},
            "color_index": colour,
            "enabled": True,
            "parent": parent,
        })
    return Profile(name=name, capture_settings=session, decoder_configuration=configuration, notes=notes)


def main() -> None:
    registry = DecoderRegistry()
    os.makedirs(OUTPUT, exist_ok=True)
    for entry in PROFILES:
        path = os.path.join(OUTPUT, entry[0] + ".json")
        write_profiles_file(path, [make(registry, entry)])
        print(path)


if __name__ == "__main__":
    main()
