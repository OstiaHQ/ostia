include(GoogleTest)

add_executable(ostia_telemetry_tests build_level_test.cpp result_test.cpp)
target_link_libraries(
  ostia_telemetry_tests
  PRIVATE ostia::telemetry GTest::gtest_main ${CMAKE_DL_LIBS}
)
target_compile_definitions(
  ostia_telemetry_tests
  PRIVATE OSTIA_EXPECT_LIBDIR="$<TARGET_FILE_DIR:ostia_telemetry>"
)
gtest_discover_tests(ostia_telemetry_tests PROPERTIES LABELS cpu)

add_executable(ostia_telemetry_c_abi c_abi_test.c)
target_link_libraries(ostia_telemetry_c_abi PRIVATE ostia::telemetry)
add_test(NAME telemetry.c_abi COMMAND ostia_telemetry_c_abi)
set_tests_properties(telemetry.c_abi PROPERTIES LABELS cpu)

# CUDA compile-only check (RFC-0001 §1.2; the nvcc-12.8 and nvcc-13 CI jobs).
if(CMAKE_CUDA_COMPILER_LOADED)
  add_library(ostia_telemetry_cuda_compile OBJECT cuda_compile.cu)
  target_link_libraries(ostia_telemetry_cuda_compile PRIVATE ostia::telemetry)
endif()
