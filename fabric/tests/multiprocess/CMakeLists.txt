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

# Two processes on one GPU (RFC-0001 §4.2): the same harness over GPU memory, CUDA IPC
# plus UCX loopback. Labels gpu and multiprocess; skipped (exit 77) without a device, or
# failed with OSTIA_REQUIRE_GPU=1 (RFC-0005 §3.2).
if(CMAKE_CUDA_COMPILER_LOADED)
  find_package(CUDAToolkit REQUIRED)
  add_executable(ostia_fabric_ucx_put_cuda ucx_put.cpp)
  target_compile_definitions(ostia_fabric_ucx_put_cuda PRIVATE OSTIA_UCX_PUT_CUDA)
  target_link_libraries(ostia_fabric_ucx_put_cuda PRIVATE ucx::ucp ucx::ucs CUDA::cudart)
  add_test(
    NAME fabric.multiprocess.gpu_ucx_put_2
    COMMAND
      ${_launch} --ranks 2 --tls cuda_ipc,cuda_copy,tcp,self --expect cuda_ipc --
      $<TARGET_FILE:ostia_fabric_ucx_put_cuda>
  )
  set_tests_properties(
    fabric.multiprocess.gpu_ucx_put_2
    PROPERTIES LABELS "gpu;multiprocess" SKIP_RETURN_CODE 77 TIMEOUT 180
  )
endif()
