# Ostia top-level build (RFC-0001 §1–§3). Start with docs/guides/building.md.

# A contract error before cmake_minimum_required, which would print no fix.
if(CMAKE_VERSION VERSION_LESS 4.1)
  include(${CMAKE_CURRENT_LIST_DIR}/cmake/OstiaMessages.cmake)
  ostia_fail(
    PROBLEM "CMake ${CMAKE_VERSION} at ${CMAKE_COMMAND} is too old"
    RULE "Ostia needs CMake 4.1 or newer"
    FIX
      "run through pixi (pixi run ostia-dev build), or point your IDE at .pixi/envs/default/bin/cmake"
    SEE "RFC-0001 §1.3"
  )
endif()
cmake_minimum_required(VERSION 4.1)
# Must precede project() to take effect (RFC-0001 §1.2).
set(CMAKE_OSX_DEPLOYMENT_TARGET 14.0 CACHE STRING "Minimum macOS version (RFC-0001 §1.2)")
project(ostia VERSION 0.0.0 LANGUAGES C CXX)

list(APPEND CMAKE_MODULE_PATH "${PROJECT_SOURCE_DIR}/cmake")
# conda prefixes (the pixi environment) always use lib/, which py-dev and the Python
# extensions' relative RPATH rely on; GNUInstallDirs would pick lib64 on RHEL hosts.
if(DEFINED ENV{CONDA_PREFIX} AND NOT DEFINED CMAKE_INSTALL_LIBDIR)
  set(CMAKE_INSTALL_LIBDIR lib CACHE PATH "Object code libraries (lib in conda prefixes)")
endif()
include(OstiaMessages)
include(OstiaToolchain)
include(OstiaComponent)
include(OstiaSummary)
include(OstiaTelemetry)

# conda's activation puts an rpath to $CONDA_PREFIX/lib ahead of the build tree, so
# tests would load libraries that `pixi run ostia-dev py-dev` installed there, not the fresh build.
# Move it behind the build tree: it is still needed for the environment's C++ runtime.
ostia_strip_conda_rpath()
if(DEFINED ENV{CONDA_PREFIX})
  # CMAKE_BUILD_RPATH entries come before CMake's automatic ones, so list every
  # component's build directory first (they are fixed: <build>/<component>).
  ostia_layering_components(_rpath_components)
  foreach(c IN LISTS _rpath_components)
    list(APPEND CMAKE_BUILD_RPATH "${PROJECT_BINARY_DIR}/${c}")
  endforeach()
  list(APPEND CMAKE_BUILD_RPATH "$ENV{CONDA_PREFIX}/lib")
endif()

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)
# Ostia uses no C++20 modules; scanning would add GCC-only flags that clang-tidy rejects.
set(CMAKE_CXX_SCAN_FOR_MODULES OFF)

# ccache when found; a launcher the user set always wins.
option(OSTIA_USE_CCACHE "Use ccache when it is found" ON)
set(_ccache "off")
if(OSTIA_USE_CCACHE AND NOT DEFINED CMAKE_CXX_COMPILER_LAUNCHER)
  find_program(OSTIA_CCACHE ccache)
  if(OSTIA_CCACHE)
    foreach(lang C CXX CUDA)
      set(CMAKE_${lang}_COMPILER_LAUNCHER "${OSTIA_CCACHE}")
    endforeach()
    set(_ccache "${OSTIA_CCACHE}")
  else()
    set(_ccache "not found")
  endif()
elseif(DEFINED CMAKE_CXX_COMPILER_LAUNCHER)
  set(_ccache "user launcher ${CMAKE_CXX_COMPILER_LAUNCHER}")
endif()
ostia_summary_set(ccache "${_ccache}")

