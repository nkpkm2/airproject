"""Dependency-free local MQTT control service for Windows development."""
from __future__ import annotations

import json, os, subprocess, threading
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
    process = subprocess.Popen(mqtt_args("mosquitto_sub.exe") + ["-t", "air/v1/#", "-v", "-q", "1"], stdout=subprocess.PIPE, text=True, encoding="utf-8")
    assert process.stdout
    for line in process.stdout:
        try:
            topic, payload = line.rstrip().split(" ", 1)
            _, _, device_id, category = topic.split("/", 3)
            try:
                body = json.loads(payload)
            except json.JSONDecodeError:
                # Availability is intentionally the retained text "online" or
                # "offline"; other non-JSON payloads stay inspectable too.
                body = payload if category == "availability" else {"raw": payload}
            with LOCK:
                record = DEVICES.setdefault(device_id, {"device_id": device_id})
                record[category] = body
        except ValueError:
            continue

class Handler(BaseHTTPRequestHandler):
    def reply(self, status: int, body: object) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def do_GET(self) -> None:
        if urlparse(self.path).path == "/health": return self.reply(200, {"status": "ok", "runtime": "stdlib"})
        if urlparse(self.path).path == "/api/v1/devices":
            with LOCK: return self.reply(200, list(DEVICES.values()))
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
