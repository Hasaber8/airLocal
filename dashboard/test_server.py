import json
import pathlib
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import server


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=pathlib.Path(__file__).parent)
        self.root = pathlib.Path(self.temporary.name)
        self.captures = self.root / "captures"
        self.captures.mkdir()
        self.reader = self.root / "standalone"
        (self.reader / "build/fitbit-air-reader.app").mkdir(parents=True)
        self.payload = bytes.fromhex("03040000024c77fb2224268d4ee043014001000000") + bytes(range(256))
        (self.captures / "standalone-primary-1791092583298.bin").write_bytes(self.payload)
        (self.captures / "dashboard-primary-1791092583299.bin").write_bytes(self.payload)
        (self.captures / "dashboard-primary-1791092583299.json").write_text(json.dumps({
            "syncType": "PRIMARY_DATA_SYNC", "timestamp": 1791092583299,
        }))
        (self.captures / "stored-dtls-key-1.bin").write_bytes(b"never-show-key!!")
        (self.captures / "stored-dtls-key-1.json").write_text(json.dumps({
            "keyId": "1732179918", "connections": [{"identity": "MD-673EF7CE-6AE67AD2"}],
        }))
        (self.reader / "private").mkdir()
        (self.reader / "private/psk.bin").write_bytes(b"never-show-key!!")
        (self.reader / "private/credential.json").write_text(json.dumps({"keyId": "673EF7CE", "keyFile": "psk.bin"}))
        self.peer = "C37BD20C-3B55-2C64-6BE9-6FC7046A093C"
        self.binding = {"peerId": self.peer, "keyId": "673EF7CE", "dtlsAuthenticated": True,
                        "bleEncryptionVerified": True}
        (self.reader / "private/binding.json").write_text(json.dumps(self.binding))
        self.http = server.DashboardServer(("127.0.0.1", 0), self.captures, self.reader)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.http.server_port}"

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()
        self.temporary.cleanup()

    def get(self, path):
        with urlopen(self.base + path, timeout=3) as response:
            return json.load(response)

    def post(self, body, origin=None):
        headers = {"Content-Type": "application/json"}
        if origin:
            headers["Origin"] = origin
        return urlopen(Request(self.base + "/api/read", data=json.dumps(body).encode(),
                               headers=headers), timeout=3)

    def wait_job(self):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            job = self.http.reader.snapshot()
            if job["status"] != "running":
                return job
            time.sleep(.01)
        self.fail("Reader job did not finish")

    def test_grouping_metadata_and_credential_exclusion(self):
        state = self.get("/api/state")
        self.assertEqual(state["summary"]["count"], 1)
        self.assertEqual(state["summary"]["copies"], 2)
        capture = state["captures"][0]
        self.assertEqual(capture["source"], "Standalone Mac")
        self.assertEqual(capture["kind"], "primary")
        self.assertEqual(capture["payloadSize"], 256)
        self.assertTrue(capture["blockAligned"])
        self.assertEqual(capture["entropy"], 8)
        self.assertFalse(state["decoder"]["metricsAvailable"])
        self.assertNotIn("never-show-key", json.dumps(state))
        self.assertNotIn("stored-dtls-key", json.dumps(state))

    def test_binary_inspection_download_and_bounds(self):
        name = "dashboard-primary-1791092583299.bin"
        detail = self.get(f"/api/captures/{name}?offset=21&limit=256")
        binary = bytes(value for row in detail["rows"] for value in row["bytes"])
        self.assertEqual(binary, bytes(range(256)))
        self.assertEqual(detail["nextOffset"], len(self.payload))
        with urlopen(self.base + f"/api/captures/{name}/download", timeout=3) as response:
            self.assertEqual(response.read(), self.payload)
        for path, expected in [
            (f"/api/captures/{name}?offset=-1", 400),
            (f"/api/captures/{name}?limit=4097", 400),
            ("/api/captures/stored-dtls-key-1.bin/download", 404),
            ("/api/captures/../stored-dtls-key-1.bin/download", 404),
            ("/../capture-megadump.py", 404),
        ]:
            with self.assertRaises(HTTPError) as error:
                self.get(path)
            self.assertEqual(error.exception.code, expected)

    def test_read_rejects_invalid_kind_and_remote_origin(self):
        for body, origin, expected in [({"kind": "pair"}, None, 400),
                                       ({"kind": "primary"}, "https://example.com", 403)]:
            with self.assertRaises(HTTPError) as error:
                self.post(body, origin)
            self.assertEqual(error.exception.code, expected)

    def test_fitbit_files_and_import_api_are_excluded(self):
        (self.captures / "megadump-1791092583300.bin").write_bytes(self.payload)
        (self.captures / "local-megadump-1791092583301.bin").write_bytes(self.payload)
        (self.http.reader.health_directory).mkdir(parents=True)
        (self.http.reader.health_directory / "fitbit-history.json").write_text("must not be read")
        state = self.get("/api/state")
        self.assertTrue(state["reader"]["available"])
        self.assertEqual(state["reader"]["keyId"], "673EF7CE")
        self.assertEqual(state["summary"]["copies"], 2)
        request = Request(self.base + "/api/import", data=b"{}", headers={"Content-Type": "application/json"})
        with self.assertRaises(HTTPError) as error:
            urlopen(request, timeout=3)
        self.assertEqual(error.exception.code, 404)

    def test_read_job_saves_metadata_and_prevents_parallel_reads(self):
        started = threading.Event()
        finish = threading.Event()

        def run(command, **kwargs):
            self.assertEqual(command[command.index("--device") + 1], f"paired:{self.peer}")
            self.assertEqual(command[command.index("--type") + 1], "sensor")
            started.set()
            self.assertTrue(finish.wait(2))
            output = pathlib.Path(command[command.index("--output") + 1])
            output.write_bytes(self.payload)
            return subprocess.CompletedProcess(command, 0, stdout="Saved a complete dump\n", stderr="")

        with mock.patch.object(server.subprocess, "run", side_effect=run):
            with self.post({"kind": "sensor"}) as response:
                self.assertEqual(response.status, 202)
                self.assertEqual(json.load(response)["status"], "running")
            self.assertTrue(started.wait(1))
            with self.assertRaises(HTTPError) as error:
                self.post({"kind": "primary"})
            self.assertEqual(error.exception.code, 409)
            finish.set()
            job = self.wait_job()
        self.assertEqual(job["status"], "complete")
        sidecar = self.captures / pathlib.Path(job["captureId"]).with_suffix(".json")
        metadata = json.loads(sidecar.read_text())
        self.assertEqual(metadata["syncType"], "SENSOR_COMPRESSED_DATA_SYNC")
        self.assertFalse(metadata["networkUpload"])
        self.assertEqual(metadata["peerId"], self.peer)
        self.assertEqual(sidecar.stat().st_mode & 0o777, 0o600)
        state = self.get("/api/state")
        self.assertEqual(state["captures"][0]["kind"], "sensor")
        self.assertEqual(state["captures"][0]["source"], "Standalone Mac")

    def test_failed_reader_is_reported_without_a_capture(self):
        failure = subprocess.CompletedProcess([], 1, stdout="", stderr="DTLS handshake timed out")
        with mock.patch.object(server.subprocess, "run", return_value=failure):
            with self.post({"kind": "primary"}):
                pass
            job = self.wait_job()
        self.assertEqual(job["status"], "failed")
        self.assertIn("DTLS handshake timed out", job["log"])
        self.assertIsNone(job["captureId"])
        self.assertEqual(len(self.http.store.list()), 1)

    def test_live_read_is_decoded_and_available_to_health_view(self):
        payload = bytes.fromhex("088e8088d6061000180020272800300038634002480050a301")

        def run(command, **kwargs):
            self.assertEqual(command[command.index("--type") + 1], "live")
            pathlib.Path(command[command.index("--output") + 1]).write_bytes(payload)
            return subprocess.CompletedProcess(command, 0, stdout="LiveActivity received", stderr="")

        with mock.patch.object(server.subprocess, "run", side_effect=run):
            with self.post({"kind": "live"}):
                pass
            job = self.wait_job()
        self.assertEqual(job["status"], "complete")
        state = self.get("/api/state")
        self.assertTrue(state["decoder"]["metricsAvailable"])
        self.assertEqual(state["health"]["live"]["heartRate"], 99)
        self.assertEqual(state["health"]["live"]["weeklyZoneMinutes"], 163)
        self.assertEqual(state["health"]["live"]["source"], "tracker_live")

    def test_invalid_live_data_does_not_create_fake_measurements(self):
        def run(command, **kwargs):
            pathlib.Path(command[command.index("--output") + 1]).write_bytes(self.payload)
            return subprocess.CompletedProcess(command, 0, stdout="Wrong payload", stderr="")

        with mock.patch.object(server.subprocess, "run", side_effect=run):
            with self.post({"kind": "live"}):
                pass
            job = self.wait_job()
        self.assertEqual(job["status"], "failed")
        self.assertIsNone(self.get("/api/state")["health"]["live"])

    def test_recording_decodes_all_frames_after_process_exit(self):
        payload = bytearray.fromhex("088e8088d6061000180020272800300038634002480050a301")

        def launch(command, **kwargs):
            self.assertEqual(command[3], "watch")
            self.assertEqual(command[command.index("--device") + 1], f"paired:{self.peer}")
            output = pathlib.Path(command[command.index("--output") + 1])
            frames = []
            for index in range(12):
                sample = bytearray(payload)
                sample[1] += index
                frames.append(json.dumps({"payloadHex": sample.hex()}))
            output.write_text("\n".join(frames) + "\n")
            process = mock.Mock()
            process.poll.return_value = 0
            process.returncode = 0
            process.communicate.return_value = ("12 live responses recorded", None)
            return process

        with mock.patch.object(server.subprocess, "Popen", side_effect=launch):
            with self.post({"kind": "record"}):
                pass
            job = self.wait_job()
        self.assertEqual(job["status"], "complete")
        self.assertEqual(job["samplesRecorded"], 12)
        state = self.get("/api/state")
        self.assertEqual(len(state["health"]["samples"]), 12)
        self.assertTrue(state["health"]["bleOnly"])

    def test_missing_binding_disables_reads_without_name_fallback(self):
        (self.reader / "private/binding.json").unlink()
        state = self.get("/api/state")
        self.assertTrue(state["reader"]["credentialsAvailable"])
        self.assertFalse(state["reader"]["available"])
        self.assertFalse(state["reader"]["paired"])
        with mock.patch.object(server.subprocess, "run") as run:
            with self.assertRaises(HTTPError) as error:
                self.post({"kind": "live"})
            self.assertEqual(error.exception.code, 503)
            run.assert_not_called()

    def test_unverified_or_mismatched_binding_is_rejected(self):
        for field, value in [("dtlsAuthenticated", False), ("bleEncryptionVerified", False),
                             ("keyId", "FFFFFFFF"), ("peerId", "name:Google Fitbit Air")]:
            with self.subTest(field=field):
                binding = {**self.binding, field: value}
                (self.reader / "private/binding.json").write_text(json.dumps(binding))
                self.assertFalse(self.http.reader.configuration()["available"])
                with self.assertRaises(HTTPError) as error:
                    self.post({"kind": "record"})
                self.assertEqual(error.exception.code, 503)

    def test_interrupted_recording_retains_completed_ble_frames(self):
        payload = "088e8088d6061000180020272800300038634002480050a301"

        def launch(command, **kwargs):
            output = pathlib.Path(command[command.index("--output") + 1])
            output.with_name(output.name + ".partial").write_text(json.dumps({"payloadHex": payload}) + "\n")
            process = mock.Mock()
            process.poll.return_value = 1
            process.returncode = 1
            process.communicate.return_value = ("BLE link lost", None)
            return process

        with mock.patch.object(server.subprocess, "Popen", side_effect=launch):
            with self.post({"kind": "record"}):
                pass
            job = self.wait_job()
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["samplesRecorded"], 1)
        self.assertEqual(self.get("/api/state")["health"]["live"]["heartRate"], 99)


if __name__ == "__main__":
    unittest.main()
