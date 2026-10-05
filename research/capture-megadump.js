import Java from "frida-java-bridge";

// App-assisted research tooling, separate from the standalone BLE runtime.

rpc.exports = {
    readlocal(address, syncType, timeout) {
        return new Promise((resolve, reject) => {
            Java.perform(function () {
                let selected = null;
                Java.choose("com.fitbit.goldengate.megadump.CoapMegadumpDeviceResource", {
                    onMatch(resource) {
                        selected = Java.retain(resource);
                        return "stop";
                    },
                    onComplete() {
                        try {
                            if (selected === null) {
                                throw new Error("No existing CoAP megadump resource. Connect the tracker first.");
                            }
                            const seconds = Java.use("java.util.concurrent.TimeUnit").SECONDS.value;
                            const type = Java.use("ped").valueOf(syncType);
                            const bytes = selected.read(address, null, type, 0)
                                .timeout(timeout, seconds).blockingGet();
                            const Array = Java.use("java.lang.reflect.Array");
                            const payload = new Uint8Array(Array.getLength(bytes));
                            for (let i = 0; i < payload.length; i++) {
                                payload[i] = Array.getByte(bytes, i) & 0xff;
                            }
                            resolve(payload.buffer);
                        } catch (error) {
                            reject(error);
                        } finally {
                            if (selected !== null) {
                                selected.$dispose();
                            }
                        }
                    },
                });
            });
        });
    },
};

