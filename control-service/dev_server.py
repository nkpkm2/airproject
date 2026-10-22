"""Dependency-free local MQTT control service for Windows development."""
from __future__ import annotations

import json, os, re, subprocess, sys, threading, time, urllib.parse, urllib.request
from datetime import datetime
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from uuid import uuid4

ROOT = Path(__file__).parent
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
from engineering_tools import environment_report, serial_ports, tools as engineering_tools
import smart_control
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
SMART_PATH = ROOT / "data" / "smart-control.json"
SMART_RUNTIME_PATH = ROOT / "data" / "smart-control-runtime.json"
DEFAULT_ROOMS = ["客厅", "餐厅", "主卧", "次卧", "书房", "厨房", "玄关 / 走廊"]
LAMP_KEYS = ("low", "medium", "high", "auto")
LAMP_INDEX = {key: index for index, key in enumerate(LAMP_KEYS)}

def load_smart_config() -> dict:
    try: return smart_control.normalize_config(json.loads(SMART_PATH.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError): return smart_control.default_config()

def save_smart_config() -> None:
    SMART_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = SMART_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(SMART_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(SMART_PATH)

def load_smart_runtime() -> dict:
    try:
        value = json.loads(SMART_RUNTIME_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError): return {}

def save_smart_runtime() -> None:
    SMART_RUNTIME_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = SMART_RUNTIME_PATH.with_suffix(".tmp")
    safe = {key: value for key, value in SMART_RUNTIME.items() if key != "weather"}
    # Queue markers are process-local. Persist the in-flight intent before the
    # MQTT publish, but never replay an unconsumed queue after a restart.
    if isinstance(safe.get("device_actions"), dict):
        safe["device_actions"] = {
            device_id: {key: value for key, value in action.items()
                        if key not in {"queue_command", "queue_origin"}}
            for device_id, action in safe["device_actions"].items() if isinstance(action, dict)
        }
    temporary.write_text(json.dumps(safe, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(SMART_RUNTIME_PATH)

SMART_CONFIG = load_smart_config()
SMART_RUNTIME = load_smart_runtime()
SMART_RUNTIME.setdefault("weather", None)
SMART_RUNTIME.setdefault("weather_fetched_at", 0)
SMART_RUNTIME.setdefault("official_pollen", None)
SMART_RUNTIME.setdefault("official_pollen_fetched_at", 0)
SMART_RUNTIME.setdefault("official_pollen_error", "")
SMART_RUNTIME.setdefault("last_peak_at", 0)
SMART_RUNTIME.setdefault("effective_tier", "weak")
SMART_RUNTIME.setdefault("candidate_tier", "weak")
SMART_RUNTIME.setdefault("candidate_since", 0)
SMART_RUNTIME.setdefault("device_actions", {})
SMART_RUNTIME.setdefault("last_error", "")

def recover_smart_runtime_after_restart(runtime: dict) -> None:
    """Fence off non-idempotent button pulses whose outcome may be ambiguous."""
    actions = runtime.get("device_actions", {})
    if not isinstance(actions, dict): return
    for action in actions.values():
        if not isinstance(action, dict): continue
        action.pop("queue_command", None)
        action.pop("queue_origin", None)
        inflight = action.get("inflight")
        if isinstance(inflight, dict):
            inflight["restart_recovery"] = True
            action["status"] = "服务重启后正在核对上次按键结果"

recover_smart_runtime_after_restart(SMART_RUNTIME)

def restarted_pulse_recovery(inflight: dict, current_mode: str, state_seen_ms: int,
                             online: bool, now_ms: int) -> str:
    """Return confirmed, wait, or uncertain without replaying a physical pulse."""
    try:
        sent_at_ms = int(inflight.get("sent_at_ms", now_ms))
        observed_at_ms = int(state_seen_ms or 0)
        expected = inflight.get("expected")
    except (TypeError, ValueError):
        return "uncertain"
    matches = expected == current_mode or (expected == "active" and current_mode in smart_control.MODES)
    if online and observed_at_ms > sent_at_ms and matches:
        return "confirmed"
    return "uncertain" if now_ms - sent_at_ms > 12_000 else "wait"

def observe_external_mode_change(action_state: dict, current_mode: str, now_ms: int,
                                 expected_mode: str | None = None) -> bool:
    """Respect physical/other-controller mode changes for a 30-minute window."""
    valid_modes = (*smart_control.MODES, "off")
    if current_mode not in valid_modes: return False
    previous = action_state.get("last_observed_mode")
    action_state["last_observed_mode"] = current_mode
    if previous not in valid_modes or previous == current_mode: return False
    expected = (expected_mode == current_mode or
                (expected_mode == "active" and current_mode in smart_control.MODES))
    if expected: return False
    action_state.update(manual_override_until_ms=max(
        int(action_state.get("manual_override_until_ms", 0)), now_ms + 30 * 60 * 1000),
        status="检测到实体按键或其他控制改档，自动调控让出 30 分钟")
    return True

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

def classify_calibrated_lamp(channel: dict, value: object, previous: str | None = None) -> str | None:
    """Classify one raw ADC value using the same safe hysteresis as the UI."""
    if not isinstance(channel, dict) or channel.get("quality") not in {None, "pass"}: return None
    try: value = float(value)
    except (TypeError, ValueError): return None
    if not (-100_000 <= value <= 100_000): return None
    rising = channel.get("direction") != "falling"
    on = channel.get("on_threshold_mv", channel.get("threshold_mv"))
    configured_off = channel.get("off_threshold_mv", channel.get("threshold_mv"))
    try: on, configured_off = float(on), float(configured_off)
    except (TypeError, ValueError): return None
    off_median = channel.get("off_median_mv")
    on_median = channel.get("on_median_mv")
    off_sigma = max(0.0, float(channel.get("off_sigma_mv") or 0))
    try: off_median = float(off_median) if off_median is not None else None
    except (TypeError, ValueError): off_median = None
    try: on_median = float(on_median) if on_median is not None else None
    except (TypeError, ValueError): on_median = None
    span = abs(on_median - off_median) if off_median is not None and on_median is not None else 0
    dark_margin = max(3 * off_sigma, min(50, span * 0.03), 2) if off_median is not None else 0
    try: off = float(channel.get("clear_threshold_mv"))
    except (TypeError, ValueError): off = configured_off
    if rising:
        off = max(configured_off, off_median + dark_margin if off_median is not None else float(channel.get("threshold_mv", configured_off)), 0)
    else:
        off = min(configured_off, off_median - dark_margin if off_median is not None else 3300)
    # Older configs with a negative fitted off edge have no distribution
    # statistics. Until recalibration, use the on edge as the clear edge.
    if off_median is None or on_median is None:
        if rising and configured_off < 0: off = max(off, on)
        if not rising and configured_off > 3300: off = min(off, on)
    if previous == "on": return "on" if (value > off if rising else value < off) else "off"
    return "on" if (value >= on if rising else value <= on) else "off"

def remember_lamp_observation(record: dict, body: object, observed_at: int) -> None:
    if not isinstance(body, dict) or not isinstance(body.get("adc_mv"), list): return
    values = body.get("adc_mv", [])[:4]
    if len(values) != 4: return
    record.setdefault("_lamp_observations", deque(maxlen=180)).append({
        "observed_at_ms": observed_at,
        "adc_mv": values,
    })

def derive_lamp_state(record: dict, profile: dict, now: int) -> dict:
    """Derive purifier mode from calibrated lamp states, including auto flicker."""
    raw = record.get("state") if isinstance(record.get("state"), dict) else {}
    calibration = profile.get("calibration") if isinstance(profile, dict) else None
    channels = calibration.get("channels") if isinstance(calibration, dict) else None
    observations = list(record.get("_lamp_observations", ()))
    if not observations and isinstance(raw.get("adc_mv"), list):
        observations = [{"observed_at_ms": now, "adc_mv": raw.get("adc_mv")}]
    if not isinstance(channels, dict):
        raw_mode = raw.get("mode") if raw.get("mode") in (*LAMP_KEYS, "off") else "unknown"
        return {"mode": raw_mode, "powered": bool(raw.get("powered")), "install_status": "unknown", "lamps": {}, "auto_blinking": False, "reason": "no_calibration"}
    previous = {key: None for key in LAMP_KEYS}
    classified = []
    for observation in observations:
        values = observation.get("adc_mv", [])
        states = {}
        for key in LAMP_KEYS:
            states[key] = classify_calibrated_lamp(channels.get(key), values[LAMP_INDEX[key]] if len(values) == 4 else None, previous[key])
        previous = states
        classified.append({"at": observation.get("observed_at_ms", now), "lamps": states})
    latest = classified[-1]["lamps"] if classified else {key: None for key in LAMP_KEYS}
    auto_transitions = 0
    last_auto = None
    recent_cutoff = now - 2_400
    for item in classified:
        if item["at"] < recent_cutoff or item["lamps"].get("auto") is None: continue
        value = item["lamps"]["auto"]
        if last_auto is not None and value != last_auto: auto_transitions += 1
        last_auto = value
    auto_blinking = auto_transitions >= 2
    base_on = [key for key in ("low", "medium", "high") if latest.get(key) == "on"]
    known = all(latest.get(key) is not None for key in LAMP_KEYS)
    if not known:
        raw_mode = raw.get("mode") if raw.get("mode") in (*LAMP_KEYS, "off") else "unknown"
        return {"mode": raw_mode, "powered": bool(raw.get("powered")), "install_status": "unknown", "lamps": latest, "auto_blinking": auto_blinking, "reason": "incomplete_adc"}
    if len(base_on) > 1:
        return {"mode": "uninstalled", "powered": True, "install_status": "uninstalled", "lamps": latest, "auto_blinking": auto_blinking, "reason": "multiple_mode_lamps_on"}
    if len(base_on) == 1:
        if latest.get("auto") == "off" or auto_blinking:
            return {"mode": base_on[0], "powered": True, "install_status": "installed", "lamps": latest, "auto_blinking": auto_blinking, "reason": "constant_mode_priority"}
        return {"mode": "uninstalled", "powered": True, "install_status": "uninstalled", "lamps": latest, "auto_blinking": auto_blinking, "reason": "mode_and_auto_steady"}
    if latest.get("auto") == "on" or auto_blinking:
        return {"mode": "auto", "powered": True, "install_status": "installed", "lamps": latest, "auto_blinking": auto_blinking, "reason": "auto_only"}
    # All four dark is intentionally represented as off: the sensor system
    # cannot distinguish a powered purifier with uninstalled lamps from off.
    return {"mode": "off", "powered": False, "install_status": "dark_or_off", "lamps": latest, "auto_blinking": False, "reason": "all_lamps_dark"}

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

def fetch_smart_weather(config: dict) -> dict:
    """Fetch bounded public weather context; it is never presented as pollen data."""
    query = urllib.parse.urlencode({
        "latitude": config["latitude"], "longitude": config["longitude"],
        "current": "temperature_2m,relative_humidity_2m,precipitation,cloud_cover",
        "hourly": "temperature_2m,relative_humidity_2m,precipitation_probability,cloud_cover",
        "daily": "precipitation_sum,sunshine_duration", "past_days": 4, "forecast_days": 4,
        "timezone": config.get("timezone", "Asia/Shanghai"),
    })
    request = urllib.request.Request(
        f"https://api.open-meteo.com/v1/forecast?{query}",
        headers={"User-Agent": "AirProject-SmartControl/1.0"},
    )
    with urllib.request.urlopen(request, timeout=8) as response:
        body = json.loads(response.read().decode("utf-8"))
    current = body.get("current", {})
    hourly = body.get("hourly", {})
    daily = body.get("daily", {})
    if not isinstance(current, dict) or not isinstance(hourly, dict) or not isinstance(daily, dict):
        raise ValueError("天气服务返回的数据格式不正确")
    # `past_days=4` prepends historical samples to the hourly arrays. Keep a
    # bounded tail rather than the first 72 entries, otherwise the six-hour
    # outlook can contain only past observations and silently never pre-ramp.
    # The fixed 4-day-past/4-day-forecast request is normally ~192 samples.
    return {
        "latitude": float(config["latitude"]),
        "longitude": float(config["longitude"]),
        "current": current,
        "hourly": {"time": hourly.get("time", [])[-256:],
                   "temperature_2m": hourly.get("temperature_2m", [])[-256:],
                   "relative_humidity_2m": hourly.get("relative_humidity_2m", [])[-256:],
                   "precipitation_probability": hourly.get("precipitation_probability", [])[-256:],
                   "cloud_cover": hourly.get("cloud_cover", [])[-256:],
                   "wind_speed_10m": hourly.get("wind_speed_10m", [])[-256:]},
        "daily": {"time": daily.get("time", [])[:9],
                  "precipitation_sum_mm": daily.get("precipitation_sum", [])[:9],
                  "sunshine_duration_s": daily.get("sunshine_duration", [])[:9]},
        "timezone": body.get("timezone", config.get("timezone")),
        "fetched_at_ms": int(time.time() * 1000),
        "attribution": "Open-Meteo weather forecast; not a Cupressaceae pollen forecast",
    }

def validate_official_pollen_url(url: str) -> str:
    """Restrict remote reads to HTTPS pages hosted by government domains."""
    parsed = urllib.parse.urlsplit(str(url or "").strip())
    host = (parsed.hostname or "").lower().rstrip(".")
    if (parsed.scheme != "https" or not host or not (host == "gov.cn" or host.endswith(".gov.cn"))
            or parsed.username or parsed.password or parsed.port not in (None, 443)
            or parsed.fragment):
        raise ValueError("官方花粉预报只允许读取 HTTPS 的 *.gov.cn 页面")
    return urllib.parse.urlunsplit(("https", parsed.netloc, parsed.path or "/", parsed.query, ""))

class _GovOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_official_pollen_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)

def fetch_official_pollen(config: dict) -> dict:
    """Read a small, fresh explicit Cupressaceae forecast from a government page."""
    source_url = validate_official_pollen_url(config.get("official_url", ""))
    request = urllib.request.Request(source_url, headers={
        "User-Agent": "AirProject-SmartControl/1.0 (+local purifier controller)",
        "Accept": "text/html,application/xhtml+xml",
    })
    opener = urllib.request.build_opener(_GovOnlyRedirectHandler())
    with opener.open(request, timeout=8) as response:
        validate_official_pollen_url(response.geturl())
        content_type = response.headers.get_content_type().lower()
        if content_type not in {"text/html", "application/xhtml+xml"}:
            raise ValueError("官方来源没有返回 HTML 页面")
        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > 500_000:
            raise ValueError("官方预报网页超过 500 KB 安全上限")
        raw = response.read(500_001)
        if len(raw) > 500_000:
            raise ValueError("官方预报网页超过 500 KB 安全上限")
        charset = response.headers.get_content_charset() or "utf-8"
    page = raw.decode(charset, errors="replace")
    result = smart_control.parse_official_pollen_page(
        page, datetime.now(smart_control.resolve_timezone(config.get("timezone", "Asia/Shanghai"))))
    result.update({"fetched_at_ms": int(time.time() * 1000), "source_url": source_url})
    return result

def geocode_smart_location(query: str) -> list[dict]:
    query = query.strip()[:120]
    if len(query) < 2: raise ValueError("请输入至少两个字的城市/地区名")
    params = urllib.parse.urlencode({"name": query, "count": 6, "language": "zh", "format": "json"})
    request = urllib.request.Request(f"https://geocoding-api.open-meteo.com/v1/search?{params}",
                                     headers={"User-Agent": "AirProject-SmartControl/1.0"})
    with urllib.request.urlopen(request, timeout=8) as response:
        body = json.loads(response.read().decode("utf-8"))
    results = []
    for item in body.get("results", [])[:6]:
        label = " · ".join(str(item.get(key, "")).strip() for key in ("name", "admin1", "country") if item.get(key))
        if isinstance(item.get("latitude"), (int, float)) and isinstance(item.get("longitude"), (int, float)):
            results.append({"label": label, "latitude": item["latitude"], "longitude": item["longitude"],
                            "timezone": item.get("timezone") or "Asia/Shanghai"})
    return results

def smart_snapshot() -> dict:
    with LOCK:
        config = json.loads(json.dumps(SMART_CONFIG))
        runtime = json.loads(json.dumps(SMART_RUNTIME))
        effective_tier = runtime.get("effective_tier", "weak")
        visible = []
        now = int(time.time() * 1000)
        for device_id in dict.fromkeys([*DEVICES, *CATALOG["devices"]]):
            record = DEVICES.get(device_id, {"device_id": device_id})
            profile = device_profile(device_id)
            if profile.get("hidden"): continue
            baseline = config["baselines"].get(device_id)
            derived = derive_lamp_state(record, profile, now)
            channels = (profile.get("calibration") or {}).get("channels", {})
            calibrated = isinstance(channels, dict) and all(isinstance(channels.get(key), dict) and channels[key].get("quality") == "pass" for key in LAMP_KEYS)
            current = derived.get("mode", "unknown")
            target = smart_control.target_mode(baseline, effective_tier, config["strength"]) if baseline else None
            visible.append({
                "device_id": device_id,
                "alias": profile.get("alias", device_id),
                "room": profile.get("room", "未分配"),
                "connection_state": classify_connection(record, now)[0],
                "current_mode": current,
                "baseline_mode": baseline,
                "target_mode": target,
                "calibrated": calibrated,
                "action": runtime.get("device_actions", {}).get(device_id, {}),
            })
        return {
            "config": config,
            "enabled": bool(config["enabled"]),
            "risk": smart_control.assess(config, runtime.get("weather"), previous_peak_at=runtime.get("last_peak_at") or None,
                                          official_signal=runtime.get("official_pollen") if isinstance(runtime.get("official_pollen"), dict) and runtime["official_pollen"].get("source_url") == config.get("official_url") else None),
            "capacity": smart_control.house_capacity(config),
            "selected_capacity": smart_control.selected_target_capacity(config, visible, effective_tier),
            "weather": runtime.get("weather"),
            "weather_error": runtime.get("weather_error", ""),
            "official_pollen": runtime.get("official_pollen") if isinstance(runtime.get("official_pollen"), dict) and runtime["official_pollen"].get("source_url") == config.get("official_url") else None,
            "official_pollen_error": runtime.get("official_pollen_error", ""),
            "effective_tier": effective_tier,
            "devices": visible,
            "updated_at_ms": now,
        }

def publish_control_command(device_id: str, command_type: str, origin: str = "smart_control") -> tuple[bool, str]:
    request_id = uuid4().hex
    now = int(time.time() * 1000)
    body = {"request_id": request_id, "type": command_type}
    with LOCK:
        record = DEVICES.get(device_id)
        if record is None: return False, "device_not_found"
        expire_commands(record, now)
        pending = [item for item in record.get("commands", []) if item.get("status") in {"sending", "pending"}]
        if pending: return False, "command_in_flight"
        item = {"request_id": request_id, "type": command_type, "origin": origin,
                "status": "sending", "sent_at_ms": now}
        record.setdefault("commands", []).append(item)
        record["commands"] = record["commands"][-MAX_COMMAND_HISTORY:]
    try:
        subprocess.run(mqtt_args("mosquitto_pub.exe") + ["-t", f"air/v1/{device_id}/command", "-m", json.dumps(body), "-q", "1"],
                       check=True, capture_output=True, timeout=5)
        with LOCK:
            found = command_for(DEVICES.get(device_id, {}), request_id)
            if found: found.update(status="pending", published_at_ms=int(time.time() * 1000))
        return True, request_id
    except Exception as error:
        with LOCK:
            found = command_for(DEVICES.get(device_id, {}), request_id)
            if found: found.update(status="failed", completed_at_ms=int(time.time() * 1000), error=str(error))
        return False, str(error)

def _refresh_smart_weather(config: dict) -> None:
    try:
        value = fetch_smart_weather(config)
        with LOCK:
            if ((SMART_CONFIG.get("latitude"), SMART_CONFIG.get("longitude"), SMART_CONFIG.get("timezone"))
                    == (config.get("latitude"), config.get("longitude"), config.get("timezone"))):
                SMART_RUNTIME["weather"] = value
                SMART_RUNTIME["weather_error"] = ""
    except Exception as error:
        with LOCK:
            if ((SMART_CONFIG.get("latitude"), SMART_CONFIG.get("longitude"), SMART_CONFIG.get("timezone"))
                    == (config.get("latitude"), config.get("longitude"), config.get("timezone"))):
                SMART_RUNTIME["weather_error"] = str(error)[:240]

def _refresh_official_pollen(config: dict) -> None:
    source_url = str(config.get("official_url") or "").strip()
    try:
        value = fetch_official_pollen(config)
        with LOCK:
            if str(SMART_CONFIG.get("official_url") or "").strip() == source_url:
                SMART_RUNTIME["official_pollen"] = value
                SMART_RUNTIME["official_pollen_error"] = ""
    except Exception as error:
        with LOCK:
            if str(SMART_CONFIG.get("official_url") or "").strip() == source_url:
                SMART_RUNTIME["official_pollen_error"] = str(error)[:240]

def smart_controller_loop() -> None:
    last_save = 0.0
    while True:
        now_s = time.time()
        with LOCK:
            config = json.loads(json.dumps(SMART_CONFIG))
            weather = SMART_RUNTIME.get("weather")
            fetched_at = float(SMART_RUNTIME.get("weather_fetched_at", 0))
        if config.get("latitude") is not None and config.get("longitude") is not None and now_s - fetched_at >= 900:
            with LOCK:
                if now_s - float(SMART_RUNTIME.get("weather_fetched_at", 0)) >= 900:
                    SMART_RUNTIME["weather_fetched_at"] = now_s
                    threading.Thread(target=_refresh_smart_weather, args=(config,), daemon=True).start()
        pollen_url = str(config.get("official_url") or "").strip()
        pollen_fetched_at = float(SMART_RUNTIME.get("official_pollen_fetched_at", 0))
        if pollen_url and now_s - pollen_fetched_at >= 3600:
            with LOCK:
                if now_s - float(SMART_RUNTIME.get("official_pollen_fetched_at", 0)) >= 3600:
                    SMART_RUNTIME["official_pollen_fetched_at"] = now_s
                    threading.Thread(target=_refresh_official_pollen, args=(config,), daemon=True).start()
        with LOCK:
            weather = SMART_RUNTIME.get("weather")
            pollen_signal = SMART_RUNTIME.get("official_pollen")
            if not pollen_signal or pollen_signal.get("source_url") != pollen_url: pollen_signal = None
            risk = smart_control.assess(config, weather, previous_peak_at=SMART_RUNTIME.get("last_peak_at") or None,
                                        official_signal=pollen_signal)
            desired = smart_control.desired_tier(risk)
            current = SMART_RUNTIME.get("effective_tier", "weak")
            candidate = SMART_RUNTIME.get("candidate_tier", current)
            candidate_since = float(SMART_RUNTIME.get("candidate_since", 0))
            # Escalation is prompt; reduction requires 90 minutes of persistent
            # lower risk, preserving indoor cleanup after outdoor peaks.
            effective, next_candidate, next_candidate_since = smart_control.apply_hysteresis(
                current, candidate, candidate_since, desired, now_s)
            SMART_RUNTIME["effective_tier"] = effective
            SMART_RUNTIME["candidate_tier"] = next_candidate
            SMART_RUNTIME["candidate_since"] = next_candidate_since
            effective = SMART_RUNTIME.get("effective_tier", "weak")
            if risk["tier"] in {"high", "extreme"} or risk["forecast_tier_6h"] in {"high", "extreme"}:
                SMART_RUNTIME["last_peak_at"] = now_s
            now_ms = int(now_s * 1000)
            action_map = SMART_RUNTIME.setdefault("device_actions", {})
            control_ids = set(action_map)
            if config.get("enabled"): control_ids.update(config.get("baselines", {}))
            for device_id in control_ids:
                    record = DEVICES.get(device_id)
                    profile = CATALOG["devices"].get(device_id, {})
                    baseline = config["baselines"].get(device_id)
                    action_state = action_map.setdefault(device_id, {})
                    if not record or profile.get("hidden"): continue
                    manual_target = action_state.get("manual_target_mode")
                    manual_until = int(action_state.get("manual_target_until_ms", 0))
                    if manual_target and now_ms >= manual_until:
                        action_state.update(manual_target_mode=None, manual_target_until_ms=0,
                                            status="手动目标已过期，未完成调整")
                        manual_target = None
                    if not manual_target and (not config.get("enabled") or not baseline): continue
                    channels = (profile.get("calibration") or {}).get("channels", {})
                    if not isinstance(channels, dict) or not all(isinstance(channels.get(key), dict) and channels[key].get("quality") == "pass" for key in LAMP_KEYS):
                        action_state.update(status="需完成光传感器标定")
                        continue
                    state, _ = classify_connection(record, now_ms)
                    derived = derive_lamp_state(record, profile, now_ms)
                    current_mode = derived.get("mode", "unknown")
                    inflight = action_state.get("inflight")
                    expected_mode = inflight.get("expected") if isinstance(inflight, dict) else None
                    if observe_external_mode_change(action_state, current_mode, now_ms, expected_mode):
                        action_state.update(inflight=None, manual_target_mode=None, manual_target_until_ms=0)
                        continue
                    if inflight:
                        if inflight.get("restart_recovery"):
                            recovery = restarted_pulse_recovery(
                                inflight, current_mode, record.get("_state_seen_ms", 0),
                                state == "online", now_ms)
                            if recovery == "confirmed":
                                action_state.update(inflight=None, last_success_at_ms=now_ms,
                                                    failure_count=0, paused=False, error="",
                                                    status="服务重启后已确认上次操作")
                                inflight = None
                            elif recovery == "uncertain":
                                action_state.update(inflight=None, paused=True,
                                                    status="重启期间按键结果不确定；请核对灯态后保存设置恢复",
                                                    error="为避免重复按键，未自动重发结果不确定的控制命令")
                                continue
                            else:
                                continue
                    if inflight:
                        prior_command = command_for(record, str(inflight.get("request_id", ""))) if inflight.get("request_id") else None
                        if prior_command and prior_command.get("status") in {"failed", "timeout"}:
                            failures = int(action_state.get("failure_count", 0)) + 1
                            paused = failures >= 3
                            action_state.update(inflight=None, failure_count=failures, paused=paused,
                                                status="连续失败，已暂停" if paused else "命令失败，稍后再试",
                                                error=prior_command.get("result") or prior_command.get("error") or "设备命令失败")
                            continue
                        if inflight.get("expected") == current_mode or (inflight.get("expected") == "active" and current_mode in smart_control.MODES):
                            action_state.update(inflight=None, last_success_at_ms=now_ms, failure_count=0, paused=False, error="")
                        elif now_ms - int(inflight.get("sent_at_ms", now_ms)) > 12_000:
                            failures = int(action_state.get("failure_count", 0)) + 1
                            paused = failures >= 3
                            action_state.update(inflight=None, failure_count=failures, paused=paused,
                                                status="连续失败，已暂停" if paused else "灯态未变化，稍后再试",
                                                error="命令已回执但灯态未按预期变化")
                        else:
                            continue
                    if not manual_target and now_ms < int(action_state.get("manual_override_until_ms", 0)):
                        action_state["status"] = "手动控制优先（暂缓自动调整）"
                        continue
                    if action_state.get("paused"):
                        action_state["status"] = "连续命令失败，已暂停；修改设置后可恢复"
                        continue
                    if state != "online" or current_mode in {"unknown", "uninstalled"}:
                        action_state["status"] = "等待在线且灯态可判定"
                        continue
                    target = manual_target or smart_control.target_mode(baseline, effective, config.get("strength", "high"))
                    if target == current_mode:
                        if manual_target:
                            action_state.update(manual_target_mode=None, manual_target_until_ms=0,
                                                status=f"手动目标已达到：{smart_control.MODE_LABELS[target]}", error="")
                        else: action_state.update(status="已达到目标", target_mode=target, error="")
                        continue
                    last_sent = int(action_state.get("last_sent_at_ms", 0))
                    if now_ms - last_sent < 1_700: continue
                    command = smart_control.next_physical_step(current_mode, target)
                    if not command: continue
                    if command == "opto.power.pulse":
                        expected = "off" if current_mode != "off" else "active"
                    else:
                        expected = smart_control.MODES[(smart_control.MODES.index(current_mode) + 1) % len(smart_control.MODES)]
                    action_state.update(status="正在调节", target_mode=target, last_sent_at_ms=now_ms,
                                        inflight={"expected": expected, "sent_at_ms": now_ms, "command": command})
                    # Queue outside the service lock below; remember one action
                    # per iteration to keep MQTT and device keys from contending.
                    action_state["queue_command"] = command
                    action_state["queue_origin"] = "manual_target" if manual_target else "smart_control"
            queued = [(device_id, action.get("queue_command"), action.get("queue_origin", "smart_control"))
                      for device_id, action in SMART_RUNTIME.get("device_actions", {}).items() if action.get("queue_command")][:5]
            for device_id, _, _ in queued:
                action = SMART_RUNTIME["device_actions"][device_id]
                action.pop("queue_command", None)
                action.pop("queue_origin", None)
            if queued or now_s - last_save >= 30:
                save_smart_runtime()
                last_save = now_s
        for item in queued:
            device_id, command, origin = item
            ok, result = publish_control_command(device_id, command, origin)
            with LOCK:
                action = SMART_RUNTIME["device_actions"].setdefault(device_id, {})
                if ok:
                    action.setdefault("inflight", {})["request_id"] = result
                else:
                    failures = int(action.get("failure_count", 0)) + 1
                    paused = failures >= 3
                    action.update(inflight=None, failure_count=failures, paused=paused,
                                  status="连续失败，已暂停" if paused else "等待后重试", error=result)
                save_smart_runtime()
        time.sleep(5)

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
                    if category in {"state", "telemetry"}:
                        remember_lamp_observation(record, body, observed_at)
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
        if path == "/api/v1/smart-control/geocode":
            try: return self.reply(200, {"results": geocode_smart_location(parse_qs(parsed.query).get("q", [""])[0])})
            except Exception as error: return self.reply(400, {"error": str(error)})
        if path == "/api/v1/smart-control": return self.reply(200, smart_snapshot())
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
                    record["derived_state"] = derive_lamp_state(source, profile, now)
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
        target_parts = path.split("/")
        if len(target_parts) == 6 and target_parts[1:4] == ["api", "v1", "devices"] and target_parts[5] == "target":
            try:
                body = self.read_json(); target = str(body.get("mode", ""))
                if target not in (*smart_control.MODES, "off"):
                    return self.reply(400, {"error": "mode must be off, low, medium, high, or auto"})
                device_id = canonical_device_id(unquote(target_parts[4])); now = int(time.time() * 1000)
                with LOCK:
                    record = DEVICES.get(device_id)
                    profile = CATALOG["devices"].get(device_id, {})
                    if not record or profile.get("hidden"): return self.reply(404, {"error": "device not found"})
                    connection, _ = classify_connection(record, now)
                    derived = derive_lamp_state(record, profile, now)
                    current_mode = derived.get("mode", "unknown")
                    channels = (profile.get("calibration") or {}).get("channels", {})
                    calibrated = isinstance(channels, dict) and all(isinstance(channels.get(key), dict) and channels[key].get("quality") == "pass" for key in LAMP_KEYS)
                    if connection != "online": return self.reply(409, {"error": "device is not online"})
                    if not calibrated or current_mode in {"unknown", "uninstalled"}:
                        return self.reply(409, {"error": "device needs a passed light calibration and a readable lamp state"})
                    action = SMART_RUNTIME.setdefault("device_actions", {}).setdefault(device_id, {})
                    if current_mode == target:
                        action.update(manual_target_mode=None, manual_target_until_ms=0, status=f"已处于{smart_control.MODE_LABELS[target]}")
                        return self.reply(200, {"status": "already_at_target", "device_id": device_id, "mode": target})
                    action.update(manual_target_mode=target, manual_target_until_ms=now + 5 * 60 * 1000,
                                  manual_override_until_ms=now + 30 * 60 * 1000,
                                  status=f"手动目标：{smart_control.MODE_LABELS[target]}，等待逐步确认")
                return self.reply(202, {"status": "accepted", "device_id": device_id, "mode": target,
                                        "detail": "每次只触发一个已确认的实体按键动作"})
            except Exception as error:
                return self.reply(400, {"error": str(error)})
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
                    "origin": "manual",
                    "status": "sending",
                    "sent_at_ms": now,
                }
                record.setdefault("commands", []).append(item)
                record["commands"] = record["commands"][-MAX_COMMAND_HISTORY:]
                SMART_RUNTIME.setdefault("device_actions", {}).setdefault(device_id, {}).update(
                    manual_override_until_ms=now + 30 * 60 * 1000,
                    status="手动控制优先（30 分钟内暂缓智能调控）",
                )
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
        if urlparse(self.path).path == "/api/v1/smart-control":
            try:
                body = self.read_json()
                config = smart_control.normalize_config(body)
                with LOCK:
                    previous = dict(SMART_CONFIG)
                    if (any(config.get(key) != previous.get(key) for key in ("official_level", "official_note"))
                            or (config["official_level"] and not config.get("official_updated_at"))):
                        config["official_updated_at"] = int(time.time() * 1000)
                    if any(config.get(key) != previous.get(key) for key in ("social_level", "social_note")) or (config["social_level"] and not config.get("social_updated_at")):
                        config["social_updated_at"] = int(time.time() * 1000)
                    if config["enabled"]:
                        calibrated = False
                        for device_id in config["baselines"]:
                            profile = CATALOG["devices"].get(device_id, {})
                            channels = (profile.get("calibration") or {}).get("channels", {})
                            if not profile.get("hidden") and isinstance(channels, dict) and all(isinstance(channels.get(key), dict) and channels[key].get("quality") == "pass" for key in LAMP_KEYS):
                                calibrated = True; break
                        if not config["baselines"] or not calibrated:
                            raise ValueError("启用前至少要为一台已完成四路光传感器标定的设备设置基准档位")
                    SMART_CONFIG.clear(); SMART_CONFIG.update(config); save_smart_config()
                    SMART_RUNTIME["weather_fetched_at"] = 0
                    location_changed = any(config.get(key) != previous.get(key)
                                           for key in ("latitude", "longitude", "timezone"))
                    if location_changed:
                        SMART_RUNTIME["weather"] = None
                        SMART_RUNTIME["weather_error"] = ""
                    if config.get("official_url") != previous.get("official_url"):
                        SMART_RUNTIME["official_pollen"] = None
                        SMART_RUNTIME["official_pollen_fetched_at"] = 0
                        SMART_RUNTIME["official_pollen_error"] = ""
                    SMART_RUNTIME["last_config_error"] = ""
                    for action in SMART_RUNTIME.setdefault("device_actions", {}).values():
                        if isinstance(action, dict): action.update(paused=False, failure_count=0, error="")
                return self.reply(200, smart_snapshot())
            except Exception as error:
                return self.reply(400, {"error": str(error)})
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
    threading.Thread(target=smart_controller_loop, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", 8780), Handler).serve_forever()
