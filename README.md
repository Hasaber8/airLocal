# Air Local

A macOS BLE reader and local health dashboard for Google Fitbit Air. Health measurements travel directly from the tracker to your Mac and are stored locally.

## Current capabilities

- Direct steps, calories, distance, heart rate, and daily/weekly active-zone minutes from `/liveactivity`.
- A one-minute recorder that collects 12 responses over one BLE/DTLS session.
- Date-selectable activity totals and heart-rate charts built from this reader's own measurements.
- Explicit laptop pairing and saved-peer selection, including BLE encryption and DTLS authentication checks.
- Historical primary/sensor dump downloads and byte-level diagnostics.

Historical dumps still contain an undecoded inner envelope. Pre-existing sleep stages and historical backfill are unavailable. Missing or invalid measurements are displayed as unavailable.

The reader currently needs a previously provisioned 16-byte mobile-data DTLS key. Initial key provisioning without Fitbit/backend support remains unresolved. Once the credential is provisioned, normal laptop reads do not require the Fitbit app, Android, or Frida.

If you do not have the key yet, follow the [initial credential setup guide](research/initial-setup.md) before provisioning the reader.

## Requirements

- macOS with Bluetooth and Apple command-line developer tools.
- Git and [uv](https://docs.astral.sh/uv/).
- Python 3.11 or newer, managed by uv. The dashboard has no third-party Python runtime dependencies.
- A compatible tracker and its existing DTLS credential.

## Quick start

```sh
git clone git@github.com:Hasaber8/airLocal.git
cd airLocal
uv run python standalone/build.py
```

The build fetches pinned Golden Gate/MbedTLS sources, applies our CoreBluetooth patch, builds the native application, and runs protocol checks.

Provision your existing credential once. Replace the example path and `KEY_ID_HEX8` with your binary key file and eight-digit hexadecimal key ID:

```sh
uv run python standalone/configure.py \
  --key-file /path/to/psk.bin --key-id KEY_ID_HEX8
```

Keep the tracker nearby and disconnected from other Bluetooth hosts. Scan and pair the intended tracker using the UUID from that scan:

```sh
uv run python standalone/run.py scan --timeout 15
uv run python standalone/run.py pair --device UUID_FROM_SCAN
```

Confirm Bluetooth permission and **Pair** if macOS prompts. Pairing authenticates DTLS, opens the tracker's timed bonding window, verifies BLE encryption, and writes the binding to `standalone/private/binding.json`.

Start the dashboard:

```sh
uv run python dashboard/server.py
```

Open **http://127.0.0.1:8765**. Use **Refresh tracker** for a live reading or **Record one minute** to build your own local history. The dashboard reconnects to the saved bonded UUID and never falls back to another same-name tracker.

## Repository layout

```text
standalone/        Native macOS reader, launcher, build setup, and protocol tests
dashboard/         Local HTTP server, decoder, browser UI, and tests
research/          App-assisted research tools and protocol findings
captures/          Local raw outputs, ignored except for the directory placeholder
```

| Guide | Contents |
| --- | --- |
| [Standalone reader](standalone/README.md) | Credential setup, pairing, CLI reads, recording, and failure handling |
| [Dashboard](dashboard/README.md) | BLE-only health data, diagnostics, persistence, and browser checks |
| [Research](research/README.md) | Transport evidence, app-assisted tooling, and undecoded historical formats |
| [Initial credential setup](research/initial-setup.md) | Rooted Android/Frida setup, active-key export, provisioning, and verification |

Private credentials/bindings, health records, captured packets, logs, screenshots, APKs, downloaded sources, and build outputs are ignored by Git. Keep credential files local; the dashboard does not serve them.

## Checks

```sh
uv run python -m unittest discover -s dashboard -p 'test_*.py'
uv run python standalone/build.py
node --check dashboard/app.js
node --check dashboard/health.js
```

The native protocol checks and dashboard unit tests do not need a connected tracker. Live pairing/read verification requires hardware. Optional browser checks are documented in the dashboard guide.

Node.js is needed only for JavaScript checks and the optional research tools. GitHub Actions runs dashboard/API tests and JavaScript checks on Linux, plus the native build/protocol checks on macOS.
