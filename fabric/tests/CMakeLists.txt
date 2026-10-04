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

# The capture core is built with the tools; its host-only tests run on every platform.
if(TARGET ostia_topo_capture_core)
  add_executable(ostia_topo_capture_tests topology/capture/deps_test.cpp)
  target_link_libraries(ostia_topo_capture_tests PRIVATE ostia_topo_capture_core GTest::gtest_main)
  if(OSTIA_SANITIZE)
    target_compile_definitions(ostia_topo_capture_tests PRIVATE OSTIA_TOPO_SANITIZED)
  endif()
  target_compile_definitions(
    ostia_topo_capture_tests
    PRIVATE
      OSTIA_TOPO_DATA_DIR="${PROJECT_SOURCE_DIR}/fabric/tests/topology/data"
      OSTIA_TOPO_FIXTURE_DIR="${PROJECT_SOURCE_DIR}/fabric/tests/fixtures/topology"
  )
  if(CMAKE_SYSTEM_NAME STREQUAL "Linux")
    target_link_libraries(ostia_topo_capture_tests PRIVATE ibverbs::headers)
  endif()
  gtest_discover_tests(ostia_topo_capture_tests PROPERTIES LABELS cpu)
endif()
