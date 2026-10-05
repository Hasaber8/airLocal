cmake_minimum_required(VERSION 3.19)
project(fitbit-air-reader LANGUAGES C OBJC)

if(NOT APPLE)
    message(FATAL_ERROR "This reader uses the macOS CoreBluetooth transport")
endif()

set(GG_ENABLE_APPS OFF CACHE BOOL "")
set(GG_ENABLE_EXAMPLES OFF CACHE BOOL "")
set(GG_ENABLE_UNIT_TESTS OFF CACHE BOOL "")
set(GG_CONFIG_DEFAULT_SOCKETS_MODULE Nip CACHE STRING "")
set(GG_CONFIG_MBEDTLS_VERSION 2 CACHE STRING "")
set(GG_CONFIG_MBEDTLS_USE_CUSTOM_ALLOCATOR ON CACHE BOOL "")
foreach(port APPLE_TIME APPLE_SEMAPHORE BSD_SELECT_LOOP NIP_SOCKETS
             NIP NIP_NETIF POSIX_SHARED_QUEUE POSIX_THREADS POSIX_MUTEX
             STDC_CONSOLE STDC_ENV STDC_RANDOM MBEDTLS_TLS MBEDTLS NULL_LOG_CONFIG)
    set(GG_PORTS_ENABLE_${port} ON CACHE BOOL "")
endforeach()

set(GG_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../golden-gate")
add_subdirectory("${GG_ROOT}/xp" golden-gate)
add_executable(fitbit-air-reader MACOSX_BUNDLE reader.c
    "${GG_ROOT}/xp/apps/stack-tool/gg_stack_tool_core_bluetooth_transport.m")
set_target_properties(fitbit-air-reader PROPERTIES
    MACOSX_BUNDLE_INFO_PLIST "${CMAKE_CURRENT_SOURCE_DIR}/Info.plist")
target_include_directories(fitbit-air-reader PRIVATE "${GG_ROOT}")
target_link_libraries(fitbit-air-reader PRIVATE gg-runtime "-framework CoreBluetooth" "-framework Foundation")
target_compile_options(fitbit-air-reader PRIVATE -Wall -Wextra)
target_compile_definitions(fitbit-air-reader PRIVATE GG_CONFIG_ENABLE_LOGGING)

enable_testing()
add_executable(reader-tests reader_tests.c
    "${GG_ROOT}/xp/apps/stack-tool/gg_stack_tool_core_bluetooth_transport.m")
target_include_directories(reader-tests PRIVATE "${GG_ROOT}")
target_link_libraries(reader-tests PRIVATE gg-runtime "-framework CoreBluetooth" "-framework Foundation")
target_compile_options(reader-tests PRIVATE -Wall -Wextra)
target_compile_definitions(reader-tests PRIVATE GG_CONFIG_ENABLE_LOGGING)
add_test(NAME reader-protocol-checks COMMAND reader-tests)
