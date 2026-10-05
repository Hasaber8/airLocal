import argparse
from collections import Counter
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
import pathlib
import re
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, unquote, urlsplit
import uuid

from health import decode_live, load_health, write_json


ROOT = pathlib.Path(__file__).resolve().parent
RESEARCH = ROOT.parent
CAPTURES = RESEARCH / "captures"
DEVICE = "Google Fitbit Air"
SYNC_TYPES = {"PRIMARY_DATA_SYNC": "primary", "SENSOR_COMPRESSED_DATA_SYNC": "sensor", "LIVE_ACTIVITY": "live"}


def read_metadata(path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


class CaptureStore:
    def __init__(self, directory):
        self.directory = directory
        self.cache = {}
        self.lock = threading.Lock()

    def paths(self):
        return sorted(path for path in self.directory.glob("*.bin")
                      if not path.is_symlink() and path.is_file()
                      and path.name.startswith(("standalone-", "dashboard-")))

    def path(self, name):
        if name != pathlib.Path(name).name:
            raise FileNotFoundError("Capture not found")
        path = self.directory / name
        if path not in self.paths():
            raise FileNotFoundError("Capture not found")
        return path

    def describe(self, path):
        metadata = read_metadata(path.with_suffix(".json"))
        stat = path.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
        with self.lock:
            cached = self.cache.get(path.name)
            if cached is not None and cached[0] == signature:
                analysis = cached[1]
            else:
                digest = hashlib.sha256()
                counts = Counter()
                with path.open("rb") as file:
                    header = file.read(21)
                    digest.update(header)
                    observed_envelope = len(header) == 21 and header[:5] == bytes.fromhex("0304000002")
                    if not observed_envelope:
                        counts.update(header)
                    while chunk := file.read(1024 * 1024):
                        digest.update(chunk)
                        counts.update(chunk)
                length = sum(counts.values())
                entropy = -sum((n / length) * math.log2(n / length) for n in counts.values()) if length else 0
                analysis = {
                    "sha256": digest.hexdigest(), "headerHex": header.hex(" "),
                    "observedEnvelope": observed_envelope,
                    "payloadSize": stat.st_size - 21 if observed_envelope else None,
                    "blockAligned": observed_envelope and (stat.st_size - 21) % 16 == 0,
                    "entropy": round(entropy, 4), "histogram": [counts[i] for i in range(256)],
                }
                self.cache[path.name] = (signature, analysis)

        kind = SYNC_TYPES.get(metadata.get("syncType"), "unknown")
        classification = "sidecar" if kind != "unknown" else "unclassified"
        if kind == "unknown" and path.name.startswith("standalone-"):
            if "live" in path.stem:
                kind, classification = "live", "filename"
            elif "primary" in path.stem:
                kind, classification = "primary", "filename"
            elif "sensor" in path.stem:
                kind, classification = "sensor", "filename"
        timestamp = metadata.get("timestamp")
        timestamp_source = "sidecar"
        if not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp):
            match = re.search(r"-(\d{13})(?:-|\.)", path.name)
            timestamp = int(match[1]) if match else int(stat.st_mtime * 1000)
            timestamp_source = "filename" if match else "file modified time"
        if path.name.startswith(("standalone-", "dashboard-")):
            source = "Standalone Mac"
        elif path.name.startswith("local-megadump-"):
            source = "Offline app-assisted"
        else:
            source = "App capture"
        return {
            "id": path.name, "size": stat.st_size, "timestamp": timestamp,
            "timestampSource": timestamp_source, "kind": kind, "classification": classification,
            "source": source, "metadata": metadata, **analysis,
        }

    def list(self):
        groups = {}
        for path in self.paths():
            try:
                capture = self.describe(path)
            except FileNotFoundError:
                continue
            groups.setdefault(capture["sha256"], []).append(capture)
        result = []
        priority = {"Standalone Mac": 3, "Offline app-assisted": 2, "App capture": 1}
        for copies in groups.values():
            chosen = max(copies, key=lambda item: (priority[item["source"]], item["timestamp"]))
            chosen = {**chosen, "copies": [item["id"] for item in copies]}
            result.append({key: value for key, value in chosen.items()
                           if key not in {"histogram", "metadata"}})
        return sorted(result, key=lambda item: item["timestamp"], reverse=True)

    def inspect(self, name, offset=0, limit=256):
        path = self.path(name)
        details = self.describe(path)
        if offset < 0 or offset > details["size"] or not 1 <= limit <= 4096:
            raise ValueError("Invalid byte range")
        with path.open("rb") as file:
            file.seek(offset)
            data = file.read(limit)
        rows = []
        for index in range(0, len(data), 16):
            chunk = data[index:index + 16]
            rows.append({"offset": offset + index, "bytes": list(chunk),
                         "ascii": "".join(chr(byte) if 32 <= byte <= 126 else "." for byte in chunk)})
        return {**details, "offset": offset, "nextOffset": offset + len(data), "rows": rows}


