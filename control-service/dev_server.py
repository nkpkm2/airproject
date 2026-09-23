"""Dependency-free local MQTT control service for Windows development."""
from __future__ import annotations

import json, os, subprocess, threading, time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

ROOT = Path(__file__).parent
ENV = dict(line.split("=", 1) for line in (ROOT / ".env").read_text(encoding="utf-8-sig").splitlines() if "=" in line)
MQTT = Path(r"C:\Program Files\mosquitto")
DEVICES: dict[str, dict] = {}
LOCK = threading.Lock()
COMMAND_TIMEOUT_MS = 15_000
ONLINE_GRACE_MS = 8_000
DEGRADED_GRACE_MS = 30_000
EXPLICIT_OFFLINE_GRACE_MS = 10_000
MAX_PENDING_COMMANDS = 3
MAX_COMMAND_HISTORY = 50

def mqtt_args(exe: str) -> list[str]:
    return [str(MQTT / exe), "-h", ENV["MQTT_HOST"], "-p", ENV["MQTT_PORT"], "-u", ENV["MQTT_USERNAME"], "-P", ENV["MQTT_PASSWORD"]]

def command_for(record: dict, request_id: str) -> dict | None:
    return next((item for item in record.get("commands", []) if item.get("request_id") == request_id), None)

def expire_commands(record: dict, now: int) -> None:
    for item in record.get("commands", []):
        if item.get("status") in {"sending", "pending"} and now - item.get("sent_at_ms", now) > COMMAND_TIMEOUT_MS:
            item.update(status="timeout", completed_at_ms=now, error="设备未在 15 秒内回执")

def classify_connection(record: dict, now: int) -> tuple[str, int | None]:
    signal_at = max(record.get("_heartbeat_seen_ms", 0), record.get("_state_seen_ms", 0))
    offline_at = record.get("_availability_seen_ms", 0) if record.get("availability") == "offline" else 0
    signal_age = now - signal_at if signal_at else None
    if offline_at > signal_at:
        return ("degraded" if now - offline_at < EXPLICIT_OFFLINE_GRACE_MS else "offline", signal_age)
    if signal_age is None:
        return "offline", None
    if signal_age <= ONLINE_GRACE_MS:
        return "online", signal_age
    if signal_age <= DEGRADED_GRACE_MS:
        return "degraded", signal_age
    return "offline", signal_age

def track_telemetry(record: dict, body: object, observed_at: int) -> None:
    samples = record.setdefault("_telemetry_times", deque(maxlen=1200))
    samples.append(observed_at)
    cutoff = observed_at - 5000
    while samples and samples[0] < cutoff:
        samples.popleft()
    span_ms = samples[-1] - samples[0] if len(samples) > 1 else 0
    received_hz = round((len(samples) - 1) * 1000 / span_ms, 1) if span_ms else 0.0
    sequence_gaps = record.get("_telemetry_sequence_gaps", 0)
    if isinstance(body, dict) and isinstance(body.get("seq"), int):
        previous = record.get("_telemetry_last_seq")
        if isinstance(previous, int) and body["seq"] > previous + 1:
            sequence_gaps += body["seq"] - previous - 1
        record["_telemetry_last_seq"] = body["seq"]
        record["_telemetry_sequence_gaps"] = sequence_gaps
    record["telemetry_stats"] = {
        "received_hz": received_hz,
        "samples_in_window": len(samples),
        "sequence_gaps": sequence_gaps,
        "window_ms": min(5000, span_ms),
    }

def subscribe() -> None:
    while True:
        # Retained state is useful to MQTT clients, but it is not proof that a
        # board is alive. Ignore it at service startup. If the broker restarts,
        # relaunch the subscriber instead of silently ending this worker.
        process = subprocess.Popen(mqtt_args("mosquitto_sub.exe") + ["-R", "-t", "air/v1/#", "-v", "-q", "1"], stdout=subprocess.PIPE, text=True, encoding="utf-8")
        assert process.stdout
        for line in process.stdout:
            try:
                topic, payload = line.rstrip().split(" ", 1)
                _, _, device_id, category = topic.split("/", 3)
                try:
                    body = json.loads(payload)
                except json.JSONDecodeError:
                    body = payload if category == "availability" else {"raw": payload}
                with LOCK:
                    record = DEVICES.setdefault(device_id, {"device_id": device_id})
                    record[category] = body
                    observed_at = int(time.time() * 1000)
                    record["last_seen_ms"] = observed_at
                    record[f"_{category}_seen_ms"] = observed_at
                    if category == "telemetry":
                        track_telemetry(record, body, observed_at)
                    elif category == "ack" and isinstance(body, dict):
                        request_id = str(body.get("request_id", ""))
                        item = command_for(record, request_id)
                        if item is None and request_id:
                            item = {"request_id": request_id, "type": "unknown", "sent_at_ms": observed_at}
                            record.setdefault("commands", []).append(item)
                        if item is not None:
                            result = str(body.get("result", "unknown"))
                            item.update(
                                status="acknowledged" if result == "ok" else "failed",
                                result=result,
                                completed_at_ms=observed_at,
                            )
            except ValueError:
                continue
        time.sleep(1)

