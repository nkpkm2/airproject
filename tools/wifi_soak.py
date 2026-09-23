"""Collect a reproducible ESP32 Wi-Fi/MQTT soak-test report over USB serial."""
from __future__ import annotations

import argparse
import json
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import serial


DISCONNECT = re.compile(r"WIFI_DISCONNECT reason=(\d+)\|([^ ]+)")
WIFI_STATUS = re.compile(r"WIFI_STATUS [^|]+\|[^|]+\|(-?\d+)$")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", default="COM6")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--duration", type=int, default=300, help="seconds")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    started_wall = datetime.now(timezone.utc)
    started = time.monotonic()
    deadline = started + args.duration
    next_progress = started + 30
    reasons: Counter[str] = Counter()
    events: list[dict[str, object]] = []
    wifi_ready = 0
    mqtt_online = 0
    radio_recoveries = 0
    scan_errors = 0
    rssi_samples: list[int] = []

    with serial.Serial(args.port, args.baud, timeout=0.25, dsrdtr=False, rtscts=False) as port:
        port.dtr = False
        port.rts = False
        while time.monotonic() < deadline:
            raw = port.readline()
            if raw:
                line = raw.decode("utf-8", errors="replace").strip()
                match = DISCONNECT.search(line)
                status_match = WIFI_STATUS.search(line)
                if status_match:
                    rssi_samples.append(int(status_match.group(1)))
                relevant = not status_match and (match or any(token in line for token in (
                    "Wi-Fi", "WIFI_", "MQTT_", "[PASS]", "[FAIL]", "[ISO", "[MATRIX", "Guru", "rst:"
                )))
                if relevant:
                    elapsed = round(time.monotonic() - started, 3)
                    events.append({"elapsed_s": elapsed, "line": line})
                    print(f"{elapsed:8.3f}s {line}", flush=True)
                if match:
                    reasons[f"{match.group(1)}:{match.group(2)}"] += 1
                if "[PASS] Wi-Fi ready." in line:
                    wifi_ready += 1
                if "MQTT_STATE online" in line:
                    mqtt_online += 1
                if "Restarting Wi-Fi radio" in line:
                    radio_recoveries += 1
                if "scan error" in line.lower():
                    scan_errors += 1
            now = time.monotonic()
            if now >= next_progress:
                print(f"PROGRESS elapsed_s={int(now - started)} events={len(events)}", flush=True)
                next_progress += 30

    report = {
        "started_at": started_wall.isoformat(),
        "duration_s": round(time.monotonic() - started, 2),
        "port": args.port,
        "wifi_ready_events": wifi_ready,
        "mqtt_online_events": mqtt_online,
        "disconnect_reasons": dict(reasons),
        "radio_recoveries": radio_recoveries,
        "scan_errors": scan_errors,
        "rssi_samples": len(rssi_samples),
        "rssi_min_dbm": min(rssi_samples) if rssi_samples else None,
        "rssi_max_dbm": max(rssi_samples) if rssi_samples else None,
        "rssi_average_dbm": round(sum(rssi_samples) / len(rssi_samples), 1) if rssi_samples else None,
        "events": events,
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("SUMMARY " + json.dumps({key: value for key, value in report.items() if key != "events"}, ensure_ascii=False))
    return 0 if wifi_ready and mqtt_online and scan_errors == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
