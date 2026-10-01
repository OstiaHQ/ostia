# Python extension for ostia-telemetry (RFC-0001 §3.5). It links the one installed native
# library and never builds or bundles it.
cmake_minimum_required(VERSION 4.1)
project(ostia_telemetry_python LANGUAGES C CXX)
include(${CMAKE_CURRENT_SOURCE_DIR}/../../cmake/OstiaMessages.cmake)
include(${CMAKE_CURRENT_SOURCE_DIR}/../../cmake/OstiaToolchain.cmake)
include(${CMAKE_CURRENT_SOURCE_DIR}/../../cmake/Dependencies.cmake)
ostia_strip_conda_rpath()

find_package(ostia 0.0 QUIET COMPONENTS telemetry)
if(NOT ostia_FOUND)
  ostia_fail(
    PROBLEM "native ostia-telemetry is not installed in $ENV{CONDA_PREFIX}"
    DETAILS "${ostia_NOT_FOUND_MESSAGE}"
    RULE "Python editables link the native libraries installed into the pixi environment"
    FIX "run pixi run ostia-dev py-dev (not pip install -e directly)"
    SEE "RFC-0001 §3.5"
  )
endif()
ostia_dep_nanobind()

nanobind_add_module(_native NB_STATIC src/_native.cpp)
target_link_libraries(_native PRIVATE ostia::telemetry ${CMAKE_DL_LIBS})
# The flavour check at import (RFC-0001 §5): the level this installed stack was built with,
# and the build-tree-only check helper from telemetry/instrument/.
target_compile_definitions(_native PRIVATE OSTIA_COMPILED_TELEMETRY_LEVEL=${ostia_TELEMETRY_LEVEL})
target_include_directories(_native PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/../../telemetry/instrument)

# Relative RPATH from site-packages/ostia/telemetry/ to the prefix's lib/, where the one
# libostia-* lives (RFC-0001 §3.5).
cmake_path(GET ostia_DIR PARENT_PATH _cmake_dir)
cmake_path(GET _cmake_dir PARENT_PATH _ostia_libdir)
file(RELATIVE_PATH _rel "${Python_SITEARCH}/ostia/telemetry" "${_ostia_libdir}")
if(APPLE)
  set(_origin "@loader_path")
else()
  set(_origin "$ORIGIN")
endif()
# Linked directly with the install RPATH: a build-tree RPATH would add the prefix's lib/
# (the imported library's directory), which survives install on Linux.
set_target_properties(
  _native
  PROPERTIES
    INSTALL_RPATH "${_origin}/${_rel}"
    BUILD_WITH_INSTALL_RPATH ON
    INSTALL_REMOVE_ENVIRONMENT_RPATH ON
)
install(TARGETS _native LIBRARY DESTINATION ostia/telemetry)
if(NOT APPLE)
  # conda-forge GCC's specs append -rpath $CONDA_PREFIX/lib to every link. Rewrite the
  # installed extension's RPATH to the one relative entry (RFC-0001 §3.5).
  install(
    CODE
      "file(RPATH_SET FILE \"\${CMAKE_INSTALL_PREFIX}/ostia/telemetry/$<TARGET_FILE_NAME:_native>\" NEW_RPATH \"${_origin}/${_rel}\")"
  )
endif()
