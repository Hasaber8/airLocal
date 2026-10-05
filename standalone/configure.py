import argparse
import json
import os
import pathlib


ROOT = pathlib.Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description="Provision the standalone reader's existing pairing credential")
    parser.add_argument("--key-file", type=pathlib.Path, required=True)
    parser.add_argument("--key-id", required=True)
    args = parser.parse_args()
    if len(args.key_id) != 8 or any(char not in "0123456789abcdefABCDEF" for char in args.key_id):
        parser.error("Key ID must be eight hexadecimal digits")
    key = args.key_file.read_bytes()
    if len(key) != 16:
        parser.error("Key file must contain exactly 16 bytes")
    private = ROOT / "private"
    private.mkdir(mode=0o700, exist_ok=True)
    if (private / "psk.bin").exists() or (private / "credential.json").exists():
        parser.error("A reader credential already exists. Preserve it before provisioning a replacement.")
    for filename, data in [("psk.bin", key), ("credential.json", json.dumps({
        "keyId": args.key_id.upper(), "keyFile": "psk.bin", "deviceName": "Google Fitbit Air",
        "bootstrap": "previously_exported_pairing_credential",
    }, indent=2).encode())]:
        descriptor = os.open(private / filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as file:
            file.write(data)
    print(f"Standalone credential provisioned for key ID {args.key_id.upper()}.")
    print("Normal BLE reads now use the reader-owned credential files, without the Fitbit app.")


if __name__ == "__main__":
    main()
