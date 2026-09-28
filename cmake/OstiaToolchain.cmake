# Toolchain decisions (RFC-0001 §1.1–§1.2) as pure functions, so `cmake -P` tests can
# exercise them without a compiler. The top-level CMakeLists.txt wires them together.
include_guard(GLOBAL)
# Modules also run under `cmake -P`, where no project sets policies.
cmake_policy(VERSION 4.1)

# Supported host compilers per CUDA major.minor: GCC min, GCC max, Clang min, Clang max.
# Source: each release's CUDA installation guide (RFC-0001 §1.2). A new row needs a PR
# that cites the guide.
set(_OSTIA_HOST_COMPILERS_12.8 11 14 17 19)
set(_OSTIA_HOST_COMPILERS_13.0 11 15 17 20)
set(_OSTIA_HOST_COMPILERS_13.4 11 16 17 22)
# Host-only floors: what GCC 11 and nvcc 12.8 support defines the usable C++20 (§1.2).
set(_OSTIA_HOST_FLOOR_GNU 11)
set(_OSTIA_HOST_FLOOR_Clang 17)

# ostia_host_compiler_range(CUDA_VERSION <v|""> COMPILER_ID <id> COMPILER_VERSION <v>
#                           OUT_OK <var> OUT_RANGE <var>)
# OUT_OK is TRUE, FALSE or UNKNOWN_CUDA; OUT_RANGE describes the supported range.
function(ostia_host_compiler_range)
  cmake_parse_arguments(PARSE_ARGV 0 A "" "CUDA_VERSION;COMPILER_ID;COMPILER_VERSION;OUT_OK;OUT_RANGE" "")
  string(REGEX MATCH "^[0-9]+" major "${A_COMPILER_VERSION}")
  set(id "${A_COMPILER_ID}")

  if("${A_CUDA_VERSION}" STREQUAL "")
    if(id STREQUAL "AppleClang")
      set(${A_OUT_OK} TRUE PARENT_SCOPE)
      set(${A_OUT_RANGE} "AppleClang (tier 3; use pixi for conda-forge Clang)" PARENT_SCOPE)
      return()
    endif()
    if(NOT DEFINED _OSTIA_HOST_FLOOR_${id})
      set(${A_OUT_OK} FALSE PARENT_SCOPE)
      set(${A_OUT_RANGE} "GNU ${_OSTIA_HOST_FLOOR_GNU}+ or Clang ${_OSTIA_HOST_FLOOR_Clang}+" PARENT_SCOPE)
      return()
    endif()
    set(floor ${_OSTIA_HOST_FLOOR_${id}})
    set(${A_OUT_RANGE} "${id} ${floor}+" PARENT_SCOPE)
    if(major LESS floor)
      set(${A_OUT_OK} FALSE PARENT_SCOPE)
    else()
      set(${A_OUT_OK} TRUE PARENT_SCOPE)
    endif()
    return()
  endif()

  string(REGEX MATCH "^[0-9]+\\.[0-9]+" cuda_mm "${A_CUDA_VERSION}")
  if(NOT DEFINED _OSTIA_HOST_COMPILERS_${cuda_mm})
    set(${A_OUT_OK} UNKNOWN_CUDA PARENT_SCOPE)
    set(${A_OUT_RANGE} "CUDA 12.8, 13.0 or 13.4" PARENT_SCOPE)
    return()
  endif()
  list(GET _OSTIA_HOST_COMPILERS_${cuda_mm} 0 gnu_min)
  list(GET _OSTIA_HOST_COMPILERS_${cuda_mm} 1 gnu_max)
  list(GET _OSTIA_HOST_COMPILERS_${cuda_mm} 2 clang_min)
  list(GET _OSTIA_HOST_COMPILERS_${cuda_mm} 3 clang_max)
  if(id STREQUAL "GNU")
    set(lo ${gnu_min})
    set(hi ${gnu_max})
  elseif(id STREQUAL "Clang")
    set(lo ${clang_min})
    set(hi ${clang_max})
  else()
    set(${A_OUT_OK} FALSE PARENT_SCOPE)
    set(${A_OUT_RANGE} "GNU ${gnu_min}–${gnu_max} or Clang ${clang_min}–${clang_max}" PARENT_SCOPE)
    return()
  endif()
  set(${A_OUT_RANGE} "${id} ${lo}–${hi}" PARENT_SCOPE)
  if(major LESS lo OR major GREATER hi)
    set(${A_OUT_OK} FALSE PARENT_SCOPE)
  else()
    set(${A_OUT_OK} TRUE PARENT_SCOPE)
  endif()
endfunction()

# ostia_parse_nvcc_version(<text> <out>): "release 12.8, V12.8.93" -> 12.8.93, else NOTFOUND.
function(ostia_parse_nvcc_version text out)
  if(text MATCHES "release [0-9.]+, V([0-9.]+)")
    set(${out} "${CMAKE_MATCH_1}" PARENT_SCOPE)
  else()
    set(${out} NOTFOUND PARENT_SCOPE)
  endif()
