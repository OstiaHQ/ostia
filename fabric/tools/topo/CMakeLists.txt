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

  # `ostia-topo diff`: equal ids exit 0, different ids exit 1, and the identity diff names the
  # NVLink count that changed.
  set(
    _ostia_diff
    ${Python3_EXECUTABLE}
    ${CMAKE_CURRENT_SOURCE_DIR}/diff-case.py
    --tool
    $<TARGET_FILE:ostia-topo>
    --fixture
    ${PROJECT_SOURCE_DIR}/fabric/tests/fixtures/topology/synthetic/broken-nvlink
  )
  add_test(
    NAME fabric.topo.diff.self
    COMMAND ${_ostia_diff} --mutation none --expect 0
  )
  add_test(
    NAME fabric.topo.diff.nvlink
    COMMAND
      ${_ostia_diff} --mutation nvlink --expect 1 --output
      "(?m)^-  \\[.*\"kind\":\"nvlink\",\"links\":[0-9]+"
  )
  add_test(
    NAME fabric.topo.diff.renumbered
    COMMAND ${_ostia_diff} --mutation renumber --expect 0
  )
  set_tests_properties(
    fabric.topo.diff.self
    fabric.topo.diff.nvlink
    fabric.topo.diff.renumbered
    PROPERTIES LABELS cpu
  )
endif()
