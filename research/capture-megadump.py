import argparse
import json
import os
import pathlib
import re
import subprocess
import threading
import time
import uuid

import frida


ROOT = pathlib.Path(__file__).resolve().parent.parent
AGENT = pathlib.Path(__file__).with_name("capture-megadump-agent.js")
CAPTURE_DIR = ROOT / "captures"
ARTIFACT_TYPES = {"megadump", "local-megadump", "dtls-key", "stored-dtls-key", "firmware"}
PACKAGE = "com.fitbit.FitbitMobile"


def write_private(path, data):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as file:
        file.write(data)


def on_message(message, data):
    if message.get("type") != "send":
        print(message, flush=True)
        return

    payload = message.get("payload", {})
    if not isinstance(payload, dict):
        print(message, flush=True)
        return

    kind = payload.get("type")
    if kind in ARTIFACT_TYPES and data is not None:
        if len(data) != payload.get("length"):
            print(f"Rejected {kind}: reported and received lengths differ", flush=True)
            return
        stem = f"{kind}-{payload['timestamp']}-{uuid.uuid4().hex[:8]}"
        path = CAPTURE_DIR / f"{stem}.bin"
        write_private(path, data)
        write_private(path.with_suffix(".json"), json.dumps(payload, indent=2).encode())
        print(f"Saved {len(data)} bytes to {path}", flush=True)
        return

    with (CAPTURE_DIR / "events.jsonl").open("a") as file:
        file.write(json.dumps(payload) + "\n")
    print(payload, flush=True)


def configure_network(device_id, blocked):
    adb = ["adb", "-s", device_id, "shell"]
    identity = subprocess.run(adb + ["id", "-u"], check=True, capture_output=True, text=True)
    if identity.stdout.strip() != "0":
        raise RuntimeError("Network control requires root adbd")
    package = subprocess.run(
        adb + ["cmd", "package", "list", "packages", "-U", PACKAGE],
        check=True, capture_output=True, text=True,
    )
    match = re.search(rf"^package:{re.escape(PACKAGE)} uid:(\d+)$", package.stdout, re.MULTILINE)
    if match is None:
        raise RuntimeError("Could not determine Fitbit's UID")
    rule = ["OUTPUT", "-m", "owner", "--uid-owner", match[1],
            "-m", "comment", "--comment", "fitbit-air-local", "-j", "REJECT"]
    for table in ("iptables", "ip6tables"):
        subprocess.run(adb + [table, "-w", "-S", "OUTPUT"], check=True, capture_output=True)
        exists = subprocess.run(adb + [table, "-w", "-C"] + rule, capture_output=True).returncode == 0
        if blocked and not exists:
            subprocess.run(adb + [table, "-w", "-I"] + rule, check=True)
        elif not blocked and exists:
            subprocess.run(adb + [table, "-w", "-D"] + rule, check=True)
    print(f"Fitbit IPv4/IPv6 network access {'blocked' if blocked else 'restored'}.", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Capture Fitbit Air device dumps and transport credentials.")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--local-read", metavar="ADDRESS", help="Read a BLE dump with Fitbit network access blocked")
    action.add_argument("--restore-network", action="store_true", help="Remove this tool's Fitbit network blocks")
    parser.add_argument("--sync-type", choices=("PRIMARY_DATA_SYNC", "SENSOR_COMPRESSED_DATA_SYNC"),
                        default="PRIMARY_DATA_SYNC")
    parser.add_argument("--timeout", type=int, default=30, help="Local read timeout in seconds")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")

    CAPTURE_DIR.mkdir(exist_ok=True)
    device = frida.get_usb_device(timeout=10)
    if args.restore_network:
        configure_network(device.id, False)
        return
    if args.local_read:
        if re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", args.local_read) is None:
            parser.error("--local-read requires a Bluetooth address")
        configure_network(device.id, True)
        print("The network block remains active after this command. Use --restore-network to remove it.", flush=True)
    source = AGENT.read_text()
    stopped = threading.Event()
    while True:
        application = next(
            (app for app in device.enumerate_applications()
             if app.identifier == PACKAGE),
            None,
        )
        if application is None or application.pid == 0:
            if args.local_read:
                raise RuntimeError("Fitbit is not running. Open the app and connect the tracker first.")
            stopped.wait(2)
            continue

        session = None
        detached = threading.Event()
        try:
            session = device.attach(application.pid)
            session.on("detached", lambda *args: detached.set())
            script = session.create_script(source)
            script.on("message", on_message)
            script.load()
            print(f"Attached to Fitbit PID {application.pid}.", flush=True)
            if args.local_read:
                data = script.exports_sync.readlocal(args.local_read.upper(), args.sync_type, args.timeout)
                if not isinstance(data, bytes) or not data:
                    raise RuntimeError("Local read returned no binary data")
                on_message({"type": "send", "payload": {
                    "type": "local-megadump", "timestamp": time.time_ns() // 1_000_000,
                    "length": len(data), "node": args.local_read.upper(),
                    "syncType": args.sync_type, "networkBlocked": True,
                }}, data)
                return
            detached.wait()
            print("Fitbit detached. Waiting to reattach.", flush=True)
        except (frida.ProcessNotFoundError, frida.InvalidOperationError) as error:
            if args.local_read:
                raise
            print(f"Attach interrupted: {error}", flush=True)
        finally:
            if session is not None and not session.is_detached:
                session.detach()
        stopped.wait(2)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
