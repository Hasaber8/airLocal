#include <errno.h>
#include <ctype.h>
#include <fcntl.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include "xp/common/gg_common.h"
#include "xp/coap/gg_coap.h"
#include "xp/coap/gg_coap_blockwise.h"
#include "xp/loop/gg_loop.h"
#include "xp/module/gg_module.h"
#include "xp/stack_builder/gg_stack_builder.h"
#include "xp/tls/gg_tls.h"
#include "xp/apps/stack-tool/gg_stack_tool_core_bluetooth_transport.h"

typedef struct {
    GG_IMPLEMENTS(GG_EventListener);
    GG_IMPLEMENTS(GG_TlsKeyResolver);
    GG_IMPLEMENTS(GG_CoapBlockwiseResponseListener);
    GG_IMPLEMENTS(GG_CoapResponseListener);
    GG_IMPLEMENTS(GG_TimerListener);
    GG_Loop* loop;
    GG_Stack* stack;
    GG_CoapEndpoint* endpoint;
    GG_StackToolBluetoothTransport* transport;
    uint32_t key_id;
    uint8_t key[16];
    char identity[21];
    const char* query;
    const char* resource;
    const char* resource_tail;
    FILE* output;
    GG_Timer* poll_timer;
    GG_DynamicBuffer* sample_buffer;
    uint32_t interval_ms;
    size_t sample_limit;
    size_t samples_recorded;
    size_t bytes_written;
    bool scanning;
    bool watching;
    bool pairing;
    bool bond_control_accepted;
    bool stack_started;
    bool request_started;
    bool complete;
    bool failed;
} Reader;

static void
Reader_Fail(Reader* self, const char* message)
{
    fprintf(stderr, "ERROR: %s\n", message);
    self->failed = true;
    GG_Loop_RequestTermination(self->loop);
}

static bool
ParseKeyId(const char* text, uint32_t* value)
{
    if (strlen(text) != 8 || strspn(text, "0123456789abcdefABCDEF") != 8) return false;
    *value = (uint32_t)strtoul(text, NULL, 16);
    return true;
}

static GG_Result
Reader_ResolveKey(GG_TlsKeyResolver* _self, const uint8_t* identity, size_t identity_size,
                  uint8_t* key, size_t* key_size)
{
    Reader* self = GG_SELF(Reader, GG_TlsKeyResolver);
    // Match the provisioned mobile-data key ID, independently of the expiration suffix.
    if (identity_size != 20 || memcmp(identity, "MD-", 3) || identity[11] != '-') {
        return GG_ERROR_NO_SUCH_ITEM;
    }
    char key_id[9] = {0};
    memcpy(key_id, identity + 3, 8);
    uint32_t offered;
    if (!ParseKeyId(key_id, &offered) || offered != self->key_id) return GG_ERROR_NO_SUCH_ITEM;
    char expiration[9] = {0};
    memcpy(expiration, identity + 12, 8);
    uint32_t ignored;
    if (!ParseKeyId(expiration, &ignored)) return GG_ERROR_NO_SUCH_ITEM;
    if (*key_size < sizeof(self->key)) {
        *key_size = sizeof(self->key);
        return GG_ERROR_NOT_ENOUGH_SPACE;
    }
    memcpy(key, self->key, sizeof(self->key));
    *key_size = sizeof(self->key);
    memcpy(self->identity, identity, identity_size);
    fprintf(stderr, "Resolved DTLS identity: %.*s\n", (int)identity_size, identity);
    return GG_SUCCESS;
}

GG_IMPLEMENT_INTERFACE(Reader, GG_TlsKeyResolver) {
    .ResolveKey = Reader_ResolveKey
};

