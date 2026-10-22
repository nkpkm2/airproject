import importlib.util
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).with_name("dev_server.py")
SPEC = importlib.util.spec_from_file_location("air_dev_server", MODULE_PATH)
server = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(server)


class DevServerStateTests(unittest.TestCase):
    def test_telemetry_rate_and_sequence_gap(self):
        record = {}
        for sequence in range(600):
            server.track_telemetry(record, {"seq": sequence}, sequence * 8)
        self.assertEqual(record["telemetry_stats"]["received_hz"], 125.0)
        self.assertEqual(record["telemetry_stats"]["sequence_gaps"], 0)
        server.track_telemetry(record, {"seq": 602}, 4808)
        self.assertEqual(record["telemetry_stats"]["sequence_gaps"], 2)

    def test_telemetry_payloads_have_incremental_cursors(self):
        record = {}
        server.track_telemetry(record, {"seq": 0, "uptime_ms": 1234, "adc_mv": [1, 2, 3, 4]}, 1000)
        server.track_telemetry(record, {"seq": 1, "adc_mv": [5, 6, 7, 8]}, 1020)
        payloads = list(record["_telemetry_payloads"])
        self.assertEqual([item["cursor"] for item in payloads], [1, 2])
        self.assertEqual(payloads[0]["uptime_ms"], 1234)
        self.assertEqual(payloads[-1]["adc_mv"], [5, 6, 7, 8])

    def test_new_stream_clears_buffer_but_keeps_cursor_monotonic(self):
        record = {}
        server.track_telemetry(record, {"seq": 5, "adc_mv": [1, 1, 1, 1]}, 1000)
        server.track_telemetry(record, {"seq": 0, "adc_mv": [2, 2, 2, 2]}, 1020)
        payloads = list(record["_telemetry_payloads"])
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["cursor"], 2)

    def test_new_stream_resets_old_gaps_and_rate_window(self):
        record = {}
        server.track_telemetry(record, {"seq": 10}, 1000)
        server.track_telemetry(record, {"seq": 20}, 2000)
        self.assertEqual(record["telemetry_stats"]["sequence_gaps"], 9)
        server.track_telemetry(record, {"seq": 0}, 3000)
        self.assertEqual(record["telemetry_stats"]["sequence_gaps"], 0)
        self.assertEqual(record["telemetry_stats"]["samples_in_window"], 1)

    def test_stale_telemetry_rate_decays_without_erasing_gaps(self):
        record = {
            "_telemetry_seen_ms": 1_000,
            "telemetry_stats": {"received_hz": 120.0, "sequence_gaps": 3},
        }
        live, live_age = server.telemetry_view(record, 2_000)
        stale, stale_age = server.telemetry_view(record, 4_000)
        self.assertEqual(live["received_hz"], 120.0)
        self.assertEqual(live_age, 1_000)
        self.assertEqual(stale["received_hz"], 0.0)
        self.assertEqual(stale["sequence_gaps"], 3)
        self.assertEqual(stale_age, 3_000)

    def test_online_degraded_offline_windows(self):
        record = {"_heartbeat_seen_ms": 100_000}
        self.assertEqual(server.classify_connection(record, 103_000)[0], "online")
        self.assertEqual(server.classify_connection(record, 103_001)[0], "degraded")
        self.assertEqual(server.classify_connection(record, 105_000)[0], "degraded")
        self.assertEqual(server.classify_connection(record, 105_001)[0], "offline")

    def test_pending_command_times_out_and_is_retained(self):
        item = {"request_id": "abc", "status": "pending", "sent_at_ms": 1_000}
        record = {"commands": [item]}
        server.expire_commands(record, 17_000)
        self.assertEqual(item["status"], "timeout")
        self.assertIn("15 秒", item["error"])


