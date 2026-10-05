# Standalone Fitbit Air reader for macOS

This reader connects directly to Fitbit Air through CoreBluetooth, establishes a Golden Gate `DSNG` connection using a provisioned DTLS PSK, and retrieves health data into local files. It supports macOS Bluetooth bonding and explicit device selection. Fitbit, Frida, and Android are not part of the reader's runtime.

Keep the tracker nearby and disconnected from other Bluetooth hosts during setup and reads. In earlier verification, disabling the phone's Bluetooth freed the tracker; stopping Fitbit alone did not. Laptop bonding and subsequent encrypted reads were also verified with the phone absent.

## Build

Requirements: macOS, Apple command-line developer tools, Git, and `uv`. Run all commands from the repository root:

```sh
uv run python standalone/build.py
```

The build script:

- Fetches Golden Gate if absent and requires revision `b8a6f2552e12aaf92c1549dc88a41c9b7e73f1aa`.
- Applies the included CoreBluetooth discovery and bonding patch, or recognizes it as already applied.
- Initializes the pinned MbedTLS submodule.
- Uses `uv`-managed CMake 3.31.6 without installing global build tools.
- Builds the native reader and runs the protocol tests.

The executable is:

```text
standalone/build/fitbit-air-reader.app/Contents/MacOS/fitbit-air-reader
```

The application bundle supplies the Bluetooth usage description required by current macOS. The launcher below uses LaunchServices, so permission belongs to the reader rather than the agent harness. Grant Bluetooth access when macOS prompts.

Golden Gate's archived sources emit existing compiler warnings, including an unused variable in the CoreBluetooth transport. The reader builds successfully and its own C source builds without warnings under `-Wall -Wextra`.

## Scan

With the tracker near the Mac and the phone's Bluetooth off:

```sh
uv run python standalone/run.py scan --timeout 15
```

The output lists macOS peripheral UUIDs, advertised names, RSSI, and the current eight-byte `180A` service-data marker. Use a fresh scan's UUID for explicit initial selection. Android Bluetooth MAC addresses are not interchangeable with these UUIDs.

After pairing, your laptop-specific peer UUID is stored in `standalone/private/binding.json`.

Before bonding, both the private BLE address and the advertisement marker can rotate. The shared name is not a device identity. `marker:HEX16` and `name:NAME` remain research selectors; the dashboard uses the saved bonded UUID, requires link encryption, and authenticates DTLS rather than selecting the first same-name advertisement.

## Provision and pair

If you do not have a mobile-data DTLS key, follow the [initial setup guide](../research/initial-setup.md) to export the active credential from your rooted Android setup.

Provision the existing mobile-data credential once:

```sh
uv run python standalone/configure.py \
  --key-file /path/to/psk.bin \
  --key-id KEY_ID_HEX8
```

This stores `private/psk.bin` and `private/credential.json` with mode `0600`. The current bootstrap credential was previously exported from Fitbit; obtaining an initial credential without Fitbit/backend provisioning remains unresolved. Normal reads use the reader-owned files.

Select the intended tracker from the fresh scan, then pair:

```sh
uv run python standalone/run.py pair --device UUID_FROM_FRESH_SCAN
```

The pairing sequence is:

1. Connect to the explicitly selected UUID and authenticate DTLS.
2. PUT `/bond/control` with protobuf `BondControl.Control.operationTimeout = 120` (`08 78`).
3. Read the secure Link Status characteristic `ABBAFD03-E56A-484C-B832-8B17CF6CBFE8`. Confirm **Pair** when macOS prompts.
4. Verify the secure read succeeded and the Link Status encrypted flag is set.
5. Atomically publish `private/binding.json`, containing the macOS peer UUID, key ID, offered DTLS identity, and verification results, with mode `0600`.

The APK's `BondCommandsHandler`, `CoapBondControlResource`, and `BondControlRequestBuilder` establish this ordering. Requesting OS pairing without first enabling the authenticated bonding window caused an SMP timeout. The corrected sequence produced macOS `Pairing succeeded` and `BLEPaired` logs, followed by successful encrypted reconnects.

Existing bindings are preserved rather than overwritten. To intentionally select another tracker, preserve the old binding and supply a new `--output` path before switching the dashboard's `private/binding.json`. Each laptop has its own macOS peer UUID. A forgotten/reset Bluetooth bond must be paired again; reads never fall back to a same-name device.

## Read a primary dump

```sh
uv run python standalone/run.py read \
  --device 'paired:PEER_UUID' \
  --key-file standalone/private/psk.bin \
  --key-id KEY_ID_HEX8 \
  --output captures/standalone-primary-next.bin
```