# Host compiler floors (RFC-0001 §1.2).
ostia_host_compiler_range(
  CUDA_VERSION ""
  COMPILER_ID ${CMAKE_CXX_COMPILER_ID}
  COMPILER_VERSION ${CMAKE_CXX_COMPILER_VERSION}
  OUT_OK _host_ok
  OUT_RANGE _host_range
)
if(NOT _host_ok)
  ostia_fail(
    PROBLEM "unsupported host compiler ${CMAKE_CXX_COMPILER_ID} ${CMAKE_CXX_COMPILER_VERSION}"
    DETAILS "compiler: ${CMAKE_CXX_COMPILER}" "supported: ${_host_range}"
    RULE "C++20 as supported by GCC 11 and nvcc 12.8 defines the floor"
    FIX "use pixi run ostia-dev build, or a listed compiler"
    SEE "RFC-0001 §1.2"
  )
endif()
if(CMAKE_CXX_COMPILER_ID STREQUAL "AppleClang")
  message(
    WARNING
    " AppleClang is best effort (tier 3); RFC-0001 §1.2 uses conda-forge Clang 19 on macOS.\n"
    " fix: build through pixi (pixi run ostia-dev build)"
  )
endif()
ostia_summary_set(
  compiler
  "${CMAKE_CXX_COMPILER_ID} ${CMAKE_CXX_COMPILER_VERSION} (${CMAKE_CXX_COMPILER})"
)

# Sanitizers for the CI sanitizer jobs (RFC-0001 §4.1): OSTIA_SANITIZE=address,undefined
# or thread. Applied to everything built here, dependencies included.
set(OSTIA_SANITIZE "" CACHE STRING "Sanitizers to build with, e.g. address,undefined or thread")
if(OSTIA_SANITIZE)
  add_compile_options(
    -fsanitize=${OSTIA_SANITIZE}
    -fno-omit-frame-pointer
    -fno-sanitize-recover=all
  )
  add_link_options(-fsanitize=${OSTIA_SANITIZE})
endif()

# Coverage instrumentation for the fuzz preset (`ostia-dev fuzz`). libFuzzer ships with Clang
# only; each fuzz target adds -fsanitize=fuzzer itself, which supplies main().
option(OSTIA_FUZZ "Build the fuzz targets (Clang)" OFF)
if(OSTIA_FUZZ)
  if(NOT CMAKE_CXX_COMPILER_ID STREQUAL "Clang")
    message(
      FATAL_ERROR
      "OSTIA_FUZZ needs Clang (libFuzzer); this build uses ${CMAKE_CXX_COMPILER_ID}.\n"
      " fix: pixi run -e clang ostia-dev fuzz"
    )
  endif()
  add_compile_options(-fsanitize=fuzzer-no-link)
endif()

# Clang source-based coverage for the coverage preset (`ostia-dev coverage`). Applied to
# everything built here; the report filters to Ostia's own sources.
option(OSTIA_COVERAGE "Build with Clang source-based coverage" OFF)
if(OSTIA_COVERAGE)
  if(NOT CMAKE_CXX_COMPILER_ID STREQUAL "Clang")
    message(
      FATAL_ERROR
      "OSTIA_COVERAGE needs Clang (source-based coverage); this build uses ${CMAKE_CXX_COMPILER_ID}.\n"
      " fix: pixi run -e clang ostia-dev coverage"
    )
  endif()
  add_compile_options(-fprofile-instr-generate -fcoverage-mapping)
  add_link_options(-fprofile-instr-generate)
endif()

# CUDA (RFC-0001 §1.1, §3.4): find nvcc, check its version and the host compiler range,
# choose the architectures, and only then enable the language.
set(
  OSTIA_CUDA_ARCH_MODE
  release
  CACHE STRING
  "CUDA architecture policy: dev or release (RFC-0001 §1.1)"
)
set_property(CACHE OSTIA_CUDA_ARCH_MODE PROPERTY STRINGS dev release)
set(
  OSTIA_CUDA_ARCHITECTURES_RELEASE
  "80-real;90-real;100"
  CACHE STRING
  "Release CUDA architectures: SASS for sm_80/90/100, PTX for sm_100 (RFC-0001 §1.1)"
)
set(_cuda_default AUTO)
if(DEFINED ENV{OSTIA_ENABLE_CUDA})
  set(_cuda_default $ENV{OSTIA_ENABLE_CUDA}) # pixi: OFF in default, ON in cuda-*
