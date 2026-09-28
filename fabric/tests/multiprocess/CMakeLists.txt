# Multi-process harness over UCX TCP loopback (RFC-0001 §4.1), label `multiprocess`.
# Built only when UCX is found (the ucx pixi environment on Linux).
add_executable(ostia_fabric_ucx_put ucx_put.cpp)
target_link_libraries(ostia_fabric_ucx_put PRIVATE ucx::ucp ucx::ucs)

find_package(Python3 3.10 REQUIRED COMPONENTS Interpreter)
set(_launch ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/launcher.py)
foreach(n 2 4)
  add_test(
    NAME fabric.multiprocess.ucx_put_${n}
    COMMAND ${_launch} --ranks ${n} -- $<TARGET_FILE:ostia_fabric_ucx_put>
  )
endforeach()
# Self-check: a corrupted byte must fail the checksum.
add_test(
  NAME fabric.multiprocess.detects_corruption
  COMMAND ${_launch} --ranks 2 -- $<TARGET_FILE:ostia_fabric_ucx_put> --corrupt
)
set_tests_properties(
  fabric.multiprocess.ucx_put_2
  fabric.multiprocess.ucx_put_4
  fabric.multiprocess.detects_corruption
  PROPERTIES LABELS multiprocess TIMEOUT 180
)
set_tests_properties(
  fabric.multiprocess.detects_corruption
  PROPERTIES PASS_REGULAR_EXPRESSION "1 of 1 slices failed their checksum"
)
