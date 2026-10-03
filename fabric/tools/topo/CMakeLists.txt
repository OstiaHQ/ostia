# ostia-topo and the fixture tests (RFC-0003 §9). The drift test keeps the committed synthetic
# fixtures equal to what their generator emits; each golden test replays one fixture through the
# model builder and compares it to its expected.json.

add_executable(ostia-topo main.cpp)
target_link_libraries(ostia-topo PRIVATE ostia_fabric_topology)

if(OSTIA_BUILD_TESTS)
  find_package(Python3 COMPONENTS Interpreter REQUIRED)
  add_test(
    NAME fabric.topo.fixtures_current
    COMMAND
      ${Python3_EXECUTABLE}
      ${PROJECT_SOURCE_DIR}/fabric/tests/fixtures/topology/synthetic/generate.py --check
  )
  set_tests_properties(fabric.topo.fixtures_current PROPERTIES LABELS cpu)

  file(
    GLOB _ostia_fixture_goldens
    CONFIGURE_DEPENDS
    ${PROJECT_SOURCE_DIR}/fabric/tests/fixtures/topology/*/*/expected.json
  )
  foreach(golden IN LISTS _ostia_fixture_goldens)
    get_filename_component(dir ${golden} DIRECTORY)
    get_filename_component(name ${dir} NAME)
    add_test(
      NAME fabric.topo.golden.${name}
      COMMAND ostia-topo model ${dir} --check ${golden}
    )
    set_tests_properties(fabric.topo.golden.${name} PROPERTIES LABELS cpu)
  endforeach()

  # A malformed pair.json must map to exit 3, not abort.
  add_test(
    NAME fabric.topo.bad_pair_exit
    COMMAND
      ${CMAKE_COMMAND} -DTOOL=$<TARGET_FILE:ostia-topo>
      -DDIR=${PROJECT_SOURCE_DIR}/fabric/tests/topology/data/bad-pair -DEXPECT=3 -P
      ${CMAKE_CURRENT_SOURCE_DIR}/expect-exit.cmake
  )
  set_tests_properties(fabric.topo.bad_pair_exit PROPERTIES LABELS cpu)
endif()
