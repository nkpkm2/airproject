"""Dependency-free local MQTT control service for Windows development."""
from __future__ import annotations

import json, os, re, subprocess, sys, threading, time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from uuid import uuid4

ROOT = Path(__file__).parent
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
from engineering_tools import environment_report, serial_ports, tools as engineering_tools
ENV = dict(line.split("=", 1) for line in (ROOT / ".env").read_text(encoding="utf-8-sig").splitlines() if "=" in line)
MQTT = Path(r"C:\Program Files\mosquitto")
DEVICES: dict[str, dict] = {}
LOCK = threading.Lock()
COMMAND_TIMEOUT_MS = 15_000
ONLINE_GRACE_MS = 3_000
DEGRADED_GRACE_MS = 5_000
EXPLICIT_OFFLINE_GRACE_MS = 5_000
MAX_PENDING_COMMANDS = 3
MAX_COMMAND_HISTORY = 50
RUNTIME_ID = uuid4().hex
CATALOG_PATH = ROOT / "data" / "device_catalog.json"
DEFAULT_ROOMS = ["客厅", "餐厅", "主卧", "次卧", "书房", "厨房", "玄关 / 走廊"]

def canonical_device_id(value: object) -> str:
    """Use one immutable identity for both raw-MAC serial and MQTT IDs."""
    text = str(value or "").strip()[:96]
    if re.fullmatch(r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}", text, re.IGNORECASE):
        return "pur-" + text.replace(":", "").lower()
    if re.fullmatch(r"pur-[0-9a-f]{12}", text, re.IGNORECASE):
        return "pur-" + text[4:].lower()
    return text

def mac_for_device_id(value: object) -> str | None:
    text = canonical_device_id(value)
    match = re.fullmatch(r"pur-([0-9a-f]{12})", text, re.IGNORECASE)
    if not match: return None
    raw = match.group(1).upper()
    return ":".join(raw[index:index + 2] for index in range(0, 12, 2))

