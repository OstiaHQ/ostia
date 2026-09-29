# cmake -DCXX= -DSOURCE= -DVARIANT=error|ok -DINCLUDES=<;-list> -P expect_compile.cmake
# Syntax-checks one C++ file: VARIANT=error must fail with a compiler error, VARIANT=ok
# (compiled with -DOSTIA_PROBE_WELL_TYPED) must succeed.
cmake_policy(VERSION 4.1)
set(defs "")
if(VARIANT STREQUAL "ok")
  set(defs -DOSTIA_PROBE_WELL_TYPED)
endif()
execute_process(
  COMMAND ${CXX} -std=c++20 -fsyntax-only ${defs} ${INCLUDES} ${SOURCE}
  RESULT_VARIABLE rc
  OUTPUT_VARIABLE out
  ERROR_VARIABLE err
)
if(VARIANT STREQUAL "error" AND (rc EQUAL 0 OR NOT err MATCHES "error"))
  message(FATAL_ERROR "the ill-typed macro call compiled at level off\n${out}${err}")
endif()
if(VARIANT STREQUAL "ok" AND NOT rc EQUAL 0)
  message(FATAL_ERROR "the well-typed probe failed to compile\n${out}${err}")
endif()
