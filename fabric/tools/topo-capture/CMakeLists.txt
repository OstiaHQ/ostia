# Capture core (RFC-0003 §1-§4): host-only so the scrub, leak check and manifest are
# tested on every platform; only the live sources and main are Linux-only.
add_library(ostia_topo_capture_core STATIC core/diagnostics.cpp)
set_target_properties(ostia_topo_capture_core PROPERTIES POSITION_INDEPENDENT_CODE ON)
target_include_directories(ostia_topo_capture_core PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
target_link_libraries(ostia_topo_capture_core PUBLIC ostia_fabric_topology)
target_compile_definitions(ostia_topo_capture_core PUBLIC OSTIA_TOOL_VERSION="${PROJECT_VERSION}")

if(CMAKE_SYSTEM_NAME STREQUAL "Linux")
  add_executable(ostia-topo-capture main.cpp)
  target_link_libraries(
    ostia-topo-capture
    PRIVATE ostia_topo_capture_core ibverbs::headers ${CMAKE_DL_LIBS}
  )
endif()