static void
Reader_OnBlock(GG_CoapBlockwiseResponseListener* _self, GG_CoapMessageBlockInfo* info,
               GG_CoapMessage* message)
{
    Reader* self = GG_SELF(Reader, GG_CoapBlockwiseResponseListener);
    if (self->failed) return;
    uint8_t code = GG_CoapMessage_GetCode(message);
    if (GG_COAP_MESSAGE_CODE_CLASS(code) != 2) {
        char error[80];
        snprintf(error, sizeof(error), "CoAP response %u.%02u", code >> 5, code & 31);
        Reader_Fail(self, error);
        return;
    }
    size_t size = GG_CoapMessage_GetPayloadSize(message);
    if (info->offset != self->bytes_written || (info->more && size != info->size)) {
        Reader_Fail(self, "Non-contiguous or incomplete CoAP response block");
        return;
    }
    if (self->watching) {
        if (GG_FAILED(GG_DynamicBuffer_AppendData(self->sample_buffer, GG_CoapMessage_GetPayload(message), size))) {
            Reader_Fail(self, "Could not buffer the live response");
            return;
        }
    } else if (size && fwrite(GG_CoapMessage_GetPayload(message), 1, size, self->output) != size) {
        Reader_Fail(self, "Could not write dump payload");
        return;
    }
    self->bytes_written += size;
    if (!info->more) {
        if (!self->bytes_written) {
            Reader_Fail(self, "The tracker returned an empty dump");
            return;
        }
        if (self->watching) {
            const uint8_t* data = GG_DynamicBuffer_GetData(self->sample_buffer);
            fputs("{\"payloadHex\":\"", self->output);
            for (size_t i = 0; i < self->bytes_written; ++i) fprintf(self->output, "%02x", data[i]);
            fputs("\"}\n", self->output);
            if (fflush(self->output) || ferror(self->output)) {
                Reader_Fail(self, "Could not save the live recording");
                return;
            }
            ++self->samples_recorded;
            fprintf(stderr, "Recorded live response %zu/%zu\n", self->samples_recorded, self->sample_limit);
            if (self->samples_recorded < self->sample_limit) {
                self->request_started = false;
                self->bytes_written = 0;
                GG_DynamicBuffer_SetDataSize(self->sample_buffer, 0);
                if (GG_FAILED(GG_Timer_Schedule(self->poll_timer, GG_CAST(self, GG_TimerListener), self->interval_ms))) {
                    Reader_Fail(self, "Could not schedule the next live read");
                }
                return;
            }
        }
        self->complete = true;
        GG_Loop_RequestTermination(self->loop);
    }
}

static void
Reader_OnCoapError(GG_CoapBlockwiseResponseListener* _self, GG_Result error, const char* message)
{
    Reader* self = GG_SELF(Reader, GG_CoapBlockwiseResponseListener);
    fprintf(stderr, "CoAP error %d: %s\n", error, message ? message : "");
    Reader_Fail(self, "Dump request failed");
}

GG_IMPLEMENT_INTERFACE(Reader, GG_CoapBlockwiseResponseListener) {
    .OnResponseBlock = Reader_OnBlock,
    .OnError = Reader_OnCoapError
};

static void
Reader_OnBondAck(GG_CoapResponseListener* _self)
{
    GG_COMPILER_UNUSED(_self);
}

static void
Reader_OnBondError(GG_CoapResponseListener* _self, GG_Result error, const char* message)
{
    Reader* self = GG_SELF(Reader, GG_CoapResponseListener);
    fprintf(stderr, "Bond control error %d: %s\n", error, message ? message : "");
    Reader_Fail(self, "Could not enable the tracker bonding window");
}

static void
Reader_OnBondResponse(GG_CoapResponseListener* _self, GG_CoapMessage* response)
{
    Reader* self = GG_SELF(Reader, GG_CoapResponseListener);
    uint8_t code = GG_CoapMessage_GetCode(response);
    if (GG_COAP_MESSAGE_CODE_CLASS(code) != 2) {
        char message[80];
        snprintf(message, sizeof(message), "Bond control rejected: CoAP %u.%02u", code >> 5, code & 31);
        Reader_Fail(self, message);
        return;
    }
    self->bond_control_accepted = true;
    fprintf(stderr, "Tracker accepted the 120-second Bluetooth bonding window.\n");
    GG_StackToolBluetoothTransport_RequestBond(self->transport);
}

GG_IMPLEMENT_INTERFACE(Reader, GG_CoapResponseListener) {
    .OnAck = Reader_OnBondAck,
    .OnError = Reader_OnBondError,
    .OnResponse = Reader_OnBondResponse
};

static void
Reader_RequestBond(Reader* self)
{
    if (self->request_started) return;
    self->request_started = true;
    GG_CoapMessageOptionParam options[] = {
        GG_COAP_MESSAGE_OPTION_PARAM_STRING(URI_PATH, "bond"),
        GG_COAP_MESSAGE_OPTION_PARAM_STRING(URI_PATH, "control")
    };
    // BondControl.Control: field 1 (operationTimeout), varint 120 seconds.
    const uint8_t payload[] = {0x08, 0x78};
    GG_CoapClientParameters parameters = {.ack_timeout = 3000, .max_resend_count = 2};
    GG_Result result = GG_CoapEndpoint_SendRequest(self->endpoint, GG_COAP_METHOD_PUT,
        options, GG_ARRAY_SIZE(options), payload, sizeof(payload), &parameters,
        GG_CAST(self, GG_CoapResponseListener), NULL);
    if (GG_FAILED(result)) Reader_Fail(self, "Could not start bond control request");
}

