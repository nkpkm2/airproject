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


if __name__ == "__main__":
    unittest.main()
