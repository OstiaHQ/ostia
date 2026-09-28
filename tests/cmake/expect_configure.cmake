# cmake -DSRC=<dir> -DBIN=<dir> -DEXPECT=pass|fail [-DREGEX=<re>] [-DARGS=a|b|c] [-DRUN=<exe>]
#       -P expect_configure.cmake
# ctest's PASS_REGULAR_EXPRESSION ignores exit codes, so negative configure tests go
# through this wrapper: it checks the exit code and the output together.
cmake_policy(VERSION 4.1)
file(REMOVE_RECURSE "${BIN}")
string(REPLACE "|" ";" args "${ARGS}")
execute_process(
  COMMAND ${CMAKE_COMMAND} -S "${SRC}" -B "${BIN}" -G Ninja ${args}
  RESULT_VARIABLE rc
  OUTPUT_VARIABLE out
  ERROR_VARIABLE err
)
set(all "${out}${err}")
if(EXPECT STREQUAL "fail" AND rc EQUAL 0)
  message(FATAL_ERROR "configure of ${SRC} succeeded but was expected to fail\n${all}")
endif()
if(EXPECT STREQUAL "pass" AND NOT rc EQUAL 0)
  message(FATAL_ERROR "configure of ${SRC} failed but was expected to pass\n${all}")
endif()
if(NOT "${REGEX}" STREQUAL "" AND NOT all MATCHES "${REGEX}")
  message(FATAL_ERROR "output does not match '${REGEX}'\n${all}")
endif()

# RUN: after a passing configure, build the project and run <BIN>/<RUN>; it must exit 0.
if(DEFINED RUN AND NOT RUN STREQUAL "")
  execute_process(COMMAND ${CMAKE_COMMAND} --build "${BIN}" RESULT_VARIABLE rc OUTPUT_VARIABLE out ERROR_VARIABLE err)
  if(NOT rc EQUAL 0)
    message(FATAL_ERROR "build of ${SRC} failed\n${out}${err}")
  endif()
  execute_process(COMMAND "${BIN}/${RUN}" RESULT_VARIABLE rc OUTPUT_VARIABLE out ERROR_VARIABLE err)
  if(NOT rc EQUAL 0)
    message(FATAL_ERROR "${BIN}/${RUN} exited with ${rc}\n${out}${err}")
  endif()
endif()
