# The M0 topology model and fixture replay (RFC-0003 §6, §9): internal, host-only, built
# everywhere (RFC-0001 §3.4). Not installed; the fabric RFC's public model replaces it.
set(topology_schema_dir ${PROJECT_SOURCE_DIR}/fabric/tools/topo-capture/schemas)
set(topology_schema_files)
foreach(
  name
  nvml
  nics
  links
  meta
  manifest
  pair
)
  list(APPEND topology_schema_files ${topology_schema_dir}/${name}.schema.json)
endforeach()

# Embed the schemas at build time so the validator has no run-time file dependency.
add_custom_command(
  OUTPUT ${CMAKE_CURRENT_BINARY_DIR}/schemas.inc
  COMMAND
    ${CMAKE_COMMAND} -DSCHEMA_DIR=${topology_schema_dir}
    -DOUT=${CMAKE_CURRENT_BINARY_DIR}/schemas.inc -P ${CMAKE_CURRENT_SOURCE_DIR}/embed_schemas.cmake
  DEPENDS ${topology_schema_files} ${CMAKE_CURRENT_SOURCE_DIR}/embed_schemas.cmake
  COMMENT "Embedding the topology fixture schemas"
  VERBATIM
)

add_library(
  ostia_fabric_topology
  STATIC
  model.cpp
  schema.cpp
  sha256.cpp
  ${CMAKE_CURRENT_BINARY_DIR}/schemas.inc
)
set_target_properties(ostia_fabric_topology PROPERTIES POSITION_INDEPENDENT_CODE ON)
target_compile_features(ostia_fabric_topology PUBLIC cxx_std_20)
target_include_directories(ostia_fabric_topology PUBLIC ${PROJECT_SOURCE_DIR}/fabric/src)
target_include_directories(ostia_fabric_topology PRIVATE ${CMAKE_CURRENT_BINARY_DIR})
target_link_libraries(ostia_fabric_topology PUBLIC hwloc::hwloc nlohmann_json::nlohmann_json)