class DeviceCatalogTests(unittest.TestCase):
    def setUp(self):
        self.original_catalog = server.CATALOG
        server.CATALOG = {"rooms": ["客厅"], "groups": [], "devices": {}}

    def tearDown(self):
        server.CATALOG = self.original_catalog

    def test_new_device_profile_has_stable_defaults(self):
        profile = server.device_profile("pur-test")
        self.assertEqual(profile["alias"], "pur-test")
        self.assertEqual(profile["room"], "未分配")
        self.assertFalse(profile["hidden"])
        self.assertIs(profile, server.device_profile("pur-test"))

    def test_group_ids_are_unique(self):
        server.CATALOG["groups"].append({"id": "group", "name": "Group", "parent_id": None})
        self.assertEqual(server.safe_group_id("Group"), "group-2")

    def test_public_catalog_is_not_a_mutable_device_alias(self):
        server.device_profile("pur-test")
        exported = server.public_catalog()
        exported["devices"]["pur-test"]["alias"] = "changed"
        self.assertEqual(server.CATALOG["devices"]["pur-test"]["alias"], "pur-test")

    def test_hidden_device_requires_real_offline_online_edge(self):
        profile = server.device_profile("pur-test")
        profile["hidden"] = True
        record = {"availability": "online"}
        self.assertFalse(server.is_device_rediscovery(record, profile, "availability", "online"))
        record["availability"] = "offline"
        self.assertTrue(server.is_device_rediscovery(record, profile, "availability", "online"))

    def test_catalog_restore_sanitizes_external_data(self):
        restored = server.restore_catalog({
            "rooms": ["客厅", "客厅", ""],
            "groups": [{"id": "Main Group!", "name": "主组"}],
            "devices": {"pur-test": {"alias": "测试", "group_ids": ["main-group", "missing"]}},
        })
        self.assertEqual(restored["rooms"], ["客厅"])
        self.assertEqual(restored["groups"][0]["id"], "main-group")
        self.assertEqual(restored["devices"]["pur-test"]["group_ids"], ["main-group"])

    def test_calibration_config_is_normalized_per_device(self):
        config = server.normalize_calibration_config("30:ED:A0:5B:00:BC", {
            "schema": "air-light-device-calibration-v1",
            "overall_status": "pass",
            "channels": {
                key: {"logical_mapping": key, "direction": "rising", "threshold_mv": 500 + index,
                      "on_threshold_mv": 600 + index, "off_threshold_mv": 350 + index,
                      "hysteresis_mv": 200, "quality": "pass"}
                for index, key in enumerate(("low", "medium", "high", "auto"))
            },
        })
        self.assertEqual(config["device_id"], "pur-30eda05b00bc")
        self.assertEqual(set(config["channels"]), {"low", "medium", "high", "auto"})
        self.assertEqual(config["channels"]["low"]["on_threshold_mv"], 600)

    def test_mac_identity_is_case_insensitive_for_calibration_binding(self):
        self.assertEqual(
            server.canonical_device_id("30:ed:a0:5b:00:bc"),
            "pur-30eda05b00bc",
        )
        config = server.normalize_calibration_config("30:ed:a0:5b:00:bc", {
            "schema": "air-light-device-calibration-v1",
            "overall_status": "pass",
            "channels": {
                key: {"threshold_mv": 500, "on_threshold_mv": 600, "off_threshold_mv": 400, "quality": "pass"}
                for key in ("low", "medium", "high", "auto")
            },
        })
        self.assertEqual(config["device_id"], "pur-30eda05b00bc")
        self.assertEqual(config["mac"], "30:ED:A0:5B:00:BC")

    def test_analysis_report_format_is_accepted_for_server_normalization(self):
        report = {
            "format": "air-light-calibration-analysis-v2",
            "overall_status": "pass",
            "channels": [
                {"channel": key, "direction": "rising", "threshold_mv_at_baseline_median": 500,
                 "on_threshold_mv_at_baseline_median": 600, "off_threshold_mv_at_baseline_median": 400,
                 "quality": "pass"}
                for key in ("low", "medium", "high", "auto")
            ],
        }
        config = server.normalize_calibration_config("30:ed:a0:5b:00:bc", report)
        self.assertEqual(config["schema"], "air-light-device-calibration-v1")
        self.assertEqual(config["device_id"], "pur-30eda05b00bc")

    def test_negative_off_edge_gets_a_safe_clear_edge(self):
        config = server.normalize_calibration_config("pur-test", {
            "schema": "air-light-device-calibration-v1",
            "overall_status": "pass",
            "channels": {
                key: {"direction": "rising", "threshold_mv": 52, "on_threshold_mv": 214,
                      "off_threshold_mv": -110, "quality": "pass"}
                for key in ("low", "medium", "high", "auto")
            },
        })
        self.assertEqual(config["channels"]["medium"]["clear_threshold_mv"], 52)


class LampModeInferenceTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "calibration": {
                "channels": {
                    key: {"direction": "rising", "threshold_mv": 100,
                          "on_threshold_mv": 500, "off_threshold_mv": 200,
                          "clear_threshold_mv": 200, "quality": "pass"}
                    for key in ("low", "medium", "high", "auto")
                }
            }
        }

    def derive(self, values, history=None):
        record = {"state": {"mode": "unknown", "powered": True}}
        record["_lamp_observations"] = deque(history or [{"observed_at_ms": 1000, "adc_mv": values}])
        return server.derive_lamp_state(record, self.config, 2000)

    def test_single_constant_mode_wins_over_flashing_auto(self):
        history = [
            {"observed_at_ms": 1000, "adc_mv": [800, 20, 20, 800]},
            {"observed_at_ms": 1800, "adc_mv": [800, 20, 20, 20]},
            {"observed_at_ms": 2600, "adc_mv": [800, 20, 20, 800]},
        ]
        result = self.derive(history[-1]["adc_mv"], history)
        self.assertEqual(result["mode"], "low")
        self.assertTrue(result["auto_blinking"])
        self.assertEqual(result["install_status"], "installed")

    def test_multiple_mode_lamps_are_uninstalled(self):
        result = self.derive([800, 800, 20, 20])
        self.assertEqual(result["mode"], "uninstalled")
        self.assertEqual(result["reason"], "multiple_mode_lamps_on")

    def test_constant_mode_and_constant_auto_are_uninstalled(self):
        result = self.derive([800, 20, 20, 800])
        self.assertEqual(result["mode"], "uninstalled")
        self.assertEqual(result["reason"], "mode_and_auto_steady")

    def test_auto_only_is_auto_and_all_dark_is_off(self):
        self.assertEqual(self.derive([20, 20, 20, 800])["mode"], "auto")
        dark = self.derive([20, 20, 20, 20])
        self.assertEqual(dark["mode"], "off")
        self.assertEqual(dark["install_status"], "dark_or_off")


