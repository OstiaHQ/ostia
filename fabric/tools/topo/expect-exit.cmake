# Runs `ostia-topo model <DIR>` and fails unless it exits with exactly EXPECT; WILL_FAIL would
# also accept a crash.
execute_process(COMMAND ${TOOL} model ${DIR} RESULT_VARIABLE _rc OUTPUT_QUIET ERROR_VARIABLE _err)
if(NOT "${_rc}" STREQUAL "${EXPECT}")
  message(FATAL_ERROR "ostia-topo exited ${_rc}, expected ${EXPECT}: ${_err}")
endif()