static void
Reader_RequestDump(Reader* self)
{
    if (self->request_started || self->failed) return;
    self->request_started = true;
    GG_CoapMessageOptionParam options[] = {
        GG_COAP_MESSAGE_OPTION_PARAM_STRING(URI_PATH, self->resource),
        GG_COAP_MESSAGE_OPTION_PARAM_STRING(URI_PATH, self->resource_tail),
        GG_COAP_MESSAGE_OPTION_PARAM_STRING(URI_QUERY, self->query)
    };
    GG_CoapClientParameters parameters = {.ack_timeout = 3000, .max_resend_count = 2};
    GG_Result result = GG_CoapEndpoint_SendBlockwiseRequest(
        self->endpoint, GG_COAP_METHOD_GET, options, self->query ? GG_ARRAY_SIZE(options) : self->resource_tail ? 2 : 1, NULL, 1024,
        &parameters, GG_CAST(self, GG_CoapBlockwiseResponseListener), NULL);
    if (GG_FAILED(result)) Reader_Fail(self, "Could not start the CoAP read");
}

static void
Reader_OnEvent(GG_EventListener* _self, const GG_Event* event)
{
    Reader* self = GG_SELF(Reader, GG_EventListener);
    if (self->failed || self->complete) return;
    if (event->type == GG_EVENT_TYPE_BLUETOOTH_TRANSPORT_SCAN) {
        const GG_StackToolBluetoothTransportScanEvent* scan =
            (const GG_StackToolBluetoothTransportScanEvent*)event;
        printf("%s\t%s\t%d dBm\t%s\n", scan->peripheral_id,
               scan->peripheral_name ? scan->peripheral_name : "(unnamed)", scan->rssi,
               scan->device_marker ? scan->device_marker : "");
    } else if (event->type == GG_EVENT_TYPE_BLUETOOTH_LINK_CONNECTED_EVENT && !self->stack_started) {
        self->stack_started = true;
        fprintf(stderr, "BLE link ready. Starting Gattlink and DTLS.\n");
        if (GG_FAILED(GG_Stack_Start(self->stack))) Reader_Fail(self, "Could not start the stack");
    } else if (event->type == GG_EVENT_TYPE_BLUETOOTH_LINK_SECURED_EVENT && self->pairing && self->bond_control_accepted) {
        const char* peer = GG_StackToolBluetoothTransport_GetPeerId(self->transport);
        if (!peer) { Reader_Fail(self, "Missing authenticated peer identifier"); return; }
        fprintf(self->output, "{\"peerId\":\"%s\",\"keyId\":\"%08" PRIX32 "\",\"identity\":\"%s\",\"dtlsAuthenticated\":true,\"bleEncryptionVerified\":true}\n",
                peer, self->key_id, self->identity);
        self->complete = true;
        GG_Loop_RequestTermination(self->loop);
    } else if (event->type == GG_EVENT_TYPE_STACK_EVENT_FORWARD) {
        const GG_Event* forwarded = ((const GG_StackForwardEvent*)event)->forwarded;
        if (forwarded->type == GG_EVENT_TYPE_TLS_STATE_CHANGE) {
            GG_DtlsProtocolStatus status;
            GG_DtlsProtocol_GetStatus((GG_DtlsProtocol*)forwarded->source, &status);
            if (status.state == GG_TLS_STATE_SESSION) {
                if (self->pairing) {
                    fprintf(stderr, "DTLS session established. Enabling Bluetooth bonding.\n");
                    Reader_RequestBond(self);
                    return;
                }
                fprintf(stderr, "DTLS session established. Reading /%s%s%s\n", self->resource,
                        self->resource_tail ? "/" : "", self->resource_tail ? self->resource_tail : "");
                Reader_RequestDump(self);
            } else if (status.state == GG_TLS_STATE_ERROR) {
                fprintf(stderr, "DTLS error: %d\n", status.last_error);
                Reader_Fail(self, "DTLS handshake failed");
            }
        }
    }
}

GG_IMPLEMENT_INTERFACE(Reader, GG_EventListener) {
    .OnEvent = Reader_OnEvent
};