For sensor-compressed data, use another output path and `--type sensor`:

```sh
uv run python standalone/run.py read \
  --device 'paired:PEER_UUID' \
  --key-file standalone/private/psk.bin \
  --key-id KEY_ID_HEX8 \
  --output captures/standalone-sensor-next.bin \
  --type sensor
```

Replace `PEER_UUID` and `KEY_ID_HEX8` with the values from your binding and credential metadata. The key file must contain exactly 16 raw bytes. The key ID is hexadecimal, not the full DTLS identity. The resolver matches `MD-<keyId>-<expiration>` by key ID, independently of the expiration suffix. Key bytes are read from the file and are not passed on the command line or printed.

The output's parent directory must exist. Existing output files are preserved and rejected before connecting. Data is written with mode `0600` to a `.partial` file, then published only after a complete, successful transfer. Handled failures remove the partial file; an interrupted process can leave one behind. A successful command prints the byte count and output path.

The default timeout is 60 seconds for the entire connection, handshake, and transfer. Override it with `--timeout SECONDS` if necessary.

From a Bluetooth-permitted terminal, the native executable also accepts the same `scan` and `read` options directly and returns a nonzero status on failure. `run.py` uses the atomically published output to determine read success because `open -W` does not propagate the application's exit status.

## Verified results

| Request | Saved file | Size |
| --- | --- | ---: |
| GET `/sync/dump?t=primary` | `captures/standalone-primary.bin` | 11,365 bytes |
| GET `/sync/dump?t=sc` | `captures/standalone-sensor.bin` | 14,533 bytes |
| Primary, name-selected before bonding | `captures/standalone-name-primary.bin` | 19,877 bytes |

All three sessions established DTLS and completed the CoAP transfer. The BLE MTU was 247, allowing 244-byte transport fragments. Pre-bond name selection resolved a fresh UUID after the original became stale. The saved files have mode `0600` and the same inner-envelope pattern as the earlier app-assisted captures. Captures are local artifacts and are not distributed in this repository.

Tests cover:

- Correct PSK resolution, changed expiration suffix, wrong key ID, and insufficient output capacity.
- Byte-preserving assembly of multiple response blocks, including every byte value.
- Rejection of missing/out-of-order blocks, short nonfinal blocks, error responses, and empty dumps.
- Invalid scan arguments and timeout values.
- Preservation of an existing output file.
- Rejected bond-control responses and prevention of binding publication before authenticated bond control.

## Scope

The reader supports historical dumps and the separate live-activity resource. Historical dump bodies still contain the opaque tracker/backend envelope. Its record schema and inner encryption have not been decoded.

`--type live` issues GET `/liveactivity`, which returns the protobuf used by Fitbit for current steps, calories, distance, heart rate, and zone minutes. It can be decoded with `dashboard/health.py` or through the health dashboard:

```sh
uv run python standalone/run.py read \
  --device 'paired:PEER_UUID' \
  --key-file standalone/private/psk.bin \
  --key-id KEY_ID_HEX8 --type live --output captures/standalone-live-current.bin
uv run python dashboard/health.py captures/standalone-live-current.bin --output dashboard/data/live-current.json
```

`watch` polls `/liveactivity` over one BLE/DTLS session and writes complete response frames as JSONL:

```sh
uv run python standalone/run.py watch \
  --device 'paired:PEER_UUID' \
  --key-file standalone/private/psk.bin --key-id KEY_ID_HEX8 \
  --count 12 --interval 5 --output captures/standalone-live-session.jsonl
```

The default recorder deadline is 90 seconds. Historical sleep and heart-rate backfill remain undecoded. `/sync/status` returned a six-byte status protobuf; `/.well-known/core` returned 4.04, and `/app/outbox` returned an empty body.

Normal health operations are GET requests. Pairing additionally sends the authenticated, timed `/bond/control` PUT. No backend upload, `/sync/response`, firmware update, or data commit is implemented. `paired:UUID` reconnects through macOS's known peripheral identity and verifies BLE encryption before starting DTLS and requesting health data. A post-bond GET `/liveactivity` saved `captures/standalone-paired-live-validation.bin` (24 bytes).

## Earlier Android verification

During earlier phone-assisted verification, Bluetooth and the temporary companion-service override were restored after debugging, while the configured IPv4/IPv6 `fitbit-air-local` network blocks were retained. The phone was absent during the latest laptop bonding and paired-read checks, so its current Bluetooth/firewall state was not inspected.

To allow ordinary Fitbit network traffic and uploads again, explicitly run the research tool's existing restoration command:

```sh
uv run --with frida==17.22.0 python research/capture-megadump.py --restore-network
```

This is separate from running the standalone Mac reader.
