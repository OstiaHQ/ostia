# cmake -P test: ostia_fail output is never re-wrapped by CMake (error-message contract).
set(long_problem "")
foreach(i RANGE 1 20)
  string(APPEND long_problem "abcdefghi_")
endforeach()
set(long_path "")
foreach(i RANGE 1 15)
  string(APPEND long_path "/directory")
endforeach()
set(helper "${CMAKE_CURRENT_BINARY_DIR}/ostia_wrap_helper.cmake")
file(
  WRITE "${helper}"
  "include(\"${CMAKE_CURRENT_LIST_DIR}/../../../cmake/OstiaMessages.cmake\")\n"
  "ostia_fail(PROBLEM \"${long_problem}\" DETAILS \"see ${long_path}/file.cpp:12\" "
  "RULE \"a rule\" FIX \"a fix\" SEE \"RFC-0001 §3.3\")\n"
)
execute_process(COMMAND ${CMAKE_COMMAND} -P "${helper}" RESULT_VARIABLE rc ERROR_VARIABLE err)
if(rc EQUAL 0)
  message(FATAL_ERROR "ostia_fail did not fail")
endif()
foreach(
  want
  " error: ${long_problem}\n"
  "   see ${long_path}/file.cpp:12\n"
  "   rule: a rule\n"
  "   fix: a fix\n"
  "   see: RFC-0001 §3.3"
)
  string(FIND "${err}" "${want}" pos)
  if(pos EQUAL -1)
    message(FATAL_ERROR "missing unwrapped line '${want}' in:\n${err}")
  endif()
endforeach()