endif()
set(
  OSTIA_ENABLE_CUDA
  ${_cuda_default}
  CACHE STRING
  "Build CUDA code: AUTO, ON or OFF (RFC-0001 §3.4)"
)
set_property(CACHE OSTIA_ENABLE_CUDA PROPERTY STRINGS AUTO ON OFF)

set(_cuda_state "off (OSTIA_ENABLE_CUDA=OFF)")
set(_cuda_toolkit "none")
set(_cuda_arch "n/a")
if(NOT OSTIA_ENABLE_CUDA STREQUAL "OFF")
  find_program(OSTIA_NVCC nvcc HINTS ENV CUDA_HOME ENV CUDA_PATH PATH_SUFFIXES bin)
  if(NOT OSTIA_NVCC)
    if(OSTIA_ENABLE_CUDA STREQUAL "ON")
      ostia_fail(
        PROBLEM "OSTIA_ENABLE_CUDA=ON but no CUDA compiler was found"
        DETAILS "detected: none" "required: CUDA >= 12.8 (nvcc)"
        RULE "CUDA must be present when OSTIA_ENABLE_CUDA=ON"
        FIX
          "install CUDA >= 12.8 (pixi run -e cuda-12 ostia-dev build), or use -DOSTIA_ENABLE_CUDA=AUTO"
        SEE "RFC-0001 §3.4"
      )
    endif()
    set(_cuda_state "off (AUTO: nvcc not found)")
  else()
    execute_process(COMMAND ${OSTIA_NVCC} --version OUTPUT_VARIABLE _nvcc_text ERROR_QUIET)
    ostia_parse_nvcc_version("${_nvcc_text}" _cuda_version)
    if(_cuda_version STREQUAL "NOTFOUND" OR _cuda_version VERSION_LESS 12.8)
      ostia_fail(
        PROBLEM "CUDA ${_cuda_version} at ${OSTIA_NVCC} is too old"
        DETAILS "detected: ${_cuda_version}" "required: CUDA >= 12.8 (nvcc)"
        RULE "CUDA 12.8 is the minimum"
        FIX "use pixi run -e cuda-12 ostia-dev build, or -DOSTIA_ENABLE_CUDA=OFF"
        SEE "RFC-0001 §1.1"
      )
    endif()
    ostia_host_compiler_range(
      CUDA_VERSION ${_cuda_version}
      COMPILER_ID ${CMAKE_CXX_COMPILER_ID}
      COMPILER_VERSION ${CMAKE_CXX_COMPILER_VERSION}
      OUT_OK _cuda_host_ok
      OUT_RANGE _cuda_host_range
    )
    if(_cuda_host_ok STREQUAL "UNKNOWN_CUDA")
      ostia_fail(
        PROBLEM "CUDA ${_cuda_version} has no row in the host-compiler table"
        DETAILS "nvcc: ${OSTIA_NVCC}" "supported: ${_cuda_host_range}"
        RULE "each CUDA minor's host-compiler range comes from its installation guide"
        FIX
          "use the cuda-12 or cuda-13 pixi environment, -DOSTIA_ENABLE_CUDA=OFF, or add a row to cmake/OstiaToolchain.cmake in a PR that cites the guide"
        SEE "RFC-0001 §1.2"
      )
    elseif(NOT _cuda_host_ok STREQUAL "TRUE")
      ostia_fail(
        PROBLEM
          "host compiler ${CMAKE_CXX_COMPILER_ID} ${CMAKE_CXX_COMPILER_VERSION} is not supported by CUDA ${_cuda_version}"
        DETAILS
          "compiler: ${CMAKE_CXX_COMPILER}"
          "nvcc: ${OSTIA_NVCC}"
          "supported: ${_cuda_host_range}"
        RULE "nvcc accepts only the host compilers its installation guide lists"
        FIX
          "use the matching pixi environment (cuda-12: GCC 11, cuda-13: GCC 14), or -DOSTIA_ENABLE_CUDA=OFF"
        SEE "RFC-0001 §1.2"
      )
    endif()
    set(CMAKE_CUDA_COMPILER "${OSTIA_NVCC}")
    set(CMAKE_CUDA_HOST_COMPILER "${CMAKE_CXX_COMPILER}")
    ostia_detect_gpus(_gpu_caps)
    ostia_cuda_arch_resolve(
      CACHE "${CMAKE_CUDA_ARCHITECTURES}"
      PREVIOUS "${_OSTIA_CUDA_ARCH_CHOSEN}"
      ENV "$ENV{CUDAARCHS}"
      MODE ${OSTIA_CUDA_ARCH_MODE}
      GPU_CAPS ${_gpu_caps}
      RELEASE_LIST ${OSTIA_CUDA_ARCHITECTURES_RELEASE}
      OUT_ARCH _arch
      OUT_REASON _arch_reason
      OUT_PREVIOUS _arch_previous
    )
    if(_arch STREQUAL "INVALID")
      ostia_fail(
        PROBLEM "${_arch_reason}"
        RULE "OSTIA_CUDA_ARCH_MODE is dev or release"
        FIX "use a preset (cmake --preset dev), or -DOSTIA_CUDA_ARCH_MODE=release"
        SEE "RFC-0001 §1.1"
      )
    endif()
    set(CMAKE_CUDA_ARCHITECTURES "${_arch}" CACHE STRING "CUDA architectures" FORCE)
    set(_OSTIA_CUDA_ARCH_CHOSEN "${_arch_previous}" CACHE INTERNAL "")
    enable_language(CUDA)
    set(CMAKE_CUDA_STANDARD 20)
    set(CMAKE_CUDA_STANDARD_REQUIRED ON)
    set(_cuda_state "on (OSTIA_ENABLE_CUDA=${OSTIA_ENABLE_CUDA})")
    set(_cuda_toolkit "${_cuda_version} (${OSTIA_NVCC})")
    set(_cuda_arch "${_arch} (${_arch_reason})")
  endif()