class EngineeringToolTests(unittest.TestCase):
    def setUp(self):
        self.tools = server.engineering_tools.__class__()

    def test_stress_parameters_are_rejected_instead_of_silently_changed(self):
        with self.assertRaisesRegex(ValueError, "1–60"):
            self.tools._validate("mqtt_stress", {"devices": 999, "rate": 20, "duration": 30})

    def test_high_load_stress_requires_explicit_confirmation(self):
        request = {"devices": 30, "rate": 60, "duration": 30}
        with self.assertRaisesRegex(ValueError, "明确确认"):
            self.tools._validate("mqtt_stress", request)
        request["confirmation"] = "我了解这是高负载测试"
        self.assertTrue(self.tools._validate("mqtt_stress", request)["high_load"])

    def test_stress_uses_isolated_topic(self):
        job = {"kind": "mqtt_stress", "params": {"devices": 3, "rate": 5, "duration": 5}}
        command = self.tools._command(job)
        self.assertIn("air/loadtest/v1", command)

    def test_unknown_serial_device_is_never_accepted_for_board_write(self):
        ordinary = {"device": "COM5", "description": "STLink", "hardware_id": "x", "serial_number": "", "vid": 1, "pid": 2, "likely_esp32": False}
        with patch.object(self.tools, "_find_port", return_value=ordinary):
            with self.assertRaisesRegex(ValueError, "未识别为 ESP32"):
                self.tools._validate("board_check", {"port": "COM5"})

    def test_flash_requires_server_side_prerequisites_and_exact_phrase(self):
        esp = {"device": "COM6", "description": "ESP32", "hardware_id": "x", "serial_number": "abc", "vid": 0x303A, "pid": 0x1001, "likely_esp32": True}
        blocked = {"blockers": ["请先编译"], "confirmation_text": "写入 COM6", "build": None}
        with patch.object(self.tools, "_find_port", return_value=esp), patch.object(self.tools, "safety_status", return_value=blocked):
            with self.assertRaisesRegex(ValueError, "请先编译"):
                self.tools._validate("flash_firmware", {"port": "COM6", "confirmation": "写入 COM6"})
        ready = {"blockers": [], "confirmation_text": "写入 COM6", "build": {"source_sha256": "source", "firmware_sha256": "firmware"}}
        with patch.object(self.tools, "_find_port", return_value=esp), patch.object(self.tools, "safety_status", return_value=ready):
            with self.assertRaisesRegex(ValueError, "准确输入"):
                self.tools._validate("flash_firmware", {"port": "COM6", "confirmation": "yes"})
            validated = self.tools._validate("flash_firmware", {"port": "COM6", "confirmation": "写入 COM6"})
            self.assertEqual(validated["firmware_sha256"], "firmware")

    def test_automatic_provision_requires_exact_target_confirmation(self):
        esp = {"device": "COM8", "description": "ESP32", "hardware_id": "x", "serial_number": "abc", "vid": 0x303A, "pid": 0x1001, "likely_esp32": True}
        with patch.object(self.tools, "_find_port", return_value=esp):
            with self.assertRaisesRegex(ValueError, "准确输入"):
                self.tools._validate("provision_board", {"port": "COM8", "confirmation": "yes"})
            values = self.tools._validate("provision_board", {"port": "COM8", "confirmation": "自动验收并写入 COM8"})
            self.assertEqual(values["port"], "COM8")

    def test_automatic_provision_stops_before_write_when_chip_check_fails(self):
        job_id = "provision-failure"
        job = {"job_id": job_id, "kind": "provision_board", "status": "running", "summary": "",
            "params": {"port": "COM8", "port_fingerprint": "fingerprint", "environment": "esp32-c3-super-mini"},
            "result": {}, "log": server.deque(maxlen=50)}
        self.tools._jobs[job_id] = job; self.tools._active_job_id = job_id
        with patch.object(self.tools, "_recheck_before_run"), patch.object(self.tools, "_wait_for_same_port"), \
                patch.object(self.tools, "_execute_process", return_value=(2, ["check failed"])) as execute:
            self.tools._run_provision(job_id, job)
        self.assertEqual(execute.call_count, 3)
        self.assertEqual(job["status"], "failed")
        self.assertFalse(job["result"]["accepted"])
        self.assertNotIn("write_performed", job["result"])

    def test_automatic_provision_reports_acceptance_only_after_runtime_checks(self):
        job_id = "provision-success"
        job = {"job_id": job_id, "kind": "provision_board", "status": "running", "summary": "",
            "params": {"port": "COM8", "port_fingerprint": "fingerprint", "environment": "esp32-c3-super-mini"},
            "result": {}, "log": server.deque(maxlen=50)}
        self.tools._jobs[job_id] = job; self.tools._active_job_id = job_id
        process_results = [(0, ["MAC: 30:ed:a0:5b:00:bc"]), (0, ["build ok"]), (0, ["upload ok"])]
        runtime = {"device_id": "pur-30eda05b00bc", "ip": "192.168.1.10", "rssi_dbm": -55,
            "boot_verified": True, "wifi_verified": True, "mqtt_verified": True, "blue_led_expected": "fast_blink"}
        with patch.object(self.tools, "_recheck_before_run"), patch.object(self.tools, "_wait_for_same_port"), \
                patch.object(self.tools, "_execute_process", side_effect=process_results), \
                patch.object(self.tools, "_verify_firmware_runtime", return_value=runtime):
            self.tools._run_provision(job_id, job)
        self.assertEqual(job["status"], "succeeded")
        self.assertTrue(job["result"]["accepted"])
        self.assertTrue(job["result"]["write_performed"])
        self.assertTrue(all(item["status"] == "passed" for item in job["result"]["steps"]))

    def test_only_flash_policy_can_write_a_board(self):
        policy_table = server.environment_report.__globals__["TOOL_POLICY"]
        writers = [name for name, policy in policy_table.items() if policy["writes_board"]]
        self.assertEqual(set(writers), {"provision_board", "flash_firmware"})

    def test_unknown_job_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unsupported"):
            self.tools.start({"kind": "arbitrary_command"})

    def test_environment_report_has_required_capabilities(self):
        report = server.environment_report()
        self.assertIn("ports", report)
        self.assertIn("platformio", report)
        self.assertIn("firmware_exists", report)
        self.assertIn("tool_policy", report)


if __name__ == "__main__":
    unittest.main()