static void
Reader_OnTimeout(GG_TimerListener* _self, GG_Timer* timer, uint32_t elapsed)
{
    Reader* self = GG_SELF(Reader, GG_TimerListener);
    GG_COMPILER_UNUSED(timer);
    GG_COMPILER_UNUSED(elapsed);
    if (timer == self->poll_timer) {
        Reader_RequestDump(self);
        return;
    }
    if (self->scanning) {
        self->complete = true;
        GG_Loop_RequestTermination(self->loop);
    } else {
        Reader_Fail(self, self->pairing && self->bond_control_accepted ? "Bluetooth pairing or encryption verification timed out" :
                    self->pairing && self->request_started ? "Bond control request timed out" :
                    self->request_started ? "Dump transfer timed out" :
                    "BLE connection or DTLS handshake timed out");
    }
}

GG_IMPLEMENT_INTERFACE(Reader, GG_TimerListener) {
    .OnTimerFired = Reader_OnTimeout
};

static void
Usage(void)
{
    fprintf(stderr,
        "Usage:\n"
        "  fitbit-air-reader scan [--timeout SECONDS]\n"
        "  fitbit-air-reader pair --device UUID|bond:HEX16 --key-file FILE --key-id HEX8 --output JSON\n"
        "  fitbit-air-reader read --device UUID|paired:UUID|name:NAME --key-file FILE --key-id HEX8\n"
        "                         --output FILE [--type primary|sensor|live|discovery|status|outbox] [--timeout SECONDS]\n"
        "  fitbit-air-reader watch --device UUID|paired:UUID|name:NAME --key-file FILE --key-id HEX8\n"
        "                          --output JSONL [--count N] [--interval SECONDS] [--timeout SECONDS]\n");
}

