#undef NDEBUG
#include <assert.h>
#define main reader_cli_main
#include "reader.c"
#undef main

static void
InitReader(Reader* reader)
{
    memset(reader, 0, sizeof(*reader));
    assert(GG_Loop_Create(&reader->loop) == GG_SUCCESS);
    assert(GG_Loop_BindToCurrentThread(reader->loop) == GG_SUCCESS);
    reader->output = tmpfile();
    assert(reader->output);
    GG_SET_INTERFACE(reader, Reader, GG_TlsKeyResolver);
    GG_SET_INTERFACE(reader, Reader, GG_CoapBlockwiseResponseListener);
    GG_SET_INTERFACE(reader, Reader, GG_CoapResponseListener);
    GG_SET_INTERFACE(reader, Reader, GG_EventListener);
}

static void
CloseReader(Reader* reader)
{
    fclose(reader->output);
    GG_Loop_Destroy(reader->loop);
}

static void
DeliverBlock(Reader* reader, size_t offset, bool more, uint8_t code,
             const uint8_t* payload, size_t size)
{
    GG_CoapMessage* message = NULL;
    assert(GG_CoapMessage_Create(code, GG_COAP_MESSAGE_TYPE_ACK, NULL, 0, 1,
                               NULL, 0, payload, size, &message) == GG_SUCCESS);
    GG_CoapMessageBlockInfo info = {.offset = offset, .size = 256, .more = more};
    GG_CoapBlockwiseResponseListener_OnResponseBlock(
        GG_CAST(reader, GG_CoapBlockwiseResponseListener), &info, message);
    GG_CoapMessage_Destroy(message);
}

