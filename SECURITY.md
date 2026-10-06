# Security policy

## Supported versions

Security fixes go into the latest release of openSciLab (application and firmware). Older
versions, the releases under the former name PiPiLogicAnalyzer and the original LogicAnalyzer 6.5
by gusmanb are not maintained here.

## Reporting a vulnerability

Please **do not open a public issue** for a security problem. Use GitHub's private vulnerability
reporting instead: *Security → Report a vulnerability* in
[this repository](https://github.com/deckerjulian/openSciLab/security/advisories/new). Only the
maintainers see the report.

Helpful in a report:

* what an attacker can achieve, and what they need to get there (a prepared capture file, a
  device on the same network, physical access to the board, …);
* the version of the application and of the firmware (the *Details* tab of the device card), and
  the device;
* a capture file, a flow, a WiFi configuration or the steps that trigger the problem.

You can expect a first answer within about a week. Fixed problems are credited in
[CHANGELOG.md](CHANGELOG.md) unless you prefer otherwise.

## Where problems are plausible

The application parses files and device data, and the firmware parses whatever arrives over USB
or WiFi:

* `.lac`, `.lac.gz`, `.sr` and profile files, and the Signal Description Language — opened from
  untrusted sources;
* projects and flows (`project.yaml`, `*.flow.yaml`): a flow can contain **Python code**
  (`control.python` nodes) and driver plugins are Python files — open projects from sources you
  trust only;
* protocol decoders in `decoders/`: these are **Python files that are executed**. A decoder from
  an untrusted source can do anything the application can do — treat adding decoders like
  installing software;
* the device protocols (`openscilab/driver/pico/protocol.py`, `openscilab/driver/arduino/`) and the
  firmware's request parsing (`firmware/pico/main.c`, `firmware/arduino/src/`), including the WiFi
  settings of the Pico W builds. The firmware has no authentication: anyone who can reach the
  analyzer's TCP port can capture, drive its outputs and reconfigure it. Keep the boards on a
  trusted network;
* the server for remote devices (`docs/remote.md`): devices prove a shared token (HMAC of a
  challenge); it listens only while remote devices are switched on.

## Out of scope

The hardware design belongs to the [original project](https://github.com/gusmanb/logicanalyzer),
and problems in the bundled sigrok decoders are best reported to
[sigrok](https://sigrok.org) as well — a note here is welcome so the bundled copy can be updated.
