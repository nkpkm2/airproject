#!/usr/bin/env python3
"""Publish a disposable virtual-device fleet to the local MQTT broker."""

from __future__ import annotations

import argparse
import json
import os
import socket
import time
from pathlib import Path

def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def mqtt_string(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return len(encoded).to_bytes(2, "big") + encoded


def remaining_length(length: int) -> bytes:
    encoded = bytearray()
    while True:
        digit = length % 128
        length //= 128
        if length:
            digit |= 0x80
        encoded.append(digit)
        if not length:
            return bytes(encoded)


def packet(kind: int, payload: bytes) -> bytes:
    return bytes([kind]) + remaining_length(len(payload)) + payload


def publish_packet(topic: str, payload: str) -> bytes:
    body = mqtt_string(topic) + payload.encode("utf-8")
    return packet(0x30, body)  # MQTT 3.1.1 QoS 0 PUBLISH


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--devices", type=int, default=30)
    parser.add_argument("--rate", type=float, default=20.0, help="telemetry Hz per device")
    parser.add_argument("--duration", type=float, default=60.0)
    args = parser.parse_args()
    if args.devices < 1 or args.rate <= 0 or args.duration <= 0:
        parser.error("devices, rate and duration must be positive")

    root = Path(__file__).resolve().parents[1]
    load_env_file(root / "control-service" / ".env")
    host = os.getenv("MQTT_HOST", "127.0.0.1")
    port = int(os.getenv("MQTT_PORT", "1883"))
    username = os.getenv("MQTT_USERNAME")
    password = os.getenv("MQTT_PASSWORD")
    flags = 0x02 | (0x80 if username else 0) | (0x40 if password else 0)
    connect_body = mqtt_string("MQTT") + bytes([4, flags]) + (20).to_bytes(2, "big")
    connect_body += mqtt_string("air-fleet-stress")
    if username:
        connect_body += mqtt_string(username)
    if password:
        connect_body += mqtt_string(password)
    sock = socket.create_connection((host, port), timeout=5)
    sock.sendall(packet(0x10, connect_body))
    connack = sock.recv(4)
    if len(connack) != 4 or connack[0] != 0x20 or connack[3] != 0:
        raise RuntimeError(f"MQTT connection rejected: {connack.hex()}")

    prefix = f"loadtest-{int(time.time())}"
    interval = 1.0 / args.rate
    started = time.monotonic()
    deadline = started + args.duration
    next_frame = started
    sequence = 0
    published = 0
    errors = 0
    try:
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now < next_frame:
                time.sleep(min(next_frame - now, 0.005))
                continue
            frame = bytearray()
            for index in range(args.devices):
                device_id = f"{prefix}-{index:02d}"
                payload = json.dumps({"seq": sequence, "adc_mv": [12, 18, 9, 14]}, separators=(",", ":"))
                frame += publish_packet(f"air/v1/{device_id}/telemetry", payload)
                if sequence % max(1, round(args.rate * 2)) == 0:
                    heartbeat = json.dumps({"uptime_ms": int((now - started) * 1000), "rssi_dbm": -60})
                    frame += publish_packet(f"air/v1/{device_id}/heartbeat", heartbeat)
            try:
                sock.sendall(frame)
                published += args.devices
            except OSError:
                errors += args.devices
                break
            sequence += 1
            next_frame += interval
    finally:
        try:
            sock.sendall(b"\xe0\x00")
        except OSError:
            pass
        sock.close()

    elapsed = time.monotonic() - started
    expected = args.devices * sequence
    print(json.dumps({
        "devices": args.devices,
        "requested_hz_per_device": args.rate,
        "duration_s": round(elapsed, 3),
        "frames_per_device": sequence,
        "expected_messages": expected,
        "accepted_messages": published,
        "publish_errors": errors,
        "aggregate_messages_per_second": round(published / elapsed, 1),
    }, ensure_ascii=False))
    return 0 if published == expected and errors == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
