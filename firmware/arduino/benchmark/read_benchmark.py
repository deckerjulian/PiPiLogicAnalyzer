# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Runs the openSciLab Arduino benchmark on a board and prints its JSON line with the serial
throughput the host measured (``host_bytes_s``).

    python read_benchmark.py <port> [baud]
"""

import json
import sys
import time

import serial


def main() -> int:
    port = sys.argv[1]
    baud = int(sys.argv[2]) if len(sys.argv) > 2 else 115200
    with serial.Serial(port, baud, timeout=5) as line:
        time.sleep(2.5)  # boards that reset on opening
        line.reset_input_buffer()
        line.write(b"x")
        host_rate = None
        while True:
            text = line.readline().decode("ascii", "replace").strip()
            if not text:
                print("no answer from the board", file=sys.stderr)
                return 1
            if text.startswith("BLAST "):
                size = int(text.split()[1])
                start = time.perf_counter()
                received = len(line.read(size))
                host_rate = received / (time.perf_counter() - start)
                continue
            if text.startswith("{"):
                result = json.loads(text)
                result["host_bytes_s"] = round(host_rate or 0)
                print(json.dumps(result, indent=2))
                return 0


if __name__ == "__main__":
    sys.exit(main())
