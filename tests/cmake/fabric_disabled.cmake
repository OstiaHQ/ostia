# Installs a tree built with -DOSTIA_BUILD_FABRIC=OFF; telemetry is still found, and
# fabric fails with "not installed" (RFC-0001 §3.2).
cmake_policy(VERSION 4.1)
file(REMOVE_RECURSE "${WORK}")
set(compilers -DCMAKE_C_COMPILER=${CMAKE_C_COMPILER} -DCMAKE_CXX_COMPILER=${CMAKE_CXX_COMPILER})
macro(run)
  execute_process(COMMAND ${ARGN} RESULT_VARIABLE rc OUTPUT_VARIABLE out ERROR_VARIABLE err)
endmacro()
run(${CMAKE_COMMAND} -S ${SOURCE} -B ${WORK}/build -G Ninja ${compilers} -DOSTIA_BUILD_FABRIC=OFF
    -DOSTIA_BUILD_EXCHANGE=OFF -DOSTIA_BUILD_RUNTIME=OFF -DOSTIA_BUILD_QUERY=OFF -DOSTIA_BUILD_TESTS=OFF)
if(NOT rc EQUAL 0)
  message(FATAL_ERROR "configure failed\n${out}${err}")
endif()
run(${CMAKE_COMMAND} --build ${WORK}/build)
run(${CMAKE_COMMAND} --install ${WORK}/build --prefix ${WORK}/stage)
if(NOT rc EQUAL 0)
  message(FATAL_ERROR "install failed\n${out}${err}")
endif()
run(${CMAKE_COMMAND} -S ${CONSUMER} -B ${WORK}/telemetry -G Ninja ${compilers} -DWANT=telemetry
    -DCMAKE_PREFIX_PATH=${WORK}/stage)
if(NOT rc EQUAL 0)
  message(FATAL_ERROR "find_package(ostia COMPONENTS telemetry) failed\n${out}${err}")
endif()
run(${CMAKE_COMMAND} -S ${CONSUMER} -B ${WORK}/fabric -G Ninja ${compilers} -DWANT=fabric
    -DCMAKE_PREFIX_PATH=${WORK}/stage)
if(rc EQUAL 0 OR NOT "${out}${err}" MATCHES "ostia component 'fabric' is not installed")
  message(FATAL_ERROR "expected 'not installed' for fabric (rc=${rc})\n${out}${err}")
endif()
