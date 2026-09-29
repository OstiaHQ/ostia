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

# ostia_telemetry_catalog(<component> <catalog.toml>): generates the component's
# storage-free handles into <build>/<component>/catalog/ostia/<component>/telemetry_catalog.hpp
# at configure time (RFC-0002 §1, RFC-0001 §5) and adds that directory as a PRIVATE
# include. The header is regenerated when the catalog or the generator changes.
function(ostia_telemetry_catalog component catalog)
  find_package(Python3 3.11 REQUIRED COMPONENTS Interpreter)
  get_filename_component(catalog "${catalog}" ABSOLUTE)
  get_property(root GLOBAL PROPERTY OSTIA_SOURCE_ROOT)
  set(generator ${root}/telemetry/tools/gen_catalog.py)
  set(dir ${CMAKE_CURRENT_BINARY_DIR}/catalog)
  ostia_relpath("${catalog}" shown)
  execute_process(
    COMMAND
      ${Python3_EXECUTABLE} ${generator} --component ${component} --catalog ${catalog} --out
      ${dir}/ostia/${component}/telemetry_catalog.hpp --source ${shown}
    RESULT_VARIABLE rc
    ERROR_VARIABLE err
  )
  if(NOT rc EQUAL 0)
    message(FATAL_ERROR " ${err}")
  endif()
  set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS ${catalog} ${generator})
  target_include_directories(ostia_${component} PRIVATE ${dir})
  set_target_properties(ostia_${component} PROPERTIES OSTIA_CATALOG_DIR ${dir})
endfunction()