endif()
ostia_summary_set(cuda "${_cuda_state}")
ostia_summary_set(cuda_toolkit "${_cuda_toolkit}")
ostia_summary_set(architectures "${_cuda_arch}")
# Telemetry build level (RFC-0001 §5). Release presets use metrics, the dev preset debug.
set(
  OSTIA_TELEMETRY
  metrics
  CACHE STRING
  "Telemetry build level: off, metrics, trace or debug (RFC-0001 §5)"
)
set_property(
  CACHE OSTIA_TELEMETRY
  PROPERTY STRINGS off metrics trace debug
)
ostia_telemetry_level("${OSTIA_TELEMETRY}" OSTIA_TELEMETRY_LEVEL)
if(OSTIA_TELEMETRY_LEVEL STREQUAL "NOTFOUND")
  ostia_fail(
    PROBLEM "OSTIA_TELEMETRY=${OSTIA_TELEMETRY} is not a telemetry level"
    DETAILS "levels: off, metrics, trace or debug"
    RULE "the telemetry level is one of the four build levels"
    FIX "configure with -DOSTIA_TELEMETRY=metrics, or use a level-* preset"
    SEE "RFC-0001 §5"
  )
endif()
ostia_summary_set(telemetry_level "${OSTIA_TELEMETRY} (${OSTIA_TELEMETRY_LEVEL})")

