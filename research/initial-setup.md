# Initial setup: obtain the mobile-data DTLS key

The standalone reader needs a **16-byte binary PSK** and its **eight-digit hexadecimal key ID**. This guide exports the credential already provisioned by Fitbit on a rooted Android phone, then installs a reader-owned copy on the Mac.

This is the current bootstrap method, not independent credential generation. The initial tracker enrollment/key provisioning uses the Fitbit app and its normal backend flow. After export and laptop pairing, normal BLE reads use local credential files and do not need Fitbit, Android, or Frida.

Run Mac commands from the repository root. Replace uppercase placeholders such as `DECIMAL_KEY_ID` and `UUID_FROM_SCAN` with your own values.

## 1. Prepare the Android connection

The verified setup uses:

- A rooted Android phone with **root `adbd`** and USB debugging enabled.
- Fitbit **5.09**, already paired with the intended tracker.
- ARM64 Android (`arm64-v8a`).
- Frida client/server **17.22.0**.
- Mac tools: ADB, Node/npm, curl, xz, uv, and the native reader build prerequisites.

The capture hooks reference obfuscated classes from Fitbit 5.09. Other app versions may require adapting the hooks. This script requires root `adbd`; it does not wrap ADB commands in `su`.

Connect the phone over USB, accept its debugging prompt, and check it:

```sh
adb devices
adb root
adb shell id -u
adb shell getprop ro.product.cpu.abi
```

`id -u` must return `0`; the ABI should be `arm64-v8a` for the server download below. If multiple ADB/Frida devices are connected, disconnect the unrelated devices for this workflow.

Open Fitbit, finish tracker enrollment if needed, and let it connect/sync successfully. Initial provisioning must be complete before the active transport credential can be exported.

## 2. Start the matching Frida server

If a matching server is already running, skip the download/start commands. Otherwise:

```sh
mkdir -p research/local
curl -fL \
  https://github.com/frida/frida/releases/download/17.22.0/frida-server-17.22.0-android-arm64.xz \
  -o research/local/frida-server.xz
xz -dk research/local/frida-server.xz
adb push research/local/frida-server /data/local/tmp/frida-server
adb shell chmod 755 /data/local/tmp/frida-server
adb shell /data/local/tmp/frida-server --version
adb shell '/data/local/tmp/frida-server >/data/local/tmp/frida-server.log 2>&1 </dev/null &'
```

The server version must be `17.22.0`. Confirm that the matching Python client can see USB applications:

```sh
uv run --with frida==17.22.0 python -c \
  'import frida; d = frida.get_usb_device(timeout=10); print([(a.identifier, a.pid) for a in d.enumerate_applications() if a.identifier == "com.fitbit.FitbitMobile"])'
```

Fitbit should appear with a nonzero PID while the app is open. Downloads in `research/local/` are ignored by Git.

## 3. Build the capture agent

Install the pinned Java bridge dependency and compile the source:

```sh
npm ci --prefix research
uv tool run --from frida-tools frida-compile research/capture-megadump.js \
  -o research/capture-megadump-agent.js
```

The generated agent and Node modules are ignored by Git. Recompile after editing the source.

## 4. Export the credential from an active connection

Keep Fitbit connected to the intended tracker, then run:

```sh
uv run --with frida==17.22.0 python research/capture-megadump.py
```

The collector attaches to Fitbit, inspects existing Golden Gate stacks, and reads the app's BKS key store through its own Android provider. It writes files such as:

```text
captures/stored-dtls-key-EXPORT_ID.bin
captures/stored-dtls-key-EXPORT_ID.json
```

The `.bin` contains the secret; the `.json` contains its ID, length, and association with active connections. The console prints output paths and public connection metadata, not key bytes. Stop the collector with **Ctrl+C** after the key-store inspection completes.

Capture-only mode does not block Fitbit network traffic. The separate `--local-read` mode and its tagged network blocks are explained in the [research guide](README.md). Those blocks are not required to run the standalone reader after setup.

## 5. Select the active 16-byte key

Do not select a key just because its filename is newest. Inspect the JSON sidecar for a candidate:

```sh
uv run python -m json.tool captures/stored-dtls-key-EXPORT_ID.json
```

Choose the candidate with:

- `length` equal to **16**.
- A nonempty `connections` array associated with the intended tracker.
- An identity in the form `MD-KEYID-EXPIRATION`.
- A decimal `keyId` that matches the identity's hexadecimal `KEYID`.

For example, the following is **illustrative metadata**, not a working credential:

```json
{
  "length": 16,
  "keyId": "305441741",
  "connections": [
    {"node": "TRACKER_ADDRESS", "identity": "MD-1234ABCD-89ABCDEF"}
  ]
}
```

Convert your sidecar's decimal ID to hexadecimal if needed:

```sh
uv run python -c 'import sys; print(f"{int(sys.argv[1]):08X}")' DECIMAL_KEY_ID
```

The result should match the first eight hex digits after `MD-`. The second component is the expiration suffix, not part of `--key-id`. Its exact semantics remain unconfirmed; use the credential associated with the active connection.

If every key has an empty `connections` array, the collector did not associate an active DTLS stack. Let Fitbit complete a connection, then stop and rerun the collector. Class lookup errors usually indicate an incompatible Fitbit version; Frida communication errors can indicate a stopped server or client/server version mismatch.

## 6. Provision the Mac reader

Build the reader if needed, then copy the selected credential into reader-owned private storage:

```sh
uv run python standalone/build.py
uv run python standalone/configure.py \
  --key-file captures/stored-dtls-key-EXPORT_ID.bin \
  --key-id KEY_ID_HEX8
```

The provisioning command validates that the binary is exactly 16 bytes and the ID is eight hex digits. It creates:

```text
standalone/private/psk.bin
standalone/private/credential.json
```

The private directory uses mode `0700`; credential files use `0600`. Existing credentials are not overwritten. The original exported keys and the reader-owned copies are ignored by Git.

## 7. Release the tracker and pair the laptop

Stop the collector and free the tracker from the phone's connection. In the verified setup, turning the phone's Bluetooth off was necessary; stopping Fitbit alone did not always release the tracker.

Keep the tracker near the Mac, then:

```sh
uv run python standalone/run.py scan --timeout 15
uv run python standalone/run.py pair --device UUID_FROM_SCAN
```

Select the intended tracker explicitly from the fresh scan. The shared name and advertisement marker are not permanent identities. Grant Bluetooth access and confirm **Pair** when macOS prompts.

The reader authenticates DTLS with the provisioned key, opens the 120-second `/bond/control` window, and requests OS pairing. It publishes `standalone/private/binding.json` only after secure-characteristic access and BLE encryption are verified. A failed handshake or pairing attempt does not publish a verified binding.

## 8. Verify a health read

```sh
uv run python dashboard/server.py
```

Open **http://127.0.0.1:8765** and choose **Refresh tracker**. The log should show a successful secure BLE read, verified link encryption, an established DTLS session, and a completed `/liveactivity` response. **Record one minute** should save 12 responses over one session.

Invalid heart-rate confidence is displayed as unavailable rather than a fabricated value. Once this read succeeds, the credential bootstrap and laptop pairing are complete. Keep the tracker free from competing Bluetooth connections when using the Mac.
