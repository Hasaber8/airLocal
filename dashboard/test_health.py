import unittest
import json
import pathlib
import tempfile

from health import decode_live, load_health, protobuf, write_json


LIVE = bytes.fromhex("088e8088d6061000180020272800300038634002480050a301")


class HealthTests(unittest.TestCase):
    def test_real_tracker_response_decodes_to_meaningful_values(self):
        sample = decode_live(LIVE, 1791098896703)
        self.assertEqual(sample["timestamp"], 1791098894000)
        self.assertEqual(sample["steps"], 0)
        self.assertEqual(sample["calories"], 39)
        self.assertEqual(sample["heartRate"], 99)
        self.assertEqual(sample["heartRateConfidenceLabel"], "good")
        self.assertEqual(sample["weeklyZoneMinutes"], 163)
        self.assertEqual(sample["distanceKm"], 0)
        self.assertEqual(sample["source"], "tracker_live")

    def test_invalid_heart_rate_is_not_displayed_as_a_valid_reading(self):
        sample = decode_live(LIVE.replace(bytes.fromhex("4002"), bytes.fromhex("4000")))
        self.assertIsNone(sample["heartRate"])
        self.assertEqual(sample["heartRateConfidenceLabel"], "invalid")

    def test_missing_fields_are_not_fabricated_as_zero(self):
        sample = decode_live(bytes.fromhex("088e8088d60638634002"))
        self.assertIsNone(sample["steps"])
        self.assertIsNone(sample["calories"])
        self.assertEqual(sample["heartRate"], 99)

    def test_truncated_and_encrypted_responses_are_rejected(self):
        for data in [b"", b"\x08\x80", b"\x12\x04\x01", bytes.fromhex("03040000024c77fb")]:
            with self.assertRaises(ValueError):
                decode_live(data)
        with self.assertRaises(ValueError):
            protobuf(b"\x08" + b"\xff" * 10)

    def test_fitbit_imports_are_never_consumed(self):
        with tempfile.TemporaryDirectory(dir=pathlib.Path(__file__).parent) as directory:
            path = pathlib.Path(directory)
            (path / "fitbit-history.json").write_text("invalid content must not even be parsed")
            write_json(path / "live-imported.json", {"source": "fitbit_app_cache", "timestamp": 1791098895000, "steps": 999999})
            write_json(path / "live-reader.json", decode_live(LIVE))
            state = load_health(path)
            self.assertTrue(state["bleOnly"])
            self.assertEqual(len(state["samples"]), 1)
            self.assertEqual(state["live"]["steps"], 0)
            self.assertEqual(state["history"]["sleep"], [])
            self.assertNotIn("fitbit_app_cache", json.dumps(state))

    def test_daily_ble_counters_are_not_added_together(self):
        with tempfile.TemporaryDirectory(dir=pathlib.Path(__file__).parent) as directory:
            path = pathlib.Path(directory)
            first = decode_live(LIVE)
            first["steps"] = 100
            second = {**first, "timestamp": first["timestamp"] + 5000, "steps": 120}
            write_json(path / "live-first.json", first)
            write_json(path / "live-second.json", second)
            state = load_health(path)
            self.assertEqual(state["history"]["days"][0]["steps"], 120)
            self.assertEqual(len(state["samples"]), 2)


if __name__ == "__main__":
    unittest.main()
