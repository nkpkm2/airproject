#!/usr/bin/env python3
"""End-to-end endurance test through the same HTTP/MQTT path as the UI."""

from __future__ import annotations

import argparse
import json
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def request_json(url: str, body: dict | None = None) -> object:
    data = json.dumps(body).encode() if body is not None else None
    request = Request(url, data=data, headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=5) as response:
        return json.loads(response.read())


class Endurance:
    def __init__(self, base_url: str, device_id: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.device_id = device_id
        self.events: list[dict[str, object]] = []
        self.failures: list[str] = []
        self.started = time.monotonic()

    def log(self, event: str, **fields: object) -> None:
        record = {"elapsed_s": round(time.monotonic() - self.started, 3), "event": event, **fields}
        self.events.append(record)
        print(json.dumps(record, ensure_ascii=False), flush=True)

    def device(self) -> dict:
        devices = request_json(f"{self.base_url}/devices")
        return next(item for item in devices if item.get("device_id") == self.device_id)

    def wait_online(self, timeout: float = 45, max_uptime: int | None = None) -> tuple[dict, float]:
        started = time.monotonic()
        while time.monotonic() - started < timeout:
            try:
                device = self.device()
                uptime = int(device.get("heartbeat", {}).get("uptime_ms", 2**63 - 1))
                if device.get("connection_state") == "online" and (max_uptime is None or uptime < max_uptime):
                    return device, time.monotonic() - started
            except (HTTPError, URLError, StopIteration, ValueError):
                pass
            time.sleep(0.5)
        raise TimeoutError("device did not return online")

    def command(self, command_type: str, **extra: object) -> tuple[str, float]:
        response = request_json(
            f"{self.base_url}/devices/{self.device_id}/commands",
            {"type": command_type, **extra},
        )
        request_id = str(response["request_id"])
        started = time.monotonic()
        while time.monotonic() - started < 20:
            device = self.device()
            command = next(
                (item for item in device.get("commands", []) if item.get("request_id") == request_id),
                None,
            )
            if command and command.get("status") == "acknowledged":
                latency = time.monotonic() - started
                self.log("command_ack", type=command_type, request_id=request_id, latency_s=round(latency, 3))
                return request_id, latency
            if command and command.get("status") in {"failed", "timeout"}:
                raise RuntimeError(f"{command_type} {command.get('status')}: {command.get('error', '')}")
            time.sleep(0.25)
        raise TimeoutError(f"{command_type} acknowledgement timeout")

    def observe(self, duration: float, label: str) -> dict:
        started = time.monotonic()
        states: dict[str, int] = {}
        rssi: list[int] = []
        disconnects: list[int] = []
        recoveries: list[int] = []
        while time.monotonic() - started < duration:
            try:
                device = self.device()
                state = str(device.get("connection_state", "missing"))
                states[state] = states.get(state, 0) + 1
                heartbeat = device.get("heartbeat", {})
                if isinstance(heartbeat.get("rssi_dbm"), int):
                    rssi.append(heartbeat["rssi_dbm"])
                disconnects.append(int(heartbeat.get("disconnects", 0)))
                recoveries.append(int(heartbeat.get("radio_recoveries", 0)))
            except (HTTPError, URLError, StopIteration, ValueError):
                states["service-error"] = states.get("service-error", 0) + 1
            time.sleep(1)
        result = {
            "label": label,
            "duration_s": round(time.monotonic() - started, 2),
            "states": states,
            "rssi_min": min(rssi) if rssi else None,
            "rssi_max": max(rssi) if rssi else None,
            "rssi_mean": round(statistics.mean(rssi), 1) if rssi else None,
            "disconnect_delta": max(disconnects) - min(disconnects) if disconnects else None,
            "recovery_delta": max(recoveries) - min(recoveries) if recoveries else None,
        }
        self.log("observation_complete", **result)
        return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8780/api/v1")
    parser.add_argument("--device", default="pur-e83dc1951534")
    parser.add_argument("--reboots", type=int, default=5)
    parser.add_argument("--rate-seconds", type=int, default=30)
    parser.add_argument("--idle-seconds", type=int, default=300)
    parser.add_argument("--report", type=Path, default=Path("docs/system-endurance-latest.json"))
    args = parser.parse_args()
    test = Endurance(args.base_url, args.device)
    report: dict[str, object] = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "device_id": args.device,
        "reboots": [],
        "rates": [],
        "observations": [],
    }
    try:
        baseline, _ = test.wait_online()
        test.log("baseline_online", heartbeat=baseline.get("heartbeat"))
        report["observations"].append(test.observe(30, "baseline"))

        for cycle in range(1, args.reboots + 1):
            before = test.device()
            old_uptime = int(before.get("heartbeat", {}).get("uptime_ms", 0))
            _, ack_latency = test.command("device.reboot")
            online, recovery = test.wait_online(timeout=60, max_uptime=old_uptime)
            row = {
                "cycle": cycle,
                "ack_latency_s": round(ack_latency, 3),
                "recovery_s": round(recovery, 3),
                "uptime_ms": online.get("heartbeat", {}).get("uptime_ms"),
                "rssi_dbm": online.get("heartbeat", {}).get("rssi_dbm"),
            }
            report["reboots"].append(row)
            test.log("reboot_recovered", **row)
            time.sleep(3)

        for rate in (10, 20, 30, 40, 50, 60, 120):
            test.command("debug.configure", adc_stream_hz=rate, expires_in_s=args.rate_seconds + 15)
            time.sleep(7)
            rates: list[float] = []
            gaps_before: int | None = None
            states: dict[str, int] = {}
            started = time.monotonic()
            while time.monotonic() - started < args.rate_seconds:
                device = test.device()
                state = str(device.get("connection_state", "missing"))
                states[state] = states.get(state, 0) + 1
                stats = device.get("telemetry_stats", {})
                measured = stats.get("received_hz")
                if isinstance(measured, (int, float)):
                    rates.append(float(measured))
                if gaps_before is None:
                    gaps_before = int(stats.get("sequence_gaps", 0))
                time.sleep(1)
            final = test.device().get("telemetry_stats", {})
            row = {
                "requested_hz": rate,
                "mean_hz": round(statistics.mean(rates), 1) if rates else None,
                "min_hz": min(rates) if rates else None,
                "max_hz": max(rates) if rates else None,
                "sequence_gap_delta": int(final.get("sequence_gaps", 0)) - (gaps_before or 0),
                "states": states,
            }
            report["rates"].append(row)
            test.log("rate_complete", **row)

        test.command("debug.configure", adc_stream_hz=0, expires_in_s=1)
        report["observations"].append(test.observe(args.idle_seconds, "final-idle"))
    except Exception as error:  # report partial progress before failing
        test.failures.append(f"{type(error).__name__}: {error}")
        test.log("fatal", error=test.failures[-1])
    report["duration_s"] = round(time.monotonic() - test.started, 2)
    report["failures"] = test.failures
    report["events"] = test.events
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("SUMMARY " + json.dumps({key: value for key, value in report.items() if key != "events"}, ensure_ascii=False), flush=True)
    return 0 if not test.failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
