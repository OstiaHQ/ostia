# Telemetry build levels (RFC-0001 §5): OSTIA_TELEMETRY=off|metrics|trace|debug selects
# what the instrumentation macros compile to. The runtime is RFC-0002.
include_guard(GLOBAL)
cmake_policy(VERSION 4.1)
include(${CMAKE_CURRENT_LIST_DIR}/OstiaMessages.cmake)

set(_OSTIA_TELEMETRY_LEVELS off metrics trace debug)

# ostia_telemetry_level(<name> <out>): 0..3 for a level name, or NOTFOUND.
function(ostia_telemetry_level name out)
  list(FIND _OSTIA_TELEMETRY_LEVELS "${name}" index)
  if(index EQUAL -1)
    set(index NOTFOUND)
  endif()
  set(${out} "${index}" PARENT_SCOPE)
endfunction()
