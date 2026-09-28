# Benchmarks (RFC-0001 §6). Built when CUDA is on and OSTIA_BUILD_BENCH is set.
ostia_dep_cccl()
ostia_dep_nvbench()
add_executable(ostia_telemetry_bench_noop noop_kernel.cu)
target_link_libraries(ostia_telemetry_bench_noop PRIVATE nvbench::main)