def load_catalog() -> dict:
    try:
        data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        source_devices = data.get("devices", {})
        devices = {}
        if isinstance(source_devices, dict):
            for device_id, profile in source_devices.items():
                if isinstance(profile, dict): devices[canonical_device_id(device_id)] = profile
        return {
            "rooms": list(data.get("rooms", DEFAULT_ROOMS)),
            "groups": list(data.get("groups", [])),
            "devices": devices,
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
    device_id = canonical_device_id(device_id)
    profile = CATALOG["devices"].setdefault(device_id, {
        "alias": device_id,
        "purifier_number": "",
        "room": "未分配",
        "group_ids": [],
        "hidden": False,
        "calibration": None,
    })
    profile.setdefault("calibration", None)
    # Migrate older saved calibration records on first use.  In particular,
    # reports produced before the dark-edge safeguard may contain a negative
    # off threshold; normalizing here prevents a low-noise 0.01 V sample from
    # being held in the lit state after a service restart.
    raw_calibration = profile.get("calibration")
    if isinstance(raw_calibration, dict) and "normalize_calibration_config" in globals():
        try:
            normalized = normalize_calibration_config(device_id, raw_calibration)
            if normalized != raw_calibration:
                profile["calibration"] = normalized
                save_catalog()
        except ValueError:
            profile["calibration"] = None
    return profile

def normalize_calibration_config(device_id: str, source: object) -> dict:
    device_id = canonical_device_id(device_id)
    if not isinstance(source, dict):
        raise ValueError("calibration config must be an object")
    source_schema = source.get("schema", source.get("format"))
    if source_schema not in {"air-light-device-calibration-v1", "air-light-calibration-analysis-v2"}:
        raise ValueError("unsupported calibration schema")
    if source.get("overall_status") not in {None, "pass"}:
        raise ValueError("calibration report is not passed")
    source_channels = source.get("channels")
    if isinstance(source_channels, list):
        source_channels = {str(item.get("channel")): item for item in source_channels if isinstance(item, dict)}
    if not isinstance(source_channels, dict):
        raise ValueError("calibration channels are required")
    channels = {}
    for key in ("low", "medium", "high", "auto"):
        item = source_channels.get(key)
        if not isinstance(item, dict) or item.get("quality") not in {None, "pass"}:
            raise ValueError(f"calibration channel {key} is not passed")
        def number(name: str, fallback: float | None = None) -> float | None:
            value = item.get(name, fallback)
            if value is None: return fallback
            try: value = float(value)
            except (TypeError, ValueError): raise ValueError(f"invalid {key}.{name}")
            if not (-100_000 <= value <= 100_000): raise ValueError(f"invalid {key}.{name}")
            return value
        threshold = number("threshold_mv", item.get("threshold_mv_at_baseline_median"))
        on_threshold = number("on_threshold_mv", item.get("on_threshold_mv_at_baseline_median", threshold))
        off_threshold = number("off_threshold_mv", item.get("off_threshold_mv_at_baseline_median", threshold))
        if threshold is None or on_threshold is None or off_threshold is None:
            raise ValueError(f"missing thresholds for {key}")
        direction = "falling" if item.get("direction") == "falling" else "rising"
        off_median = number("off_median_mv")
        on_median = number("on_median_mv")
        off_sigma = number("off_sigma_mv", 0) or 0
        on_sigma = number("on_sigma_mv", 0) or 0
        # A calibration made with a wide hysteresis can put the raw off edge
        # below zero (for example medium: -110 mV).  That is mathematically
        # valid for a line fit, but unsafe for a physical ADC: every positive
        # noise sample would then keep an already-lit lamp latched on.  Store a
        # conservative clear edge at the dark baseline (or threshold when an
        # older config has no distribution statistics).
        if direction == "rising":
            dark_edge = (off_median + max(3 * off_sigma, 2)) if off_median is not None else max(0, threshold)
            clear_threshold = max(off_threshold, dark_edge)
            if clear_threshold >= on_threshold: clear_threshold = min(off_threshold, on_threshold - 1)
        else:
            dark_edge = (off_median - max(3 * off_sigma, 2)) if off_median is not None else min(3_300, threshold)
            clear_threshold = min(off_threshold, dark_edge)
            if clear_threshold <= on_threshold: clear_threshold = max(off_threshold, on_threshold + 1)
        channels[key] = {
            "logical_mapping": str(item.get("logical_mapping", ""))[:64],
            "direction": direction,
            "off_median_mv": round(off_median, 3) if off_median is not None else None,
            "on_median_mv": round(on_median, 3) if on_median is not None else None,
            "off_sigma_mv": round(off_sigma, 3),
            "on_sigma_mv": round(on_sigma, 3),
            "delta_mv": round(number("delta_mv", 0) or 0, 3),
            "effect_size": round(number("effect_size", 0) or 0, 3),
            "distribution_overlap": round(number("distribution_overlap", 0) or 0, 5),
            "threshold_mv": round(threshold, 3),
            "on_threshold_mv": round(on_threshold, 3),
            "off_threshold_mv": round(off_threshold, 3),
            "clear_threshold_mv": round(clear_threshold, 3),
            "hysteresis_mv": round(number("hysteresis_mv", 0) or 0, 3),
            "quality": "pass",
        }
    saved_at = source.get("saved_at_ms")
    try: saved_at = int(saved_at) if saved_at is not None else int(time.time() * 1000)
    except (TypeError, ValueError): saved_at = int(time.time() * 1000)
    return {
        "schema": "air-light-device-calibration-v1",
        "device_id": device_id,
        "mac": mac_for_device_id(device_id),
        "saved_at_ms": saved_at,
        "source_format": str(source.get("source_format", source.get("format", "")))[:80],
        "expected_sequence": [str(value) for value in source.get("expected_sequence", [])][:12],
        "channels": channels,
        "quality": dict(source.get("quality", {})) if isinstance(source.get("quality"), dict) else {},
    }

def public_catalog() -> dict:
    return {
        "rooms": list(CATALOG["rooms"]),
        "groups": list(CATALOG["groups"]),
        "devices": {key: dict(value) for key, value in CATALOG["devices"].items()},
    }

def restore_catalog(data: object) -> dict:
    if not isinstance(data, dict): raise ValueError("catalog must be an object")
    rooms = [str(value).strip()[:40] for value in data.get("rooms", []) if str(value).strip()]
    groups = []
    for source in data.get("groups", []):
        if not isinstance(source, dict): continue
        group_id = re.sub(r"[^a-z0-9-]+", "-", str(source.get("id", "")).lower()).strip("-")[:48]
        name = str(source.get("name", "")).strip()[:40]
        if group_id and name: groups.append({"id": group_id, "name": name, "parent_id": source.get("parent_id") or None})
    valid_groups = {item["id"] for item in groups}
    devices = {}
    for device_id, source in data.get("devices", {}).items():
        if not isinstance(source, dict): continue
        key = canonical_device_id(device_id)
        if not key: continue
        raw_calibration = source.get("calibration")
        try:
            calibration = normalize_calibration_config(key, raw_calibration) if isinstance(raw_calibration, dict) else None
        except ValueError:
            calibration = None
        devices[key] = {
            "alias": str(source.get("alias", key)).strip()[:64] or key,
            "purifier_number": str(source.get("purifier_number", "")).strip()[:64],
            "room": str(source.get("room", "未分配")).strip()[:64] or "未分配",
            "group_ids": [str(value) for value in source.get("group_ids", []) if str(value) in valid_groups],
            "hidden": bool(source.get("hidden", False)),
            "calibration": calibration,
        }
    return {"rooms": list(dict.fromkeys(rooms or DEFAULT_ROOMS)), "groups": groups, "devices": devices}

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

def is_device_rediscovery(record: dict, profile: dict, category: str, body: object) -> bool:
    return (
        category == "availability"
        and body == "online"
        and record.get("availability") != "online"
        and bool(profile.get("hidden"))
    )

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
            record.setdefault("_telemetry_payloads", deque(maxlen=600)).clear()
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
    if isinstance(body, dict) and isinstance(body.get("adc_mv"), list):
        cursor = record.get("_telemetry_cursor", 0) + 1
        record["_telemetry_cursor"] = cursor
        record.setdefault("_telemetry_payloads", deque(maxlen=600)).append({
            "cursor": cursor,
            "observed_at_ms": observed_at,
            "seq": body.get("seq"),
            "uptime_ms": body.get("uptime_ms"),
            "adc_mv": body["adc_mv"],
            "opto": body.get("opto", {}),
        })
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
                device_id = canonical_device_id(device_id)
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
                    # A device hidden during an active session must not pop
                    # straight back into the directory when firmware repeats
                    # availability=online.  Only a real offline -> online edge
                    # proves that it has connected again after being removed.
                    rediscovered = is_device_rediscovery(record, profile, category, body)
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
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
        self.end_headers(); self.wfile.write(raw)
    def do_OPTIONS(self) -> None:
        return self.reply(204, {})
    def do_GET(self) -> None:
        parsed = urlparse(self.path); path = parsed.path
        if path == "/health": return self.reply(200, {"status": "ok", "runtime": "stdlib"})
        if path == "/api/v1/tools/environment": return self.reply(200, environment_report())
        if path == "/api/v1/tools/ports": return self.reply(200, serial_ports())
        if path == "/api/v1/tools/safety":
            return self.reply(200, engineering_tools.safety_status(parse_qs(parsed.query).get("port", [""])[0]))
        if path == "/api/v1/tools/jobs": return self.reply(200, engineering_tools.list_jobs())
        if len(path.split("/")) == 6 and path.split("/")[1:5] == ["api", "v1", "tools", "jobs"]:
            job = engineering_tools.get_job(path.split("/")[5])
            return self.reply(200, job) if job else self.reply(404, {"error": "job not found"})
        if path == "/api/v1/catalog":
            with LOCK: return self.reply(200, public_catalog())
        parts = path.split("/")
        if len(parts) == 6 and parts[1:4] == ["api", "v1", "devices"] and parts[5] == "calibration":
            device_id = canonical_device_id(unquote(parts[4]))
            with LOCK:
                profile = device_profile(device_id)
                return self.reply(200, {"device_id": device_id, "config": profile.get("calibration")})
        if len(parts) == 6 and parts[1:4] == ["api", "v1", "devices"] and parts[5] == "telemetry":
            device_id = canonical_device_id(unquote(parts[4]))
            try: after = max(0, int(parse_qs(parsed.query).get("after", ["0"])[0]))
            except ValueError: after = 0
            with LOCK:
                record = DEVICES.get(device_id)
                if record is None: return self.reply(404, {"error": "device not found"})
                buffered = record.get("_telemetry_payloads", ())
                values = [dict(sample) for sample in buffered if sample["cursor"] > after]
                return self.reply(200, {
                    "runtime_id": RUNTIME_ID,
                    "cursor": record.get("_telemetry_cursor", 0),
                    "samples": values,
                })
        if path == "/api/v1/devices":
            with LOCK:
                now = int(time.time() * 1000)
                devices = []
                # The directory is persistent management data.  After the
                # service restarts, previously discovered but currently
                # silent devices must remain visible as offline instead of
                # disappearing until their next MQTT packet.
                device_ids = list(dict.fromkeys([*DEVICES, *CATALOG["devices"]]))
                for device_id in device_ids:
                    source = DEVICES.get(device_id, {"device_id": device_id})
                    profile = device_profile(device_id)
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
        if path == "/api/v1/tools/jobs":
            try: return self.reply(202, engineering_tools.start(self.read_json()))
            except Exception as error: return self.reply(400, {"error": str(error)})
        if path == "/api/v1/catalog/restore":
            try:
                restored = restore_catalog(self.read_json().get("catalog", {}))
                with LOCK:
                    CATALOG.clear(); CATALOG.update(restored); save_catalog()
                return self.reply(200, public_catalog())
            except Exception as error: return self.reply(400, {"error": str(error)})
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
            device_id = canonical_device_id(unquote(parts[4]))
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
    def do_PUT(self) -> None:
        parts = urlparse(self.path).path.split("/")
        if len(parts) != 6 or parts[1:4] != ["api", "v1", "devices"] or parts[5] != "calibration":
            return self.reply(404, {"error": "not found"})
        try:
            body = self.read_json()
            device_id = canonical_device_id(unquote(parts[4]))
            source = body.get("config", body)
            config = normalize_calibration_config(device_id, source)
            with LOCK:
                profile = device_profile(device_id)
                profile["calibration"] = config
                profile["hidden"] = False
                save_catalog()
            return self.reply(200, {"device_id": device_id, "config": config})
        except Exception as error:
            return self.reply(400, {"error": str(error)})

    def do_PATCH(self) -> None:
        parts = urlparse(self.path).path.split("/")
        if len(parts) != 5 or parts[1:4] != ["api", "v1", "devices"]:
            return self.reply(404, {"error": "not found"})
        try:
            body = self.read_json(); device_id = canonical_device_id(unquote(parts[4]))
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
                    device_id = canonical_device_id(unquote(parts[4]))
                    profile = device_profile(device_id); profile["hidden"] = True; save_catalog()
                    return self.reply(200, {"status": "hidden", "device_id": device_id})
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
