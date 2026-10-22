import unittest
from datetime import datetime
from threading import Thread
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo
import json
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch

import smart_control as smart
from test_dev_server import server


class SmartControlTests(unittest.TestCase):
    def test_restart_recovery_drops_unpublished_queue_and_marks_inflight_pulse_uncertain(self):
        runtime = {"device_actions": {"pur-test": {
            "queue_command": "opto.mode.pulse", "queue_origin": "smart_control",
            "inflight": {"expected": "medium", "sent_at_ms": 1234, "request_id": "req-1"}}}}
        server.recover_smart_runtime_after_restart(runtime)
        action = runtime["device_actions"]["pur-test"]
        self.assertNotIn("queue_command", action)
        self.assertNotIn("queue_origin", action)
        self.assertTrue(action["inflight"]["restart_recovery"])
        self.assertIn("重启后", action["status"])

    def test_restarted_pulse_requires_a_new_matching_online_lamp_report(self):
        inflight = {"expected": "medium", "sent_at_ms": 1_000}
        self.assertEqual(server.restarted_pulse_recovery(inflight, "medium", 999, True, 2_000), "wait")
        self.assertEqual(server.restarted_pulse_recovery(inflight, "medium", 1_001, False, 2_000), "wait")
        self.assertEqual(server.restarted_pulse_recovery(inflight, "low", 1_001, True, 2_000), "wait")
        self.assertEqual(server.restarted_pulse_recovery(inflight, "medium", 1_001, True, 2_000), "confirmed")
        self.assertEqual(server.restarted_pulse_recovery(inflight, "low", 1_001, True, 13_001), "uncertain")
        self.assertEqual(server.restarted_pulse_recovery({"sent_at_ms": "bad"}, "low", 2_000, True, 3_000),
                         "uncertain")

    def test_smart_runtime_checkpoint_persists_intent_but_not_ephemeral_queue_or_weather(self):
        runtime = {"weather": {"current": {"temperature_2m": 15}},
                   "device_actions": {"pur-test": {
                       "queue_command": "opto.mode.pulse", "queue_origin": "smart_control",
                       "inflight": {"expected": "medium", "sent_at_ms": 1234}}}}
        with tempfile.TemporaryDirectory() as folder, patch.object(server, "SMART_RUNTIME", runtime), \
                patch.object(server, "SMART_RUNTIME_PATH", Path(folder) / "runtime.json"):
            server.save_smart_runtime()
            saved = json.loads((Path(folder) / "runtime.json").read_text(encoding="utf-8"))
        self.assertNotIn("weather", saved)
        self.assertNotIn("queue_command", saved["device_actions"]["pur-test"])
        self.assertIn("inflight", saved["device_actions"]["pur-test"])

    def test_smart_control_status_endpoint(self):
        httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        worker = Thread(target=httpd.serve_forever, daemon=True)
        worker.start()
        try:
            with urlopen(f"http://127.0.0.1:{httpd.server_address[1]}/api/v1/smart-control", timeout=3) as response:
                body = json.loads(response.read().decode("utf-8"))
            self.assertFalse(body["enabled"])
            self.assertEqual(body["capacity"]["volume_m3"], 400)
            self.assertEqual(body["selected_capacity"]["projected_airflow_m3h"], 0)
            self.assertIn("risk", body)
        finally:
            httpd.shutdown(); httpd.server_close(); worker.join(timeout=2)

    def test_smart_control_settings_are_persisted_but_unverified_devices_cannot_be_enabled(self):
        with tempfile.TemporaryDirectory() as folder:
            config = smart.default_config()
            runtime = {"weather": None, "weather_fetched_at": 0, "last_peak_at": 0,
                       "effective_tier": "weak", "candidate_tier": "weak", "candidate_since": 0,
                       "device_actions": {}, "last_error": ""}
            with patch.object(server, "SMART_PATH", Path(folder) / "settings.json"), \
                    patch.object(server, "SMART_CONFIG", config), patch.object(server, "SMART_RUNTIME", runtime), \
                    patch.object(server, "CATALOG", {"devices": {}}), patch.object(server, "DEVICES", {}):
                httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
                worker = Thread(target=httpd.serve_forever, daemon=True); worker.start()
                base = f"http://127.0.0.1:{httpd.server_address[1]}/api/v1/smart-control"
                try:
                    payload = smart.default_config(); payload.update(location_name="Singapore", latitude=1.35, longitude=103.8)
                    request = urlopen(Request(
                        base, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="PUT"), timeout=3)
                    saved = json.loads(request.read().decode())
                    self.assertFalse(saved["enabled"])
                    self.assertTrue((Path(folder) / "settings.json").exists())
                    payload.update(enabled=True, baselines={"pur-test": "low"})
                    request = Request(
                        base, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="PUT")
                    with self.assertRaises(HTTPError) as raised: urlopen(request, timeout=3)
                    self.assertEqual(raised.exception.code, 400)
                finally:
                    httpd.shutdown(); httpd.server_close(); worker.join(timeout=2)

    def test_manual_device_target_is_queued_without_bypassing_lamp_confirmation(self):
        device_id = "pur-test"
        profile = {"alias": "test", "room": "客厅", "hidden": False,
                   "calibration": {"channels": {key: {"direction": "rising", "threshold_mv": 500,
                       "on_threshold_mv": 500, "off_threshold_mv": 200, "clear_threshold_mv": 200,
                       "off_median_mv": 20, "on_median_mv": 800, "off_sigma_mv": 0, "quality": "pass"}
                       for key in smart.MODES}}}
        now_ms = int(time.time() * 1000)
        device = {"device_id": device_id, "state": {"adc_mv": [800, 20, 20, 20]},
                  "_state_seen_ms": now_ms, "last_seen_ms": now_ms}
        runtime = {"weather": None, "weather_fetched_at": 0, "last_peak_at": 0,
                   "effective_tier": "weak", "candidate_tier": "weak", "candidate_since": 0,
                   "device_actions": {}, "last_error": ""}
        with patch.object(server, "CATALOG", {"devices": {device_id: profile}}), \
                patch.object(server, "DEVICES", {device_id: device}), patch.object(server, "SMART_RUNTIME", runtime):
            httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
            worker = Thread(target=httpd.serve_forever, daemon=True); worker.start()
            try:
                url = f"http://127.0.0.1:{httpd.server_address[1]}/api/v1/devices/{device_id}/target"
                request = Request(url, data=json.dumps({"mode": "high"}).encode(),
                                  headers={"Content-Type": "application/json"}, method="POST")
                with urlopen(request, timeout=3) as response:
                    result = json.loads(response.read().decode())
                self.assertEqual(result["status"], "accepted")
                action = runtime["device_actions"][device_id]
                self.assertEqual(action["manual_target_mode"], "high")
                self.assertGreater(action["manual_override_until_ms"], now_ms)
            finally:
                httpd.shutdown(); httpd.server_close(); worker.join(timeout=2)

    def test_house_capacity_uses_airflow_not_cadr(self):
        config = smart.normalize_config({})
        result = smart.house_capacity(config)
        self.assertEqual(result["volume_m3"], 400)
        self.assertEqual(result["total_airflow_m3h"], {"low": 460, "medium": 1610, "high": 3450})
        self.assertEqual(result["nominal_air_changes_per_hour"]["low"], 1.15)
        self.assertEqual(result["nominal_air_changes_per_hour"]["high"], 8.63)
        self.assertIn("不等于颗粒物 CADR", result["warning"])
        self.assertFalse(result["high_meets_15x_nominal"])
        self.assertIn("低于 15 次/小时", result["capacity_caveat"])
        self.assertEqual(result["minimum_high_devices_for_15x"], 40)
        self.assertEqual(result["minimum_high_devices_for_20x"], 54)
        selected = smart.house_capacity(smart.normalize_config({"baselines": {"pur-a": "low", "pur-b": "auto"}}))
        self.assertEqual(selected["baselined_device_count"], 2)
        self.assertEqual(selected["baselined_airflow_m3h"], 20)
        self.assertEqual(selected["auto_baseline_devices_unrated"], 1)

    def test_selected_target_capacity_counts_only_calibrated_devices_and_separates_offline(self):
        config = smart.normalize_config({"strength": "high", "baselines": {
            "low-online": "low", "auto-online": "auto", "high-offline": "high", "uncalibrated": "low"}})
        devices = [
            {"device_id": "low-online", "baseline_mode": "low", "calibrated": True, "connection_state": "online"},
            {"device_id": "auto-online", "baseline_mode": "auto", "calibrated": True, "connection_state": "online"},
            {"device_id": "high-offline", "baseline_mode": "high", "calibrated": True, "connection_state": "offline"},
            {"device_id": "uncalibrated", "baseline_mode": "low", "calibrated": False, "connection_state": "online"},
        ]
        result = smart.selected_target_capacity(config, devices, "high")
        self.assertEqual(result["configured_device_count"], 4)
        self.assertEqual(result["calibrated_device_count"], 3)
        self.assertEqual(result["online_calibrated_device_count"], 2)
        self.assertEqual(result["projected_airflow_m3h"], 300)
        self.assertEqual(result["online_airflow_m3h"], 150)
        self.assertEqual(result["projected_air_changes_per_hour"], 0.75)
        self.assertEqual(result["online_air_changes_per_hour"], 0.38)
        self.assertEqual(result["auto_unrated_count"], 1)

    def test_official_pollen_page_parser_requires_fresh_cupressaceae_evidence(self):
        now = datetime(2026, 4, 1, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
        page = "<html><p>发布时间：2026年4月1日</p><p>柏科花粉浓度极高，侧柏进入散粉高峰</p><p>4月5日至8日柏科花粉预计进入高峰</p></html>"
        parsed = smart.parse_official_pollen_page(page, now)
        self.assertEqual(parsed["level"], 4)
        self.assertEqual(parsed["predicted_peak_at"], "2026-04-05T10:00:00+08:00")
        stale = page.replace("2026年4月1日", "2026年3月20日")
        with self.assertRaisesRegex(ValueError, "最近 7 天"):
            smart.parse_official_pollen_page(stale, now)
        weeds_only = page.replace("柏科", "蒿草").replace("侧柏", "杂草")
        with self.assertRaisesRegex(ValueError, "未明确提到柏科"):
            smart.parse_official_pollen_page(weeds_only, now)
        undated = "<p>柏科花粉浓度极高，侧柏进入散粉高峰</p>"
        with self.assertRaisesRegex(ValueError, "发布日期"):
            smart.parse_official_pollen_page(undated, now)

        # Representative official forecast wording: publication date overlaps
        # the range start, so the range parser must still identify the window.
        official_style = "<p>发布日期：2026-03-17</p><p>3月17日至21日，北京大部分地区柏科花粉将陆续达到浓度高峰日。</p>"
        parsed_peak = smart.parse_official_pollen_page(
            official_style, datetime(2026, 3, 17, 8, tzinfo=ZoneInfo("Asia/Shanghai")))
        self.assertEqual(parsed_peak["level"], 3)
        self.assertEqual(parsed_peak["predicted_peak_at"], "2026-03-17T10:00:00+08:00")

    def test_official_page_fetch_is_limited_to_https_gov_cn(self):
        self.assertEqual(server.validate_official_pollen_url("https://www.beijing.gov.cn/a/b.html"),
                         "https://www.beijing.gov.cn/a/b.html")
        for url in ("http://beijing.gov.cn/a", "https://beijing.gov.cn.evil.test/a",
                    "https://user@beijing.gov.cn/a", "https://127.0.0.1/a"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                server.validate_official_pollen_url(url)

    def test_official_page_fetch_reads_bounded_html_and_records_source(self):
        class Headers:
            def get_content_type(self): return "text/html"
            def get_content_charset(self): return "utf-8"
            def get(self, key): return None
        class Response:
            headers = Headers()
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def geturl(self): return "https://www.beijing.gov.cn/forecast.html"
            def read(self, limit):
                self.read_limit = limit
                return f"<p>发布日期：{datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y年%m月%d日')}</p><p>柏科花粉浓度高</p>".encode()
        class Opener:
            def __init__(self): self.response = Response()
            def open(self, request, timeout):
                self.request = request; self.timeout = timeout
                return self.response
        opener = Opener()
        config = smart.default_config(); config["official_url"] = opener.response.geturl()
        with patch.object(server.urllib.request, "build_opener", return_value=opener):
            result = server.fetch_official_pollen(config)
        self.assertEqual(result["level"], 3)
        self.assertEqual(result["source_url"], opener.response.geturl())
        self.assertEqual(opener.response.read_limit, 500_001)
        self.assertEqual(opener.timeout, 8)

    def test_background_official_refresh_discards_result_after_source_changes(self):
        config = smart.default_config(); config["official_url"] = "https://www.beijing.gov.cn/a"
        runtime = {"official_pollen": None, "official_pollen_error": ""}
        result = {"level": 3, "source_url": config["official_url"]}
        with patch.object(server, "SMART_CONFIG", config), patch.object(server, "SMART_RUNTIME", runtime), \
                patch.object(server, "fetch_official_pollen", return_value=result):
            server._refresh_official_pollen(config)
            self.assertEqual(runtime["official_pollen"], result)
            changed = dict(config, official_url="https://www.example.org/not-allowed")
            with patch.object(server, "fetch_official_pollen", return_value={"level": 4, "source_url": config["official_url"]}):
                server._refresh_official_pollen(changed)
            self.assertEqual(runtime["official_pollen"], result)

    def test_automatic_official_signal_overrides_manual_only_when_fresh(self):
        now = datetime(2026, 4, 1, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
        config = smart.normalize_config({"official_level": 1})
        signal = {"level": 4, "published_at_ms": int(now.timestamp() * 1000),
                  "fetched_at_ms": int(now.timestamp() * 1000)}
        result = smart.assess(config, None, now, official_signal=signal)
        self.assertEqual(result["sources"]["official_pollen"], "verified_gov_cn_page")
        self.assertEqual(result["tier"], "extreme")
        stale = dict(signal, published_at_ms=int((now.timestamp() - 49 * 3600) * 1000))
        result = smart.assess(config, None, now, official_signal=stale)
        self.assertEqual(result["sources"]["official_pollen"], "operator_entry")
        self.assertEqual(result["tier"], "weak")

    def test_fresh_government_peak_date_can_trigger_preboost_without_manual_peak_entry(self):
        now = datetime(2026, 4, 1, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
        config = smart.normalize_config({"preboost_minutes": 45})
        signal = {"level": 3, "published_at_ms": int(now.timestamp() * 1000),
                  "fetched_at_ms": int(now.timestamp() * 1000),
                  "predicted_peak_at": (now + smart.timedelta(minutes=30)).isoformat()}
        result = smart.assess(config, None, now, official_signal=signal)
        self.assertTrue(result["preboost_active"])
        self.assertEqual(result["forecast_tier_6h"], "extreme")

    def test_enable_requires_location(self):
        with self.assertRaisesRegex(ValueError, "坐标"):
            smart.normalize_config({"enabled": True})

    def test_weather_and_social_cannot_claim_extreme_without_official_pollen(self):
        config = smart.normalize_config({"social_level": 3})
        weather = {"current": {"temperature_2m": 15, "precipitation": 0, "cloud_cover": 10},
                   "fetched_at_ms": int(datetime(2026, 4, 1, 12, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000),
                   "daily": {"sunshine_duration_s": [36000] * 4, "precipitation_sum_mm": [0] * 4}}
        risk = smart.assess(config, weather, datetime(2026, 4, 1, 12, tzinfo=ZoneInfo("Asia/Shanghai")))
        self.assertNotEqual(risk["tier"], "extreme")

    def test_rain_is_bounded_suppression_and_official_level_remains_primary(self):
        now = datetime(2026, 4, 1, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
        config = smart.normalize_config({"official_level": 2, "official_updated_at": int(now.timestamp() * 1000)})
        fetched_at_ms = int(now.timestamp() * 1000)
        dry = {"current": {"temperature_2m": 15, "precipitation": 0, "cloud_cover": 10},
               "fetched_at_ms": fetched_at_ms}
        wet = {"current": {"temperature_2m": 15, "precipitation": 2, "cloud_cover": 90},
               "fetched_at_ms": fetched_at_ms}
        self.assertGreater(smart.assess(config, dry, now)["score"], smart.assess(config, wet, now)["score"])
        self.assertGreaterEqual(smart.assess(config, wet, now)["score"], 0)

    def test_rain_to_sun_and_past_sunny_streak_are_distinct_bounded_signals(self):
        now = datetime(2026, 4, 1, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
        config = smart.normalize_config({})
        current = {"temperature_2m": 15, "precipitation": 0, "cloud_cover": 80}
        times = ["2026-03-28", "2026-03-29", "2026-03-30", "2026-03-31", "2026-04-01",
                 "2026-04-02", "2026-04-03", "2026-04-04", "2026-04-05"]
        fetched_at_ms = int(now.timestamp() * 1000)
        rain_then_sun = {"current": current, "fetched_at_ms": fetched_at_ms, "daily": {"time": times,
            "sunshine_duration_s": [0, 0, 0, 0, 25200, 36000, 36000, 36000, 36000],
            "precipitation_sum_mm": [0, 0, 0, 12, 0, 0, 0, 0, 0]}}
        no_recent_rain = {"current": current, "fetched_at_ms": fetched_at_ms, "daily": {"time": times,
            "sunshine_duration_s": [0, 0, 0, 0, 25200, 36000, 36000, 36000, 36000],
            "precipitation_sum_mm": [0] * 9}}
        wet_clear = smart.assess(config, rain_then_sun, now)
        clear_only = smart.assess(config, no_recent_rain, now)
        self.assertEqual(wet_clear["score"], clear_only["score"] + 3)

        past_streak = {"current": current, "fetched_at_ms": fetched_at_ms, "daily": {"time": times,
            "sunshine_duration_s": [36000] * 9, "precipitation_sum_mm": [0] * 9}}
        future_only = {"current": current, "fetched_at_ms": fetched_at_ms, "daily": {"time": times,
            "sunshine_duration_s": [0, 0, 0, 0, 36000, 36000, 36000, 36000, 36000],
            "precipitation_sum_mm": [0] * 9}}
        self.assertEqual(smart.assess(config, past_streak, now)["score"],
                         smart.assess(config, future_only, now)["score"] - 3)

    def test_stale_missing_or_future_weather_timestamp_is_ignored(self):
        now = datetime(2026, 4, 1, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
        config = smart.normalize_config({"official_level": 2, "official_updated_at": int(now.timestamp() * 1000)})
        current_rain = {"current": {"temperature_2m": 15, "precipitation": 10, "cloud_cover": 90}}
        fresh = dict(current_rain, fetched_at_ms=int(now.timestamp() * 1000))
        stale = dict(current_rain, fetched_at_ms=int((now.timestamp() - 61 * 60) * 1000))
        future = dict(current_rain, fetched_at_ms=int((now.timestamp() + 6 * 60) * 1000))

        baseline = smart.assess(config, None, now)
        self.assertLess(smart.assess(config, fresh, now)["score"], baseline["score"])
        for invalid in (current_rain, stale, future):
            with self.subTest(timestamp=invalid.get("fetched_at_ms")):
                result = smart.assess(config, invalid, now)
                self.assertEqual(result["score"], baseline["score"])
                self.assertEqual(result["sources"]["weather"], "unavailable_or_stale")
                self.assertTrue(any("天气数据" in reason for reason in result["reasons"]))

    def test_weather_fetch_requests_and_preserves_four_days_of_daily_history(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self):
                return json.dumps({"current": {}, "hourly": {}, "daily": {
                    "time": [f"2026-03-{day:02d}" for day in range(28, 32)] + [f"2026-04-0{day}" for day in range(1, 6)],
                    "precipitation_sum": [0] * 9, "sunshine_duration": [36000] * 9}}).encode()
        config = smart.default_config(); config.update(latitude=1.3, longitude=103.8)
        with patch.object(server.urllib.request, "urlopen", return_value=Response()) as request:
            result = server.fetch_smart_weather(config)
        self.assertEqual(len(result["daily"]["time"]), 9)
        self.assertEqual(len(result["daily"]["precipitation_sum_mm"]), 9)
        query = server.urllib.parse.parse_qs(server.urllib.parse.urlsplit(request.call_args.args[0].full_url).query)
        self.assertEqual(query["past_days"], ["4"])

    def test_official_peak_preboosts_one_tier_within_configured_window(self):
        now = datetime(2026, 4, 1, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
        config = smart.normalize_config({"official_level": 1, "official_peak_at": "2026-04-01T12:30",
                                         "official_updated_at": int(now.timestamp() * 1000), "preboost_minutes": 45})
        result = smart.assess(config, None, now)
        self.assertEqual(result["tier"], "weak")
        self.assertEqual(result["forecast_tier_6h"], "normal")
        self.assertTrue(result["preboost_active"])

    def test_old_manual_and_social_signals_expire(self):
        now = datetime(2026, 4, 3, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
        old_ms = int(datetime(2026, 4, 1, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000)
        config = smart.normalize_config({"official_level": 3, "social_level": 3,
                                         "official_updated_at": old_ms, "social_updated_at": old_ms})
        risk = smart.assess(config, None, now)
        self.assertEqual(risk["tier"], "weak")
        self.assertTrue(any("缺少有效时间戳" in reason or "超过 48 小时" in reason for reason in risk["reasons"]))

    def test_manual_signals_without_valid_timestamps_are_not_reused_forever(self):
        now = datetime(2026, 4, 1, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
        missing = smart.normalize_config({"official_level": 4, "social_level": 3})
        risk = smart.assess(missing, None, now)
        self.assertEqual(risk["tier"], "weak")
        self.assertTrue(any("缺少有效时间戳" in reason for reason in risk["reasons"]))
        future = int((now.timestamp() + 600) * 1000)
        invalid_future = smart.normalize_config({"official_level": 4, "social_level": 3,
            "official_updated_at": future, "social_updated_at": future})
        risk = smart.assess(invalid_future, None, now)
        self.assertEqual(risk["tier"], "weak")
        valid = smart.normalize_config({"official_level": 4, "social_level": 3,
            "official_updated_at": int(now.timestamp() * 1000), "social_updated_at": int(now.timestamp() * 1000)})
        self.assertEqual(smart.assess(valid, None, now)["tier"], "extreme")

    def test_target_policy_has_explicit_non_auto_speed_cap(self):
        self.assertEqual(smart.target_mode("low", "weak", "extra"), "low")
        self.assertEqual(smart.target_mode("low", "normal", "normal"), "medium")
        self.assertEqual(smart.target_mode("low", "high", "high"), "high")
        self.assertEqual(smart.target_mode("low", "extreme", "extra"), "high")
        self.assertEqual(smart.target_mode("auto", "extreme", "extra"), "auto")
        self.assertEqual(smart.target_mode("off", "extreme", "extra"), "off")

    def test_actuation_is_one_physical_press_at_a_time(self):
        self.assertEqual(smart.next_physical_step("off", "high"), "opto.power.pulse")
        self.assertEqual(smart.next_physical_step("medium", "high"), "opto.mode.pulse")
        self.assertEqual(smart.next_physical_step("high", "medium"), "opto.mode.pulse")
        self.assertEqual(smart.next_physical_step("high", "off"), "opto.power.pulse")
        self.assertIsNone(smart.next_physical_step("unknown", "high"))

    def test_escalation_is_immediate_but_deescalation_has_dwell(self):
        current, candidate, since = smart.apply_hysteresis("weak", "weak", 0, "high", 10)
        self.assertEqual((current, candidate, since), ("high", "high", 10))
        current, candidate, since = smart.apply_hysteresis(current, candidate, since, "normal", 20)
        self.assertEqual(current, "high")
        current, candidate, since = smart.apply_hysteresis(current, candidate, since, "normal", 5_419)
        self.assertEqual(current, "high")
        current, candidate, since = smart.apply_hysteresis(current, candidate, since, "normal", 5_420)
        self.assertEqual(current, "normal")

    def test_noisy_hourly_pollen_signal_does_not_chatter_across_risk_tiers(self):
        effective, candidate, candidate_since = "weak", "weak", 0.0
        transitions = []
        start = 1_000_000.0
        for tick in range(48 * 12):  # 48h, sampled every five minutes
            now_s = start + tick * 300
            # One five-minute high spike every hour, normal the rest of the time.
            desired = "high" if tick % 12 == 0 else "normal"
            previous = effective
            effective, candidate, candidate_since = smart.apply_hysteresis(
                effective, candidate, candidate_since, desired, now_s)
            if effective != previous: transitions.append((now_s, previous, effective))
        self.assertEqual(len(transitions), 1)
        self.assertEqual(transitions[0][2], "high")


if __name__ == "__main__":
    unittest.main()
