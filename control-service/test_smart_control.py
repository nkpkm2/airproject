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
    def test_smart_control_status_endpoint(self):
        httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        worker = Thread(target=httpd.serve_forever, daemon=True)
        worker.start()
        try:
            with urlopen(f"http://127.0.0.1:{httpd.server_address[1]}/api/v1/smart-control", timeout=3) as response:
                body = json.loads(response.read().decode("utf-8"))
            self.assertFalse(body["enabled"])
            self.assertEqual(body["capacity"]["volume_m3"], 400)
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

    def test_enable_requires_location(self):
        with self.assertRaisesRegex(ValueError, "坐标"):
            smart.normalize_config({"enabled": True})

    def test_weather_and_social_cannot_claim_extreme_without_official_pollen(self):
        config = smart.normalize_config({"social_level": 3})
        weather = {"current": {"temperature_2m": 15, "precipitation": 0, "cloud_cover": 10},
                   "daily": {"sunshine_duration_s": [36000] * 4, "precipitation_sum_mm": [0] * 4}}
        risk = smart.assess(config, weather, datetime(2026, 4, 1, 12, tzinfo=ZoneInfo("Asia/Shanghai")))
        self.assertNotEqual(risk["tier"], "extreme")

    def test_rain_is_bounded_suppression_and_official_level_remains_primary(self):
        now = datetime(2026, 4, 1, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
        config = smart.normalize_config({"official_level": 2})
        dry = {"current": {"temperature_2m": 15, "precipitation": 0, "cloud_cover": 10}}
        wet = {"current": {"temperature_2m": 15, "precipitation": 2, "cloud_cover": 90}}
        self.assertGreater(smart.assess(config, dry, now)["score"], smart.assess(config, wet, now)["score"])
        self.assertGreaterEqual(smart.assess(config, wet, now)["score"], 0)

    def test_official_peak_preboosts_one_tier_within_configured_window(self):
        config = smart.normalize_config({"official_level": 1, "official_peak_at": "2026-04-01T12:30",
                                         "preboost_minutes": 45})
        now = datetime(2026, 4, 1, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
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
        self.assertTrue(any("已自动过期" in reason for reason in risk["reasons"]))

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


if __name__ == "__main__":
    unittest.main()
