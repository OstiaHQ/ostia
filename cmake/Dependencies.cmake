# Source dependencies fetched by CPM (RFC-0001 §2.1, §2.3). Every CPMAddPackage pins a
# tag and a full commit SHA (§2.4); `ostia-dev check cpm-pins` enforces the form below.
include_guard(GLOBAL)
cmake_policy(VERSION 4.1)
include(${CMAKE_CURRENT_LIST_DIR}/CPM.cmake)

# ostia_cpm_add(<CPMAddPackage arguments>): announces the pin, then fetches it, so a
# failed fetch names what it wanted (RFC-0001, Failure handling). Each pin appears once,
# in the form Renovate's regex manager updates (renovate.json).
macro(ostia_cpm_add)
  cmake_parse_arguments(_ostia_pin "" "NAME;VERSION;GIT_TAG" "" ${ARGN})
  message(
    STATUS
    "ostia: ${_ostia_pin_NAME} v${_ostia_pin_VERSION} @ ${_ostia_pin_GIT_TAG} (RFC-0001 §2.4); "
    "offline or failing? see docs/guides/building.md#offline, or -DCPM_${_ostia_pin_NAME}_SOURCE=<dir>"
  )
  CPMAddPackage(${ARGN})
endmacro()

function(ostia_dep_googletest)
  ostia_cpm_add(
    NAME googletest
    GITHUB_REPOSITORY
    google/googletest
    VERSION 1.18.0
    GIT_TAG 063de7e9578f82b369302001269680b4b1553359
    OPTIONS
    "INSTALL_GTEST OFF"
    "gtest_force_shared_crt ON"
  )
endfunction()

# One pinned nanobind for every extension (RFC-0001 §3.5). Python is found first, as
# nanobind requires; CPM's git fetch includes nanobind's submodules (ext/robin_map).
# A macro, so the Python_* results stay visible to the caller.
macro(ostia_dep_nanobind)
  find_package(Python 3.11 REQUIRED COMPONENTS Interpreter Development.Module)
  ostia_cpm_add(
    NAME nanobind
    GITHUB_REPOSITORY
    wjakob/nanobind
    VERSION 3.1.0
    GIT_TAG 68480a9e6883bb1bf65e1925423321e2a89d07f9
  )
endmacro()

# nlohmann/json for the topology model and fixtures (RFC-0003 §1). Header-only.
function(ostia_dep_nlohmann_json)
  ostia_cpm_add(
    NAME nlohmann_json
    GITHUB_REPOSITORY
    nlohmann/json
    VERSION 3.12.0
    GIT_TAG 55f93686c01528224f448c19128836e7df245f72
    OPTIONS
    "JSON_BuildTests OFF"
    "JSON_Install OFF"
  )
endfunction()

# hwloc from pixi or the distro (RFC-0001 §2.3), floor 2.4 (the oldest tier-2 distro,
# Rocky 9, whose XML import the goldens are checked against).
macro(ostia_dep_hwloc)
  find_package(hwloc 2.4 MODULE)
  if(NOT hwloc_FOUND)
    ostia_fail(
      PROBLEM "hwloc 2.4 or newer was not found (found: ${hwloc_VERSION})"
      DETAILS "the topology model replays hwloc XML on every platform (RFC-0001 §3.4)"
      RULE "ostia-fabric's topology target needs hwloc >= 2.4"
      FIX "use pixi (pixi install), or install libhwloc-dev (Debian/Ubuntu) or hwloc-devel (Rocky/RHEL, CRB repository)"
      SEE "RFC-0003 §9"
    )
  endif()
endmacro()

# CUDA benchmark dependencies (RFC-0001 §2.3, §6.1): CCCL from GitHub, and nvbench.
# nvbench has no C++ release tags; its python-X.Y.Z tags mark the whole repository.
function(ostia_dep_cccl)
  ostia_cpm_add(
    NAME CCCL
    GITHUB_REPOSITORY
    NVIDIA/cccl
    VERSION 3.4.2
    GIT_TAG 81a339a47dae98fa66b0779215bd04581d6efdbb
  )
endfunction()

function(ostia_dep_nvbench)
  ostia_cpm_add(
    NAME nvbench
    GITHUB_REPOSITORY
    NVIDIA/nvbench
    VERSION 0.3.0
    GIT_TAG deb95d3da687fb1e57ba65364210e354aa67198c
    OPTIONS
    "NVBench_ENABLE_EXAMPLES OFF"
    "NVBench_ENABLE_TESTING OFF"
    "NVBench_ENABLE_CUPTI OFF"
    "NVBench_ENABLE_NVML OFF"
  )
endfunction()

# A dependency that still declares cmake_minimum_required < 3.5 is configured with the
# policy minimum scoped to that one package (RFC-0001 §1.3), for example:
#   CPMAddPackage(NAME foo ... OPTIONS "CMAKE_POLICY_VERSION_MINIMUM 3.5")
# Re-check this for each new dependency.
