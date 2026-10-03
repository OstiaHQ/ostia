include(GoogleTest)

add_executable(ostia_fabric_tests build_level_test.cpp)
target_link_libraries(ostia_fabric_tests PRIVATE ostia::fabric GTest::gtest_main)
gtest_discover_tests(ostia_fabric_tests PROPERTIES LABELS cpu)

find_package(ucx CONFIG QUIET)
if(ucx_FOUND)
  add_subdirectory(multiprocess)
endif()

add_executable(
  ostia_fabric_topology_tests
  topology/builder_test.cpp
  topology/deps_test.cpp
  topology/fixture_source_test.cpp
  topology/identity_corpus_test.cpp
  topology/identity_test.cpp
  topology/model_test.cpp
  topology/schema_test.cpp
  topology/sha256_test.cpp
)
target_link_libraries(ostia_fabric_topology_tests PRIVATE ostia_fabric_topology GTest::gtest_main)
# Sanitizer builds run several times slower, so timing tests widen their bound.
if(OSTIA_SANITIZE)
  target_compile_definitions(ostia_fabric_topology_tests PRIVATE OSTIA_TOPO_SANITIZED)
endif()
target_compile_definitions(
  ostia_fabric_topology_tests
  PRIVATE OSTIA_TOPO_FIXTURE_DIR="${PROJECT_SOURCE_DIR}/fabric/tests/fixtures/topology"
)
gtest_discover_tests(ostia_fabric_topology_tests PROPERTIES LABELS cpu)
