from collections import defaultdict
from datetime import date, datetime, timezone
import json
import os
import uuid


LIVE_FIELDS = {1: "timestampSeconds", 2: "steps", 3: "distanceMillimeters", 4: "calories",
               5: "elevation", 6: "veryActiveMinutes", 7: "heartRate", 8: "heartRateConfidence",
               9: "activeZoneMinutes", 10: "weeklyZoneMinutes"}
CONFIDENCE = {0: "invalid", 1: "tentative", 2: "good", 3: "microstill"}


def varint(data, offset):
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            raise ValueError("Truncated protobuf varint")
        byte = data[offset]
        offset += 1
        if shift == 63 and byte > 1:
            raise ValueError("Protobuf varint exceeds 64 bits")
        value |= (byte & 127) << shift
        if byte < 128:
            return value, offset
    raise ValueError("Invalid protobuf varint")


def protobuf(data):
    fields = defaultdict(list)
    offset = 0
    while offset < len(data):
        tag, offset = varint(data, offset)
        number, wire = tag >> 3, tag & 7
        if not 1 <= number < 2 ** 29:
            raise ValueError("Invalid protobuf field number")
        if wire == 0:
            value, offset = varint(data, offset)
        elif wire in {1, 5}:
            length = 8 if wire == 1 else 4
            if offset + length > len(data):
                raise ValueError("Truncated fixed-width protobuf field")
            value = data[offset:offset + length]
            offset += length
        elif wire == 2:
            length, offset = varint(data, offset)
            if offset + length > len(data):
                raise ValueError("Truncated protobuf message")
            value = data[offset:offset + length]
            offset += length
        else:
            raise ValueError("Unsupported protobuf wire type")
        fields[number].append((wire, value))
    return fields


def scalar(fields, number, wire=0, default=None):
    entries = fields.get(number, [])
    if not entries:
        return default
    actual_wire, value = entries[-1]
    if actual_wire != wire:
        raise ValueError(f"Unexpected wire type for field {number}")
    return value


def nested(fields, number):
    return protobuf(scalar(fields, number, 2, b""))


def signed(value):
    return value - 2 ** 64 if value >= 2 ** 63 else value


def local_day(milliseconds, offset_seconds=None):
    if offset_seconds is None:
        return datetime.fromtimestamp(milliseconds / 1000).astimezone().date().isoformat()
    return datetime.fromtimestamp(milliseconds / 1000 + offset_seconds, timezone.utc).date().isoformat()


def decode_live(data, captured_at=None):
    fields = protobuf(data)
    values = {name: scalar(fields, number) for number, name in LIVE_FIELDS.items()}
    if values["timestampSeconds"] is None or not any(values[name] is not None for name in ("steps", "heartRate", "calories")):
        raise ValueError("Response is not a LiveActivity measurement")
    if any(value is not None and value > 0xffffffff for value in values.values()):
        raise ValueError("LiveActivity value exceeds its uint32 field")
    timestamp = values.pop("timestampSeconds") * 1000
    confidence = CONFIDENCE.get(values["heartRateConfidence"], "unknown")
    raw_heart_rate = values["heartRate"]
    if raw_heart_rate == 0 or confidence in {"invalid", "unknown"}:
        values["heartRate"] = None
    return {**values, "timestamp": timestamp, "date": local_day(timestamp),
            "capturedAt": captured_at, "source": "tracker_live", "sourceLabel": "Direct tracker read",
            "heartRateConfidenceLabel": confidence,
            "distanceKm": values["distanceMillimeters"] / 1_000_000 if values["distanceMillimeters"] is not None else None}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as file:
            json.dump(value, file, indent=2, allow_nan=False)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def load_health(directory):
    samples = []
    for path in directory.glob("live-*.json"):
        try:
            sample = json.loads(path.read_text())
            if isinstance(sample, dict) and sample.get("source") == "tracker_live" and isinstance(sample.get("timestamp"), int):
                samples.append(sample)
        except (OSError, ValueError):
            continue
    samples.sort(key=lambda item: item["timestamp"])
    # Persisted readings from our BLE reader are our history. Fitbit imports are never read.
    unique = {sample["timestamp"]: sample for sample in samples}
    samples = list(unique.values())
    days = {}
    for sample in samples:
        days[sample["date"]] = sample
    return {"history": {"source": "ble_capture_history", "days": list(days.values()), "sleep": []},
            "samples": samples, "live": samples[-1] if samples else None,
            "today": date.today().isoformat(), "bleOnly": True,
            "capabilities": {"liveActivity": "verified", "recordedHistory": "verified",
                             "sleepHistory": "decoder_required", "deviceBackfill": "decoder_required"}}


if __name__ == "__main__":
    import argparse
    import pathlib

    parser = argparse.ArgumentParser(description="Decode the verified Fitbit LiveActivity protobuf")
    parser.add_argument("file", type=pathlib.Path)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args()
    sample = decode_live(args.file.read_bytes(), int(args.file.stat().st_mtime * 1000))
    write_json(args.output, sample)
    print(json.dumps(sample, indent=2))