def find_credentials(reader_directory):
    private = reader_directory / "private"
    metadata = read_metadata(private / "credential.json")
    key_id = metadata.get("keyId", "")
    path = private / "psk.bin"
    try:
        if metadata.get("keyFile") != "psk.bin" or not isinstance(key_id, str) or not re.fullmatch(r"[0-9A-Fa-f]{8}", key_id):
            return None
        if path.is_symlink() or path.stat().st_size != 16:
            return None
        return {"path": path, "keyId": key_id.upper()}
    except OSError:
        return None


def find_binding(reader_directory, credentials):
    path = reader_directory / "private/binding.json"
    if not credentials or path.is_symlink():
        return None
    binding = read_metadata(path)
    peer = binding.get("peerId")
    if (not isinstance(peer, str) or not re.fullmatch(r"[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}", peer)
            or binding.get("keyId") != credentials["keyId"]
            or binding.get("dtlsAuthenticated") is not True
            or binding.get("bleEncryptionVerified") is not True):
        return None
    return {"peerId": peer.upper(), "device": f"paired:{peer.upper()}"}


class ReaderController:
    def __init__(self, directory, reader_directory):
        self.directory = directory
        self.reader_directory = reader_directory
        self.health_directory = directory.parent / "dashboard/data"
        self.lock = threading.Lock()
        self.job = None

    def configuration(self):
        credentials = find_credentials(self.reader_directory)
        binding = find_binding(self.reader_directory, credentials)
        built = (self.reader_directory / "build/fitbit-air-reader.app").is_dir()
        return {"available": built and binding is not None, "built": built,
                 "credentialsAvailable": credentials is not None, "deviceName": DEVICE,
                "paired": binding is not None, "peerId": binding["peerId"] if binding else None,
                "keyId": credentials["keyId"] if credentials else None,
                "bleOnly": True,
                "transport": "BLE / Gattlink / IPv4 / UDP / DTLS / CoAP"}

    def snapshot(self):
        with self.lock:
            return dict(self.job) if self.job else None

    def start(self, kind):
        if kind not in {"primary", "sensor", "live", "record"}:
            raise ValueError("Choose live, primary, or sensor")
        with self.lock:
            if self.job and self.job["status"] == "running":
                raise RuntimeError("A tracker read is already running")
            credentials = find_credentials(self.reader_directory)
            if not credentials or not self.configuration()["built"]:
                raise FileNotFoundError("Build the standalone reader and provision its pairing credential first")
            binding = find_binding(self.reader_directory, credentials)
            if not binding:
                raise FileNotFoundError("Pair the tracker with this laptop using standalone/run.py pair --device UUID first")
            credentials = {**credentials, **binding}
            identifier = uuid.uuid4().hex[:12]
            timestamp = time.time_ns() // 1_000_000
            output = self.directory / f"dashboard-{kind}-{timestamp}-{identifier}{'.jsonl' if kind == 'record' else '.bin'}"
            self.job = {"id": identifier, "kind": kind, "status": "running", "startedAt": timestamp,
                        "finishedAt": None, "captureId": None,
                        "log": "Starting the standalone reader. Waiting for Bluetooth discovery and transfer.\n"}
            job = self.job
            threading.Thread(target=self._read, args=(job, credentials, output), daemon=True).start()
            return dict(job)

    def _read(self, job, credentials, output):
        if job["kind"] == "record":
            self._record(job, credentials, output)
            return
        command = [sys.executable, "-u", str(self.reader_directory / "run.py"), "read",
                   "--device", credentials["device"], "--key-file", str(credentials["path"]),
                   "--key-id", credentials["keyId"], "--output", str(output),
                   "--type", job["kind"], "--timeout", "60"]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=90)
            log = (result.stdout + result.stderr)[-65536:]
            if result.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
                raise RuntimeError(log.strip() or "Reader failed. No complete dump was saved.")
            finished = time.time_ns() // 1_000_000
            if job["kind"] == "live":
                sample = decode_live(output.read_bytes(), finished)
                write_json(self.health_directory / f"live-{job['id']}.json", sample)
                log += f"\nDecoded live activity: {sample['steps']} steps, {sample['calories']} kcal, heart rate {sample['heartRate']} bpm.\n"
            metadata = {"type": "live-activity" if job["kind"] == "live" else "standalone-megadump", "source": "standalone", "timestamp": finished,
                        "length": output.stat().st_size, "deviceName": DEVICE, "transport": "DSNG",
                        "syncType": {"primary": "PRIMARY_DATA_SYNC", "sensor": "SENSOR_COMPRESSED_DATA_SYNC", "live": "LIVE_ACTIVITY"}[job["kind"]],
                         "keyId": credentials["keyId"], "networkUpload": False}
            metadata["peerId"] = credentials["peerId"]
            descriptor = os.open(output.with_suffix(".json"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as file:
                json.dump(metadata, file, indent=2)
            with self.lock:
                job.update(status="complete", finishedAt=finished, captureId=output.name, log=log)
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
            with self.lock:
                job.update(status="failed", finishedAt=time.time_ns() // 1_000_000, log=str(error))

    def _record(self, job, credentials, output):
        command = [sys.executable, "-u", str(self.reader_directory / "run.py"), "watch",
                   "--device", credentials["device"], "--key-file", str(credentials["path"]),
                   "--key-id", credentials["keyId"], "--output", str(output),
                   "--count", "12", "--interval", "5", "--timeout", "90"]
        position = 0
        count = 0
        process = None
        drained_after_exit = False
        try:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            deadline = time.monotonic() + 100
            while True:
                path = output if output.is_file() else output.with_name(output.name + ".partial")
                try:
                    with path.open("rb") as file:
                        file.seek(position)
                        for line in file:
                            if not line.endswith(b"\n"):
                                break
                            frame = json.loads(line)
                            sample = decode_live(bytes.fromhex(frame["payloadHex"]), time.time_ns() // 1_000_000)
                            write_json(self.health_directory / f"live-{job['id']}-{count:04d}.json", sample)
                            count += 1
                            position += len(line)
                            with self.lock:
                                job.update(samplesRecorded=count, log=f"Recording over one BLE/DTLS session. {count}/12 measurements saved.\nLatest: {sample['heartRate']} bpm, {sample['steps']} steps.\n")
                except FileNotFoundError:
                    pass
                if process.poll() is not None:
                    if drained_after_exit:
                        break
                    drained_after_exit = True
                    continue
                if time.monotonic() >= deadline:
                    raise RuntimeError("BLE recording exceeded its deadline")
                time.sleep(.25)
            log = process.communicate(timeout=5)[0][-65536:]
            if process.returncode or count != 12:
                raise RuntimeError(log.strip() or f"Recording ended after {count}/12 measurements")
            with self.lock:
                job.update(status="complete", finishedAt=time.time_ns() // 1_000_000, samplesRecorded=count,
                           log=log + f"\nSaved {count} interpreted BLE measurements locally.\n")
        except (OSError, ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired) as error:
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
            with self.lock:
                job.update(status="failed", finishedAt=time.time_ns() // 1_000_000,
                           samplesRecorded=count, log=f"{error}\n{count} completed BLE measurements retained.")

class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, directory=CAPTURES, reader_directory=RESEARCH / "standalone"):
        self.store = CaptureStore(directory)
        self.reader = ReaderController(directory, reader_directory)
        super().__init__(address, DashboardHandler)


class DashboardHandler(BaseHTTPRequestHandler):
    def respond(self, status, data, content_type="application/json", filename=None):
        if content_type == "application/json":
            data = json.dumps(data, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(data)

    def local_request(self):
        port = self.server.server_port
        host = self.headers.get("Host", "")
        if host not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
            self.respond(403, {"error": "Use the local dashboard address"})
            return False
        origin = self.headers.get("Origin")
        if origin and origin != f"http://{host}":
            self.respond(403, {"error": "Origin does not match the dashboard"})
            return False
        return True

    def do_GET(self):
        if not self.local_request():
            return
        request = urlsplit(self.path)
        path = unquote(request.path)
        try:
            if path == "/api/state":
                captures = self.server.store.list()
                health = load_health(self.server.reader.health_directory)
                self.respond(200, {"captures": captures, "reader": self.server.reader.configuration(),
                                   "health": health,
                                   "job": self.server.reader.snapshot(),
                                   "summary": {"count": len(captures), "bytes": sum(item["size"] for item in captures),
                                               "copies": sum(len(item["copies"]) for item in captures),
                                               "standaloneCount": sum(item["source"] == "Standalone Mac" for item in captures)},
                                    "decoder": {"status": "historical_pending", "metricsAvailable": bool(health["samples"])}})
            elif path.startswith("/api/captures/"):
                name = path.removeprefix("/api/captures/")
                if name.endswith("/download"):
                    file = self.server.store.path(name.removesuffix("/download"))
                    self.respond(200, file.read_bytes(), "application/octet-stream", file.name)
                else:
                    query = parse_qs(request.query)
                    offset = int(query.get("offset", ["0"])[0])
                    limit = int(query.get("limit", ["256"])[0])
                    self.respond(200, self.server.store.inspect(name, offset, limit))
            else:
                assets = {"/": ("index.html", "text/html; charset=utf-8"),
                          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                          "/style.css": ("style.css", "text/css; charset=utf-8")}
                assets["/health.css"] = ("health.css", "text/css; charset=utf-8")
                assets["/health.js"] = ("health.js", "text/javascript; charset=utf-8")
                if path not in assets:
                    self.respond(404, {"error": "Not found"})
                    return
                filename, content_type = assets[path]
                self.respond(200, (ROOT / filename).read_bytes(), content_type)
        except FileNotFoundError:
            self.respond(404, {"error": "Capture not found"})
        except ValueError as error:
            self.respond(400, {"error": str(error)})
        except OSError as error:
            self.respond(500, {"error": str(error)})

    def do_POST(self):
        if not self.local_request():
            return
        if self.path != "/api/read":
            self.respond(404, {"error": "Not found"})
            return
        try:
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise ValueError("Expected JSON")
            length = int(self.headers.get("Content-Length", "0"))
            if not 1 <= length <= 1024:
                raise ValueError("Invalid request body size")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("Expected a JSON object")
            self.respond(202, self.server.reader.start(body.get("kind")))
        except FileNotFoundError as error:
            self.respond(503, {"error": str(error)})
        except RuntimeError as error:
            self.respond(409, {"error": str(error)})
        except (ValueError, TypeError) as error:
            self.respond(400, {"error": str(error)})


def main():
    parser = argparse.ArgumentParser(description="Local Fitbit Air capture dashboard")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = DashboardServer(("127.0.0.1", args.port))
    print(f"Fitbit Air dashboard: http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
