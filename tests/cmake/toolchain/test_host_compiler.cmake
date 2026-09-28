# cmake -P test: host-compiler ranges per CUDA version (RFC-0001 §1.2).
include(${CMAKE_CURRENT_LIST_DIR}/../../../cmake/OstiaToolchain.cmake)

macro(check cuda id ver want)
  ostia_host_compiler_range(
    CUDA_VERSION "${cuda}"
    COMPILER_ID ${id}
    COMPILER_VERSION ${ver}
    OUT_OK ok
    OUT_RANGE range
  )
  if(NOT ok STREQUAL "${want}")
    message(FATAL_ERROR "${cuda}/${id}/${ver}: want ${want}, got ${ok} (${range})")
  endif()
endmacro()

check("12.8.93" GNU 11.4.0 TRUE)
check("12.8.93" GNU 14.2.0 TRUE)
check("12.8.93" GNU 15.1.0 FALSE)
check("12.8.93" GNU 10.5.0 FALSE)
check("12.8.93" Clang 19.1.7 TRUE)
check("12.8.93" Clang 20.1.0 FALSE)
check("13.4.0" GNU 16.1.0 TRUE)
check("13.4.0" Clang 22.1.0 TRUE)
check("13.4.0" Clang 23.0.0 FALSE)
check("13.0.1" GNU 16.1.0 FALSE)
check("13.0.1" Clang 20.1.0 TRUE)
check("12.6.0" GNU 12.0.0 UNKNOWN_CUDA)
check("" GNU 10.5.0 FALSE) # host-only floor GCC 11
check("" GNU 11.1.0 TRUE)
check("" GNU 16.1.0 TRUE) # host-only: no upper bound
check("" Clang 16.0.6 FALSE) # host-only floor Clang 17
check("" Clang 19.1.7 TRUE)
check("" AppleClang 16.0.0 TRUE) # allowed (tier 3); configure warns

ostia_host_compiler_range(
  CUDA_VERSION "12.8.93"
  COMPILER_ID GNU
  COMPILER_VERSION 15.1.0
  OUT_OK ok
  OUT_RANGE range
)
if(NOT range STREQUAL "GNU 11–14")
  message(FATAL_ERROR "range text: '${range}'")
endif()
