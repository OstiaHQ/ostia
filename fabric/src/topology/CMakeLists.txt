# The M0 topology model and fixture replay (RFC-0003 §6, §9): internal, host-only, built
# everywhere (RFC-0001 §3.4). Not installed; the fabric RFC's public model replaces it.
# Generated at configure time, not build time: `ostia-dev check macros` parses the sources
# from the compile database right after configure, before anything is built.
set(topology_schema_dir ${PROJECT_SOURCE_DIR}/fabric/tools/topo-capture/schemas)
set(topology_schema_inc)
foreach(
  name
  nvml
  nics
  links
  meta
  manifest
  pair
)
  set(schema_file ${topology_schema_dir}/${name}.schema.json)
  set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS ${schema_file})
  file(READ ${schema_file} body)
  string(FIND "${body}" ")OSTIA\"" clash)
  if(NOT clash EQUAL -1)
    message(FATAL_ERROR "${name}.schema.json contains the raw-string terminator )OSTIA\"")
  endif()
  string(APPEND topology_schema_inc "{\"${name}\", R\"OSTIA(${body})OSTIA\"},\n")
endforeach()
file(
  CONFIGURE
  OUTPUT ${CMAKE_CURRENT_BINARY_DIR}/schemas.inc
  CONTENT "${topology_schema_inc}"
  NEWLINE_STYLE UNIX
  @ONLY
)

add_library(
  ostia_fabric_topology
  STATIC
  builder.cpp
  fixture_source.cpp
  identity.cpp
  model.cpp
  schema.cpp
  sha256.cpp
)
set_target_properties(ostia_fabric_topology PROPERTIES POSITION_INDEPENDENT_CODE ON)
target_compile_features(ostia_fabric_topology PUBLIC cxx_std_20)
target_include_directories(ostia_fabric_topology PUBLIC ${PROJECT_SOURCE_DIR}/fabric/src)
target_include_directories(ostia_fabric_topology PRIVATE ${CMAKE_CURRENT_BINARY_DIR})
target_link_libraries(ostia_fabric_topology PUBLIC hwloc::hwloc nlohmann_json::nlohmann_json)