int
main(void)
{
    GG_Module_Initialize();
    Reader reader;
    InitReader(&reader);
    reader.key_id = 0x673EF7CE;
    for (size_t i = 0; i < sizeof(reader.key); ++i) reader.key[i] = (uint8_t)i;
    uint8_t key[16];
    size_t size = sizeof(key);
    const uint8_t* identity = (const uint8_t*)"MD-673EF7CE-6AE67AD2";
    assert(GG_TlsKeyResolver_ResolveKey(GG_CAST(&reader, GG_TlsKeyResolver), identity, 20, key, &size) == GG_SUCCESS);
    assert(size == 16 && memcmp(key, reader.key, 16) == 0);
    identity = (const uint8_t*)"MD-673EF7CE-00000000";
    assert(GG_TlsKeyResolver_ResolveKey(GG_CAST(&reader, GG_TlsKeyResolver), identity, 20, key, &size) == GG_SUCCESS);
    identity = (const uint8_t*)"MD-FFFFFFFF-6AE67AD2";
    assert(GG_TlsKeyResolver_ResolveKey(GG_CAST(&reader, GG_TlsKeyResolver), identity, 20, key, &size) == GG_ERROR_NO_SUCH_ITEM);
    identity = (const uint8_t*)"MD-673EF7CE-NOTHEX00";
    assert(GG_TlsKeyResolver_ResolveKey(GG_CAST(&reader, GG_TlsKeyResolver), identity, 20, key, &size) == GG_ERROR_NO_SUCH_ITEM);
    identity = (const uint8_t*)"MD-673EF7CE-6AE67AD2";
    size = 15;
    assert(GG_TlsKeyResolver_ResolveKey(GG_CAST(&reader, GG_TlsKeyResolver), identity, 20, key, &size) == GG_ERROR_NOT_ENOUGH_SPACE);
    assert(size == 16);

    uint8_t data[259];
    for (size_t i = 0; i < sizeof(data); ++i) data[i] = (uint8_t)i;
    DeliverBlock(&reader, 0, true, 69, data, 256);
    DeliverBlock(&reader, 256, false, 69, data + 256, 3);
    assert(reader.complete && !reader.failed && reader.bytes_written == sizeof(data));
    rewind(reader.output);
    uint8_t copied[259];
    assert(fread(copied, 1, sizeof(copied), reader.output) == sizeof(copied));
    assert(memcmp(copied, data, sizeof(data)) == 0);
    CloseReader(&reader);

    InitReader(&reader);
    reader.pairing = true;
    GG_Event secure = {.type = GG_EVENT_TYPE_BLUETOOTH_LINK_SECURED_EVENT};
    GG_EventListener_OnEvent(GG_CAST(&reader, GG_EventListener), &secure);
    assert(!reader.complete && !reader.failed && ftell(reader.output) == 0);
    GG_CoapMessage* rejected = NULL;
    assert(GG_CoapMessage_Create(131, GG_COAP_MESSAGE_TYPE_ACK, NULL, 0, 1,
                               NULL, 0, NULL, 0, &rejected) == GG_SUCCESS);
    GG_CoapResponseListener_OnResponse(GG_CAST(&reader, GG_CoapResponseListener), rejected);
    assert(reader.failed && !reader.complete && !reader.bond_control_accepted);
    assert(ftell(reader.output) == 0);
    GG_CoapMessage_Destroy(rejected);
    CloseReader(&reader);

    InitReader(&reader);
    DeliverBlock(&reader, 256, false, 69, data, 3);
    assert(reader.failed && !reader.complete && reader.bytes_written == 0);
    CloseReader(&reader);
    InitReader(&reader);
    DeliverBlock(&reader, 0, true, 69, data, 255);
    assert(reader.failed && !reader.complete && reader.bytes_written == 0);
    CloseReader(&reader);
    InitReader(&reader);
    DeliverBlock(&reader, 0, false, 129, data, 3);
    assert(reader.failed && !reader.complete && reader.bytes_written == 0);
    CloseReader(&reader);
    InitReader(&reader);
    DeliverBlock(&reader, 0, false, 69, NULL, 0);
    assert(reader.failed && !reader.complete);
    CloseReader(&reader);

    InitReader(&reader);
    GG_SET_INTERFACE(&reader, Reader, GG_TimerListener);
    reader.watching = true;
    reader.sample_limit = 2;
    reader.interval_ms = 1000;
    assert(GG_DynamicBuffer_Create(16, &reader.sample_buffer) == GG_SUCCESS);
    assert(GG_TimerScheduler_CreateTimer(GG_Loop_GetTimerScheduler(reader.loop), &reader.poll_timer) == GG_SUCCESS);
    DeliverBlock(&reader, 0, false, 69, data, 3);
    assert(reader.samples_recorded == 1 && !reader.complete && !reader.failed);
    assert(GG_Timer_IsScheduled(reader.poll_timer));
    DeliverBlock(&reader, 0, false, 69, data + 3, 3);
    assert(reader.samples_recorded == 2 && reader.complete && !reader.failed);
    rewind(reader.output);
    char frames[100] = {0};
    assert(fread(frames, 1, sizeof(frames) - 1, reader.output) > 0);
    assert(strcmp(frames, "{\"payloadHex\":\"000102\"}\n{\"payloadHex\":\"030405\"}\n") == 0);
    GG_Timer_Destroy(reader.poll_timer);
    GG_DynamicBuffer_Release(reader.sample_buffer);
    CloseReader(&reader);
    GG_Module_Terminate();

    char* invalid_scan[] = {"reader", "scan", "--device", "invalid"};
    assert(reader_cli_main(4, invalid_scan) == 1);
    char* invalid_timeout[] = {"reader", "scan", "--timeout", "0"};
    assert(reader_cli_main(4, invalid_timeout) == 1);

    char key_path[] = ".reader-test-key-XXXXXX";
    char output_path[] = ".reader-test-output-XXXXXX";
    int key_fd = mkstemp(key_path);
    int output_fd = mkstemp(output_path);
    assert(key_fd >= 0 && output_fd >= 0);
    assert(write(key_fd, key, sizeof(key)) == sizeof(key));
    assert(write(output_fd, "keep", 4) == 4);
    close(key_fd);
    close(output_fd);
    char* existing_output[] = {
        "reader", "read", "--device", "09E229DE-80B1-6047-9CC1-E6F9BC82E5CA",
        "--key-file", key_path, "--key-id", "673EF7CE", "--output", output_path
    };
    assert(reader_cli_main(10, existing_output) == 1);
    FILE* preserved = fopen(output_path, "rb");
    char content[4];
    assert(preserved && fread(content, 1, sizeof(content), preserved) == sizeof(content));
    assert(memcmp(content, "keep", sizeof(content)) == 0);
    fclose(preserved);
    unlink(key_path);
    unlink(output_path);
    puts("PSK resolution, binary block assembly, and failure-path checks passed");
    return 0;
}
