# The topology fuzz target (`ostia-dev fuzz`); built only with OSTIA_FUZZ, under the fuzz preset.
add_executable(ostia_fabric_topology_fuzz topology_json_fuzz.cpp)
target_link_libraries(ostia_fabric_topology_fuzz PRIVATE ostia_fabric_topology)
target_link_options(ostia_fabric_topology_fuzz PRIVATE -fsanitize=fuzzer)
set(fixture_dir ${PROJECT_SOURCE_DIR}/fabric/tests/fixtures/topology/synthetic)
target_compile_definitions(
  ostia_fabric_topology_fuzz
  PRIVATE OSTIA_FUZZ_BASE_FIXTURE="${fixture_dir}/multi-numa"
)

# Seeds: every synthetic fixture's JSON behind the selector the target reads ('0' nvml,
# '1' nics, '2' links). Regenerated on every configure, which `ostia-dev fuzz` runs first.
set(seed_dir ${CMAKE_CURRENT_BINARY_DIR}/seeds)
file(REMOVE_RECURSE ${seed_dir})
file(MAKE_DIRECTORY ${seed_dir})
set(selector_nvml 0)
set(selector_nics 1)
set(selector_links 2)
file(GLOB_RECURSE seed_sources CONFIGURE_DEPENDS ${fixture_dir}/*.json)
foreach(source IN LISTS seed_sources)
  get_filename_component(kind ${source} NAME_WE)
  if(NOT DEFINED selector_${kind})
    continue()
  endif()
  file(RELATIVE_PATH rel ${fixture_dir} ${source})
  string(REPLACE "/" "-" seed_name ${rel})
  file(READ ${source} body)
  file(WRITE ${seed_dir}/${seed_name} "${selector_${kind}}${body}")
endforeach()
