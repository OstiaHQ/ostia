# M0 gate workload programs (RFC-0001 §6.4). They need nothing beyond CUDA and UCX, so
# they build with the tests as well as with OSTIA_BUILD_BENCH: CPU CI smoke-tests the
# UCX programs over TCP loopback, and ostia-dev remote's bench-smoke suite (RFC-0005) the
# CUDA programs on one L4 node (--smoke). The gate runs themselves happen on the rented
# setups (RFC-0004, Rollout PR 7).
find_package(ucx CONFIG QUIET)
set(_programs "")

if(ucx_FOUND)
  add_library(ostia_fabric_bench_ucx STATIC common/ucx.cpp)
  target_include_directories(ostia_fabric_bench_ucx PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
  target_link_libraries(ostia_fabric_bench_ucx PUBLIC ucx::ucp ucx::ucs)
  if(CMAKE_CUDA_COMPILER_LOADED)
    find_package(CUDAToolkit REQUIRED)
    target_compile_definitions(ostia_fabric_bench_ucx PUBLIC OSTIA_BENCH_CUDA)
    target_link_libraries(ostia_fabric_bench_ucx PUBLIC CUDA::cudart)
  endif()
  foreach(w rdma_put gdr_stream tcp_put)
    add_executable(ostia_fabric_bench_${w} ${w}.cpp)
    target_link_libraries(ostia_fabric_bench_${w} PRIVATE ostia_fabric_bench_ucx)
    list(APPEND _programs ${w})
  endforeach()
endif()

if(CMAKE_CUDA_COMPILER_LOADED)
  foreach(w p2p_copy pipelining batching dual_link)
    add_executable(ostia_fabric_bench_${w} ${w}.cu)
    target_include_directories(ostia_fabric_bench_${w} PRIVATE ${CMAKE_CURRENT_SOURCE_DIR})
    list(APPEND _programs ${w})
  endforeach()
  if(ucx_FOUND)
    target_compile_definitions(ostia_fabric_bench_dual_link PRIVATE OSTIA_BENCH_UCX)
    target_link_libraries(ostia_fabric_bench_dual_link PRIVATE ostia_fabric_bench_ucx)
  endif()
endif()

if(NOT OSTIA_BUILD_TESTS OR _programs STREQUAL "")
  return()
endif()

find_package(Python3 3.10 REQUIRED COMPONENTS Interpreter)
set(
  _launch
  ${Python3_EXECUTABLE}
  ${PROJECT_SOURCE_DIR}/fabric/tests/multiprocess/launcher.py
  --ranks
  2
)
set(_corrupted "failed their checksum")
set(_skipped "skipped: no CUDA device")

# ostia_bench_smoke(<name> <program> [MULTI] [GPU] [CORRUPT] [LAUNCH <args>] ARGS <args>)
function(ostia_bench_smoke name program)
  cmake_parse_arguments(PARSE_ARGV 2 A "MULTI;GPU;CORRUPT" "" "LAUNCH;ARGS")
  set(cmd $<TARGET_FILE:ostia_fabric_bench_${program}> --smoke ${A_ARGS})
  set(labels "")
  if(A_MULTI)
    set(cmd ${_launch} ${A_LAUNCH} -- ${cmd})
    list(APPEND labels multiprocess)
  endif()
  if(A_GPU)
    list(APPEND labels gpu)
  else()
    list(APPEND labels cpu)
  endif()
  if(A_CORRUPT)
    list(APPEND cmd --corrupt)
  endif()
  add_test(NAME fabric.bench.${name} COMMAND ${cmd})
  set_tests_properties(
    fabric.bench.${name}
    PROPERTIES
      LABELS "${labels}"
      TIMEOUT 300
      SKIP_RETURN_CODE 77
      SKIP_REGULAR_EXPRESSION "${_skipped}"
  )
  if(A_CORRUPT)
    # Self-check: a corrupted byte must fail the checksum.
    set_tests_properties(fabric.bench.${name} PROPERTIES PASS_REGULAR_EXPRESSION "${_corrupted}")
  endif()
endfunction()

if(ucx_FOUND)
  # Host memory over UCX TCP loopback: runs on every Linux CPU runner with UCX.
  ostia_bench_smoke(rdma_put_host rdma_put MULTI ARGS --mem host)
  ostia_bench_smoke(rdma_put_host_corrupt rdma_put MULTI CORRUPT ARGS --mem host)
  ostia_bench_smoke(gdr_stream_host gdr_stream MULTI ARGS --mem host --chunk 1MiB)
  ostia_bench_smoke(gdr_stream_host_corrupt gdr_stream MULTI CORRUPT ARGS --mem host)
  ostia_bench_smoke(tcp_put tcp_put MULTI)
  if(CMAKE_CUDA_COMPILER_LOADED)
    # GPU memory between two processes on one GPU: CUDA IPC plus UCX loopback.
    set(_gpu_launch --tls cuda_ipc,cuda_copy,tcp,self --expect cuda_ipc)
    ostia_bench_smoke(rdma_put_cuda rdma_put MULTI GPU LAUNCH ${_gpu_launch} ARGS --mem cuda)
    ostia_bench_smoke(gdr_stream_cuda gdr_stream MULTI GPU LAUNCH ${_gpu_launch} ARGS --mem cuda)
    # Two "rails" over loopback: exercises the two-context path, not two NICs.
    ostia_bench_smoke(
      dual_link_rails
      dual_link
      MULTI
      GPU
      LAUNCH ${_gpu_launch}
      ARGS --mode rails --nics lo,lo --mem cuda
    )
  endif()
endif()

if(CMAKE_CUDA_COMPILER_LOADED)
  foreach(w p2p_copy pipelining batching dual_link)
    ostia_bench_smoke(${w} ${w} GPU)
    ostia_bench_smoke(${w}_corrupt ${w} GPU CORRUPT)
  endforeach()
endif()
