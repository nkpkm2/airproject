import importlib.util
import unittest
from pathlib import Path


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
        self.assertEqual(server.classify_connection(record, 107_000)[0], "online")
        self.assertEqual(server.classify_connection(record, 115_000)[0], "degraded")
        self.assertEqual(server.classify_connection(record, 131_000)[0], "offline")

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


if __name__ == "__main__":
    unittest.main()