endfunction()

# ostia_cuda_arch_user_value(CACHE <v> PREVIOUS <v> ENV <v> OUT <var>)
# The user's CUDA architectures, or "" when the cache holds only Ostia's own previous
# choice. CUDAARCHS (ENV) counts when the cache is empty.
function(ostia_cuda_arch_user_value)
  cmake_parse_arguments(PARSE_ARGV 0 A "" "CACHE;PREVIOUS;ENV;OUT" "")
  if(NOT "${A_CACHE}" STREQUAL "" AND NOT "${A_CACHE}" STREQUAL "${A_PREVIOUS}")
    set(${A_OUT} "${A_CACHE}" PARENT_SCOPE)
  elseif("${A_CACHE}" STREQUAL "" AND NOT "${A_ENV}" STREQUAL "")
    set(${A_OUT} "${A_ENV}" PARENT_SCOPE)
  else()
    set(${A_OUT} "" PARENT_SCOPE)
  endif()
endfunction()

# ostia_choose_cuda_architectures(USER_VALUE <v|""> MODE <dev|release> GPU_CAPS <list>
#                                 RELEASE_LIST <list> OUT <var> OUT_REASON <var>)
# A user value always wins; `dev` uses native only when a GPU was detected (§1.1).
# Pass GPU_CAPS and RELEASE_LIST unquoted: PARSE_ARGV escapes ";" inside one argument.
function(ostia_choose_cuda_architectures)
  cmake_parse_arguments(PARSE_ARGV 0 A "" "USER_VALUE;MODE;OUT;OUT_REASON" "GPU_CAPS;RELEASE_LIST")
  if(NOT "${A_USER_VALUE}" STREQUAL "")
    set(${A_OUT} "${A_USER_VALUE}" PARENT_SCOPE)
    set(${A_OUT_REASON} "CMAKE_CUDA_ARCHITECTURES set by user" PARENT_SCOPE)
  elseif(A_MODE STREQUAL "dev")
    if(A_GPU_CAPS)
      list(JOIN A_GPU_CAPS ", " caps)
      set(${A_OUT} native PARENT_SCOPE)
      set(${A_OUT_REASON} "dev preset, GPU detected (${caps})" PARENT_SCOPE)
    else()
      set(${A_OUT} "${A_RELEASE_LIST}" PARENT_SCOPE)
      set(${A_OUT_REASON} "dev preset, no GPU detected: release list" PARENT_SCOPE)
    endif()
  elseif(A_MODE STREQUAL "release")
    set(${A_OUT} "${A_RELEASE_LIST}" PARENT_SCOPE)
    set(${A_OUT_REASON} "release list" PARENT_SCOPE)
  else()
    set(${A_OUT} INVALID PARENT_SCOPE)
    set(${A_OUT_REASON} "unknown OSTIA_CUDA_ARCH_MODE '${A_MODE}' (expected dev or release)" PARENT_SCOPE)
  endif()
endfunction()

# ostia_detect_gpus(<out_caps>): compute capabilities of the visible GPUs, or an empty
# list. OSTIA_GPU_DETECT_COMMAND can be overridden (tests, unusual drivers).
function(ostia_detect_gpus out)
  if(NOT DEFINED OSTIA_GPU_DETECT_COMMAND)
    set(OSTIA_GPU_DETECT_COMMAND nvidia-smi --query-gpu=compute_cap --format=csv,noheader)
  endif()
  execute_process(COMMAND ${OSTIA_GPU_DETECT_COMMAND}
    RESULT_VARIABLE rc OUTPUT_VARIABLE text ERROR_QUIET OUTPUT_STRIP_TRAILING_WHITESPACE)
  set(caps "")
  if(rc EQUAL 0 AND NOT text STREQUAL "")
    string(REPLACE "\n" ";" lines "${text}")
    foreach(line IN LISTS lines)
      string(STRIP "${line}" line)
      if(line MATCHES "^[0-9]+\\.[0-9]+$")
        list(APPEND caps "${line}")
      endif()
    endforeach()
  endif()
  set(${out} "${caps}" PARENT_SCOPE)
endfunction()

# ostia_strip_conda_rpath(): conda's compiler activation puts -Wl,-rpath,$CONDA_PREFIX/lib
# in LDFLAGS. As a literal linker flag it precedes the build tree's RPATH and survives
# install, so tests could load stale installed libraries and installed binaries would
# carry an absolute path. Remove it; callers add back what they need (RFC-0001 §3.5).
macro(ostia_strip_conda_rpath)
  if(DEFINED ENV{CONDA_PREFIX})
    foreach(_ostia_v CMAKE_EXE_LINKER_FLAGS CMAKE_SHARED_LINKER_FLAGS CMAKE_MODULE_LINKER_FLAGS)
      string(REPLACE "-Wl,-rpath,$ENV{CONDA_PREFIX}/lib" "" ${_ostia_v} "${${_ostia_v}}")
    endforeach()
  endif()
endmacro()
