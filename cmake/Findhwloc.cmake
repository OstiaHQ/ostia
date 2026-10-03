# hwloc ships no CMake package and pixi has no pkg-config, so this module finds the
# header and library directly (RFC-0001 §2.1: pixi provides, CMake finds).
include_guard(GLOBAL)
include(FindPackageHandleStandardArgs)

find_path(hwloc_INCLUDE_DIR hwloc.h)
find_library(hwloc_LIBRARY NAMES hwloc)
if(hwloc_INCLUDE_DIR AND EXISTS "${hwloc_INCLUDE_DIR}/hwloc/autogen/config.h")
  file(
    STRINGS "${hwloc_INCLUDE_DIR}/hwloc/autogen/config.h"
    _hwloc_version_line
    REGEX "^#define HWLOC_VERSION \"[0-9.]+"
  )
  string(REGEX MATCH "[0-9]+\\.[0-9]+(\\.[0-9]+)?" hwloc_VERSION "${_hwloc_version_line}")
endif()
find_package_handle_standard_args(
  hwloc
  REQUIRED_VARS hwloc_LIBRARY hwloc_INCLUDE_DIR hwloc_VERSION
  VERSION_VAR hwloc_VERSION
)
if(hwloc_FOUND AND NOT TARGET hwloc::hwloc)
  add_library(hwloc::hwloc UNKNOWN IMPORTED)
  set_target_properties(
    hwloc::hwloc
    PROPERTIES
      IMPORTED_LOCATION "${hwloc_LIBRARY}"
      INTERFACE_INCLUDE_DIRECTORIES "${hwloc_INCLUDE_DIR}"
  )
endif()
