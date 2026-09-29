# The level configuration every component compiles against, as a build-tree-only
# INTERFACE target: never installed or exported (RFC-0001 §5).
configure_file(config.h.in ${CMAKE_CURRENT_BINARY_DIR}/config/ostia/telemetry/config.h @ONLY)
add_library(ostia_telemetry_config INTERFACE)
add_library(ostia::telemetry_config ALIAS ostia_telemetry_config)
target_include_directories(
  ostia_telemetry_config
  INTERFACE ${CMAKE_CURRENT_BINARY_DIR}/config ${CMAKE_CURRENT_SOURCE_DIR}/instrument
)

ostia_add_component(NAME telemetry RANK 0)
target_sources(ostia_telemetry PRIVATE src/telemetry.cpp)
ostia_telemetry_catalog(telemetry telemetry.toml)
if(OSTIA_BUILD_TESTS)
  add_subdirectory(tests)
endif()
