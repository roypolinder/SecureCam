#!/usr/bin/env python3
"""Read a PIR sensor directly and print every change. Useful before trusting the service."""

from __future__ import annotations

import argparse
import sys
import time


def main() -> int:
    parser = argparse.ArgumentParser(description="Watch a PIR sensor on a BCM GPIO pin")
    parser.add_argument("--gpio", type=int, default=17, help="BCM GPIO number wired to the sensor's OUT pin")
    parser.add_argument("--seconds", type=float, default=20.0, help="how long to watch (0 = forever)")
    parser.add_argument("--pull", choices=["down", "up", "none"], default="down")
    parser.add_argument(
        "--active-low",
        action="store_true",
        help="the sensor pulls the line low on motion (use together with --pull up)",
    )
    args = parser.parse_args()

    try:
        from gpiozero import DigitalInputDevice
    except ImportError:
        print(
            "gpiozero is not installed.\n"
            "  What still works: nothing in this script.\n"
            "  Do this next: sudo apt-get install python3-gpiozero python3-lgpio",
            file=sys.stderr,
        )
        return 2

    try:
        if args.pull == "none":
            sensor = DigitalInputDevice(args.gpio, pull_up=None, active_state=not args.active_low)
        else:
            sensor = DigitalInputDevice(args.gpio, pull_up=(args.pull == "up"))
    except Exception as exc:
        print(
            f"Could not open BCM GPIO {args.gpio}: {exc}\n"
            "  Likely cause: the pin number is wrong, the pin is already in use, or you lack permission.\n"
            "  Do this next: run with sudo, check the wiring, and confirm the BCM number (not the physical pin).",
            file=sys.stderr,
        )
        return 1

    print(f"Watching BCM GPIO {args.gpio}. Walk in front of the sensor. Press Ctrl+C to stop.")
    deadline = time.monotonic() + args.seconds if args.seconds > 0 else float("inf")
    last = None
    changes = 0
    try:
        while time.monotonic() < deadline:
            value = bool(sensor.value)
            if value != last:
                changes += 1 if last is not None else 0
                stamp = time.strftime("%H:%M:%S")
                print(f"  {stamp}  {'HIGH  motion' if value else 'LOW   idle'}")
                last = value
            time.sleep(0.05)
    except KeyboardInterrupt:
        print()
    finally:
        sensor.close()

    if changes == 0:
        print(
            "\nThe pin never changed state.\n"
            "  Likely cause: the sensor is not powered (VCC needs 5V on physical pin 2), the OUT wire is on a\n"
            "  different GPIO, or the sensor is still in its 60 second warm-up.\n"
            "  Do this next: wait a minute, then rerun. If it still never changes, try another GPIO number."
        )
        return 1

    print(f"\nThe sensor changed state {changes} time(s). Wiring and pin number look correct.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
