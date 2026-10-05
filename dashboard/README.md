# Air Local dashboard

A BLE-only local health dashboard showing steps, calories, distance, active-zone minutes, heart rate, and the daily history collected by this reader. It uses Python's standard library and local HTML, CSS, and JavaScript.

## Start

From the repository root:

```sh
uv run python dashboard/server.py
```

Open <http://127.0.0.1:8765>. The server binds only to `127.0.0.1`. Choose another port with `--port 8766` if necessary.

## What is available

- Date-selectable daily steps, calories, distance, and active-zone minutes.
- Heart-rate charts containing direct BLE measurements recorded on this Mac.
- Daily history using the last captured cumulative total for each date, without summing repeated readings.
- Sleep and resting heart rate explicitly shown as unavailable until a device-derived decoder is implemented.
- A **Refresh tracker** button that reads and decodes `/liveactivity` directly over BLE.
- A **Record one minute** button that collects 12 live responses at five-second intervals over one BLE/DTLS session.
- Raw capture browsing, downloads, byte inspection, and reader logs under the collapsed Diagnostics section.

Every health value comes from this reader's own BLE captures. Fitbit databases, imported snapshots, and app-assisted captures are excluded. Missing values are displayed as unavailable, not invented as zero. Previously stored `fitbit-history.json` files are ignored, and the import API has been removed.

The capture browser lists only `standalone-*.bin` and `dashboard-*.bin`. Credential files, firmware artifacts, and app-assisted captures are not served.

## Reading from the dashboard

Build the standalone reader first if needed:

```sh
uv run python standalone/build.py
```

Provision the reader's existing credential and pair an explicitly selected tracker as described in `standalone/README.md`:

```sh
uv run python standalone/run.py scan --timeout 15
uv run python standalone/run.py pair --device UUID_FROM_FRESH_SCAN
```

The dashboard uses `standalone/private/psk.bin`, `credential.json`, and the verified `binding.json`. Reads are disabled if the binding is missing, unverified, or associated with another key ID. The public peer UUID and key ID are exposed; key bytes are not.

Keep Fitbit Air near the Mac and free from another host's connection. **Refresh tracker** reconnects to the saved bonded UUID using `paired:UUID`, verifies BLE encryption and DTLS, and fetches `/liveactivity`. It never selects another same-name tracker. The dashboard decodes the response and updates the health cards. New readings are saved in `dashboard/data/live-*.json`; original bytes and read metadata remain in `captures/`.

The dashboard does not change the phone's Bluetooth or firewall settings. If discovery times out immediately after disconnecting the phone, allow the tracker to resume advertising, then retry. `uv run python standalone/run.py scan --timeout 15` can verify visibility.

Primary and sensor historical dump reads remain available in Diagnostics.

Reads run in the background. The recorder decodes each completed JSONL frame as it arrives and updates the chart and measurement count. Completed measurements are retained on handled recording failure. Failed reads appear in the console and do not create a successful dashboard capture. Job status is held in memory; captures, samples, and sidecars remain after restart.

## Data formats and remaining limits

`health.py` decodes the actual `LiveActivity.Activity` protobuf fields found in Fitbit 5.09: timestamp, steps, distance in millimeters, calories, elevation, very-active minutes, heart rate, confidence, and daily/weekly zone minutes. Invalid heart-rate confidence is displayed as unavailable. Distance is converted to kilometers for display.

The encrypted historical megadump envelope is still undecoded. Pre-existing historical heart-rate and sleep records therefore remain unavailable. The daily history and heart-rate charts grow from our own BLE recordings. No health measurement is inferred from file size, entropy, or ciphertext.

Times from sidecars and timestamped filenames are preferred. Existing standalone files without timestamps use their file-modification time, explicitly labeled in the inspector. Filenames are used to classify the earlier standalone primary/sensor captures; new reads record the requested type in metadata. The existing app-assisted reader may fall back from a typed request, so request metadata is not proof of a decoded record type.

## Verification

```sh
uv run python -m unittest discover -s dashboard -p 'test_*.py'
```

The tests cover verified protobuf decoding, missing/invalid measurements, BLE-only provenance, daily grouping, credential exclusion, capture downloads, concurrent-read rejection, saved-peer selection, invalid bindings, recorder frames, and interrupted-recording retention. Reader job unit tests simulate subprocesses and do not initiate BLE reads.

Optional browser checks require a running, paired dashboard with at least one local live reading and a sensor capture in Diagnostics:

```sh
uv run --with playwright python -m playwright install chromium
uv run --with playwright python dashboard/check_browser.py
```

These check BLE-only health totals, unavailable sleep, absence of imports, the capture library, hex paging, binary downloads, simulated controls, and mobile layout. Read requests are intercepted and do not start BLE reads. Playwright is only used for verification.

Actual dashboard live reads and full 12-response recorder sessions completed with the phone absent, both before and after laptop bonding. Paired reads verified BLE encryption and DTLS before requesting health data. The 18 dashboard tests, native protocol checks, and BLE-only desktop/mobile browser checks passed, including disabled controls when a verified binding is absent. Personal captures and screenshots are local, ignored artifacts. See `standalone/README.md` for the authenticated bonding sequence and bootstrap limits.
