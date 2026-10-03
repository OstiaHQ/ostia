# The M0 topology model and fixture replay (RFC-0003 §6, §9): internal, host-only, built
# everywhere (RFC-0001 §3.4). Not installed; the fabric RFC's public model replaces it.
add_library(ostia_fabric_topology STATIC model.cpp)
set_target_properties(ostia_fabric_topology PROPERTIES POSITION_INDEPENDENT_CODE ON)
target_compile_features(ostia_fabric_topology PUBLIC cxx_std_20)
target_include_directories(ostia_fabric_topology PUBLIC ${PROJECT_SOURCE_DIR}/fabric/src)
target_link_libraries(ostia_fabric_topology PUBLIC hwloc::hwloc nlohmann_json::nlohmann_json)
