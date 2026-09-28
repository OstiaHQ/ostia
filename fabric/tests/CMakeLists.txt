include(GoogleTest)

add_executable(ostia_fabric_tests build_level_test.cpp)
target_link_libraries(ostia_fabric_tests PRIVATE ostia::fabric GTest::gtest_main)
gtest_discover_tests(ostia_fabric_tests PROPERTIES LABELS cpu)
