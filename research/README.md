# Fitbit Air protocol research

These app-assisted tools support protocol investigation and the current one-time credential bootstrap. They are separate from the [standalone BLE reader](../standalone/README.md) and [dashboard](../dashboard/README.md). The BLE-only dashboard never imports Fitbit databases or app-assisted captures.

For the complete first-time workflow, see [Initial credential setup](initial-setup.md).

## Verified checkpoint

The native Mac reader retrieves historical megadumps and `/liveactivity` without Fitbit, Frida, or Android at runtime. The LiveActivity protobuf is decoded; the historical dump's inner envelope is still opaque.

Laptop bonding and encrypted reconnects are verified. The dashboard uses the saved bonded UUID, requires BLE encryption and DTLS, and does not fall back to a same-name tracker. Full 12-response recordings completed with the phone absent.

Research used Fitbit 5.09 (`com.fitbit.FitbitMobile`) on a rooted Android device with root `adbd`, and Frida/server 17.22.0. Personal device identifiers, keys, health records, APKs, packet captures, and generated outputs are not included in the repository.

## App-assisted tooling

The optional research dependency `frida-java-bridge` is pinned to the already-used version 7.0.13. Install it and generate the Frida agent from the repository root:

```sh
npm ci --prefix research
uv tool run --from frida-tools frida-compile research/capture-megadump.js \
  -o research/capture-megadump-agent.js
```

The agent is generated output and is ignored by Git. Edit `capture-megadump.js`, not the bundle. Running these tools requires USB debugging, root `adbd`, a matching Frida server, and a compatible Fitbit APK. Hooks include version-specific obfuscated class names.

With Frida Server running and Fitbit already connected to the tracker, replace `TRACKER_ADDRESS` with its current Bluetooth address:

```sh
uv run --with frida==17.22.0 python research/capture-megadump.py \
  --local-read TRACKER_ADDRESS
uv run --with frida==17.22.0 python research/capture-megadump.py \
  --local-read TRACKER_ADDRESS --sync-type SENSOR_COMPRESSED_DATA_SYNC
```

The tool verifies root access, determines the package UID, and adds tagged IPv4/IPv6 OUTPUT rules before requesting a local dump. Both rules must succeed. It invokes `CoapMegadumpDeviceResource.read()` directly rather than the site's sync service, and saves results in the root `captures/` directory with private file permissions.

**The `fitbit-air-local` network blocks remain active after the command exits, including on failure.** They are not reboot-persistent. To explicitly allow ordinary Fitbit traffic and uploads again:

```sh
uv run --with frida==17.22.0 python research/capture-megadump.py --restore-network
```

Without either action flag, the script is a passive collector. It reattaches after app restart, captures CoAP dump bodies, exports existing DTLS keys, and copies firmware files when `FirmwareImage` objects are constructed. Passive collection alone does not block uploads.

## Transport and credential evidence

- Stack descriptor: `DSNG`, meaning DTLS, UDP socket, network interface, Gattlink.
- Mobile role: hub (`isNode=false`), DTLS server.
- Native library: `libxp.so` from the decoded ARM64 APK.
- Offered identity format: `MD-<8 hex keyId>-<8 hex keyExpiration>`.
- The mobile-data resolver looks up the 16-byte PSK by key ID. Expiration semantics and units remain unconfirmed.
- The key is a transport credential. It is not proven to decrypt the inner historical envelope.

The app stores keys in `files/MobileDataKeyManagerKeyStore.keystore`, a BKS store. `oxz.c().d()` accesses the manager; `qaa.k()` loads the store and `qaa.j()` provides the entry password. The password concatenates app-scoped `android_id`, `mobileDataInstanceId`, and decimal app UID. Aliases are decimal key IDs; `qqh.b(long)` performs the raw key lookup. The collector associates stored keys with established DTLS identities without printing key bytes.

