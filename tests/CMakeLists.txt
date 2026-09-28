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
  cmake_parse_arguments(PARSE_ARGV 1 A "" "SOURCE;EXPECT;REGEX" "ARGS")
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
      -DEXPECT=${A_EXPECT} "-DREGEX=${A_REGEX}" "-DARGS=${args}" -P
      ${CMAKE_CURRENT_SOURCE_DIR}/cmake/expect_configure.cmake
  )
  set_tests_properties(${name} PROPERTIES LABELS cpu)
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
