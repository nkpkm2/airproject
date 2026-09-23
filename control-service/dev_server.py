"""Dependency-free local MQTT control service for Windows development."""
from __future__ import annotations

import json, os, subprocess, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

ROOT = Path(__file__).parent
ENV = dict(line.split("=", 1) for line in (ROOT / ".env").read_text(encoding="utf-8-sig").splitlines() if "=" in line)
MQTT = Path(r"C:\Program Files\mosquitto")
DEVICES: dict[str, dict] = {}
LOCK = threading.Lock()

def mqtt_args(exe: str) -> list[str]:
    return [str(MQTT / exe), "-h", ENV["MQTT_HOST"], "-p", ENV["MQTT_PORT"], "-u", ENV["MQTT_USERNAME"], "-P", ENV["MQTT_PASSWORD"]]

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
                    record = dict(source)
                    age = now - record.get("last_seen_ms", 0)
                    # The appliance sends a 2-second heartbeat.  A generous
                    # 7-second grace window avoids UI flicker but makes an
                    # actual broker/Wi-Fi loss visible promptly.
                    signal_at = max(record.get("_heartbeat_seen_ms", 0), record.get("_state_seen_ms", 0))
                    offline_at = record.get("_availability_seen_ms", 0) if record.get("availability") == "offline" else 0
                    record["last_seen_age_ms"] = age
                    record["online"] = now - signal_at <= 7000 and signal_at >= offline_at
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
            subprocess.run(mqtt_args("mosquitto_pub.exe") + ["-t", f"air/v1/{parts[4]}/command", "-m", json.dumps(body), "-q", "1"], check=True, capture_output=True)
            return self.reply(202, {"request_id": body["request_id"], "status": "queued"})
        except Exception as error: return self.reply(503, {"error": str(error)})
    def log_message(self, *_: object) -> None: pass

if __name__ == "__main__":
    threading.Thread(target=subscribe, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", 8780), Handler).serve_forever()