int
main(int argc, char** argv)
{
    if (argc < 2 || (!strcmp(argv[1], "--help"))) { Usage(); return argc < 2 ? 1 : 0; }
    Reader reader = {0};
    reader.scanning = !strcmp(argv[1], "scan");
    reader.watching = !strcmp(argv[1], "watch");
    reader.pairing = !strcmp(argv[1], "pair");
    if (!reader.scanning && !reader.watching && !reader.pairing && strcmp(argv[1], "read")) { Usage(); return 1; }
    const char* device_id = reader.scanning ? "scan" : NULL;
    const char* key_file = NULL;
    const char* key_id = NULL;
    const char* output = NULL;
    reader.query = reader.watching ? NULL : "t=primary";
    reader.resource = reader.watching ? "liveactivity" : "sync";
    reader.resource_tail = reader.watching ? NULL : "dump";
    reader.sample_limit = 12;
    reader.interval_ms = 5000;
    uint32_t timeout = reader.scanning ? 10 : reader.pairing ? 120 : reader.watching ? 90 : 60;
    for (int i = 2; i < argc; i += 2) {
        if (i + 1 >= argc) { Usage(); return 1; }
        if (reader.scanning && strcmp(argv[i], "--timeout")) { Usage(); return 1; }
        const char* value = argv[i + 1];
        if (!strcmp(argv[i], "--device")) device_id = value;
        else if (!strcmp(argv[i], "--key-file")) key_file = value;
        else if (!strcmp(argv[i], "--key-id")) key_id = value;
        else if (!strcmp(argv[i], "--output")) output = value;
        else if (!strcmp(argv[i], "--type")) {
            if (reader.watching) { Usage(); return 1; }
            if (!strcmp(value, "primary")) reader.query = "t=primary";
            else if (!strcmp(value, "sensor")) reader.query = "t=sc";
            else if (!strcmp(value, "live")) { reader.query = NULL; reader.resource = "liveactivity"; reader.resource_tail = NULL; }
            else if (!strcmp(value, "discovery")) { reader.query = NULL; reader.resource = ".well-known"; reader.resource_tail = "core"; }
            else if (!strcmp(value, "status")) { reader.query = NULL; reader.resource = "sync"; reader.resource_tail = "status"; }
            else if (!strcmp(value, "outbox")) { reader.query = NULL; reader.resource = "app"; reader.resource_tail = "outbox"; }
            else { Usage(); return 1; }
        } else if (!strcmp(argv[i], "--count") || !strcmp(argv[i], "--interval")) {
            if (!reader.watching) { Usage(); return 1; }
            char* end;
            unsigned long quantity = strtoul(value, &end, 10);
            if (!value[0] || *end || !quantity || quantity > 1000) { Usage(); return 1; }
            if (!strcmp(argv[i], "--count")) reader.sample_limit = quantity;
            else reader.interval_ms = (uint32_t)quantity * 1000;
        } else if (!strcmp(argv[i], "--timeout")) {
            char* end;
            unsigned long seconds = strtoul(value, &end, 10);
            if (!value[0] || *end || seconds == 0 || seconds > UINT32_MAX / 1000) {
                fprintf(stderr, "ERROR: invalid timeout\n"); return 1;
            }
            timeout = (uint32_t)seconds;
        } else { Usage(); return 1; }
    }
    if (!device_id || (!reader.scanning && (!key_file || !key_id || !output))) { Usage(); return 1; }
    if (!reader.scanning) {
        bool by_name = !strncmp(device_id, "name:", 5) && device_id[5];
        bool by_marker = !strncmp(device_id, "marker:", 7) && strlen(device_id + 7) == 16 && strspn(device_id + 7, "0123456789abcdefABCDEF") == 16;
        bool by_bond = !strncmp(device_id, "bond:", 5) && strlen(device_id + 5) == 16 && strspn(device_id + 5, "0123456789abcdefABCDEF") == 16;
        bool by_paired = !strncmp(device_id, "paired:", 7);
        if ((!reader.pairing && by_bond) || (reader.pairing && (by_name || by_marker || by_paired))) { Usage(); return 1; }
        const char* peer_id = by_paired ? device_id + 7 : device_id;
        if (!by_name && !by_marker && !by_bond && strlen(peer_id) != 36) { fprintf(stderr, "ERROR: expected a peripheral UUID or device marker\n"); return 1; }
        for (size_t i = 0; !by_name && !by_marker && !by_bond && i < 36; ++i) {
            bool separator = i == 8 || i == 13 || i == 18 || i == 23;
            if (separator ? peer_id[i] != '-' : !isxdigit((unsigned char)peer_id[i])) {
                fprintf(stderr, "ERROR: invalid macOS peripheral UUID\n"); return 1;
            }
        }
        if (!ParseKeyId(key_id, &reader.key_id)) {
            fprintf(stderr, "ERROR: key ID must be eight hex digits\n"); return 1;
        }
        FILE* input = fopen(key_file, "rb");
        if (!input) { perror("key file"); return 1; }
        size_t size = fread(reader.key, 1, sizeof(reader.key), input);
        bool valid = size == sizeof(reader.key) && fgetc(input) == EOF && !ferror(input);
        fclose(input);
        if (!valid) { fprintf(stderr, "ERROR: key file must contain exactly 16 bytes\n"); return 1; }
    }

    char* partial = NULL;
    if (!reader.scanning) {
        struct stat existing;
        if (lstat(output, &existing) == 0) {
            fprintf(stderr, "ERROR: output already exists: %s\n", output); return 1;
        }
        if (errno != ENOENT) { perror("output file"); return 1; }
        size_t size = strlen(output) + sizeof(".partial");
        partial = malloc(size);
        if (!partial) return 1;
        snprintf(partial, size, "%s.partial", output);
        int fd = open(partial, O_WRONLY | O_CREAT | O_EXCL, 0600);
        if (fd < 0) { perror("output file"); free(partial); return 1; }
        reader.output = fdopen(fd, "wb");
        if (!reader.output) { close(fd); unlink(partial); free(partial); return 1; }
    }

    GG_SET_INTERFACE(&reader, Reader, GG_EventListener);
    GG_SET_INTERFACE(&reader, Reader, GG_TlsKeyResolver);
    GG_SET_INTERFACE(&reader, Reader, GG_CoapBlockwiseResponseListener);
    GG_SET_INTERFACE(&reader, Reader, GG_CoapResponseListener);
    GG_SET_INTERFACE(&reader, Reader, GG_TimerListener);
    if (!getenv("GG_LOG_CONFIG")) setenv("GG_LOG_CONFIG", "plist:.level=WARNING", 1);
    GG_Module_Initialize();
    GG_Timer* timer = NULL;
    GG_StackToolBluetoothTransport* transport = NULL;
    GG_StackElementPortInfo top = {0};
    GG_Result result = GG_Loop_Create(&reader.loop);
    if (GG_FAILED(result)) goto cleanup;
    result = GG_Loop_BindToCurrentThread(reader.loop);
    if (GG_FAILED(result)) goto cleanup;
    result = GG_StackToolBluetoothTransport_Create(reader.loop, device_id, &transport);
    if (GG_FAILED(result)) goto cleanup;
    reader.transport = transport;

    if (!reader.scanning) {
        static const uint16_t suites[] = {
            GG_TLS_PSK_WITH_AES_128_CCM,
            GG_TLS_PSK_WITH_AES_128_CCM_8,
            GG_TLS_PSK_WITH_AES_128_GCM_SHA256,
            GG_TLS_ECDHE_PSK_WITH_AES_128_CBC_SHA256
        };
        GG_TlsServerOptions tls = {
            .base = {.cipher_suites = suites, .cipher_suites_count = GG_ARRAY_SIZE(suites)},
            .key_resolver = GG_CAST(&reader, GG_TlsKeyResolver)
        };
        GG_StackBuilderParameters parameters = {
            .element_type = GG_STACK_ELEMENT_TYPE_DTLS_SERVER, .element_parameters = &tls
        };
        result = GG_StackBuilder_BuildStack("DSNG", &parameters, 1, GG_STACK_ROLE_HUB, NULL,
            reader.loop, GG_StackToolBluetoothTransport_AsDataSource(transport),
            GG_StackToolBluetoothTransport_AsDataSink(transport), &reader.stack);
        if (GG_FAILED(result)) goto cleanup;
        GG_EventEmitter_SetListener(GG_Stack_AsEventEmitter(reader.stack), GG_CAST(&reader, GG_EventListener));
        GG_StackToolBluetoothTransport_SetMtuListener(transport, GG_Stack_AsEventListener(reader.stack));
        result = GG_Stack_GetPortById(reader.stack, GG_STACK_ELEMENT_ID_TOP, GG_STACK_PORT_ID_TOP, &top);
        if (GG_FAILED(result)) goto cleanup;
        result = GG_CoapEndpoint_Create(GG_Loop_GetTimerScheduler(reader.loop), top.sink, top.source, &reader.endpoint);
        if (GG_FAILED(result)) goto cleanup;
    }
    GG_StackToolBluetoothTransport_SetScanListener(transport, GG_CAST(&reader, GG_EventListener));
    GG_StackToolBluetoothTransport_SetConnectionListener(transport, GG_CAST(&reader, GG_EventListener));
    result = GG_TimerScheduler_CreateTimer(GG_Loop_GetTimerScheduler(reader.loop), &timer);
    if (GG_FAILED(result)) goto cleanup;
    result = GG_Timer_Schedule(timer, GG_CAST(&reader, GG_TimerListener), timeout * 1000);
    if (GG_FAILED(result)) goto cleanup;
    if (reader.watching) {
        result = GG_DynamicBuffer_Create(128, &reader.sample_buffer);
        if (GG_FAILED(result)) goto cleanup;
        result = GG_TimerScheduler_CreateTimer(GG_Loop_GetTimerScheduler(reader.loop), &reader.poll_timer);
        if (GG_FAILED(result)) goto cleanup;
    }
    setvbuf(stdout, NULL, _IONBF, 0);
    GG_StackToolBluetoothTransport_Start(transport);
    result = GG_Loop_Run(reader.loop);

cleanup:
    if (GG_FAILED(result)) fprintf(stderr, "ERROR: Golden Gate returned %d\n", result);
    if (reader.output && fclose(reader.output)) reader.failed = true;
    int exit_code = reader.complete && !reader.failed && GG_SUCCEEDED(result) ? 0 : 1;
    if (partial) {
        // Publish a complete dump atomically without overwriting an existing output.
        if (exit_code == 0 && link(partial, output)) { perror("publish output"); exit_code = 1; }
        unlink(partial);
        free(partial);
    }
    if (exit_code == 0 && !reader.scanning) {
        if (reader.pairing) fprintf(stderr, "Saved verified laptop pairing to %s\n", output);
        else if (reader.watching) fprintf(stderr, "Saved %zu live responses to %s\n", reader.samples_recorded, output);
        else fprintf(stderr, "Saved %zu bytes to %s\n", reader.bytes_written, output);
    }
    if (top.source) GG_DataSource_SetDataSink(top.source, NULL);
    if (reader.endpoint) GG_CoapEndpoint_Destroy(reader.endpoint);
    if (timer) GG_Timer_Destroy(timer);
    if (reader.poll_timer) GG_Timer_Destroy(reader.poll_timer);
    if (reader.sample_buffer) GG_DynamicBuffer_Release(reader.sample_buffer);
    if (reader.stack) GG_Stack_Destroy(reader.stack);
    if (transport) GG_StackToolBluetoothTransport_Destroy(transport);
    if (reader.loop) GG_Loop_Destroy(reader.loop);
    GG_Module_Terminate();
    return exit_code;
}
