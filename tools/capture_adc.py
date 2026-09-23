"""Capture ESP32-C3 ADC and Wi-Fi status frames from the USB serial console."""

import argparse
import time

import serial


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", default="COM6")
    parser.add_argument("--seconds", type=float, default=10.0)
    args = parser.parse_args()

    with serial.Serial(args.port, 115200, timeout=0.25) as device:
        device.reset_input_buffer()
        started = time.monotonic()
        while time.monotonic() - started < args.seconds:
            line = device.readline().decode("utf-8", "replace").strip()
            if line.startswith(("ADC_STATUS", "WIFI_STATUS")):
                elapsed = time.monotonic() - started
                print(f"{elapsed:7.3f}s {line}")


if __name__ == "__main__":
    main()
