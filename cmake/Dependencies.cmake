# Source dependencies fetched by CPM (RFC-0001 §2.1, §2.3). Every CPMAddPackage pins a
# tag and a full commit SHA (§2.4); tools/ci/check_cpm_pins.py enforces the form below.
include_guard(GLOBAL)
cmake_policy(VERSION 4.1)
include(${CMAKE_CURRENT_LIST_DIR}/CPM.cmake)

# Each call announces its pin first, so a failed fetch names it (RFC-0001, Failure handling).
macro(_ostia_announce name tag sha)
  message(
    STATUS
    "ostia: ${name} ${tag} @ ${sha} (RFC-0001 §2.4); offline or failing? "
    "see docs/guides/building.md#offline, or -DCPM_${name}_SOURCE=<dir>"
  )
endmacro()

function(ostia_dep_googletest)
  _ostia_announce(googletest v1.18.0 063de7e9578f82b369302001269680b4b1553359)
  CPMAddPackage(
    NAME googletest
    GITHUB_REPOSITORY google/googletest
    VERSION 1.18.0
    GIT_TAG
      063de7e9578f82b369302001269680b4b1553359 # v1.18.0
    OPTIONS "INSTALL_GTEST OFF" "gtest_force_shared_crt ON"
  )
endfunction()

# One pinned nanobind for every extension (RFC-0001 §3.5). Python is found first, as
# nanobind requires; CPM's git fetch includes nanobind's submodules (ext/robin_map).
# A macro, so the Python_* results stay visible to the caller.
macro(ostia_dep_nanobind)
  find_package(Python 3.11 REQUIRED COMPONENTS Interpreter Development.Module)
  _ostia_announce(nanobind v3.1.0 68480a9e6883bb1bf65e1925423321e2a89d07f9)
  CPMAddPackage(
    NAME nanobind
    GITHUB_REPOSITORY wjakob/nanobind
    VERSION 3.1.0
    GIT_TAG
      68480a9e6883bb1bf65e1925423321e2a89d07f9 # v3.1.0
  )
endmacro()

# A dependency that still declares cmake_minimum_required < 3.5 is configured with the
# policy minimum scoped to that one package (RFC-0001 §1.3), for example:
#   CPMAddPackage(NAME foo ... OPTIONS "CMAKE_POLICY_VERSION_MINIMUM 3.5")
# Re-check this for each new dependency (nvbench and CCCL arrive in PR 5a).
