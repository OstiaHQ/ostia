# Compiler warnings for Ostia's own targets (OpenSSF Best Practices: warnings, warnings_strict).
include_guard(GLOBAL)
cmake_policy(VERSION 4.1)

# Off for local builds so a newer or tier-3 compiler can't break them; GitHub Actions sets CI.
if(DEFINED ENV{CI})
  set(_ostia_werror_default ON)
else()
  set(_ostia_werror_default OFF)
endif()
option(
  OSTIA_WARNINGS_AS_ERRORS
  "Treat C/C++ compiler warnings in Ostia targets as errors"
  ${_ostia_werror_default}
)

# ostia_apply_warnings(<dir>): adds the warning flags to every ostia* target defined in
# <dir> and below. It runs after every directory is added, and matches on the name because
# CPM dependencies (googletest, nvbench) are built inside our directories and must keep
# their own flags. CUDA sources are left alone: nvcc forwards host flags differently.
function(ostia_apply_warnings dir)
  get_property(targets DIRECTORY ${dir} PROPERTY BUILDSYSTEM_TARGETS)
  foreach(t IN LISTS targets)
    get_target_property(type ${t} TYPE)
    if(type STREQUAL "INTERFACE_LIBRARY" OR NOT t MATCHES "^ostia")
      continue()
    endif()
    target_compile_options(
      ${t}
      PRIVATE
        $<$<COMPILE_LANGUAGE:C,CXX>:-Wall;-Wextra;-Wpedantic>
        $<$<AND:$<COMPILE_LANGUAGE:C,CXX>,$<BOOL:${OSTIA_WARNINGS_AS_ERRORS}>>:-Werror>
    )
  endforeach()
  get_property(subdirs DIRECTORY ${dir} PROPERTY SUBDIRECTORIES)
  foreach(d IN LISTS subdirs)
    ostia_apply_warnings(${d})
  endforeach()
endfunction()
