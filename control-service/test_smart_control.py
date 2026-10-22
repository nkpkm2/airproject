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

    def test_restart_discards_unavailable_weather_and_restarts_downshift_observation_dwell(self):
        runtime = {"weather": None, "weather_fetched_at": 1_000_000,
                   "effective_tier": "high", "candidate_tier": "weak", "candidate_since": 1,
                   "device_actions": {}}
        restarted_at = 20_000.0
        with patch.object(server.time, "monotonic", return_value=restarted_at):
            server.recover_smart_runtime_after_restart(runtime)

        self.assertIsNone(runtime["weather"])
        self.assertEqual(runtime["weather_fetched_at"], 0)
        self.assertEqual(runtime["effective_tier"], "high")
        self.assertEqual(runtime["candidate_tier"], "high")
        self.assertEqual(runtime["candidate_since"], restarted_at)

        # Even if a lower tier is observed immediately after a long outage,
        # that outage cannot satisfy the 90-minute continuous-evidence dwell.
        current, candidate, since = smart.apply_hysteresis(
            runtime["effective_tier"], runtime["candidate_tier"], runtime["candidate_since"],
            "weak", restarted_at + 5)
        self.assertEqual(current, "high")
        self.assertEqual(candidate, "weak")
        self.assertEqual(since, restarted_at + 5)

    def test_restarted_pulse_requires_a_new_matching_online_lamp_report(self):
        inflight = {"expected": "medium", "sent_at_ms": 1_000}
        self.assertEqual(server.restarted_pulse_recovery(inflight, "medium", 999, True, 2_000), "wait")
        self.assertEqual(server.restarted_pulse_recovery(inflight, "medium", 1_001, False, 2_000), "wait")
        self.assertEqual(server.restarted_pulse_recovery(inflight, "low", 1_001, True, 2_000), "wait")
        self.assertEqual(server.restarted_pulse_recovery(inflight, "medium", 1_001, True, 2_000), "confirmed")
        self.assertEqual(server.restarted_pulse_recovery(inflight, "low", 1_001, True, 13_001), "uncertain")
        self.assertEqual(server.restarted_pulse_recovery({"sent_at_ms": "bad"}, "low", 2_000, True, 3_000),
                         "uncertain")

    def test_physical_mode_changes_yield_to_manual_control_but_expected_steps_do_not(self):
        action = {}
        now_ms = 1_000_000
        self.assertFalse(server.observe_external_mode_change(action, "low", now_ms))
        self.assertFalse(server.observe_external_mode_change(action, "low", now_ms + 5_000))
        self.assertFalse(server.observe_external_mode_change(action, "medium", now_ms + 10_000,
                                                              expected_mode="medium"))
        self.assertNotIn("manual_override_until_ms", action)
        self.assertTrue(server.observe_external_mode_change(action, "high", now_ms + 15_000))
        self.assertEqual(action["manual_override_until_ms"], now_ms + 15_000 + 30 * 60 * 1000)
        self.assertIn("实体按键", action["status"])
        self.assertFalse(server.observe_external_mode_change(action, "off", now_ms + 20_000,
                                                              expected_mode="off"))
        self.assertEqual(action["manual_override_until_ms"], now_ms + 15_000 + 30 * 60 * 1000)

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

    def test_manual_signal_expiry_refresh_requires_explicit_review(self):
        with tempfile.TemporaryDirectory() as folder:
            now_ms = int(time.time() * 1000)
            config = smart.normalize_config({"official_level": 2, "official_note": "今日园林公告",
                "official_updated_at": now_ms - 49 * 60 * 60 * 1000,
                "social_level": 1, "social_note": "公开讨论增多",
                "social_updated_at": now_ms - 13 * 60 * 60 * 1000})
            runtime = {"weather": None, "weather_fetched_at": 0, "last_peak_at": 0,
                "effective_tier": "weak", "candidate_tier": "weak", "candidate_since": 0,
                "device_actions": {}, "last_error": ""}
            with patch.object(server, "SMART_PATH", Path(folder) / "settings.json"), \
                    patch.object(server, "SMART_CONFIG", config), patch.object(server, "SMART_RUNTIME", runtime), \
                    patch.object(server, "CATALOG", {"devices": {}}), patch.object(server, "DEVICES", {}):
                httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
                worker = Thread(target=httpd.serve_forever, daemon=True); worker.start()
                url = f"http://127.0.0.1:{httpd.server_address[1]}/api/v1/smart-control"
                try:
                    saved = json.loads(urlopen(url, timeout=3).read().decode())
                    self.assertEqual(saved["risk"]["sources"]["official_pollen"], "operator_entry")
                    self.assertTrue(any("超过 48 小时" in reason for reason in saved["risk"]["reasons"]))

                    time.sleep(0.01)
                    payload = dict(config, refresh_official_signal=True)
                    request = Request(url, data=json.dumps(payload).encode(),
                        headers={"Content-Type": "application/json"}, method="PUT")
                    reviewed = json.loads(urlopen(request, timeout=3).read().decode())
                    self.assertGreater(reviewed["config"]["official_updated_at"], now_ms)
                    self.assertNotIn("refresh_official_signal", reviewed["config"])
                    self.assertEqual(reviewed["risk"]["sources"]["official_pollen"], "operator_entry")

                    payload = dict(config, refresh_social_signal=True)
                    request = Request(url, data=json.dumps(payload).encode(),
                        headers={"Content-Type": "application/json"}, method="PUT")
                    social_reviewed = json.loads(urlopen(request, timeout=3).read().decode())
                    self.assertGreater(social_reviewed["config"]["social_updated_at"], now_ms)

                    payload.update(official_level=0, refresh_official_signal=True)
                    request = Request(url, data=json.dumps(payload).encode(),
                        headers={"Content-Type": "application/json"}, method="PUT")
                    with self.assertRaises(HTTPError) as raised: urlopen(request, timeout=3)
                    self.assertEqual(raised.exception.code, 400)
                finally:
                    httpd.shutdown(); httpd.server_close(); worker.join(timeout=2)

    def test_smart_control_settings_are_persisted_but_unverified_devices_cannot_be_enabled(self):
        with tempfile.TemporaryDirectory() as folder:
            config = smart.default_config()
            runtime = {"weather": {"latitude": 0, "longitude": 0}, "weather_fetched_at": 1,
                       "last_peak_at": 0,
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
                    self.assertIsNone(runtime["weather"], "changing home location must clear the prior location's forecast")
                    payload.update(official_level=3, official_note="柏科高", official_url="https://www.beijing.gov.cn/a")
                    request = urlopen(Request(
                        base, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="PUT"), timeout=3)
                    first_official = json.loads(request.read().decode())["config"]["official_updated_at"]
                    payload["official_updated_at"] = first_official
                    time.sleep(0.02)
                    payload["official_url"] = "https://www.beijing.gov.cn/b"
                    request = urlopen(Request(
                        base, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="PUT"), timeout=3)
                    second_official = json.loads(request.read().decode())["config"]["official_updated_at"]
                    self.assertEqual(second_official, first_official, "changing scraper URL must not refresh the manual pollen level")
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
        self.assertEqual(parsed_peak["predicted_peak_until"], "2026-03-21T23:59:59+08:00")

        ongoing_window = official_style.replace("2026-03-17", "2026-03-19")
        parsed_ongoing = smart.parse_official_pollen_page(
            ongoing_window, datetime(2026, 3, 20, 8, tzinfo=ZoneInfo("Asia/Shanghai")))
        self.assertEqual(parsed_ongoing["predicted_peak_at"], "2026-03-17T10:00:00+08:00")
        self.assertEqual(parsed_ongoing["predicted_peak_until"], "2026-03-21T23:59:59+08:00")

        official_page_excerpt = (
            "<p>日期：2026-03-17 16:07 来源：北京市疾病预防控制中心</p>"
            "<p>据最新预警信息，3月17日至21日，北京大部分地区柏科花粉将陆续达到浓度高峰日，"
            "每日10:00-17:00更是全天花粉浓度最高时段。</p>"
        )
        parsed_real_wording = smart.parse_official_pollen_page(
            official_page_excerpt, datetime(2026, 3, 17, 17, tzinfo=ZoneInfo("Asia/Shanghai")))
        self.assertEqual(parsed_real_wording["level"], 3)
        self.assertEqual(parsed_real_wording["predicted_peak_at"], "2026-03-17T10:00:00+08:00")
        self.assertEqual(parsed_real_wording["predicted_peak_until"], "2026-03-21T23:59:59+08:00")

    def test_official_peak_window_keeps_recent_multiday_forecast_valid_through_its_end(self):
        config = smart.normalize_config({})
        published = datetime(2026, 3, 17, 16, 7, tzinfo=ZoneInfo("Asia/Shanghai"))
        now = datetime(2026, 3, 20, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
        signal = {
            "level": 3,
            "published_at_ms": int(published.timestamp() * 1000),
            "fetched_at_ms": int(now.timestamp() * 1000),
            "predicted_peak_at": "2026-03-17T10:00:00+08:00",
            "predicted_peak_until": "2026-03-21T23:59:59+08:00",
        }
        active = smart.assess(config, None, now, official_signal=signal)
        self.assertEqual(active["score"], 52)
        self.assertEqual(active["sources"]["official_pollen"], "verified_gov_cn_page")

        # A fetched page stays useful during the stated peak, not indefinitely.
        after_grace = datetime(2026, 3, 23, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        expired = smart.assess(config, None, after_grace, official_signal=signal)
        self.assertEqual(expired["score"], 0)
        self.assertNotEqual(expired["sources"]["official_pollen"], "verified_gov_cn_page")

    def test_annual_pollen_season_handles_calendar_year_rollover(self):
        config = smart.normalize_config({"season_start_md": "11-15", "season_end_md": "02-15"})
        in_season_dates = ("2026-11-20T12:00:00+08:00", "2027-01-05T12:00:00+08:00",
                           "2027-02-15T12:00:00+08:00")
        out_of_season_dates = ("2026-11-14T12:00:00+08:00", "2027-02-16T12:00:00+08:00",
                               "2027-06-01T12:00:00+08:00")
        for value in in_season_dates:
            with self.subTest(date=value):
                self.assertEqual(smart.assess(config, None, datetime.fromisoformat(value))["score"], 4)
        for value in out_of_season_dates:
            with self.subTest(date=value):
                self.assertEqual(smart.assess(config, None, datetime.fromisoformat(value))["score"], 0)

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
        self.assertEqual(result["score"], 0)
        self.assertEqual(result["forecast_tier_6h"], "normal")

    def test_future_high_official_peak_is_not_treated_as_current_until_preboost_or_peak(self):
        peak_at = datetime(2026, 4, 4, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
        config = smart.normalize_config({"preboost_minutes": 45})
        published = datetime(2026, 4, 1, 12, tzinfo=ZoneInfo("Asia/Shanghai"))

        def signal_for(now):
            return {"level": 4, "published_at_ms": int(published.timestamp() * 1000),
                    "fetched_at_ms": int(now.timestamp() * 1000),
                    "predicted_peak_at": peak_at.isoformat(),
                    "predicted_peak_until": (peak_at + smart.timedelta(days=1)).isoformat()}

        days_ahead = smart.assess(config, None, published, official_signal=signal_for(published))
        self.assertEqual(days_ahead["score"], 0)
        self.assertEqual(days_ahead["tier"], "weak")
        self.assertEqual(days_ahead["official_forecast_level"], 4)
        self.assertFalse(days_ahead["preboost_active"])

        before_peak = peak_at - smart.timedelta(minutes=30)
        preboost = smart.assess(config, None, before_peak, official_signal=signal_for(before_peak))
        self.assertTrue(preboost["preboost_active"])
        self.assertEqual(preboost["score"], 0)
        self.assertEqual(preboost["forecast_tier_6h"], "normal")

        during_peak = smart.assess(config, None, peak_at, official_signal=signal_for(peak_at))
        self.assertEqual(during_peak["score"], 72)
        self.assertEqual(during_peak["tier"], "extreme")

    def test_evening_boost_includes_exact_start_and_excludes_exact_end(self):
        config = smart.normalize_config({"evening_start": "17:10", "evening_minutes": 40})
        expected = [("17:09", False, 0), ("17:10", True, 35),
                    ("17:49", True, 35), ("17:50", False, 0)]
        for clock, active, score in expected:
            with self.subTest(clock=clock):
                now = datetime.fromisoformat(f"2026-04-01T{clock}:00+08:00")
                risk = smart.assess(config, None, now)
                self.assertEqual(risk["evening_boost"], active)
                self.assertEqual(risk["score"], score)
                if active:
                    self.assertEqual(smart.desired_tier(risk), "normal")
                    self.assertEqual(smart.target_mode("low", smart.desired_tier(risk), "normal"), "medium")

    def test_peak_preboost_window_includes_exact_boundary_only(self):
        now = datetime(2026, 4, 1, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
        config = smart.normalize_config({"official_level": 1, "preboost_minutes": 45,
                                         "official_updated_at": int(now.timestamp() * 1000)})
        config["official_peak_at"] = (now + smart.timedelta(minutes=45)).isoformat()
        self.assertTrue(smart.assess(config, None, now)["preboost_active"])
        config["official_peak_at"] = (now + smart.timedelta(minutes=46)).isoformat()
        self.assertFalse(smart.assess(config, None, now)["preboost_active"])

    def test_indoor_residual_tail_holds_normal_target_for_configured_duration(self):
        now = datetime(2026, 4, 1, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
        config = smart.normalize_config({"residual_hours": 18})
        for age_hours in (0.25, 2, 9, 17.99):
            with self.subTest(age_hours=age_hours):
                peak_at = now.timestamp() - age_hours * 3600
                risk = smart.assess(config, None, now, previous_peak_at=peak_at)
                self.assertTrue(risk["residual_active"])
                self.assertEqual(smart.desired_tier(risk), "normal")
        expired = smart.assess(config, None, now, previous_peak_at=now.timestamp() - 18 * 3600)
        self.assertFalse(expired["residual_active"])
        self.assertEqual(smart.desired_tier(expired), "weak")

    def test_desired_tier_combines_forecast_preboost_and_residual_floor(self):
        self.assertEqual(smart.desired_tier({"tier": "weak", "forecast_tier_6h": "high"}), "high")
        self.assertEqual(smart.desired_tier({"tier": "normal", "forecast_tier_6h": "weak"}), "normal")
        self.assertEqual(smart.desired_tier({"tier": "weak", "forecast_tier_6h": "weak",
                                             "residual_active": True}), "normal")

    def test_peak_decay_simulation_holds_cleanup_then_downshifts_without_chatter(self):
        current, candidate, candidate_since = "weak", "weak", 0.0
        peak_at = None
        transitions = []
        start = 1_000_000.0
        for minute in range(0, 26 * 60, 5):
            now_s = start + minute * 60
            direct = "high" if minute <= 60 else "weak"
            if direct == "high": peak_at = now_s
            residual = peak_at is not None and now_s - peak_at < 18 * 3600
            desired = smart.desired_tier({"tier": direct, "forecast_tier_6h": "weak",
                                           "residual_active": residual})
            previous = current
            current, candidate, candidate_since = smart.apply_hysteresis(
                current, candidate, candidate_since, desired, now_s)
            if current != previous: transitions.append((minute, current))
        self.assertEqual([tier for _, tier in transitions], ["high", "normal", "weak"])
        self.assertGreaterEqual(transitions[1][0], 90)
        self.assertGreaterEqual(transitions[2][0], 60 + 18 * 60 + 90)

    def test_enable_requires_location(self):
        with self.assertRaisesRegex(ValueError, "坐标"):
            smart.normalize_config({"enabled": True})

    def test_smart_config_rejects_ambiguous_or_nonfinite_control_values(self):
        invalid_configs = [
            {"enabled": "false"},
            {"area_m2": True},
            {"latitude": True},
            {"device_count": 23.5},
            {"evening_minutes": float("inf")},
            {"airflow_m3h": {"low": float("nan"), "medium": 70, "high": 150}},
            {"official_level": 2.5},
            {"social_level": True},
            {"strength": "unknown"},
            {"season_start_md": "03-01"},
            {"baselines": {"pur-test": "turbo"}},
        ]
        for invalid in invalid_configs:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                smart.normalize_config(invalid)

        # Leap-day season boundaries are valid in the recurring calendar.
        valid = smart.normalize_config({"season_start_md": "02-29", "season_end_md": "05-15"})
        self.assertEqual(valid["season_start_md"], "02-29")

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
        dry = {"current": {"temperature_2m": 15, "precipitation": 0, "interval": 900, "cloud_cover": 10},
               "fetched_at_ms": fetched_at_ms}
        wet = {"current": {"temperature_2m": 15, "precipitation": 2, "interval": 900, "cloud_cover": 90},
               "fetched_at_ms": fetched_at_ms}
        self.assertGreater(smart.assess(config, dry, now)["score"], smart.assess(config, wet, now)["score"])
        self.assertGreaterEqual(smart.assess(config, wet, now)["score"], 0)

    def test_current_rain_suppression_scales_with_intensity(self):
        now = datetime(2026, 4, 1, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
        fetched_at_ms = int(now.timestamp() * 1000)
        config = smart.normalize_config({"official_level": 2, "official_updated_at": fetched_at_ms})

        def score_for_rain(mm):
            weather = {"fetched_at_ms": fetched_at_ms, "current": {
                "temperature_2m": 15, "precipitation": mm, "interval": 900, "cloud_cover": 80}}
            return smart.assess(config, weather, now)

        dry = score_for_rain(0)
        drizzle = score_for_rain(0.01)
        light_rain = score_for_rain(0.1)
        steady_rain = score_for_rain(0.5)
        heavy_rain = score_for_rain(1)

        # Tiny rolling accumulations should not be mistaken for a wash-off.
        self.assertEqual(drizzle["score"], dry["score"])
        self.assertEqual(dry["score"] - light_rain["score"], 2)
        self.assertEqual(dry["score"] - steady_rain["score"], 6)
        self.assertEqual(dry["score"] - heavy_rain["score"], 10)
        self.assertTrue(any("少量降水" in reason for reason in light_rain["reasons"]))
        self.assertTrue(any("明显下调" in reason for reason in heavy_rain["reasons"]))

        # A precipitation amount without its averaging interval is ambiguous.
        missing_interval = {"fetched_at_ms": fetched_at_ms, "current": {
            "temperature_2m": 15, "precipitation": 5, "cloud_cover": 80}}
        self.assertEqual(smart.assess(config, missing_interval, now)["score"], dry["score"])

    def test_wind_speed_alone_is_not_used_as_a_positive_pollen_proxy(self):
        now = datetime(2026, 4, 1, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
        config = smart.normalize_config({"season_start_md": "03-01", "season_end_md": "05-01"})
        fetched = int(now.timestamp() * 1000)
        low_wind = {"fetched_at_ms": fetched, "current": {"temperature_2m": 15,
            "precipitation": 0, "cloud_cover": 80, "wind_speed_10m": 2}}
        high_wind = {"fetched_at_ms": fetched, "current": {"temperature_2m": 15,
            "precipitation": 0, "cloud_cover": 80, "wind_speed_10m": 15}}
        self.assertEqual(smart.assess(config, low_wind, now)["score"],
                         smart.assess(config, high_wind, now)["score"])

    def test_relative_humidity_is_a_small_bidirectional_contextual_signal(self):
        now = datetime(2026, 4, 1, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
        fetched = int(now.timestamp() * 1000)
        config = smart.normalize_config({"official_level": 2, "official_updated_at": fetched})
        dry = {"fetched_at_ms": fetched, "current": {"temperature_2m": 15,
            "relative_humidity_2m": 30, "precipitation": 0, "cloud_cover": 80}}
        humid = {"fetched_at_ms": fetched, "current": {"temperature_2m": 15,
            "relative_humidity_2m": 70, "precipitation": 0, "cloud_cover": 80}}
        dry_risk = smart.assess(config, dry, now)
        humid_risk = smart.assess(config, humid, now)
        self.assertEqual(dry_risk["score"] - humid_risk["score"], 4)
        self.assertTrue(any("相对湿度 30%" in reason for reason in dry_risk["reasons"]))
        self.assertTrue(any("相对湿度 70%" in reason for reason in humid_risk["reasons"]))

    def test_rain_to_sun_is_bounded_and_sunny_streak_does_not_assume_depletion(self):
        now = datetime(2026, 4, 1, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
        config = smart.normalize_config({"season_start_md": "03-01", "season_end_md": "05-01"})
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
        # Four sunny days are not treated as evidence that airborne pollen
        # has been depleted; that should come from measured/official decline.
        self.assertEqual(smart.assess(config, past_streak, now)["score"],
                         smart.assess(config, future_only, now)["score"])

    def test_weather_alone_cannot_raise_risk_outside_a_pollen_context(self):
        now = datetime(2026, 7, 1, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
        fetched = int(now.timestamp() * 1000)
        weather = {
            "fetched_at_ms": fetched, "timezone": "Asia/Shanghai",
            "current": {"temperature_2m": 20, "precipitation": 0,
                        "cloud_cover": 10, "wind_speed_10m": 3},
            "hourly": {
                "time": [f"2026-07-01T{hour:02d}:00" for hour in range(24)],
                "temperature_2m": [20] * 24,
                "precipitation_probability": [0] * 24,
                "cloud_cover": [10] * 24,
            },
            "daily": {"time": ["2026-07-01"], "sunshine_duration_s": [36000],
                      "precipitation_sum_mm": [0]},
        }
        no_pollen_context = smart.assess(smart.normalize_config({}), weather, now)
        self.assertFalse(no_pollen_context["weather_context_active"])
        self.assertEqual(no_pollen_context["score"], 0)
        self.assertEqual(no_pollen_context["forecast_score_6h"], 0)
        self.assertEqual(smart.desired_tier(no_pollen_context), "weak")

        local_season = smart.normalize_config({"season_start_md": "03-01", "season_end_md": "05-01"})
        in_season = smart.assess(local_season, weather, datetime(2026, 4, 1, 12, tzinfo=ZoneInfo("Asia/Shanghai")))
        self.assertTrue(in_season["weather_context_active"])
        self.assertGreater(in_season["score"], 0)

    def test_stale_missing_or_future_weather_timestamp_is_ignored(self):
        now = datetime(2026, 4, 1, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
        config = smart.normalize_config({"official_level": 2, "official_updated_at": int(now.timestamp() * 1000)})
        current_rain = {"current": {"temperature_2m": 15, "precipitation": 10,
                                     "interval": 900, "cloud_cover": 90}}
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

    def test_weather_from_previous_location_or_timezone_is_never_scored(self):
        now = datetime(2026, 4, 1, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
        fetched = int(now.timestamp() * 1000)
        config = smart.normalize_config({"latitude": 39.9, "longitude": 116.4,
            "official_level": 2, "official_updated_at": fetched})
        weather = {"fetched_at_ms": fetched, "latitude": 1.3, "longitude": 103.8,
            "timezone": "Asia/Singapore",
            "current": {"temperature_2m": 15, "precipitation": 0, "cloud_cover": 10}}
        result = smart.assess(config, weather, now)
        baseline = smart.assess(config, None, now)
        self.assertEqual(result["score"], baseline["score"])
        self.assertEqual(result["sources"]["weather"], "unavailable_or_stale")
        self.assertTrue(any("地点/时区" in reason for reason in result["reasons"]))

    def test_missing_weather_fields_are_unknown_not_clear_sky(self):
        now = datetime(2026, 4, 1, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
        fetched = int(now.timestamp() * 1000)
        config = smart.normalize_config({"official_level": 1, "official_updated_at": fetched})
        baseline = smart.assess(config, None, now)
        missing = {"fetched_at_ms": fetched, "current": {"temperature_2m": 15,
            "relative_humidity_2m": None, "precipitation": None, "cloud_cover": None},
            "daily": {"time": ["2026-04-01"], "sunshine_duration_s": [None],
                      "precipitation_sum_mm": [None]},
            "hourly": {"temperature_2m": [15], "precipitation_probability": [None],
                       "cloud_cover": [None]}}
        result = smart.assess(config, missing, now)
        self.assertEqual(result["score"], baseline["score"] + 6)  # temperature alone is known
        self.assertEqual(result["forecast_score_6h"], result["score"])
        self.assertFalse(any("晴朗" in reason or "降水" in reason for reason in result["reasons"]))
        self.assertFalse(any("相对湿度" in reason for reason in result["reasons"]))

    def test_six_hour_weather_outlook_uses_future_local_hours_not_midnight_prefix(self):
        now = datetime(2026, 4, 1, 14, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
        fetched = int(now.timestamp() * 1000)
        config = smart.normalize_config({"official_level": 2, "official_updated_at": fetched})
        times = [f"2026-04-01T{hour:02d}:00" for hour in range(24)]
        # Past hours are rainy; the next six hours are warm, dry and clear.
        # The old [:6] implementation saw only these past rainy hours.
        weather = {
            "fetched_at_ms": fetched,
            "timezone": "Asia/Shanghai",
            "current": {"temperature_2m": None, "precipitation": None,
                        "cloud_cover": None, "wind_speed_10m": None},
            "hourly": {
                "time": times,
                "temperature_2m": [15] * 24,
                "precipitation_probability": [100] * 24,
                "cloud_cover": [90] * 24,
            },
        }
        for hour in range(15, 21):
            weather["hourly"]["precipitation_probability"][hour] = 0
            weather["hourly"]["cloud_cover"][hour] = 10
        risk = smart.assess(config, weather, now)
        self.assertEqual(risk["score"], 30)
        self.assertEqual(risk["forecast_score_6h"], 36)
        self.assertEqual(risk["forecast_hours_used"], 6)
        self.assertEqual(risk["forecast_weather_adjustment"], 6)
        self.assertEqual(risk["forecast_components"], {
            "warm_clear_hours": 6, "low_humidity_hours": 0,
            "high_precipitation_probability_hours": 0})
        self.assertTrue(any("6 个整点样本" in reason for reason in risk["reasons"]))

        # Conversely, future rain must be considered even if the first six
        # entries (past midnight hours) were clear.
        for hour in range(15, 21):
            weather["hourly"]["precipitation_probability"][hour] = 100
            weather["hourly"]["cloud_cover"][hour] = 90
        for hour in range(0, 6):
            weather["hourly"]["precipitation_probability"][hour] = 0
            weather["hourly"]["cloud_cover"][hour] = 10
        rainy_future = smart.assess(config, weather, now)
        # A wetter outlook must not lower today's direct pollen assessment.
        self.assertEqual(rainy_future["forecast_score_6h"], 30)
        self.assertEqual(rainy_future["forecast_components"]["high_precipitation_probability_hours"], 6)

    def test_malformed_weather_shapes_and_nonfinite_values_do_not_crash_or_boost(self):
        now = datetime(2026, 4, 1, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
        config = smart.normalize_config({})
        fetched = int(now.timestamp() * 1000)
        baseline = smart.assess(config, None, now)["score"]
        malformed = [
            ["not an object"],
            {"fetched_at_ms": fetched, "current": "not an object", "hourly": [1]},
            {"fetched_at_ms": fetched, "current": {"temperature_2m": float("nan"),
                "precipitation": float("inf"), "cloud_cover": True, "wind_speed_10m": float("nan")}},
            {"fetched_at_ms": fetched, "current": {"temperature_2m": 10 ** 10000}},
            {"fetched_at_ms": fetched, "current": {}, "daily": "not an object",
             "hourly": {"temperature_2m": [None], "precipitation_probability": [float("nan")],
                        "cloud_cover": [0]}},
        ]
        for index, weather in enumerate(malformed):
            with self.subTest(index=index):
                result = smart.assess(config, weather, now)
                self.assertEqual(result["score"], baseline)
                self.assertEqual(result["forecast_score_6h"], baseline)

    def test_out_of_range_weather_fields_are_unknown_not_favorable_signals(self):
        now = datetime(2026, 4, 1, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
        fetched = int(now.timestamp() * 1000)
        config = smart.normalize_config({"official_level": 1, "official_updated_at": fetched})
        baseline = smart.assess(config, None, now)
        weather = {
            "fetched_at_ms": fetched,
            "current": {"temperature_2m": 999, "relative_humidity_2m": -1,
                        "precipitation": -0.1, "interval": 900, "cloud_cover": 10},
            "daily": {"time": ["2026-04-01"], "sunshine_duration_s": [100_000],
                      "precipitation_sum_mm": [-1]},
            "hourly": {"time": ["2026-04-01T13:00"], "temperature_2m": [15],
                       "relative_humidity_2m": [101], "precipitation_probability": [-1],
                       "cloud_cover": [-1]},
        }
        result = smart.assess(config, weather, now)
        self.assertEqual(result["score"], baseline["score"])
        self.assertEqual(result["forecast_score_6h"], baseline["forecast_score_6h"])

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
        self.assertEqual(result["latitude"], config["latitude"])
        self.assertEqual(result["longitude"], config["longitude"])
        query = server.urllib.parse.parse_qs(server.urllib.parse.urlsplit(request.call_args.args[0].full_url).query)
        self.assertEqual(query["past_days"], ["4"])
        self.assertIn("relative_humidity_2m", query["current"][0])
        self.assertIn("relative_humidity_2m", query["hourly"][0])

    def test_weather_fetch_result_is_discarded_if_location_changes_mid_request(self):
        requested = smart.normalize_config({"latitude": 39.9, "longitude": 116.4})
        current = smart.normalize_config({"latitude": 1.3, "longitude": 103.8})
        runtime = {"weather": None, "weather_error": ""}
        fetched = {"latitude": 39.9, "longitude": 116.4, "timezone": requested["timezone"]}
        with patch.object(server, "SMART_CONFIG", current), patch.object(server, "SMART_RUNTIME", runtime), \
                patch.object(server, "fetch_smart_weather", return_value=fetched):
            server._refresh_smart_weather(requested)
        self.assertIsNone(runtime["weather"])

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

    def test_evening_only_boost_ends_on_schedule_without_bypassing_pollen_dwell(self):
        start = datetime(2026, 4, 1, 17, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
        config = smart.normalize_config({"evening_start": "17:10", "evening_minutes": 40})
        active_risk = smart.assess(config, None, start)
        self.assertTrue(active_risk["evening_boost"])
        self.assertEqual(active_risk["tier_before_evening"], "weak")
        current, candidate, since = smart.apply_hysteresis(
            "weak", "weak", 0, smart.desired_tier(active_risk), 100)
        self.assertEqual(current, "normal")

        end = datetime(2026, 4, 1, 17, 50, tzinfo=ZoneInfo("Asia/Shanghai"))
        ended_risk = smart.assess(config, None, end)
        self.assertFalse(ended_risk["evening_boost"])
        desired = smart.desired_tier(ended_risk)
        ended = smart.end_evening_boost(current, candidate, since, desired,
                                        active_risk["tier_before_evening"], False, 2_500)
        self.assertEqual(ended, ("weak", "weak", 2_500, True))

        # If pollen or indoor residual already justified normal, keep the
        # ordinary dwell rather than treating it as schedule-only.
        protected = smart.end_evening_boost("normal", "weak", 1_000, "weak",
                                            "normal", False, 2_500)
        residual = smart.end_evening_boost("normal", "weak", 1_000, "weak",
                                           "weak", True, 2_500)
        self.assertEqual(protected[:3], ("normal", "weak", 1_000))
        self.assertFalse(protected[3])
        self.assertEqual(residual[:3], ("normal", "weak", 1_000))
        self.assertFalse(residual[3])

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
