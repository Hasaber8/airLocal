import argparse
import json
import os
import pathlib
import subprocess
import tempfile


ROOT = pathlib.Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description="Launch the standalone macOS Fitbit Air reader.")
    commands = parser.add_subparsers(dest="command", required=True)
    scan = commands.add_parser("scan")
    scan.add_argument("--timeout", type=int, default=10)
    for name in ("read", "watch", "pair"):
        command = commands.add_parser(name)
        command.add_argument("--device", required=True)
        command.add_argument("--key-file", type=pathlib.Path, required=name != "pair")
        command.add_argument("--key-id", required=name != "pair")
        command.add_argument("--output", type=pathlib.Path, required=name != "pair")
        command.add_argument("--timeout", type=int, default=90 if name == "watch" else 120 if name == "pair" else 60)
        if name == "read":
            command.add_argument("--type", choices=("primary", "sensor", "live", "discovery", "status", "outbox"), default="primary")
        elif name == "watch":
            command.add_argument("--count", type=int, default=12)
            command.add_argument("--interval", type=int, default=5)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.command == "pair":
        if args.key_file is None or args.key_id is None:
            try:
                credential = json.loads((ROOT / "private/credential.json").read_text())
                if credential.get("keyFile") != "psk.bin":
                    raise ValueError("Expected the reader-owned psk.bin credential")
                args.key_file = args.key_file or ROOT / "private/psk.bin"
                args.key_id = args.key_id or credential["keyId"]
                if not isinstance(args.key_id, str):
                    raise ValueError("Invalid credential key ID")
            except (OSError, ValueError, KeyError, AttributeError) as error:
                parser.error(f"Provision the reader credential with standalone/configure.py first: {error}")
        args.output = args.output or ROOT / "private/binding.json"
    bundle = ROOT / "build/fitbit-air-reader.app"
    if not bundle.is_dir():
        parser.error("Build the reader with standalone/build.py first")
    native_args = [args.command, "--timeout", str(args.timeout)]
    if args.command in {"read", "watch", "pair"}:
        args.output = args.output.resolve()
        if args.output.exists() or args.output.is_symlink():
            parser.error(f"Output already exists: {args.output}")
        native_args += ["--device", args.device, "--key-file", str(args.key_file.resolve()),
                        "--key-id", args.key_id, "--output", str(args.output)]
        if args.command == "read":
            native_args += ["--type", args.type]
        elif args.command == "watch":
            if not 1 <= args.count <= 1000 or not 1 <= args.interval <= 1000:
                parser.error("--count and --interval must be between 1 and 1000")
            native_args += ["--count", str(args.count), "--interval", str(args.interval)]
    # LaunchServices makes the reader itself responsible for Bluetooth permission.
    # Direct launch from AgentsView otherwise inherits its missing usage description.
    with tempfile.TemporaryDirectory(prefix="launch-", dir=ROOT / "build") as directory:
        stdout = pathlib.Path(directory) / "stdout.log"
        stderr = pathlib.Path(directory) / "stderr.log"
        subprocess.run([
            "/usr/bin/open", "-n", "-W", "--stdout", str(stdout), "--stderr", str(stderr),
            "--env", "GG_LOG_CONFIG=" + os.environ.get("GG_LOG_CONFIG", "plist:.level=WARNING"),
            str(bundle), "--args", *native_args,
        ], check=True)
        out = stdout.read_text() if stdout.exists() else ""
        err = stderr.read_text() if stderr.exists() else ""
        print(out, end="")
        print(err, end="")
        if args.command in {"read", "watch", "pair"} and not args.output.is_file():
            raise SystemExit("Pairing failed. No verified binding was saved." if args.command == "pair" else
                             "Reader failed. No complete dump was saved.")
        if args.command == "scan" and ("ERROR:" in err or "Usage:" in err):
            raise SystemExit(1)


if __name__ == "__main__":
    main()
