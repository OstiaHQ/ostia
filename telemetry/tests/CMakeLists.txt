include(GoogleTest)

add_executable(
  ostia_telemetry_tests
  build_level_test.cpp
  level_check_test.cpp
  macros_test.cpp
  result_test.cpp
)
target_link_libraries(
  ostia_telemetry_tests
  PRIVATE ostia::telemetry ostia::telemetry_config GTest::gtest_main ${CMAKE_DL_LIBS}
)
get_target_property(_catalog_dir ostia_telemetry OSTIA_CATALOG_DIR)
target_include_directories(ostia_telemetry_tests PRIVATE ${_catalog_dir})
target_compile_definitions(
  ostia_telemetry_tests
  PRIVATE OSTIA_EXPECT_LIBDIR="$<TARGET_FILE_DIR:ostia_telemetry>"
)
gtest_discover_tests(ostia_telemetry_tests PROPERTIES LABELS cpu)

add_executable(ostia_telemetry_c_abi c_abi_test.c)
target_link_libraries(ostia_telemetry_c_abi PRIVATE ostia::telemetry ostia::telemetry_config)
add_test(NAME telemetry.c_abi COMMAND ostia_telemetry_c_abi)
set_tests_properties(telemetry.c_abi PROPERTIES LABELS cpu)

# CUDA compile-only check (RFC-0001 §1.2; the nvcc-12.8 and nvcc-13 CI jobs).
if(CMAKE_CUDA_COMPILER_LOADED)
  add_library(ostia_telemetry_cuda_compile OBJECT cuda_compile.cu)
  target_link_libraries(ostia_telemetry_cuda_compile PRIVATE ostia::telemetry)
endif()

# A type error inside a disabled macro still fails to compile at level off (RFC-0001 §5):
# compile the probe with a level-0 config.h, expecting an error, and its well-typed
# variant, expecting success.
set(OSTIA_TELEMETRY_LEVEL 0)
set(OSTIA_TELEMETRY off)
configure_file(../config.h.in ${CMAKE_CURRENT_BINARY_DIR}/level0/ostia/telemetry/config.h @ONLY)
set(
  _probe_includes
  -I${CMAKE_CURRENT_BINARY_DIR}/level0
  -I${PROJECT_SOURCE_DIR}/telemetry/instrument
  -I${_catalog_dir}
  -I${PROJECT_SOURCE_DIR}/telemetry/include
  -I${PROJECT_BINARY_DIR}/telemetry/include
)
foreach(variant error ok)
  add_test(
    NAME telemetry.macro_type_error_at_off.${variant}
    COMMAND
      ${CMAKE_COMMAND} -DCXX=${CMAKE_CXX_COMPILER}
      -DSOURCE=${CMAKE_CURRENT_SOURCE_DIR}/macro_type_error.cpp -DVARIANT=${variant}
      "-DINCLUDES=${_probe_includes}" -P ${PROJECT_SOURCE_DIR}/tests/cmake/expect_compile.cmake
  )
  set_tests_properties(telemetry.macro_type_error_at_off.${variant} PROPERTIES LABELS cpu)
endforeach()
