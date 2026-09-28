# The error-message contract (RFC-0001, Failure handling): every message names the
# problem, the offending item, the rule that was broken, the exact fix and the RFC
# section. Each line starts with a space so CMake prints it as written, unwrapped.
include_guard(GLOBAL)
# Modules also run under `cmake -P`, where no project sets policies.
cmake_policy(VERSION 4.1)

get_filename_component(_ostia_root "${CMAKE_CURRENT_LIST_DIR}/.." ABSOLUTE)
set_property(GLOBAL PROPERTY OSTIA_SOURCE_ROOT "${_ostia_root}")

# ostia_relpath(<abs> <out>): <abs> relative to the repository root, for messages.
function(ostia_relpath abs out)
  get_property(root GLOBAL PROPERTY OSTIA_SOURCE_ROOT)
  file(RELATIVE_PATH rel "${root}" "${abs}")
  if(rel MATCHES "^\\.\\./")
    set(rel "${abs}") # outside the repository: keep the absolute path
  endif()
  set(${out} "${rel}" PARENT_SCOPE)
endfunction()

# ostia_fail(PROBLEM <s> [DETAILS <line>...] RULE <s> FIX <s> SEE <s>)
function(ostia_fail)
  cmake_parse_arguments(PARSE_ARGV 0 A "" "PROBLEM;RULE;FIX;SEE" "DETAILS")
  set(msg " error: ${A_PROBLEM}\n")
  foreach(line IN LISTS A_DETAILS)
    string(APPEND msg "   ${line}\n")
  endforeach()
  string(APPEND msg "   rule: ${A_RULE}\n   fix: ${A_FIX}\n   see: ${A_SEE}")
  message(FATAL_ERROR "${msg}")
endfunction()
