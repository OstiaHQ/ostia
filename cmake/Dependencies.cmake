# Source dependencies fetched by CPM (RFC-0001 §2.1, §2.3). Every CPMAddPackage pins a
# tag and a full commit SHA (§2.4); tools/ci/check_cpm_pins.py enforces the form below.
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

# A dependency that still declares cmake_minimum_required < 3.5 is configured with the
# policy minimum scoped to that one package (RFC-0001 §1.3), for example:
#   CPMAddPackage(NAME foo ... OPTIONS "CMAKE_POLICY_VERSION_MINIMUM 3.5")
# Re-check this for each new dependency (nvbench and CCCL arrive in PR 5a).
