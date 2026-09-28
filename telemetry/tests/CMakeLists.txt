include(GoogleTest)

add_executable(ostia_telemetry_tests build_level_test.cpp)
target_link_libraries(ostia_telemetry_tests PRIVATE ostia::telemetry GTest::gtest_main ${CMAKE_DL_LIBS})
target_compile_definitions(
  ostia_telemetry_tests
  PRIVATE OSTIA_EXPECT_LIBDIR="$<TARGET_FILE_DIR:ostia_telemetry>"
)
gtest_discover_tests(ostia_telemetry_tests PROPERTIES LABELS cpu)

add_executable(ostia_telemetry_c_abi c_abi_test.c)
target_link_libraries(ostia_telemetry_c_abi PRIVATE ostia::telemetry)
add_test(NAME telemetry.c_abi COMMAND ostia_telemetry_c_abi)
set_tests_properties(telemetry.c_abi PROPERTIES LABELS cpu)
