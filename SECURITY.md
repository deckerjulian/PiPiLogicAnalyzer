# Security policy

## Supported versions

Security fixes go into the latest release of PiPiLogicAnalyzer 7 (application and firmware). Older
versions and the original LogicAnalyzer 6.5 by gusmanb are not maintained here.

## Reporting a vulnerability

Please **do not open a public issue** for a security problem. Use GitHub's private vulnerability
reporting instead: *Security → Report a vulnerability* in
[this repository](https://github.com/deckerjulian/PiPiLogicAnalyzer/security/advisories/new). Only the
maintainers see the report.

Helpful in a report:

* what an attacker can achieve, and what they need to get there (a prepared capture file, a
  device on the same network, physical access to the board, …);
* the version of the application and of the firmware (*Device → Device information…*), and the
  board;
* a capture file, a WiFi configuration or the steps that trigger the problem.

You can expect a first answer within about a week. Fixed problems are credited in
[CHANGELOG.md](CHANGELOG.md) unless you prefer otherwise.

## Where problems are plausible

The application parses files and device data, and the firmware parses whatever arrives over USB
or WiFi:

* `.lac`, `.lac.gz` and profile files, and the Signal Description Language — opened from
  untrusted sources;
* protocol decoders in `decoders/`: these are **Python files that are executed**. A decoder from
  an untrusted source can do anything the application can do — treat adding decoders like
  installing software;
* the device protocol (`pipilogicanalyzer/driver/protocol.py`) and the firmware's request parsing
  (`firmware/PiPiLogicAnalyzer/PiPiLogicAnalyzer.c`), including the WiFi settings of the Pico W
  builds. The firmware has no authentication: anyone who can reach the analyzer's TCP port can
  capture and reconfigure. Keep the boards on a trusted network.

## Out of scope

The hardware design belongs to the [original project](https://github.com/gusmanb/logicanalyzer),
and problems in the bundled sigrok decoders are best reported to
[sigrok](https://sigrok.org) as well — a note here is welcome so the bundled copy can be updated.