option(OSTIA_BUILD_TESTS "Build the tests" ${PROJECT_IS_TOP_LEVEL})
option(OSTIA_BUILD_BENCH "Build the benchmarks (with CUDA; fetches nvbench and CCCL)" OFF)
option(OSTIA_BUILD_TOOLS "Build the developer tools (ostia-topo; ostia-topo-capture on Linux)" ON)
include(Dependencies)
set(_deps "")
ostia_dep_hwloc()
ostia_dep_nlohmann_json()
list(APPEND _deps "hwloc ${hwloc_VERSION}" "nlohmann_json 3.12.0 (CPM)")
if(OSTIA_BUILD_TESTS)
  enable_testing()
  ostia_dep_googletest()
  list(APPEND _deps "googletest 1.18.0 (CPM)")
endif()
ostia_summary_set(tools "${OSTIA_BUILD_TOOLS}")

# Components in rank order (RFC-0001 §3.1).
ostia_layering_components(_all_components)
foreach(c IN LISTS _all_components)
  string(TOUPPER ${c} C)
  option(OSTIA_BUILD_${C} "Build ostia-${c}" ON)
  if(OSTIA_BUILD_${C})
    add_subdirectory(${c})
  endif()
endforeach()
ostia_check_layering()

if(OSTIA_BUILD_TESTS)
  add_subdirectory(tests)
endif()
include(OstiaWarnings)
ostia_apply_warnings(${CMAKE_CURRENT_SOURCE_DIR})

# Package config (RFC-0001 §3.1–§3.2). A component is real unless it declared itself a
# placeholder; a real component that was disabled simply has no targets file.
include(CMakePackageConfigHelpers)
get_property(OSTIA_PLACEHOLDER_COMPONENTS GLOBAL PROPERTY OSTIA_PLACEHOLDERS)
ostia_layering_components(OSTIA_REAL_COMPONENTS)
if(OSTIA_PLACEHOLDER_COMPONENTS)
  list(REMOVE_ITEM OSTIA_REAL_COMPONENTS ${OSTIA_PLACEHOLDER_COMPONENTS})
endif()
set(OSTIA_CONFIG_DEPS "")
foreach(c IN LISTS OSTIA_REAL_COMPONENTS)
  get_property(_d GLOBAL PROPERTY OSTIA_DEPENDS_${c})
  string(APPEND OSTIA_CONFIG_DEPS "set(_ostia_deps_${c} \"${_d}\")\n")
endforeach()
set(OSTIA_CONFIG_RFCS "")
foreach(c IN LISTS OSTIA_PLACEHOLDER_COMPONENTS)
  ostia_layering_rfc(${c} _rfc)
  string(APPEND OSTIA_CONFIG_RFCS "set(_ostia_rfc_${c} \"${_rfc}\")\n")
endforeach()
configure_package_config_file(
  cmake/ostiaConfig.cmake.in
  ${PROJECT_BINARY_DIR}/ostiaConfig.cmake
  INSTALL_DESTINATION ${CMAKE_INSTALL_LIBDIR}/cmake/ostia
)
write_basic_package_version_file(
  ${PROJECT_BINARY_DIR}/ostiaConfigVersion.cmake
  COMPATIBILITY SameMinorVersion
)
install(
  FILES ${PROJECT_BINARY_DIR}/ostiaConfig.cmake ${PROJECT_BINARY_DIR}/ostiaConfigVersion.cmake
  DESTINATION ${CMAKE_INSTALL_LIBDIR}/cmake/ostia
)

get_property(_components GLOBAL PROPERTY OSTIA_COMPONENTS)
get_property(_placeholders GLOBAL PROPERTY OSTIA_PLACEHOLDERS)
set(_component_text "")
foreach(c IN LISTS _components)
  if(c IN_LIST _placeholders)
    list(APPEND _component_text "${c} (placeholder)")
  else()
    list(APPEND _component_text "${c}")
  endif()
endforeach()
list(JOIN _component_text ", " _component_text)
ostia_summary_set(components "${_component_text}")
list(JOIN _deps ", " _deps)
ostia_summary_set(dependencies "${_deps}")
ostia_print_summary()
