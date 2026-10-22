"""Risk scoring and capacity planning for pollen-aware purifier control.

Weather is supporting evidence only. Cupressaceae pollen and social signals must
be supplied from a local official forecast/operator observation because the
available global pollen API does not cover this pollen family in China.
"""
from __future__ import annotations

from datetime import date, datetime, time as daytime, timedelta, timezone
import calendar
import math
import re
from html import unescape
from zoneinfo import ZoneInfo
from typing import Any

MODES = ("low", "medium", "high", "auto")
MODE_LABELS = {"off": "关机", "low": "低速", "medium": "中速", "high": "高速", "auto": "自动"}
STRENGTHS = {"normal": 1, "high": 2, "extra": 3}
LEVELS = ("weak", "normal", "high", "extreme")

_FIXED_ZONE_FALLBACKS = {
    "UTC": 0, "Etc/UTC": 0, "Asia/Shanghai": 480, "Asia/Singapore": 480,
    "Asia/Hong_Kong": 480, "Asia/Taipei": 480, "Asia/Manila": 480,
    "Asia/Tokyo": 540, "Asia/Seoul": 540, "Asia/Kolkata": 330,
    "Asia/Bangkok": 420, "Asia/Jakarta": 420, "Asia/Dubai": 240,
}


def resolve_timezone(name: str):
    try: return ZoneInfo(name)
    except Exception:
        if name in _FIXED_ZONE_FALLBACKS:
            return timezone(timedelta(minutes=_FIXED_ZONE_FALLBACKS[name]), name)
        raise ValueError("当前系统缺少该时区数据库；请安装 tzdata 或选择常用亚洲时区")


def default_config() -> dict[str, Any]:
    return {
        "schema": "air-smart-control-v1", "enabled": False,
        "location_name": "", "latitude": None, "longitude": None,
        "timezone": "Asia/Shanghai", "area_m2": 160, "ceiling_m": 2.5,
        "device_count": 23, "airflow_m3h": {"low": 20, "medium": 70, "high": 150},
        "strength": "high", "baselines": {},
        "official_level": 0, "official_forecast_level": 0,
        "official_note": "", "official_url": "",
        "official_updated_at": None, "official_forecast_updated_at": None,
        "official_valid_until": None, "official_peak_at": "", "official_forecast_until": "",
        "season_start_md": "", "season_end_md": "",
        "social_level": 0, "social_note": "", "social_updated_at": None,
        "evening_start": "17:10", "evening_minutes": 40,
        "preboost_minutes": 45, "residual_hours": 18,
        "rain_suppression": True,
    }


def _config_number(value: Any, field: str, minimum: float, maximum: float,
                  *, integer: bool = False) -> int | float:
    if isinstance(value, bool):
        raise ValueError(f"{field} 必须是数字")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{field} 参数无效")
    if not math.isfinite(number):
        raise ValueError(f"{field} 必须是有限数字")
    if not minimum <= number <= maximum:
        raise ValueError(f"{field} 参数超出范围")
    if integer:
        if not number.is_integer():
            raise ValueError(f"{field} 必须是整数")
        return int(number)
    return number


def normalize_config(source: object) -> dict[str, Any]:
    if not isinstance(source, dict):
        raise ValueError("设置必须是对象")
    value = default_config()
    value.update(source)
    value["schema"] = "air-smart-control-v1"
    if not isinstance(value.get("enabled"), bool):
        raise ValueError("enabled 必须是布尔值")
    value["location_name"] = str(value.get("location_name", "")).strip()[:80]
    for key, low, high in (("latitude", -90, 90), ("longitude", -180, 180)):
        raw = value.get(key)
        if raw in (None, ""):
            value[key] = None
        else:
            label = "纬度" if key == "latitude" else "经度"
            value[key] = _config_number(raw, label, low, high)
    if value["enabled"] and (value["latitude"] is None or value["longitude"] is None):
        raise ValueError("启用自动调控前必须填写家庭所在地坐标")
    value["timezone"] = str(value.get("timezone") or "Asia/Shanghai")[:48]
    try: resolve_timezone(value["timezone"])
    except Exception: raise ValueError("时区名称无效，请使用 IANA 时区，例如 Asia/Shanghai")
    integer_fields = {"device_count", "evening_minutes", "preboost_minutes", "residual_hours"}
    for key, low, high in (("area_m2", 1, 10000), ("ceiling_m", 1, 10), ("device_count", 1, 200),
                           ("evening_minutes", 0, 240), ("preboost_minutes", 0, 360), ("residual_hours", 0, 72)):
        value[key] = _config_number(value.get(key), key, low, high, integer=key in integer_fields)
    if value.get("strength") not in STRENGTHS:
        raise ValueError("控制力度必须为正常、高或特强")
    flow = value.get("airflow_m3h")
    if not isinstance(flow, dict):
        raise ValueError("airflow_m3h 必须包含低、中、高三档风量")
    value["airflow_m3h"] = {
        key: _config_number(flow.get(key, default), f"{key} 档风量", 0, 2000)
        for key, default in (("low", 20), ("medium", 70), ("high", 150))
    }
    for key in ("official_level", "official_forecast_level", "social_level"):
        maximum = 4 if key == "official_level" else 3
        if key == "official_forecast_level": maximum = 4
        label = "官方花粉等级" if key == "official_level" else "社交趋势等级"
        if key == "official_forecast_level": label = "官方未来预报等级"
        value[key] = _config_number(value.get(key, 0), label, 0, maximum, integer=True)
    value["official_note"] = str(value.get("official_note", "")).strip()[:500]
    value["official_url"] = str(value.get("official_url", "")).strip()[:500]
    value["official_peak_at"] = str(value.get("official_peak_at", "")).strip()[:40]
    value["official_forecast_until"] = str(value.get("official_forecast_until", "")).strip()[:40]
    forecast_start = forecast_end = None
    for key, label in (("official_peak_at", "花粉预报开始时间"),
                       ("official_forecast_until", "花粉预报结束时间")):
        if value[key]:
            try:
                parsed = datetime.fromisoformat(value[key])
            except ValueError:
                raise ValueError(f"{label}无效")
            if key == "official_peak_at": forecast_start = parsed
            else: forecast_end = parsed
    if value["official_forecast_level"]:
        if forecast_start is None or forecast_end is None:
            raise ValueError("填写人工未来预报等级时，必须同时填写高峰开始和结束时间")
        if forecast_start.tzinfo is None: forecast_start = forecast_start.replace(tzinfo=resolve_timezone(value["timezone"]))
        if forecast_end.tzinfo is None: forecast_end = forecast_end.replace(tzinfo=resolve_timezone(value["timezone"]))
        if forecast_end <= forecast_start or forecast_end - forecast_start > timedelta(days=10):
            raise ValueError("花粉预报结束时间必须晚于开始时间，且窗口不能超过 10 天")
    for key in ("season_start_md", "season_end_md"):
        value[key] = str(value.get(key, "")).strip()[:5]
        if value[key]:
            try: datetime.strptime(f"2000-{value[key]}", "%Y-%m-%d")
            except ValueError: raise ValueError("花粉季日期请按 MM-DD 填写")
    if bool(value["season_start_md"]) != bool(value["season_end_md"]):
        raise ValueError("花粉季起始和结束日期需要同时填写")
    value["social_note"] = str(value.get("social_note", "")).strip()[:500]
    if not isinstance(value.get("baselines"), dict):
        raise ValueError("baselines 必须是设备 ID 到基准档位的对象")
    normalized_baselines = {}
    for device_id, mode in value["baselines"].items():
        device_key = str(device_id).strip()
        if not device_key or len(device_key) > 96:
            raise ValueError("设备 ID 不能为空且不能超过 96 个字符")
        if mode == "":
            continue
        if mode not in (*MODES, "off"):
            raise ValueError(f"设备 {device_key} 的基准档位无效")
        normalized_baselines[device_key] = mode
    value["baselines"] = normalized_baselines
    try:
        start = str(value.get("evening_start", "17:10"))
        daytime.fromisoformat(start)
    except ValueError: raise ValueError("下班增强开始时间应为 HH:MM")
    value["evening_start"] = start
    return value


