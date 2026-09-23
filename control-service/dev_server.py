"""Dependency-free local MQTT control service for Windows development."""
from __future__ import annotations

import json, os, re, subprocess, threading, time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse
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
CATALOG_PATH = ROOT / "data" / "device_catalog.json"
DEFAULT_ROOMS = ["客厅", "餐厅", "主卧", "次卧", "书房", "厨房", "玄关 / 走廊"]

def load_catalog() -> dict:
    try:
        data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        return {
            "rooms": list(data.get("rooms", DEFAULT_ROOMS)),
            "groups": list(data.get("groups", [])),
            "devices": dict(data.get("devices", {})),
        }
    except (OSError, ValueError, TypeError):
        return {"rooms": list(DEFAULT_ROOMS), "groups": [], "devices": {}}

CATALOG = load_catalog()

def save_catalog() -> None:
    CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = CATALOG_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(CATALOG, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(CATALOG_PATH)

def device_profile(device_id: str) -> dict:
    profile = CATALOG["devices"].setdefault(device_id, {
        "alias": device_id,
        "purifier_number": "",
        "room": "未分配",
        "group_ids": [],
        "hidden": False,
    })
    return profile

def public_catalog() -> dict:
    return {
        "rooms": list(CATALOG["rooms"]),
        "groups": list(CATALOG["groups"]),
        "devices": {key: dict(value) for key, value in CATALOG["devices"].items()},
    }

def safe_group_id(name: str) -> str:
    stem = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "group"
    candidate, suffix = stem, 2
    used = {item["id"] for item in CATALOG["groups"]}
    while candidate in used:
        candidate, suffix = f"{stem}-{suffix}", suffix + 1
    return candidate

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
    sequence_gaps = record.get("_telemetry_sequence_gaps", 0)
    if isinstance(body, dict) and isinstance(body.get("seq"), int):
        previous = record.get("_telemetry_last_seq")
        # Firmware starts each requested debug stream at sequence zero. Treat a
        # lower number as a new measurement session so an old stress-test gap
        # does not remain on screen forever or contaminate the rate window.
        if isinstance(previous, int) and body["seq"] < previous:
            samples.clear()
            sequence_gaps = 0
    samples.append(observed_at)
    cutoff = observed_at - 5000
    while samples and samples[0] < cutoff:
        samples.popleft()
    span_ms = samples[-1] - samples[0] if len(samples) > 1 else 0
    received_hz = round((len(samples) - 1) * 1000 / span_ms, 1) if span_ms else 0.0
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

def telemetry_view(record: dict, now: int) -> tuple[dict, int | None]:
    stats = dict(record.get("telemetry_stats", {}))
    seen_at = record.get("_telemetry_seen_ms")
    age = now - seen_at if isinstance(seen_at, int) else None
    # A completed debug stream must not leave a convincing but stale frequency
    # on the console. Preserve the last gap count while decaying live rate.
    if age is None or age > 2_000:
        stats["received_hz"] = 0.0
    return stats, age

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
                    is_new_profile = device_id not in CATALOG["devices"]
                    profile = device_profile(device_id)
                    # Deleting removes a device from the directory, not from
                    # the network. A later MQTT online announcement proves a
                    # new session and automatically discovers it again.
                    rediscovered = category == "availability" and body == "online" and profile.get("hidden")
                    if rediscovered:
                        profile["hidden"] = False
                    if is_new_profile or rediscovered:
                        save_catalog()
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
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")
        self.end_headers(); self.wfile.write(raw)
    def do_OPTIONS(self) -> None:
        return self.reply(204, {})
    def do_GET(self) -> None:
        if urlparse(self.path).path == "/health": return self.reply(200, {"status": "ok", "runtime": "stdlib"})
        if urlparse(self.path).path == "/api/v1/catalog":
            with LOCK: return self.reply(200, public_catalog())
        if urlparse(self.path).path == "/api/v1/devices":
            with LOCK:
                now = int(time.time() * 1000)
                devices = []
                for source in DEVICES.values():
                    profile = device_profile(source["device_id"])
                    if profile.get("hidden"): continue
                    expire_commands(source, now)
                    record = dict(source)
                    record["profile"] = dict(profile)
                    age = now - record.get("last_seen_ms", 0)
                    connection_state, signal_age = classify_connection(record, now)
                    record["last_seen_age_ms"] = age
                    record["heartbeat_age_ms"] = signal_age
                    record["connection_state"] = connection_state
                    # Degraded devices remain present in the fleet count. One
                    # delayed heartbeat must not make a real appliance vanish.
                    record["online"] = connection_state != "offline"
                    telemetry_stats, telemetry_age = telemetry_view(source, now)
                    record["telemetry_stats"] = telemetry_stats
                    record["telemetry_age_ms"] = telemetry_age
                    record["commands"] = list(record.get("commands", []))[-MAX_COMMAND_HISTORY:]
                    for key in [key for key in record if key.startswith("_")]: del record[key]
                    devices.append(record)
                return self.reply(200, devices)
        return self.reply(404, {"error": "not found"})
    def read_json(self) -> dict:
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path in {"/api/v1/rooms", "/api/v1/groups"}:
            try:
                body = self.read_json(); name = str(body.get("name", "")).strip()[:40]
                if not name: return self.reply(400, {"error": "name is required"})
                with LOCK:
                    if path.endswith("rooms"):
                        if name not in CATALOG["rooms"]: CATALOG["rooms"].append(name)
                        result = {"name": name}
                    else:
                        item = {"id": safe_group_id(name), "name": name, "parent_id": body.get("parent_id") or None}
                        CATALOG["groups"].append(item); result = item
                    save_catalog()
                return self.reply(201, result)
            except Exception as error: return self.reply(400, {"error": str(error)})
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
    def do_PATCH(self) -> None:
        parts = urlparse(self.path).path.split("/")
        if len(parts) != 5 or parts[1:4] != ["api", "v1", "devices"]:
            return self.reply(404, {"error": "not found"})
        try:
            body = self.read_json(); device_id = parts[4]
            with LOCK:
                profile = device_profile(device_id)
                for key in ("alias", "purifier_number", "room"):
                    if key in body: profile[key] = str(body[key]).strip()[:64]
                if "group_ids" in body:
                    valid = {item["id"] for item in CATALOG["groups"]}
                    profile["group_ids"] = [str(value) for value in body["group_ids"] if str(value) in valid]
                profile["hidden"] = False
                save_catalog(); result = dict(profile)
            return self.reply(200, result)
        except Exception as error: return self.reply(400, {"error": str(error)})
    def do_DELETE(self) -> None:
        parts = urlparse(self.path).path.split("/")
        try:
            with LOCK:
                if len(parts) == 5 and parts[1:4] == ["api", "v1", "devices"]:
                    profile = device_profile(parts[4]); profile["hidden"] = True; save_catalog()
                    return self.reply(200, {"status": "hidden", "device_id": parts[4]})
                if len(parts) == 5 and parts[1:4] == ["api", "v1", "rooms"]:
                    name = unquote(parts[4])
                    if name in CATALOG["rooms"]: CATALOG["rooms"].remove(name)
                    for profile in CATALOG["devices"].values():
                        if profile.get("room") == name: profile["room"] = "未分配"
                    save_catalog(); return self.reply(200, {"status": "deleted"})
                if len(parts) == 5 and parts[1:4] == ["api", "v1", "groups"]:
                    group_id = parts[4]
                    descendants = {group_id}
                    changed = True
                    while changed:
                        before = len(descendants)
                        descendants.update(item["id"] for item in CATALOG["groups"] if item.get("parent_id") in descendants)
                        changed = len(descendants) != before
                    CATALOG["groups"] = [item for item in CATALOG["groups"] if item["id"] not in descendants]
                    for profile in CATALOG["devices"].values():
                        profile["group_ids"] = [value for value in profile.get("group_ids", []) if value not in descendants]
                    save_catalog(); return self.reply(200, {"status": "deleted"})
            return self.reply(404, {"error": "not found"})
        except Exception as error: return self.reply(400, {"error": str(error)})
    def log_message(self, *_: object) -> None: pass

if __name__ == "__main__":
    threading.Thread(target=subscribe, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", 8780), Handler).serve_forever()
