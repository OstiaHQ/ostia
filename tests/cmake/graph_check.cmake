# cmake -DSRC= -DBIN= -DPYTHON= -DCHECK= -DOSTIA_SOURCE_DIR= -P graph_check.cmake
# Configures a fixture with --graphviz (link walk skipped) and expects check_graph.py to
# flag the upward edge hidden in a generator expression (RFC-0001 §3.3, part 2).
cmake_policy(VERSION 4.1)
file(REMOVE_RECURSE "${BIN}")
execute_process(
  COMMAND ${CMAKE_COMMAND} -S "${SRC}" -B "${BIN}" -G Ninja -DOSTIA_SOURCE_DIR=${OSTIA_SOURCE_DIR}
          -DOSTIA_SKIP_LINK_WALK=ON --graphviz=${BIN}/graph.dot
  RESULT_VARIABLE rc OUTPUT_VARIABLE out ERROR_VARIABLE err
)
if(NOT rc EQUAL 0)
  message(FATAL_ERROR "configure failed\n${out}${err}")
endif()
execute_process(COMMAND ${PYTHON} ${CHECK} --dot ${BIN}/graph.dot RESULT_VARIABLE rc OUTPUT_VARIABLE out)
if(rc EQUAL 0 OR NOT out MATCHES "ostia_fabric links ostia_exchange")
  message(FATAL_ERROR "check_graph.py did not flag fabric -> exchange (rc=${rc})\n${out}")
endif()
