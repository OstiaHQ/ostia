# ostia-topo and the fixture tests (RFC-0003 §9). The drift test keeps the committed synthetic
# fixtures equal to what their generator emits; ostia-topo joins this file with the replay tests.

if(OSTIA_BUILD_TESTS)
  find_package(Python3 COMPONENTS Interpreter REQUIRED)
  add_test(
    NAME fabric.topo.fixtures_current
    COMMAND
      ${Python3_EXECUTABLE}
      ${PROJECT_SOURCE_DIR}/fabric/tests/fixtures/topology/synthetic/generate.py --check
  )
  set_tests_properties(fabric.topo.fixtures_current PROPERTIES LABELS cpu)
endif()
