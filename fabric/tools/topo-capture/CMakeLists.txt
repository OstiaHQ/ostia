# Capture core (RFC-0003 §1-§4): host-only so the scrub, leak check and manifest are
# tested on every platform; only the live sources and main are Linux-only.
add_library(
  ostia_topo_capture_core
  STATIC
  core/diagnostics.cpp
  core/sysfs.cpp
  core/nics.cpp
  core/emit_xml.cpp
  core/writers.cpp
  core/leak.cpp
  core/manifest.cpp
  core/cleanup.cpp
  core/pipeline.cpp
)
set_target_properties(ostia_topo_capture_core PROPERTIES POSITION_INDEPENDENT_CODE ON)
target_include_directories(ostia_topo_capture_core PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
target_link_libraries(ostia_topo_capture_core PUBLIC ostia_fabric_topology)
target_compile_definitions(ostia_topo_capture_core PUBLIC OSTIA_TOOL_VERSION="${PROJECT_VERSION}")

# The live sources and the executable are Linux-only (RFC-0003 §1). NVML and ibverbs are dlopened,
# never linked, so the tool runs on machines that lack either library.
if(CMAKE_SYSTEM_NAME STREQUAL "Linux")
  add_library(ostia_topo_capture_live OBJECT live/hwloc_live.cpp live/nvml_dl.cpp live/verbs_dl.cpp)
  target_link_libraries(
    ostia_topo_capture_live
    PUBLIC ostia_topo_capture_core ibverbs::headers ${CMAKE_DL_LIBS}
  )
  add_executable(ostia-topo-capture main.cpp)
  target_link_libraries(ostia-topo-capture PRIVATE ostia_topo_capture_live)

  if(OSTIA_BUILD_TESTS)
    # A fake libnvidia-ml.so.1 in its own directory, found through LD_LIBRARY_PATH by e2e.py.
    add_library(ostia_fake_nvml SHARED tests/fake_nvml.cpp)
    set_target_properties(
      ostia_fake_nvml
      PROPERTIES
        OUTPUT_NAME nvidia-ml
        SOVERSION 1
        LIBRARY_OUTPUT_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR}/fake-nvml
    )
    target_include_directories(ostia_fake_nvml PRIVATE ${CMAKE_CURRENT_SOURCE_DIR})
    target_link_libraries(ostia_fake_nvml PRIVATE nlohmann_json::nlohmann_json)
    find_package(Python3 COMPONENTS Interpreter REQUIRED)
    add_test(
      NAME fabric.topo.capture_e2e
      COMMAND
        ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/e2e.py --binary
        $<TARGET_FILE:ostia-topo-capture> --nvml-dir $<TARGET_FILE_DIR:ostia_fake_nvml> --data
        ${PROJECT_SOURCE_DIR}/fabric/tests/topology/data
    )
    set_tests_properties(fabric.topo.capture_e2e PROPERTIES LABELS cpu)
  endif()
endif()
