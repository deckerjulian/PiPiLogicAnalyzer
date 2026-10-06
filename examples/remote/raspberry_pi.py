"""A Raspberry Pi as a remote device of openSciLab - a template to start from.

Copy the folder ``openscilab_device`` (from the openSciLab repository) next to this script on the
Pi, switch remote devices on in openSciLab (Settings → Remote devices) and run::

    python3 raspberry_pi.py --server 192.168.1.20 --token <the token openSciLab shows>

The Pi then appears in openSciLab's device list as "pi-lab" with:

* ``cpu_temperature`` - the temperature of the CPU, once a second (a value with its time);
* ``button`` - GPIO 17 as an input, every change with the time it happened;
* ``led`` - GPIO 27 as an output, switched at the time openSciLab asks for;
* ``SYNC`` - GPIO 22 as a sync output: wire it to a channel of your logic analyzer and use the node
  remote.sync for microsecond alignment (without it the clock is measured over the network);
* the command ``uptime``.

Without a Pi (no RPi.GPIO) the pins are simulated, so the script runs on any computer.
"""

import argparse
import time

from openscilab_device import Device

try:
    import RPi.GPIO as GPIO  # noqa: N814 - the usual name

    GPIO.setmode(GPIO.BCM)
    GPIO.setup(17, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.setup(27, GPIO.OUT)
    GPIO.setup(22, GPIO.OUT)
except ImportError:  # not a Pi: pretend
    GPIO = None


def cpu_temperature() -> float:
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as handle:
            return int(handle.read()) / 1000.0
    except OSError:
        return 42.0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", help="host[:port] of openSciLab (default: found in the local network)")
    parser.add_argument("--token", default="")
    arguments = parser.parse_args()

    dev = Device("pi-lab", server=arguments.server, token=arguments.token,
                 description="Raspberry Pi with a button, an LED and a sync output")
    temperature = dev.input("cpu_temperature", kind="scalar", unit="°C")
    button = dev.input("button", kind="bool", description="GPIO 17, pressed: true")
    led = dev.output("led", kind="bool", default=False, description="GPIO 27")
    dev.sync_output("SYNC", lambda level: GPIO.output(22, level) if GPIO else None,
                    description="GPIO 22: wire it to a channel of the logic analyzer")

    @led.on_set
    def switch(value, at):  # called at the time openSciLab asked for (at: this Pi's clock)
        if GPIO:
            GPIO.output(27, value)

    @dev.command(description="Seconds since the Pi started")
    def uptime():
        with open("/proc/uptime") as handle:
            return float(handle.read().split()[0])

    if GPIO:  # every edge of the button with the time the Pi saw it
        GPIO.add_event_detect(17, GPIO.BOTH, callback=lambda pin: button.send(GPIO.input(17) == 0),
                              bouncetime=5)
    dev.start()
    print("pi-lab: connecting ... Ctrl+C ends it")
    try:
        while True:
            temperature.send(cpu_temperature())
            if not dev.connected and dev.last_error:
                print("not connected:", dev.last_error)
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        dev.stop()
        if GPIO:
            GPIO.cleanup()


if __name__ == "__main__":
    main()