Java.perform(function () {
    function observe(callback) {
        try {
            callback();
        } catch (error) {
            console.log("Capture error: " + error.stack);
        }
    }

    function emit(type, bytes, metadata = {}) {
        const payload = new Uint8Array(bytes.length);
        for (let i = 0; i < bytes.length; i++) {
            payload[i] = bytes[i] & 0xff;
        }
        send(
            {type, length: bytes.length, timestamp: Date.now(), ...metadata},
            payload.buffer
        );
    }

    function hex(bytes) {
        let result = "";
        for (let i = 0; i < bytes.length; i++) {
            result += (bytes[i] & 0xff).toString(16).padStart(2, "0");
        }
        return result;
    }

    function nodeName(node) {
        return Java.cast(node, Java.use("java.lang.Object")).toString();
    }

    const resource = Java.use(
        "com.fitbit.goldengate.megadump.CoapMegadumpDeviceResource"
    );
    const capture = resource["dataStreamFrom$lambda$0"].overload("[B");
    const read = resource.read.overload(
        "java.lang.String",
        "com.fitbit.goldengate.protobuf.SyncRequestOuterClass$SyncRequest",
        "ped",
        "int"
    );

    read.implementation = function (name, request, syncType, timeout) {
        observe(function () {
            send({
                type: "megadump-request",
                timestamp: Date.now(),
                node: name.toString(),
                syncType: syncType === null ? null : syncType.toString(),
                timeout,
                request: request === null ? null : request.toString(),
            });
        });
        return read.call(this, name, request, syncType, timeout);
    };

    capture.implementation = function (bytes) {
        observe(function () { emit("megadump", bytes); });
        return capture.call(this, bytes);
    };

    const keyProvider = Java.use("qqh");
    const lookupKey = keyProvider.b.overload("long");
    const seenKeys = new Set();
    lookupKey.implementation = function (keyId) {
        const key = lookupKey.call(this, keyId);
        observe(function () {
            if (key === null) {
                return;
            }
            const identifier = keyId.toString() + ":" + hex(key);
            if (!seenKeys.has(identifier)) {
                emit("dtls-key", key, {
                    keyId: keyId.toString(),
                });
                seenKeys.add(identifier);
            }
        });
        return key;
    };

    const stackClass = Java.use("com.fitbit.goldengate.bindings.stack.Stack");
    const create = stackClass.create.overloads[0];
    create.implementation = function (...args) {
        observe(function () {
            send({
                type: "stack-created",
                timestamp: Date.now(),
                node: nodeName(args[0]),
                descriptor: args[1].toString(),
                isNode: args[2],
                localAddress: args[5] === null ? null : args[5].getHostAddress().toString(),
                localPort: args[6],
                remoteAddress: args[7] === null ? null : args[7].getHostAddress().toString(),
                remotePort: args[8],
                rxWindow: args[10],
                txWindow: args[11],
                bufferThresholdHysteresis: args[12],
                expectedAckTimeout: args[13],
            });
        });
        return create.call(this, ...args);
    };

    const imageClass = Java.use("com.fitbit.data.domain.device.FirmwareImage");
    const imageInit = imageClass.$init.overload(
        "oia", "com.fitbit.common.util.FirmwareVersion", "oib", "java.net.URI"
    );
    const File = Java.use("java.io.File");
    const Files = Java.use("java.nio.file.Files");
    imageInit.implementation = function (mode, version, dataType, uri) {
        imageInit.call(this, mode, version, dataType, uri);
        observe(function () {
            if (uri !== null) {
                const file = File.$new(uri);
                emit("firmware", Files.readAllBytes(file.toPath()), {
                    uri: uri.toString(),
                    mode: mode === null ? null : mode.toString(),
                    version: version === null ? null : version.toString(),
                    dataType: dataType === null ? null : dataType.toString(),
                });
            }
        });
    };

    // Inspect existing connections so an already-completed handshake is not missed.
    const activeIdentities = new Map();
    Java.choose("com.fitbit.goldengate.bindings.stack.Stack", {
        onMatch(stack) {
            observe(function () {
                if (stack.stackClosed.value) {
                    return;
                }
                const config = stack.stackConfig.value;
                send({
                    type: "stack-snapshot",
                    timestamp: Date.now(),
                    node: nodeName(stack.nodeKey.value),
                    descriptor: config.getConfigDescriptor().toString(),
                    isNode: stack.isNode.value,
                    localAddress: config.getLocalAddress().getHostAddress().toString(),
                    localPort: config.getLocalPort(),
                    remoteAddress: config.getRemoteAddress().getHostAddress().toString(),
                    remotePort: config.getRemotePort(),
                    dtlsState: stack.lastDtlsState.value.toString(),
                    identityHex: stack.lastDtlsPskIdentity.value === null
                        ? null : hex(stack.lastDtlsPskIdentity.value),
                });
                if (stack.lastDtlsPskIdentity.value !== null) {
                    const storedIdentity = stack.lastDtlsPskIdentity.value;
                    let identity = "";
                    for (let i = 0; i < storedIdentity.length; i++) {
                        identity += String.fromCharCode(storedIdentity[i] & 0xff);
                    }
                    if (/^MD-[0-9a-fA-F]{8}-[0-9a-fA-F]{8}$/.test(identity)) {
                        const keyId = parseInt(identity.slice(3, 11), 16).toString();
                        const connections = activeIdentities.get(keyId) || [];
                        connections.push({
                            node: nodeName(stack.nodeKey.value), identity,
                            identityHex: hex(storedIdentity),
                        });
                        activeIdentities.set(keyId, connections);
                    }
                }
            });
        },
        onComplete() { console.log("Existing stack inspection complete"); },
    });

    // Read the app's existing BKS store through its own Android provider.
    observe(function () {
        const manager = Java.use("oxz").c().d();
        const store = manager.k();
        const password = manager.j();
        const aliases = store.aliases();
        while (aliases.hasMoreElements()) {
            const alias = aliases.nextElement().toString();
            const key = store.getKey(alias, password);
            if (key !== null) {
                emit("stored-dtls-key", key.getEncoded(), {
                    keyId: alias, connections: activeIdentities.get(alias) || [],
                });
            }
        }
        console.log("Existing key store inspection complete");
    });

    console.log("Megadump, DTLS, stack, and firmware hooks installed");
});