The standalone build uses the archived [Google-Health-API/golden-gate](https://github.com/Google-Health-API/golden-gate) revision `b8a6f2552e12aaf92c1549dc88a41c9b7e73f1aa` and its pinned MbedTLS 2 submodule. Relevant upstream code includes `xp/gattlink/`, `xp/protocols/gg_ipv4_protocol.c`, `xp/stack_builder/`, and DTLS tests under `xp/unit_tests/dtls/`.

GATT uses service `FD62` / `ABBAFF00-E56A-484C-B832-8B17CF6CBFE8` and RX/TX characteristics `ABBAFF01-...` / `ABBAFF02-...`. Discover the actual GATT table rather than relying on historical numeric handles.

## Laptop bonding

Pre-bond macOS UUIDs and eight-byte `180A` advertisement markers rotate. The shared advertised name is not a durable device identity. Setup uses an explicit fresh scan UUID; subsequent reads use the saved macOS bonded identity.

The APK's `BondCommandsHandler` enables `CoapBondControlResource` before requesting an OS bond. The resource sends PUT `/bond/control` with `BondControl.Control.operationTimeout` (field 1, integer 0-255). The native reader follows this sequence:

1. Authenticate DTLS using the provisioned mobile-data credential.
2. PUT `/bond/control` with `08 78`, opening a 120-second bonding window.
3. Read secure characteristic `ABBAFD03-E56A-484C-B832-8B17CF6CBFE8` and let macOS request user confirmation.
4. Verify successful secure access and current-link encryption using Link Status `ABBAFD02-...`, flag bit 1.
5. Publish `standalone/private/binding.json` only after both checks succeed.

Flag bit 0 only says the tracker has bonded to some host before. Requesting OS pairing before opening the authenticated bonding window produced an SMP timeout and `CBATTErrorDomain` code 5. The corrected sequence produced macOS `smpPairingCompleted ... status=0`, `Pairing succeeded`, and `BLEPaired`, followed by successful encrypted reconnects and health reads.

## LiveActivity decoding

The APK's `LiveActivity.Activity` schema establishes:

| Field | Meaning |
| --- | --- |
| 1 | Timestamp, seconds |
| 2 | Steps |
| 3 | Distance, millimeters |
| 4 | Calories |
| 5 | Elevation |
| 6 | Very-active minutes |
| 7 | Heart rate |
| 8 | Heart-rate confidence |
| 9 | Daily active-zone minutes |
| 10 | Weekly active-zone minutes |

Confidence values are 0 invalid, 1 tentative, 2 good, and 3 microstill. Missing values remain unavailable; invalid/zero heart rate is not a valid measurement. Distance is divided by 1,000,000 for kilometers.

GET `/sync/status` returned a six-byte protobuf matching `SyncStatus.Status`. GET `/.well-known/core` returned 4.04. GET `/app/outbox` returned an empty body; this did not yield historical health records.

## Historical envelope and backend flow

The hook `CoapMegadumpDeviceResource.dataStreamFrom$lambda$0(byte[])` executes after `IncomingBody.asData()`. Its output is the complete CoAP body above DTLS/Gattlink, not raw Gattlink fragments.

The legacy upload path in `sle.e(...)` Base64-encodes the complete tracker body and sends it with `Device-Data-Encoding: base64`. No local inner decryption was found in that path. `cache/sync-*.bin` files are decoded backend responses subsequently sent through PUT `/sync/response`; they are not tracker upload caches.

Observed bodies start with `03 04 00 00 02` and have an empirical 21-byte prefix. Every observed remainder is divisible by 16. This is compatible with a block-cipher envelope but does not establish the cipher, mode, IV, authenticated fields, or actual ciphertext boundaries. Bytes 5 and 6 change together; treating one byte as the complete counter is not justified.

App-assisted local reads were compared byte-for-byte with corresponding hook captures and matched. Repeated primary dumps remained available without a backend response. This does not establish individual record retention/deletion semantics. A typed request may fall back without query parameters on Bad Request or Unauthorized, so request metadata alone does not prove the inner record type.

After a backend response is delivered, the app performs tracker challenge verifications before POST `devices/client/tracker/data/ack`. The inner commitment/deletion fields remain unknown.

## Firmware findings

Static analysis found POST `devices/client/tracker/data/<productId>/firmware`, optionally `?optimize=BLE`, with an encoded tracker dump. Responses include `targetVersions` and `images`, with `deviceMode`, versioned data flags, and Base64 image data.

`peb` writes `cache/fwup-*.bin`; `pvl.f()` deletes these temporary files. The `FirmwareImage(oia, FirmwareVersion, oib, URI)` constructor receives the completed file URI, which the research hook can copy before cleanup.

No firmware image has been captured or analyzed. Applicability to the connected Air and its product ID remains unverified. Do not assume legacy Airlink firmware transport matches Golden Gate.

## Remaining research

1. Provision an initial mobile-data credential without Fitbit/backend support.
2. Identify the historical envelope key/format and device-derived sleep/health schemas.
3. Determine local record commitment and retention semantics before implementing complete historical sync.
