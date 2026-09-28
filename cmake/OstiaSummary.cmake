# Configure summary (RFC-0001, Failure handling): printed at configure time and written
# to <build>/ostia-summary.txt, which `pixi run doctor` reads. Keys are fixed.
include_guard(GLOBAL)
cmake_policy(VERSION 4.1)

# ostia_summary_set(<key> <value>): record one fact for the summary.
function(ostia_summary_set key value)
  set_property(GLOBAL PROPERTY OSTIA_SUMMARY_${key} "${value}")
endfunction()

function(ostia_print_summary)
  set(keys compiler cuda cuda_toolkit architectures telemetry_level components dependencies ccache)
  set(text "")
  message(STATUS "Ostia configuration (see docs/guides/building.md):")
  foreach(key IN LISTS keys)
    get_property(value GLOBAL PROPERTY OSTIA_SUMMARY_${key})
    if("${value}" STREQUAL "")
      set(value "none")
    endif()
    message(STATUS "  ${key}: ${value}")
    string(APPEND text "${key}: ${value}\n")
  endforeach()
  file(WRITE "${CMAKE_BINARY_DIR}/ostia-summary.txt" "${text}")
endfunction()
