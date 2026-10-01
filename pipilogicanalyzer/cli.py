# Copyright (C) 2026 Julian Decker
#
# Part of PiPiLogicAnalyzer.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Command line interface: ``pipilogicanalyzer-cli``.

::

    pipilogicanalyzer-cli devices
    pipilogicanalyzer-cli info pico:/dev/cu.usbmodem1
    pipilogicanalyzer-cli capture --channels 0-7 --rate 10M --duration 5ms --pre 1000 \\
        --trigger edge:0:rising -o capture.sr
    pipilogicanalyzer-cli capture --channels 0,1 --rate 1M --samples 100000 --trigger immediate \\
        --decode i2c:scl=0,sda=1 --annotations i2c.csv -o capture.lac
    pipilogicanalyzer-cli decode capture.sr --decoder uart:rx=0,baudrate=115200 -o uart.json
    pipilogicanalyzer-cli convert capture.lac capture.vcd
    pipilogicanalyzer-cli decoders

Errors end the program with a non-zero exit code and one line on stderr. The user interface
(Qt widgets) is never loaded.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Optional, Sequence

from . import __version__

PROGRAM = "pipilogicanalyzer-cli"


class CliError(Exception):
    """An error shown as one line on stderr."""


# ---------------------------------------------------------------------- parsing
def parse_channels(text: str) -> list[int]:
    """``"0-7,9"`` -> ``[0, 1, ..., 7, 9]`` (channel numbers counted from 0)."""
    channels: list[int] = []
    for part in (text or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if "-" in part:
                first, last = (int(value) for value in part.split("-", 1))
                if last < first:
                    raise ValueError
                channels.extend(range(first, last + 1))
            else:
                channels.append(int(part))
        except ValueError:
            raise CliError(f"Invalid channel list '{text}': use e.g. 0-7,9.") from None
    if not channels:
        raise CliError("Select at least one channel.")
    return list(dict.fromkeys(channels))


def parse_trigger(text: Optional[str]):
    """``edge:0[:rising|falling]``, ``pattern:0=1,1=0``, ``fast:0=1,1=0``, ``immediate`` or
    ``simulation[:<pattern>]``; ``None``: the default of the device."""
    from . import api

    if not text:
        return None
    kind, _, rest = text.strip().partition(":")
    kind = kind.lower()
    try:
        if kind == "immediate" and not rest:
            return api.Immediate()
        if kind == "simulation":
            return api.Simulation(int(rest) if rest else 0)
        if kind == "edge":
            channel, _, edge = rest.partition(":")
            edge = (edge or "rising").lower()
            if edge not in ("rising", "falling", "r", "f", "pos", "neg"):
                raise ValueError
            return api.Edge(int(channel), rising=edge in ("rising", "r", "pos"))
        if kind in ("pattern", "fast"):
            levels = {}
            for item in rest.split(","):
                channel, _, level = item.partition("=")
                levels[int(channel)] = int(level)
            return api.Pattern(levels, fast=kind == "fast")
    except ValueError:
        pass
    raise CliError(
        f"Invalid trigger '{text}': use edge:<channel>:rising|falling, pattern:<ch>=<0|1>,..., "
        "fast:<ch>=<0|1>,... or immediate."
    )


def parse_decoder_spec(text: str) -> tuple[str, dict[str, str]]:
    """``"i2c:scl=0,sda=1,address_format=unshifted"`` -> ``("i2c", {...})``."""
    decoder, _, rest = text.strip().partition(":")
    if not decoder:
        raise CliError(f"Invalid decoder '{text}': use <decoder>:<channel>=<n>,<option>=<value>.")
    settings: dict[str, str] = {}
    for item in rest.split(","):
        if not item.strip():
            continue
        key, separator, value = item.partition("=")
        if not separator or not key.strip():
            raise CliError(f"Invalid decoder setting '{item}' in '{text}': use <name>=<value>.")
        settings[key.strip()] = value.strip()
    return decoder, settings


# --------------------------------------------------------------------- decoding
def run_decoders(capture, specs: Sequence[str], registry) -> list:
    """Run the ``--decode`` specifications; a decoder that works on another decoder's output is
    stacked on the decoder before it."""
    chains: list[dict[str, Any]] = []
    for spec in specs:
        decoder_id, settings = parse_decoder_spec(spec)
        info = registry.get(decoder_id)
        if info is None:
            raise CliError(f"Decoder '{decoder_id}' is not installed (list them with '{PROGRAM} decoders').")
        channel_keys = {key.lower() for channel in info.channels for key in (channel.id, channel.name)}
        channels = {key: value for key, value in settings.items() if key.lower() in channel_keys}
        options = {key: value for key, value in settings.items() if key.lower() not in channel_keys}
        if info.is_base_decoder or not chains:
            chains.append({"decoder": decoder_id, "channels": channels, "options": options, "stack": []})
        else:
            if channels:
                raise CliError(f"'{decoder_id}' decodes the output of another decoder: it takes no channels.")
            chains[-1]["stack"].append((decoder_id, options))

    groups = []
    for chain in chains:
        groups.extend(capture.decode_groups(registry=registry, **chain))
    for group in groups:
        if group.error:
            print(f"{PROGRAM}: warning: {group.decoder_name}: {group.error}", file=sys.stderr)
    return groups


def _write_annotations(capture, groups, path: Optional[str]) -> None:
    from .core.annotation_export import annotation_records

    if path:
        count = capture.export_annotations(path, groups)
        print(f"{count} annotations written to {path}")
        return
    for record in annotation_records(groups, capture.session):
        print(f"{record.start_time:.9f}\t{record.decoder}\t{record.row}\t{record.value}")


def _registry(args):
    from . import api

    return api.decoder_registry(tuple(args.decoders_dir or ()))


# --------------------------------------------------------------------- commands
def command_devices(args) -> int:
    from . import api

    found = api.devices()
    if not found:
        print("No device found (network boards: pico-net:<address>:<port>).", file=sys.stderr)
        return 1
    for device in found:
        print(f"{device.id}\t{device.label}")
    return 0


def command_info(args) -> int:
    from .core.sigrok_session import samplerate_string

    with _open(args) as device:
        driver = device.driver
        for title, rows in device.describe():
            print(title)
            for name, value in rows:
                print(f"  {name}: {value}")
        print("Capture")
        print(f"  Channels: {device.channel_count}")
        print(f"  Highest rate: {samplerate_string(device.max_frequency)}")
        print(f"  Modes: {', '.join(device.acquisition_modes())}")
        for mode in device.acquisition_modes():
            acquisition = mode if driver.acquisition_modes() else None
            channels = list(range(device.channel_count))
            limits = driver.get_limits(channels, acquisition)
            rate = driver.max_frequency_for(channels, acquisition)
            print(
                f"  {mode.capitalize()}, all channels: up to {limits.max_total_samples:,} samples, "
                f"{samplerate_string(rate)}"
            )
        capabilities = sorted(device.capabilities)
        if capabilities:
            print(f"  Capabilities: {', '.join(capabilities)}")
    return 0


def command_capture(args) -> int:
    from .core.sigrok_session import samplerate_string

    channels = parse_channels(args.channels)
    trigger = parse_trigger(args.trigger)
    names = [name.strip() for name in args.names.split(",")] if args.names else None
    with _open(args) as device:
        session = device.build_session(
            channels=channels,
            rate=args.rate,
            samples=args.samples,
            duration=args.duration,
            pre_trigger=args.pre,
            trigger=trigger,
            mode=args.mode,
            names=names,
            threshold=args.threshold,
            software_trigger=args.software_trigger,
        )
        if not args.quiet:
            print(
                f"Capturing {session.total_samples:,} samples of {len(channels)} channels at "
                f"{samplerate_string(session.frequency)} "
                f"(trigger: {session.trigger_description()})...",
                file=sys.stderr,
            )
        capture = device.run(session, timeout=args.timeout)

    capture.save(args.output, include_time=args.time)
    print(f"{capture.sample_count:,} samples of {len(capture.channels)} channels written to {args.output}")
    if args.decode:
        groups = run_decoders(capture, args.decode, _registry(args))
        _write_annotations(capture, groups, args.annotations)
    elif args.annotations:
        raise CliError("--annotations needs at least one --decode.")
    return 0


def command_decode(args) -> int:
    from . import api

    capture = api.load(args.file)
    groups = run_decoders(capture, args.decoder, _registry(args))
    _write_annotations(capture, groups, args.output)
    return 0


def command_convert(args) -> int:
    from . import api

    capture = api.load(args.input)
    capture.save(args.output, include_time=args.time)
    print(f"{args.input} -> {args.output}")
    return 0


def command_decoders(args) -> int:
    registry = _registry(args)
    infos = registry.decoders
    if not infos:
        print("No decoders found.", file=sys.stderr)
        return 1
    for info in infos:
        channels = ",".join(channel.id for channel in info.channels)
        inputs = "" if info.is_base_decoder else f" (on {', '.join(info.inputs)})"
        print(f"{info.id}\t{info.longname}{inputs}\t{channels}")
    return 0


def _open(args):
    from . import api

    return api.open(args.device, download_bitstream=getattr(args, "download_bitstream", False))


# ------------------------------------------------------------------------ main
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description="Capture, convert and decode logic analyzer data without the user interface.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--decoders-dir", action="append", metavar="DIR",
        help="additional directory with sigrok decoders (repeatable)",
    )
    commands = parser.add_subparsers(dest="command", metavar="command")
    commands.required = True

    devices = commands.add_parser("devices", help="list the connected devices")
    devices.set_defaults(handler=command_devices)

    device_help = (
        "device: pico:<port>, pico-net:<address>:<port>, pico-multi:<port>,<port>, dslogic[:<bus>:<address>], "
        "emulated, a serial port or <address>:<port> (default: the first device found)"
    )
    info = commands.add_parser("info", help="describe a device")
    info.add_argument("device", nargs="?", help=device_help)
    info.add_argument("--download-bitstream", action="store_true", help="download a missing DSLogic FPGA bitstream")
    info.set_defaults(handler=command_info)

    capture = commands.add_parser("capture", help="capture and save the samples")
    capture.add_argument("--device", "-d", help=device_help)
    capture.add_argument("--channels", "-c", default="0-7", help="channels, counted from 0 (default: 0-7)")
    capture.add_argument("--names", help="channel names, comma separated")
    capture.add_argument("--rate", "-r", help="samples per second: 10M, 100k, 1e6 (default: the highest)")
    length = capture.add_mutually_exclusive_group()
    length.add_argument("--samples", "-n", type=int, help="total samples, including --pre")
    length.add_argument("--duration", "-t", help="time to capture: 5ms, 250us, 1s")
    capture.add_argument("--pre", type=int, help="samples before the trigger")
    capture.add_argument(
        "--trigger",
        help="edge:<ch>:rising|falling, pattern:<ch>=<0|1>,..., fast:<ch>=<0|1>,..., immediate "
        "or simulation[:<n>] (default: immediate if the device can)",
    )
    capture.add_argument("--mode", choices=("buffer", "stream"), default="buffer", help="acquisition mode")
    capture.add_argument("--threshold", type=float, help="input threshold in volts (DSLogic)")
    capture.add_argument(
        "--software-trigger", action="store_true",
        help="look for the trigger in a stream (needs --mode stream): any channel, also on devices without stream triggers",
    )
    capture.add_argument("--timeout", type=float, help="seconds to wait for the trigger")
    capture.add_argument("--output", "-o", required=True, help="output file: .lac, .lac.gz, .sr, .csv or .vcd")
    capture.add_argument("--time", action="store_true", help="CSV: add a time column")
    capture.add_argument(
        "--decode", action="append", metavar="DECODER",
        help="decode the capture: <decoder>:<channel>=<n>,<option>=<value> (repeatable)",
    )
    capture.add_argument("--annotations", "-a", help="annotations file (.csv or .json; default: stdout)")
    capture.add_argument("--download-bitstream", action="store_true", help="download a missing DSLogic FPGA bitstream")
    capture.add_argument("--quiet", "-q", action="store_true", help="no progress message")
    capture.set_defaults(handler=command_capture)

    decode = commands.add_parser("decode", help="decode a capture file")
    decode.add_argument("file", help="capture: .lac, .lac.gz or .sr")
    decode.add_argument(
        "--decoder", "-D", action="append", required=True, metavar="DECODER",
        help="<decoder>:<channel>=<n>,<option>=<value>; a decoder on another decoder's output "
        "is stacked on the one before it (repeatable)",
    )
    decode.add_argument("--output", "-o", help="annotations file (.csv or .json; default: stdout)")
    decode.set_defaults(handler=command_decode)

    convert = commands.add_parser("convert", help="convert a capture file")
    convert.add_argument("input", help=".lac, .lac.gz or .sr")
    convert.add_argument("output", help=".lac, .lac.gz, .sr, .csv or .vcd")
    convert.add_argument("--time", action="store_true", help="CSV: add a time column")
    convert.set_defaults(handler=command_convert)

    decoders = commands.add_parser("decoders", help="list the available sigrok decoders")
    decoders.set_defaults(handler=command_decoders)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    from .driver.base import DeviceConnectionError, UnsupportedFeatureError
    from .driver.dslogic.driver import BitstreamMissingError

    try:
        return int(args.handler(args) or 0)
    except BitstreamMissingError as missing:
        from .driver.dslogic import resources

        hint = (
            "run again with --download-bitstream to fetch it from the DSView repository"
            if resources.downloadable(missing.name)
            else "install DSView"
        )
        _error(
            f"The {missing.model} needs its FPGA bitstream {missing.name} (part of DSView): {hint}, "
            f"or copy it into {resources.cache_directory()}."
        )
    except BrokenPipeError:
        # the output was piped into a program that stopped reading (head)
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except OSError:
            pass
        return 1
    except KeyboardInterrupt:
        _error("Interrupted.")
        return 130
    except (
        CliError, DeviceConnectionError, UnsupportedFeatureError, ValueError, KeyError, OSError,
        RuntimeError, TimeoutError,
    ) as error:
        message = error.args[0] if isinstance(error, KeyError) and error.args else error
        _error(str(message) or type(error).__name__)
    return 1


def _error(message: str) -> None:
    print(f"{PROGRAM}: error: {' '.join(str(message).split())}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