def house_capacity(config: dict[str, Any]) -> dict[str, Any]:
    volume = float(config["area_m2"]) * float(config["ceiling_m"])
    count = int(config["device_count"])
    rates = config["airflow_m3h"]
    totals = {mode: count * float(rates[mode]) for mode in ("low", "medium", "high")}
    baselines = config.get("baselines", {})
    baseline_airflow = sum(float(rates[mode]) for mode in baselines.values() if mode in rates)
    auto_baselines = sum(1 for mode in baselines.values() if mode == "auto")
    high_rate = float(rates["high"])
    return {
        "volume_m3": round(volume, 1),
        "total_airflow_m3h": {key: round(value, 1) for key, value in totals.items()},
        "nominal_air_changes_per_hour": {key: math.floor(value / volume * 100 + 0.5) / 100 if volume else 0 for key, value in totals.items()},
        "target_15x_m3h": round(volume * 15, 1), "target_20x_m3h": round(volume * 20, 1),
        "minimum_high_devices_for_15x": math.ceil(volume * 15 / high_rate) if high_rate > 0 else None,
        "minimum_high_devices_for_20x": math.ceil(volume * 20 / high_rate) if high_rate > 0 else None,
        "baselined_device_count": len(baselines),
        "baselined_airflow_m3h": round(baseline_airflow, 1),
        "auto_baseline_devices_unrated": auto_baselines,
        "high_meets_15x_nominal": totals["high"] >= volume * 15,
        "high_meets_20x_nominal": totals["high"] >= volume * 20,
        "capacity_caveat": ("标称总风量低于 15 次/小时目标；且实际 CADR 通常不高于风量，不能据此宣称满足该洁净目标。"
                            if totals["high"] < volume * 15 else
                            "标称风量达到 15 次/小时对照值，但仍需设备 CADR、滤网效率和房间实测验证。"),
        "warning": "按设备标称风量估算的循环风量，不等于颗粒物 CADR；真实洁净能力还取决于滤网效率、密封和实际风量。",
    }


def _local_now(config: dict[str, Any], now: datetime | None = None) -> datetime:
    zone = resolve_timezone(config.get("timezone", "Asia/Shanghai"))
    now = now or datetime.now(zone)
    if now.tzinfo is None: return now.replace(tzinfo=zone)
    return now.astimezone(zone)


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _bounded_weather_number(value: Any, minimum: float, maximum: float) -> float | None:
    """Reject malformed/out-of-physical-range weather fields as unknown."""
    number = _finite_number(value)
    return number if number is not None and minimum <= number <= maximum else None


