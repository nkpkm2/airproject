# ESP32-C3 CH34x Wi-Fi variant

This is the isolated first-stage firmware for the ESP32-C3 board exposed as a
CH34x USB serial device (currently COM7). It intentionally enables only board
identity, Wi-Fi scan/connect/auto-reconnect, IP reporting, and 200 ms RSSI
telemetry. ADC and optocoupler pins remain untouched until this board's pinout
is confirmed.

The root project remains the native-USB ESP32-C3 Super Mini target.