class Handler(BaseHTTPRequestHandler):
    def reply(self, status: int, body: object) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        # The static console is served at localhost:8765 while this API uses
        # 127.0.0.1:8780.  They are separate browser origins even on the same
        # PC, so explicitly permit the trusted local development console.
        self.send_header("Access-Control-Allow-Origin", "http://localhost:8765")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers(); self.wfile.write(raw)
    def do_OPTIONS(self) -> None:
        return self.reply(204, {})
    def do_GET(self) -> None:
        if urlparse(self.path).path == "/health": return self.reply(200, {"status": "ok", "runtime": "stdlib"})
        if urlparse(self.path).path == "/api/v1/devices":
            with LOCK:
                now = int(time.time() * 1000)
                devices = []
                for source in DEVICES.values():
                    expire_commands(source, now)
                    record = dict(source)
                    age = now - record.get("last_seen_ms", 0)
                    connection_state, signal_age = classify_connection(record, now)
                    record["last_seen_age_ms"] = age
                    record["heartbeat_age_ms"] = signal_age
                    record["connection_state"] = connection_state
                    # Degraded devices remain present in the fleet count. One
                    # delayed heartbeat must not make a real appliance vanish.
                    record["online"] = connection_state != "offline"
                    record["commands"] = list(record.get("commands", []))[-MAX_COMMAND_HISTORY:]
                    for key in [key for key in record if key.startswith("_")]: del record[key]
                    devices.append(record)
                return self.reply(200, devices)
        return self.reply(404, {"error": "not found"})
    def do_POST(self) -> None:
        parts = urlparse(self.path).path.split("/")
        if len(parts) != 6 or parts[1:4] != ["api", "v1", "devices"] or parts[5] != "commands": return self.reply(404, {"error": "not found"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            body.setdefault("request_id", uuid4().hex)
            device_id = parts[4]
            now = int(time.time() * 1000)
            with LOCK:
                record = DEVICES.setdefault(device_id, {"device_id": device_id})
                expire_commands(record, now)
                pending = [item for item in record.get("commands", []) if item.get("status") in {"sending", "pending"}]
                if len(pending) >= MAX_PENDING_COMMANDS:
                    return self.reply(429, {"error": "最多允许 3 条命令等待回执", "pending": len(pending)})
                item = {
                    "request_id": body["request_id"],
                    "type": body.get("type", "unknown"),
                    "status": "sending",
                    "sent_at_ms": now,
                }
                record.setdefault("commands", []).append(item)
                record["commands"] = record["commands"][-MAX_COMMAND_HISTORY:]
            try:
                subprocess.run(mqtt_args("mosquitto_pub.exe") + ["-t", f"air/v1/{device_id}/command", "-m", json.dumps(body), "-q", "1"], check=True, capture_output=True)
                with LOCK:
                    item = command_for(DEVICES[device_id], body["request_id"])
                    if item: item.update(status="pending", published_at_ms=int(time.time() * 1000))
                return self.reply(202, {"request_id": body["request_id"], "status": "pending"})
            except Exception as error:
                with LOCK:
                    item = command_for(DEVICES[device_id], body["request_id"])
                    if item: item.update(status="failed", completed_at_ms=int(time.time() * 1000), error=str(error))
                return self.reply(503, {"request_id": body["request_id"], "status": "failed", "error": str(error)})
        except Exception as error: return self.reply(400, {"error": str(error)})
    def log_message(self, *_: object) -> None: pass

if __name__ == "__main__":
    threading.Thread(target=subscribe, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", 8780), Handler).serve_forever()