def assess(config: dict[str, Any], weather: dict[str, Any] | None, now: datetime | None = None,
           previous_peak_at: float | None = None,
           official_signal: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return explainable 0–100 outdoor/indoor pollen risk and a stable tier."""
    now_local = _local_now(config, now)
    official = int(config.get("official_level", 0))
    official_source = "operator_entry"
    social = int(config.get("social_level", 0))
    official_time = config.get("official_updated_at")
    social_time = config.get("social_updated_at")
    reasons: list[str] = []
    if weather is not None and not isinstance(weather, dict):
        weather = None
        reasons.append("天气数据结构无效，天气信号已忽略")

    def fresh_age_seconds(value: Any, limit_s: float) -> float | None:
        try:
            age = now_local.timestamp() - float(value) / 1000
            return max(0.0, age) if -300 <= age <= limit_s else None
        except (TypeError, ValueError):
            return None

    if official and official_source == "operator_entry" and fresh_age_seconds(official_time, 48 * 3600) is None:
        official = 0
        reasons.append("人工录入的柏科预报缺少有效时间戳、已超过 48 小时或时间异常，已忽略")
    if social and fresh_age_seconds(social_time, 12 * 3600) is None:
        social = 0
        reasons.append("社交/过敏讨论信号缺少有效时间戳、已超过 12 小时或时间异常，已忽略")

    # Weather is refreshed every 15 minutes, but a failed refresh intentionally
    # leaves the last good payload available for diagnostics. Never let that
    # cached payload influence control indefinitely: after one hour (or if the
    # timestamp is missing/implausibly in the future), treat it as unavailable.
    if weather:
        weather_age = fresh_age_seconds(weather.get("fetched_at_ms"), 60 * 60)
        if weather_age is None:
            weather = None
            reasons.append("天气数据缺少有效抓取时间、已超过 60 分钟或时间异常，天气信号已忽略")
    if weather and ("latitude" in weather or "longitude" in weather):
        weather_latitude = _finite_number(weather.get("latitude"))
        weather_longitude = _finite_number(weather.get("longitude"))
        expected_latitude = _finite_number(config.get("latitude"))
        expected_longitude = _finite_number(config.get("longitude"))
        source_timezone = weather.get("timezone")
        location_mismatch = (
            weather_latitude is None or weather_longitude is None
            or expected_latitude is None or expected_longitude is None
            or not math.isclose(weather_latitude, expected_latitude, rel_tol=0, abs_tol=1e-6)
            or not math.isclose(weather_longitude, expected_longitude, rel_tol=0, abs_tol=1e-6)
            or (source_timezone is not None and source_timezone != config.get("timezone"))
        )
        if location_mismatch:
            weather = None
            reasons.append("天气数据地点/时区与当前设置不一致，已忽略并等待重新抓取")

    official_signal_valid = False
    official_forecast_level = 0
    official_forecast_peak_at = None
    if official_signal:
        fetched_age = fresh_age_seconds(official_signal.get("fetched_at_ms"), 2 * 3600)
        published_age = fresh_age_seconds(official_signal.get("published_at_ms"), 48 * 3600)
        peak_window_active = False
        peak_window_expired = False
        peak_start_at = None
        peak_until = official_signal.get("predicted_peak_until")
        peak_start = official_signal.get("predicted_peak_at")
        if isinstance(peak_start, str) and peak_start:
            try:
                peak_start_at = datetime.fromisoformat(peak_start)
                if peak_start_at.tzinfo is None:
                    peak_start_at = peak_start_at.replace(tzinfo=now_local.tzinfo)
                peak_until_at = (datetime.fromisoformat(peak_until)
                                 if isinstance(peak_until, str) and peak_until
                                 else peak_start_at + timedelta(hours=24))
                if peak_until_at.tzinfo is None:
                    peak_until_at = peak_until_at.replace(tzinfo=now_local.tzinfo)
                peak_window_active = now_local <= peak_until_at + timedelta(hours=12)
                peak_window_expired = not peak_window_active
            except (TypeError, ValueError, OverflowError):
                peak_window_active = False
                peak_start_at = None
        if published_age is None and peak_window_active:
            # A fresh fetch of a recent article remains usable through its
            # explicitly predicted multi-day peak window, but never beyond a
            # seven-day publication-age ceiling or twelve-hour post-window grace.
            published_age = fresh_age_seconds(official_signal.get("published_at_ms"), 7 * 24 * 3600)
        try: candidate = int(official_signal.get("level", 0))
        except (TypeError, ValueError): candidate = 0
        if (fetched_age is not None and published_age is not None and 1 <= candidate <= 4
                and not peak_window_expired):
            official_signal_valid = True
            if peak_start_at is not None and now_local < peak_start_at:
                official_forecast_level = candidate
                official_forecast_peak_at = peak_start_at.isoformat()
                if official == 0:
                    official_source = "verified_gov_cn_page_forecast"
                reasons.append(
                    f"官方柏科预报等级 {candidate}/4 对应未来峰值时段，预计始于 {peak_start_at.isoformat()}；"
                    "到达预升档窗口前不把它当作当前浓度"
                )
            else:
                official = max(official, candidate)
                official_source = "verified_gov_cn_page"
        else:
            reason = "官方柏科高峰窗口已结束" if peak_window_expired else "自动读取的官方预报超过控制有效期或时间异常"
            reasons.append(f"{reason}，未参与控制")
    # A manually verified future bulletin is not a current concentration
    # reading. Stage it in the forecast lane, then promote it during its
    # explicit peak window; this mirrors the government-page parser path.
    manual_forecast = int(config.get("official_forecast_level", 0))
    if manual_forecast:
        forecast_age = fresh_age_seconds(config.get("official_forecast_updated_at"), 7 * 24 * 3600)
        try:
            manual_start = datetime.fromisoformat(config.get("official_peak_at", ""))
            manual_end = datetime.fromisoformat(config.get("official_forecast_until", ""))
            if manual_start.tzinfo is None: manual_start = manual_start.replace(tzinfo=now_local.tzinfo)
            if manual_end.tzinfo is None: manual_end = manual_end.replace(tzinfo=now_local.tzinfo)
            manual_start = manual_start.astimezone(now_local.tzinfo)
            manual_end = manual_end.astimezone(now_local.tzinfo)
            forecast_window_valid = (manual_end >= manual_start
                                     and manual_start <= now_local + timedelta(days=7)
                                     and manual_end >= now_local - timedelta(hours=12))
        except (TypeError, ValueError, OverflowError):
            forecast_window_valid = False
            manual_start = manual_end = None
        if forecast_age is not None and forecast_window_valid:
            if now_local < manual_start:
                if (manual_forecast > official_forecast_level
                        or official_forecast_peak_at is None):
                    official_forecast_peak_at = manual_start.isoformat()
                official_forecast_level = max(official_forecast_level, manual_forecast)
                if official == 0:
                    official_source = "operator_forecast"
                reasons.append(
                    f"人工核验的柏科预报等级 {manual_forecast}/4 对应未来时段 "
                    f"{manual_start.isoformat()} 至 {manual_end.isoformat()}；按提前量预升档，不作当前浓度"
                )
            elif now_local <= manual_end + timedelta(hours=12):
                official = max(official, manual_forecast)
                if official == manual_forecast:
                    official_source = "operator_forecast_current"
                reasons.append(
                    f"人工核验的柏科预报窗口当前生效（等级 {manual_forecast}/4，至 "
                    f"{manual_end.isoformat()}，含 12 小时室内余效宽限）"
                )
        else:
            reasons.append("人工未来柏科预报已过期、窗口无效或距窗口超过 7 天，未参与控制")
    score = (0.0, 12.0, 30.0, 52.0, 72.0)[official]
    season_start, season_end = config.get("season_start_md", ""), config.get("season_end_md", "")
    in_season = False
    if season_start and season_end:
        current_md = (now_local.month, now_local.day)
        start_md = tuple(map(int, season_start.split("-")))
        end_md = tuple(map(int, season_end.split("-")))
        in_season = start_md <= current_md <= end_md if start_md <= end_md else current_md >= start_md or current_md <= end_md
        if in_season:
            score += 4
            reasons.append("处于本地配置的柏科花粉季节；季节信息只作观察提示")
    if official:
        reasons.append(f"本地官方/人工柏科花粉级别 {official}/4（主信号）")
    elif official_forecast_level:
        reasons.append(f"未来官方柏科花粉预报等级 {official_forecast_level}/4（尚未按当前浓度计分）")
    else:
        reasons.append("尚未录入当地柏科花粉预报；季节和天气不能单独证明空气中花粉浓度")

    # A distant forecast bulletin belongs only to the forecast lane. It must
    # not turn today's sunny/warm weather into a present-risk boost before the
    # declared peak or its lead window; current weather needs a current pollen
    # season, a live level, or a fresh reviewed symptom trend.
    weather_context_active = in_season or official > 0 or social > 0
    weather_score = 0.0
    if weather and not weather_context_active:
        reasons.append("天气背景未计入当前花粉风险：未来高峰公告不会提前充当当前浓度；需处于本地花粉季或有当前有效柏科/人工趋势信号")
    if weather and weather_context_active and isinstance(weather.get("current"), dict):
        current = weather["current"]
        temp = _bounded_weather_number(current.get("temperature_2m"), -90, 60)
        humidity = _bounded_weather_number(current.get("relative_humidity_2m"), 0, 100)
        rain = _bounded_weather_number(current.get("precipitation"), 0, 10_000)
        rain_interval_s = _bounded_weather_number(current.get("interval"), 1, 7_200)
        cloud = _bounded_weather_number(current.get("cloud_cover"), 0, 100)
        temp_points = 0.0
        if temp is not None:
            # A trapezoid encodes a broad, uncertain favorable range without
            # an artificial six-point jump at exactly 5°C or 22°C.
            if 5 < temp < 10: temp_points = 6 * (temp - 5) / 5
            elif 10 <= temp <= 18: temp_points = 6
            elif 18 < temp < 22: temp_points = 6 * (22 - temp) / 4
            weather_score += temp_points
            if temp_points > 0:
                reasons.append(f"气温 {temp:g}°C 的温度辅助约 +{temp_points:.1f} 分（平滑弱权重，非花粉测量）")
        humidity_points = 0.0
        if humidity is not None:
            # Drying gradually adds at most +2; humid-air deposition gradually
            # subtracts at most -2. These deliberately remain weak modifiers.
            if humidity < 30: humidity_points = 2
            elif humidity < 50: humidity_points = 2 * (50 - humidity) / 20
            elif 60 < humidity < 80: humidity_points = -2 * (humidity - 60) / 20
            elif humidity >= 80: humidity_points = -2
            weather_score += humidity_points
            if humidity_points > 0:
                reasons.append(f"相对湿度 {humidity:g}% 偏干，辅助约 +{humidity_points:.1f} 分")
            elif humidity_points < 0:
                reasons.append(f"相对湿度 {humidity:g}% 偏湿，辅助约 {humidity_points:.1f} 分")
        # Open-Meteo's current precipitation is a backward-looking sum whose
        # duration is returned as `current.interval` (often 15 minutes). Use
        # a normalized intensity proxy so different accumulation windows are
        # not compared as if they had the same duration. Unknown intervals are
        # not scored rather than guessed.
        rain_rate = (rain * 3600 / rain_interval_s
                     if rain is not None and rain_interval_s is not None else None)
        if rain_rate is not None and not math.isfinite(rain_rate):
            rain_rate = None
        rain_penalty = 0.0
        if rain_rate is not None:
            if rain_rate <= 0.2: rain_penalty = 0
            elif rain_rate < 1: rain_penalty = -2 * (rain_rate - 0.2) / 0.8
            elif rain_rate < 3: rain_penalty = -2 - 4 * (rain_rate - 1) / 2
            elif rain_rate < 8: rain_penalty = -6 - 4 * (rain_rate - 3) / 5
            else: rain_penalty = -10
            weather_score += rain_penalty
            if rain_penalty < 0:
                reasons.append(f"近 {rain_interval_s / 60:g} 分钟降水 {rain:g} mm（折算雨强 {rain_rate:g} mm/h），连续雨强辅助约 {rain_penalty:.1f} 分")
        cloud_points = 0.0
        if rain_rate is not None and rain_rate < 0.2 and cloud is not None and cloud < 35:
            cloud_points = 5 * max(0.0, min(1.0, (35 - cloud) / 25))
            weather_score += cloud_points
            if cloud_points > 0:
                reasons.append(f"当前云量 {cloud:g}% 较少，晴朗辅助约 +{cloud_points:.1f} 分")
        # Rain/sunshine effects are bounded and never replace direct pollen evidence.
        forecast = weather.get("daily", {})
        forecast = forecast if isinstance(forecast, dict) else {}
        sunny = forecast.get("sunshine_duration_s", [])
        rain_sum = forecast.get("precipitation_sum_mm", [])
        sunny = sunny if isinstance(sunny, list) else []
        rain_sum = rain_sum if isinstance(rain_sum, list) else []
        if sunny and rain_sum:
            days = forecast.get("time", [])
            days = days if isinstance(days, list) else []
            try: today_index = days.index(now_local.date().isoformat())
            except (ValueError, AttributeError): today_index = 0
            today_index = min(today_index, len(sunny) - 1, len(rain_sum) - 1)
            today_sun_s = _bounded_weather_number(sunny[today_index], 0, 86_400)
            today_sun = today_sun_s / 3600 if today_sun_s is not None else None
            yesterday = today_index - 1
            yesterday_rain = _bounded_weather_number(rain_sum[yesterday], 0, 10_000) if yesterday >= 0 else None
            if (yesterday_rain is not None and yesterday_rain > 1 and rain_rate is not None and rain_rate < 0.2
                    and today_sun is not None and today_sun >= 6 and temp_points > 0):
                weather_score += 3
                reasons.append("前一日降雨后转为温暖晴朗，释放/扩散可能性仅作轻微上调")
        score += max(-12, min(12, weather_score))

    if social:
        # Allergy discussion is nonspecific and operator-reviewed, so it can
        # justify only a modest normal-tier response even at the highest level.
        # Keep it fresh for just 12 hours and never let it claim high/extreme.
        social_points = social * 6
        score += social_points
        reasons.append(f"过敏/花粉社交信号级别 {social}/3（人工核验 +{social_points} 分；最多促成正常档，不代表柏科实测）")
    residual_active = False
    residual_remaining_h = 0.0
    if previous_peak_at and config.get("residual_hours", 0) > 0:
        try:
            age_h = (now_local.timestamp() - float(previous_peak_at)) / 3600
        except (TypeError, ValueError):
            age_h = -1.0
        if 0 <= age_h < float(config["residual_hours"]):
            residual_active = True
            residual_remaining_h = float(config["residual_hours"]) - age_h
            residual = min(18.0, 18.0 * max(0.0, 1 - age_h / float(config["residual_hours"])))
            score = max(score, residual)
            reasons.append(f"花粉峰后室内清洁余效中（剩余约 {residual_remaining_h:.1f} 小时）")
            if residual >= 2: reasons.append(f"室内沉降/滞留余效评分约 {residual:.0f}/18")

    start = daytime.fromisoformat(config.get("evening_start", "17:10"))
    local_clock = now_local.timetz().replace(tzinfo=None)
    elapsed_min = (local_clock.hour * 60 + local_clock.minute) - (start.hour * 60 + start.minute)
    evening = 0 <= elapsed_min < int(config.get("evening_minutes", 40))
    score_before_evening = score
    if evening:
        score = max(score, 35)
        reasons.append("下班回家时段增强已生效（开始时间及持续时间可配置）")

    forecast_score = score
    forecast_hours_used = 0
    forecast_weather_adjustment = 0.0
    forecast_components = {"warm_clear_hours": 0, "low_humidity_hours": 0,
                           "high_precipitation_probability_hours": 0,
                           "warm_clear_contribution": 0.0,
                           "low_humidity_contribution": 0.0,
                           "rain_suppression_contribution": 0.0}
    if weather and weather_context_active and isinstance(weather.get("hourly"), dict):
        hourly = weather["hourly"]
        times = hourly.get("time", [])
        temps = hourly.get("temperature_2m", [])
        humidities = hourly.get("relative_humidity_2m", [])
        probs = hourly.get("precipitation_probability", [])
        clouds = hourly.get("cloud_cover", [])
        times = times if isinstance(times, list) else []
        temps = temps if isinstance(temps, list) else []
        humidities = humidities if isinstance(humidities, list) else []
        probs = probs if isinstance(probs, list) else []
        clouds = clouds if isinstance(clouds, list) else []
        # Open-Meteo's local hourly arrays begin at midnight. Taking [:6]
        # therefore examines past hours for most of the day and can pre-ramp
        # (or suppress) the controller for weather that has already happened.
        # Select timestamped observations in the actual next-six-hour window.
        hourly_zone = resolve_timezone(weather.get("timezone") or config.get("timezone", "Asia/Shanghai"))
        window_end = now_local + timedelta(hours=6)
        forecast_indices: list[int] = []
        for index, value in enumerate(times):
            if index >= max(len(temps), len(humidities), len(probs), len(clouds)) or not isinstance(value, str):
                continue
            try:
                sample_at = datetime.fromisoformat(value)
                if sample_at.tzinfo is None:
                    sample_at = sample_at.replace(tzinfo=hourly_zone)
                sample_at = sample_at.astimezone(now_local.tzinfo)
            except (TypeError, ValueError, OverflowError):
                continue
            if now_local <= sample_at <= window_end:
                forecast_indices.append(index)
        # Use continuous, bounded proxies for the next-six-hour outlook. The
        # old boolean tests (e.g. rain probability 29% vs 30%) made a single
        # forecast-point change jump the score by a whole point. These signals
        # remain deliberately weak and may only raise the forecast tier; they
        # can never reduce today's directly assessed risk.
        delta = 0.0
        for index in forecast_indices:
            temp = _bounded_weather_number(temps[index], -90, 60) if index < len(temps) else None
            humidity = _bounded_weather_number(humidities[index], 0, 100) if index < len(humidities) else None
            rain_probability = _bounded_weather_number(probs[index], 0, 100) if index < len(probs) else None
            cloud = _bounded_weather_number(clouds[index], 0, 100) if index < len(clouds) else None
            warm_factor = 0.0
            if temp is not None:
                if 5 < temp < 10: warm_factor = (temp - 5) / 5
                elif 10 <= temp <= 18: warm_factor = 1.0
                elif 18 < temp < 22: warm_factor = (22 - temp) / 4
            clear_factor = max(0.0, min(1.0, (60 - cloud) / 40)) if cloud is not None else None
            dry_forecast_factor = max(0.0, min(1.0, (70 - rain_probability) / 60)) if rain_probability is not None else None
            if warm_factor > 0 and clear_factor is not None and dry_forecast_factor is not None:
                contribution = warm_factor * clear_factor * dry_forecast_factor
                delta += contribution
                forecast_components["warm_clear_contribution"] += contribution
            if (temp is not None and 5 <= temp <= 22 and rain_probability is not None
                    and rain_probability < 30 and cloud is not None and cloud < 45):
                forecast_components["warm_clear_hours"] += 1
            if humidity is not None and humidity < 40:
                forecast_components["low_humidity_hours"] += 1
            if humidity is not None:
                contribution = 0.25 * max(0.0, min(1.0, (50 - humidity) / 20))
                delta += contribution
                forecast_components["low_humidity_contribution"] += contribution
            if rain_probability is not None:
                suppression = 1.5 * max(0.0, min(1.0, (rain_probability - 20) / 60))
                delta -= suppression
                forecast_components["rain_suppression_contribution"] += suppression
            if rain_probability is not None and rain_probability >= 60:
                forecast_components["high_precipitation_probability_hours"] += 1
        forecast_hours_used = len(forecast_indices)
        forecast_score = max(score, max(0, min(100, score + max(-6, min(6, delta)))))
        forecast_weather_adjustment = round(forecast_score - score, 1)
        for key in ("warm_clear_contribution", "low_humidity_contribution", "rain_suppression_contribution"):
            forecast_components[key] = round(forecast_components[key], 2)
        if forecast_hours_used:
            reasons.append(
                f"未来 6 小时天气参考 {forecast_hours_used} 个整点样本"
                f"（温暖晴朗 {forecast_components['warm_clear_hours']} 小时、"
                f"低湿 {forecast_components['low_humidity_hours']} 小时、"
                f"高降水概率 {forecast_components['high_precipitation_probability_hours']} 小时），"
                f"连续分量：暖晴 +{forecast_components['warm_clear_contribution']:g}、"
                f"低湿 +{forecast_components['low_humidity_contribution']:g}、"
                f"降雨抑制 -{forecast_components['rain_suppression_contribution']:g} 分；"
                f"预测评分净上调 {forecast_weather_adjustment:g} 分（上限 +6；天气不会下调当前花粉等级）"
            )
    preboost = False
    peak_at = official_forecast_peak_at or config.get("official_peak_at")
    if peak_at:
        try:
            forecast_peak = datetime.fromisoformat(peak_at)
            if forecast_peak.tzinfo is None: forecast_peak = forecast_peak.replace(tzinfo=now_local.tzinfo)
            minutes_until_peak = (forecast_peak - now_local).total_seconds() / 60
            preboost_level = max(official, official_forecast_level)
            preboost = 0 < minutes_until_peak <= int(config.get("preboost_minutes", 45)) and preboost_level >= 1
        except ValueError: preboost = False
    score = round(max(0, min(100, score)), 1)
    forecast_score = round(max(score, min(100, forecast_score)), 1)
    if score < 18: tier = "weak"
    elif score < 42: tier = "normal"
    elif score < 68: tier = "high"
    else: tier = "extreme"
    if tier == "extreme" and official < 2:
        tier = "high"  # weather/social chatter alone can never claim extreme pollen
    if score_before_evening < 18: tier_before_evening = "weak"
    elif score_before_evening < 42: tier_before_evening = "normal"
    elif score_before_evening < 68: tier_before_evening = "high"
    else: tier_before_evening = "extreme"
    if tier_before_evening == "extreme" and official < 2:
        tier_before_evening = "high"
    forecast_tier = "weak" if forecast_score < 18 else "normal" if forecast_score < 42 else "high" if forecast_score < 68 else "extreme"
    if preboost:
        rank = {name: index for index, name in enumerate(LEVELS)}
        forecast_tier = LEVELS[min(len(LEVELS) - 1, max(rank[forecast_tier], rank[tier] + 1))]
        reasons.append(f"官方预报高峰即将到来，提前 {config.get('preboost_minutes', 45)} 分钟预升档")
    return {"score": score, "forecast_score_6h": forecast_score, "tier": tier, "forecast_tier_6h": forecast_tier,
            "tier_label": {"weak": "弱", "normal": "正常", "high": "高", "extreme": "极高"}[tier],
            "forecast_hours_used": forecast_hours_used,
            "forecast_weather_adjustment": forecast_weather_adjustment,
            "forecast_components": forecast_components,
            "weather_context_active": weather_context_active,
            "official_forecast_level": official_forecast_level,
            "official_forecast_peak_at": official_forecast_peak_at,
            "reasons": reasons, "evening_boost": evening,
            "tier_before_evening": tier_before_evening, "preboost_active": preboost,
            "residual_active": residual_active, "residual_remaining_hours": round(residual_remaining_h, 2),
            "as_of": now_local.isoformat(),
            "sources": {"official_pollen": official_source,
                        "weather": "Open-Meteo weather forecast" if weather else "unavailable_or_stale",
                        "social": "operator_entry"}}


def desired_tier(risk: dict[str, Any]) -> str:
    """Combine today's risk, short forecast preboost, and indoor cleanup tail."""
    rank = {name: index for index, name in enumerate(LEVELS)}
    current = risk.get("tier", "weak")
    forecast = risk.get("forecast_tier_6h", current)
    desired = max((current, forecast), key=lambda item: rank.get(item, 0))
    if risk.get("residual_active") and rank.get(desired, 0) < rank["normal"]:
        return "normal"
    return desired


def _has_unnegated_phrase(pattern: str, text: str) -> bool:
    """Match a forecast phrase only when nearby Chinese wording affirms it."""
    negated_before = re.compile(
        r"(?:并非|并未|尚未|还未|未能|未曾|未达到|未达|未进入|未出现|未见|未到|"
        r"没有|不会|不再|不是|不属|未必|非)[^，。；！？!?]{0,12}$"
    )
    negated_after = re.compile(
        r"^[^，。；！？!?]{0,12}(?:尚未|还未|未能|未曾|未达到|未达|未进入|未出现|未见|未到|"
        r"没有|不会|不再|并未|未必)"
    )
    for match in re.finditer(pattern, text, re.I):
        before = text[max(0, match.start() - 18):match.start()]
        after = text[match.end():match.end() + 18]
        if negated_before.search(before) or negated_after.search(after):
            continue
        return True
    return False


def parse_official_pollen_page(page: str, now: datetime | None = None) -> dict[str, Any]:
    """Extract only fresh, explicit Cupressaceae forecast statements from HTML."""
    if not isinstance(page, str) or len(page) > 500_000:
        raise ValueError("官方预报网页超出安全读取范围")
    text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", page)
    text = unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        raise ValueError("官方预报网页没有可读正文")
    now = now or datetime.now(resolve_timezone("Asia/Shanghai"))
    if now.tzinfo is None: now = now.replace(tzinfo=resolve_timezone("Asia/Shanghai"))
    date_match = re.search(r"(?:发布日期|发布时间|发布于|更新于|日期|published|updated)[^0-9]{0,24}(20\d{2})\s*(?:年|[-/.])\s*(\d{1,2})\s*(?:月|[-/.])\s*(\d{1,2})", text, re.I)
    if not date_match:
        date_match = re.search(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", text)
    if not date_match:
        raise ValueError("找不到公告发布日期；为防旧文章误触发，不采信")
    try: published = datetime(*(int(part) for part in date_match.groups()), tzinfo=now.tzinfo)
    except ValueError: raise ValueError("公告发布日期无效")
    age_days = (now - published).total_seconds() / 86_400
    if age_days < -1 or age_days > 7:
        raise ValueError("该公告不是最近 7 天发布，已忽略旧预报")
    pollen_terms = re.compile(r"柏科|侧柏|圆柏|沙地柏|柏树|cupressaceae|cypress", re.I)
    positions = [match.start() for match in pollen_terms.finditer(text)]
    if not positions:
        raise ValueError("公告未明确提到柏科/侧柏，避免把杂草花粉误当柏花粉")
    context = " … ".join(text[max(0, index - 300):min(len(text), index + 1_200)] for index in positions[:4])
    if _has_unnegated_phrase(r"极高|爆表|非常高|\b(?:4|5)\s*级\b|4\s*(?:到|至|[-~～])\s*5\s*级", context): level = 4
    elif _has_unnegated_phrase(r"高峰|高浓度|浓度(?:较)?高|水平较高|高风险|\b3\s*级\b|3\s*(?:到|至|[-~～])\s*4\s*级", context): level = 3
    elif _has_unnegated_phrase(r"中等|中度|\b2\s*级\b|适中", context): level = 2
    elif _has_unnegated_phrase(r"低浓度|较低|偏低|\b1\s*级\b|低风险", context): level = 1
    else: raise ValueError("公告提到柏科但没有可判读的浓度级别/高峰信息")
    peak_at = None
    peak_until = None
    approximate_peak_window = False
    date_pattern = re.compile(
        r"(?:(20\d{2})\s*年\s*)?(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]"
        r"(?:\s*(?:至|到|[-~～])\s*(?:(20\d{2})\s*年\s*)?(?:(\d{1,2})\s*月\s*)?(\d{1,2})\s*[日号])?"
    )
    for peak_match in re.finditer(r"高峰(?:日|期)?|达峰|峰值", text):
        window_start = max(0, peak_match.start() - 180)
        window = text[window_start:min(len(text), peak_match.end() + 100)]
        if not pollen_terms.search(window): continue
        date_matches = list(date_pattern.finditer(window))
        # Ignore the article's own publication date and prefer the explicit
        # forecast date nearest the peak wording.
        date_matches.sort(key=lambda item: abs(window_start + item.start() - peak_match.start()))
        for date_match in date_matches:
            first_year, first_month, first_day, second_year, second_month, second_day = date_match.groups()
            try:
                year = int(first_year or published.year)
                month = int(first_month)
                day = int(first_day)
                forecast_day = date(year, month, day)
                # A date range such as “3月17日至21日” omits the second month/year.
                end_day = forecast_day
                if second_day:
                    end_year = int(second_year or year)
                    end_month = int(second_month or month)
                    if not second_year and end_month < month:
                        end_year += 1
                    end_day = date(end_year, end_month, int(second_day))
                # Forecast windows may already have started when an article is
                # published. Keep an ongoing range; reject only windows fully
                # in the past so their peak can still be shown to the operator.
                if end_day < published.date(): continue
                peak_at = datetime(year, month, day, 10, 0, tzinfo=published.tzinfo).isoformat()
                # The forecast window covers the full final calendar day;
                # 10:00 is only the estimated first-day preboost reference.
                peak_until = datetime(end_day.year, end_day.month, end_day.day,
                                      23, 59, 59, tzinfo=published.tzinfo).isoformat()
                break
            except (TypeError, ValueError):
                continue
        if peak_at: break
    if peak_at is None:
        # Official phenology notices often forecast a broad span such as
        # “3月中下旬至4月上旬”, not an exact day. Preserve that uncertainty
        # as a date window instead of silently losing the advance forecast.
        period_pattern = re.compile(
            r"(?:(20\d{2})\s*年\s*)?(\d{1,2})\s*月\s*(中下旬|上中旬|上旬|中旬|下旬)"
            r"(?:\s*(?:至|到|[-~～])\s*(?:(20\d{2})\s*年\s*)?"
            r"(?:(\d{1,2})\s*月\s*)?(中下旬|上中旬|上旬|中旬|下旬))?"
        )
        def period_bounds(year: int, month: int, label: str) -> tuple[date, date]:
            last = calendar.monthrange(year, month)[1]
            ranges = {"上旬": (1, 10), "中旬": (11, 20), "下旬": (21, last),
                      "上中旬": (1, 20), "中下旬": (11, last)}
            first_day, last_day = ranges[label]
            return date(year, month, first_day), date(year, month, last_day)
        for peak_match in re.finditer(r"高峰(?:日|期)?|达峰|峰值", text):
            window_start = max(0, peak_match.start() - 180)
            window = text[window_start:min(len(text), peak_match.end() + 100)]
            if not pollen_terms.search(window): continue
            periods = list(period_pattern.finditer(window))
            periods.sort(key=lambda item: abs(window_start + item.start() - peak_match.start()))
            for period in periods:
                first_year, first_month, first_label, second_year, second_month, second_label = period.groups()
                try:
                    year = int(first_year or published.year)
                    month = int(first_month)
                    start_day, _ = period_bounds(year, month, first_label)
                    end_year = int(second_year or year) if second_label else year
                    end_month = int(second_month or month) if second_label else month
                    end_label = second_label or first_label
                    if second_label and not second_year and end_month < month:
                        end_year += 1
                    _, end_day = period_bounds(end_year, end_month, end_label)
                    # A freshly published annual phenology note late in the
                    # year may refer to the coming spring, even if it omits
                    # the year. Resolve only an unambiguous >180-day rollover.
                    if not first_year and end_day < published.date() and (published.date() - end_day).days > 180:
                        start_day, _ = period_bounds(year + 1, month, first_label)
                        if not second_year:
                            end_year += 1
                        _, end_day = period_bounds(end_year, end_month, end_label)
                    if end_day < published.date(): continue
                    peak_at = datetime(start_day.year, start_day.month, start_day.day,
                                       10, 0, tzinfo=published.tzinfo).isoformat()
                    peak_until = datetime(end_day.year, end_day.month, end_day.day,
                                          23, 59, 59, tzinfo=published.tzinfo).isoformat()
                    approximate_peak_window = True
                    break
                except (TypeError, ValueError, OverflowError):
                    continue
            if peak_at: break
    return {"level": level, "published_at_ms": int(published.timestamp() * 1000),
            "predicted_peak_at": peak_at, "predicted_peak_until": peak_until,
            "peak_time_precision": ("approximate_month_decade_10_local" if approximate_peak_window
                                    else "date_window_10_local" if peak_at else None),
            "evidence": context[:600], "age_days": round(age_days, 2)}


def target_mode(baseline: str, tier: str, strength: str) -> str:
    if baseline not in (*MODES, "off"): raise ValueError("baseline mode is not configured")
    if baseline == "off" or tier == "weak": return baseline
    # Automatic mode is an appliance-managed thermostat, not a guaranteed
    # higher airflow. Keep it as configured and only promise discrete fan steps
    # for the three explicit speed settings.
    if baseline == "auto": return "auto"
    speed_steps = ("low", "medium", "high")
    base_index = speed_steps.index(baseline)
    tier_step = {"weak": 0, "normal": 1, "high": 2, "extreme": 3}[tier]
    strength = strength if strength in STRENGTHS else "high"
    if strength == "normal":
        requested_steps = min(tier_step, 1)
    elif strength == "high":
        requested_steps = min(tier_step, 2)
    else:
        # “Extra” shifts the response one risk band earlier. This makes it
        # observably stronger than “high” while respecting the physical
        # low/medium/high ceiling; it never invents a turbo mode.
        requested_steps = min(len(speed_steps) - 1, tier_step + 1)
    target_index = min(len(speed_steps) - 1, base_index + requested_steps)
    return speed_steps[target_index]


def selected_target_capacity(config: dict[str, Any], devices: list[dict[str, Any]], tier: str) -> dict[str, Any]:
    """Estimate nominal airflow for configured, calibrated devices at this tier.

    This is a planning estimate, not measured CADR. Offline calibrated units are
    included in the commissioned projection, but excluded from the online subtotal.
    Auto and off baselines are not assigned an invented airflow value.
    """
    rates = config["airflow_m3h"]
    volume = float(config["area_m2"]) * float(config["ceiling_m"])
    configured = [device for device in devices if device.get("baseline_mode")]
    eligible = [device for device in configured if device.get("calibrated")]
    online = [device for device in eligible if device.get("connection_state") == "online"]

    def estimate(rows: list[dict[str, Any]]) -> tuple[float, int]:
        total = 0.0
        unrated_auto = 0
        for device in rows:
            baseline = device.get("baseline_mode")
            mode = target_mode(baseline, tier, config["strength"])
            if mode in rates:
                total += float(rates[mode])
            elif mode == "auto":
                unrated_auto += 1
        return round(total, 1), unrated_auto

    planned_flow, planned_auto = estimate(eligible)
    online_flow, online_auto = estimate(online)
    return {
        "configured_device_count": len(configured),
        "calibrated_device_count": len(eligible),
        "online_calibrated_device_count": len(online),
        "tier": tier,
        "projected_airflow_m3h": planned_flow,
        "projected_air_changes_per_hour": round(planned_flow / volume, 2),
        "online_airflow_m3h": online_flow,
        "online_air_changes_per_hour": round(online_flow / volume, 2),
        "auto_unrated_count": planned_auto,
        "online_auto_unrated_count": online_auto,
        "meets_15x_nominal": planned_flow >= volume * 15,
        "meets_20x_nominal": planned_flow >= volume * 20,
        "caveat": "基于已标定设备的目标档位与标称风量估算；不含滤网效率，不能替代 CADR 或实测洁净效果。",
    }


def next_physical_step(current: str, target: str) -> str | None:
    """Return the one physical action to move one verified cycle step closer."""
    if current == target: return None
    if current == "unknown": return None
    if current == "off": return "opto.power.pulse"
    if target == "off": return "opto.power.pulse"
    if current not in MODES or target not in MODES: return None
    return "opto.mode.pulse"


def apply_hysteresis(current: str, candidate: str, candidate_since: float,
                     desired: str, now_s: float, downshift_dwell_s: float = 5_400) -> tuple[str, str, float]:
    """Escalate promptly, but require a stable low-risk dwell before reducing."""
    rank = {name: index for index, name in enumerate(LEVELS)}
    if current not in rank: current = "weak"
    if desired not in rank: desired = current
    if rank[desired] > rank[current]:
        return desired, desired, now_s
    if rank[desired] < rank[current]:
        if candidate != desired:
            return current, desired, now_s
        if now_s - candidate_since >= downshift_dwell_s:
            return desired, desired, now_s
        return current, candidate, candidate_since
    return current, desired, now_s


def end_evening_boost(current: str, candidate: str, candidate_since: float,
                      desired: str, base_tier: str | None, residual_active: bool,
                      now_s: float) -> tuple[str, str, float, bool]:
    """End a schedule-only normal boost on time without shortening pollen dwell.

    The fixed homecoming boost is a one-time daily window, not a noisy sensor.
    If that window alone lifted weak to normal, let it end at its configured
    boundary. Any independently elevated tier or indoor residual still uses
    the ordinary 90-minute hysteresis.
    """
    if (base_tier == "weak" and current == "normal" and desired == "weak"
            and not residual_active):
        return desired, desired, now_s, True
    return current, candidate, candidate_since, False

