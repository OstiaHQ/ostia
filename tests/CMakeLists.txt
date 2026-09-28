# Repository-wide tests: cmake -P tests of the build modules and configure tests of the
# top-level build and fixture projects (RFC-0001 Testing).

# cmake -P tests of pure build functions.
file(GLOB _script_tests RELATIVE ${CMAKE_CURRENT_SOURCE_DIR} cmake/*/test_*.cmake)
foreach(t IN LISTS _script_tests)
  get_filename_component(dir ${t} DIRECTORY)
  get_filename_component(name ${t} NAME_WE)
  get_filename_component(dir ${dir} NAME)
  add_test(NAME cmake.${dir}.${name} COMMAND ${CMAKE_COMMAND} -P ${CMAKE_CURRENT_SOURCE_DIR}/${t})
  set_tests_properties(cmake.${dir}.${name} PROPERTIES LABELS cpu)
endforeach()

# The preset's release list and the CMake default must agree (RFC-0001 §1.1).
add_test(
  NAME cmake.presets_match_default
  COMMAND
    ${CMAKE_COMMAND} -DPRESETS=${PROJECT_SOURCE_DIR}/CMakePresets.json
    "-DDEFAULT=80-real|90-real|100" -P ${CMAKE_CURRENT_SOURCE_DIR}/cmake/presets_match.cmake
)
set_tests_properties(cmake.presets_match_default PROPERTIES LABELS cpu)

# ostia_configure_test(<name> SOURCE <dir> EXPECT pass|fail [REGEX <re>] [ARGS <a>...])
function(ostia_configure_test name)
  cmake_parse_arguments(PARSE_ARGV 1 A "" "SOURCE;EXPECT;REGEX;RUN;FIXTURE" "ARGS")
  list(
    APPEND A_ARGS
    -DCMAKE_C_COMPILER=${CMAKE_C_COMPILER}
    -DCMAKE_CXX_COMPILER=${CMAKE_CXX_COMPILER}
    -DOSTIA_SOURCE_DIR=${PROJECT_SOURCE_DIR}
  )
  list(JOIN A_ARGS "|" args)
  add_test(
    NAME ${name}
    COMMAND
      ${CMAKE_COMMAND} -DSRC=${A_SOURCE} -DBIN=${CMAKE_CURRENT_BINARY_DIR}/cfg/${name}
      -DEXPECT=${A_EXPECT} "-DREGEX=${A_REGEX}" "-DARGS=${args}" "-DRUN=${A_RUN}" -P
      ${CMAKE_CURRENT_SOURCE_DIR}/cmake/expect_configure.cmake
  )
  set_tests_properties(${name} PROPERTIES LABELS cpu)
  if(A_FIXTURE)
    set_tests_properties(${name} PROPERTIES FIXTURES_REQUIRED ${A_FIXTURE})
  endif()
endfunction()

if(NOT CMAKE_CUDA_COMPILER_LOADED)
  ostia_configure_test(
    configure.cuda_on_without_toolkit
    SOURCE ${PROJECT_SOURCE_DIR}
    EXPECT fail
    REGEX "required: CUDA >= 12\\.8 \\(nvcc\\).*see: RFC-0001 §3\\.4"
    ARGS -DOSTIA_ENABLE_CUDA=ON -DOSTIA_BUILD_TESTS=OFF -DOSTIA_NVCC=OSTIA_NVCC-NOTFOUND
  )
endif()

# Layering enforcement, parts 1 and 2 (RFC-0001 §3.3): fixture projects that must fail
# with the named message, and one legitimate transitive dependency that must pass.
set(_layering ${CMAKE_CURRENT_SOURCE_DIR}/cmake/layering)
ostia_configure_test(layering.upward_depends SOURCE ${_layering}/upward_depends EXPECT fail
  REGEX "DEPENDS runtime.*exchange may depend on: fabric, telemetry.*RFC-0001 §3\\.3"
)
ostia_configure_test(layering.upward_raw_link SOURCE ${_layering}/upward_raw_link EXPECT fail
  REGEX "ostia_telemetry LINK_LIBRARIES contains ostia::fabric.*telemetry may depend on: nothing"
)
ostia_configure_test(layering.upward_interface_link SOURCE ${_layering}/upward_interface_link
  EXPECT fail REGEX "ostia_telemetry INTERFACE_LINK_LIBRARIES contains ostia_fabric"
)
ostia_configure_test(layering.upward_genex_link SOURCE ${_layering}/upward_genex_link EXPECT fail
  REGEX "ostia_telemetry LINK_LIBRARIES contains [^\n]*ostia::fabric"
)
ostia_configure_test(layering.unknown_ostia_ref SOURCE ${_layering}/unknown_ostia_ref EXPECT fail
  REGEX "references ostia::fabirc, which is not a component"
)
ostia_configure_test(layering.disabled_dependency SOURCE ${_layering}/disabled_dependency
  EXPECT fail REGEX "needs ostia-telemetry, which is disabled.*RFC-0001 §3\\.3"
)
ostia_configure_test(layering.rank_mismatch SOURCE ${_layering}/rank_mismatch EXPECT fail
  REGEX "declares RANK 3.*write RANK 2"
)
ostia_configure_test(layering.unknown_component SOURCE ${_layering}/unknown_component EXPECT fail
  REGEX "unknown component 'storage'"
)
ostia_configure_test(layering.transitive_ok SOURCE ${_layering}/transitive_ok EXPECT pass)
ostia_configure_test(configure.telemetry_off SOURCE ${PROJECT_SOURCE_DIR} EXPECT fail
  REGEX "needs ostia-telemetry, which is disabled" ARGS -DOSTIA_BUILD_TELEMETRY=OFF -DOSTIA_BUILD_TESTS=OFF
)

# Part 2's resolved-graph check: an upward edge inside a generator expression. Needs
# Python 3.10+ (the container jobs without pixi may lack it; CI's lint job runs it too).
find_package(Python3 3.10 COMPONENTS Interpreter)
if(Python3_FOUND)
  add_test(
    NAME layering.graph_genex
    COMMAND
      ${CMAKE_COMMAND} -DSRC=${_layering}/graph_genex
      -DBIN=${CMAKE_CURRENT_BINARY_DIR}/cfg/graph_genex -DPYTHON=${Python3_EXECUTABLE}
      -DCHECK=${PROJECT_SOURCE_DIR}/tools/ci/check_graph.py -DOSTIA_SOURCE_DIR=${PROJECT_SOURCE_DIR}
      -P ${CMAKE_CURRENT_SOURCE_DIR}/cmake/graph_check.cmake
  )
  set_tests_properties(layering.graph_genex PROPERTIES LABELS cpu)
endif()

# Install and package config (RFC-0001 §3.1–§3.2): consumers of an installed tree.
set(_stage ${CMAKE_CURRENT_BINARY_DIR}/stage)
add_test(
  NAME install.stage
  COMMAND ${CMAKE_COMMAND} -E rm -rf ${_stage}
)
add_test(
  NAME install.stage_install
  COMMAND ${CMAKE_COMMAND} --install ${PROJECT_BINARY_DIR} --prefix ${_stage}
)
set_tests_properties(install.stage PROPERTIES FIXTURES_SETUP ostia_stage_clean LABELS cpu)
set_tests_properties(
  install.stage_install
  PROPERTIES FIXTURES_SETUP ostia_stage FIXTURES_REQUIRED ostia_stage_clean LABELS cpu
)
set(_consumer ${CMAKE_CURRENT_SOURCE_DIR}/cmake/find_package)
# A consumer of sanitized libraries links the sanitizer runtime too.
set(_consumer_flags "")
if(OSTIA_SANITIZE)
  set(_consumer_flags "-DCMAKE_EXE_LINKER_FLAGS=-fsanitize=${OSTIA_SANITIZE}")
endif()
ostia_configure_test(find_package.fabric SOURCE ${_consumer} EXPECT pass RUN consumer
  FIXTURE ostia_stage ARGS -DWANT=fabric -DCMAKE_PREFIX_PATH=${_stage} ${_consumer_flags}
)
ostia_configure_test(find_package.exchange SOURCE ${_consumer} EXPECT fail FIXTURE ostia_stage
  REGEX "ostia component 'exchange' is a placeholder.*the ostia-exchange RFC.*RFC-0001 §3\\.1"
  ARGS -DWANT=exchange -DCMAKE_PREFIX_PATH=${_stage}
)
ostia_configure_test(find_package.unknown SOURCE ${_consumer} EXPECT fail FIXTURE ostia_stage
  REGEX "unknown ostia component 'storage'" ARGS -DWANT=storage -DCMAKE_PREFIX_PATH=${_stage}
)
ostia_configure_test(find_package.none SOURCE ${_consumer} EXPECT pass FIXTURE ostia_stage
  ARGS -DWANT= -DCMAKE_PREFIX_PATH=${_stage}
)
add_test(
  NAME install.no_placeholders
  COMMAND
    ${CMAKE_COMMAND} -DSTAGE=${_stage} -P ${CMAKE_CURRENT_SOURCE_DIR}/cmake/install_check.cmake
)
set_tests_properties(install.no_placeholders PROPERTIES FIXTURES_REQUIRED ostia_stage LABELS cpu)
add_test(
  NAME install.fabric_disabled
  COMMAND
    ${CMAKE_COMMAND} -DSOURCE=${PROJECT_SOURCE_DIR}
    -DWORK=${CMAKE_CURRENT_BINARY_DIR}/cfg/fabric_disabled -DCONSUMER=${_consumer}
    -DCMAKE_C_COMPILER=${CMAKE_C_COMPILER} -DCMAKE_CXX_COMPILER=${CMAKE_CXX_COMPILER} -P
    ${CMAKE_CURRENT_SOURCE_DIR}/cmake/fabric_disabled.cmake
)
set_tests_properties(install.fabric_disabled PROPERTIES LABELS cpu)

# Every public header compiles on its own, as C++ (and as C for .h, CUDA when enabled).
add_subdirectory(headers)

# CUDA version checks with a stand-in nvcc: they run before enable_language(CUDA), so
# they need no toolkit (RFC-0001 §1.1–§1.2).
set(_fake_nvcc ${CMAKE_CURRENT_SOURCE_DIR}/cmake/fake_nvcc)
ostia_configure_test(configure.cuda_unlisted_minor SOURCE ${PROJECT_SOURCE_DIR} EXPECT fail
  REGEX "CUDA 12\\.9\\.41 has no row in the host-compiler table.*supported: CUDA 12\\.8, 13\\.0 or 13\\.4"
  ARGS -DOSTIA_ENABLE_CUDA=AUTO -DOSTIA_BUILD_TESTS=OFF -DOSTIA_NVCC=${_fake_nvcc}/nvcc-12.9
)
ostia_configure_test(configure.cuda_too_old SOURCE ${PROJECT_SOURCE_DIR} EXPECT fail
  REGEX "CUDA 12\\.4\\.131 at .* is too old.*required: CUDA >= 12\\.8"
  ARGS -DOSTIA_ENABLE_CUDA=AUTO -DOSTIA_BUILD_TESTS=OFF -DOSTIA_NVCC=${_fake_nvcc}/nvcc-12.4
)

# The host-compiler range check in the real top-level configure (RFC-0001 §1 Done when):
# registered where the compiler is outside CUDA 12.8's range (the gcc15 pixi environment).
string(REGEX MATCH "^[0-9]+" _cxx_major "${CMAKE_CXX_COMPILER_VERSION}")
if(
  (CMAKE_CXX_COMPILER_ID STREQUAL "GNU" AND _cxx_major GREATER 14)
  OR (CMAKE_CXX_COMPILER_ID STREQUAL "Clang" AND _cxx_major GREATER 19)
)
  ostia_configure_test(configure.cuda_host_compiler_out_of_range SOURCE ${PROJECT_SOURCE_DIR}
    EXPECT fail
    REGEX "is not supported by CUDA 12\\.8\\.93.*supported: ${CMAKE_CXX_COMPILER_ID} [0-9]+–[0-9]+"
    ARGS -DOSTIA_ENABLE_CUDA=ON -DOSTIA_BUILD_TESTS=OFF -DOSTIA_NVCC=${_fake_nvcc}/nvcc-12.8
  )
endif()

# Telemetry build levels (RFC-0001 §5).
ostia_configure_test(configure.telemetry_bad_level SOURCE ${PROJECT_SOURCE_DIR} EXPECT fail
  REGEX "OSTIA_TELEMETRY=verbose is not a telemetry level.*off, metrics, trace or debug.*RFC-0001 §5"
  ARGS -DOSTIA_TELEMETRY=verbose -DOSTIA_BUILD_TESTS=OFF
)
