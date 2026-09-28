# cmake -P test: CUDA architecture selection (RFC-0001 §1.1).
include(${CMAKE_CURRENT_LIST_DIR}/../../../cmake/OstiaToolchain.cmake)

set(release "80-real;90-real;100")
macro(choose user mode caps want want_reason)
  ostia_choose_cuda_architectures(USER_VALUE "${user}" MODE ${mode} GPU_CAPS ${caps}
                                  RELEASE_LIST ${release} OUT arch OUT_REASON reason)
  if(NOT "${arch}" STREQUAL "${want}")
    message(FATAL_ERROR "${user}/${mode}/${caps}: want '${want}', got '${arch}'")
  endif()
  if(NOT "${reason}" STREQUAL "${want_reason}")
    message(FATAL_ERROR "${user}/${mode}/${caps}: reason '${reason}'")
  endif()
endmacro()

choose("75" dev "8.9" "75" "CMAKE_CUDA_ARCHITECTURES set by user")      # user_set_wins
choose("" dev "8.9" "native" "dev preset, GPU detected (8.9)")            # dev_with_gpu
choose("" dev "" "${release}" "dev preset, no GPU detected: release list") # dev_without_gpu
choose("" release "9.0" "${release}" "release list")                      # release
choose("" other "" "INVALID" "unknown OSTIA_CUDA_ARCH_MODE 'other' (expected dev or release)")

macro(user_value cache previous env want)
  ostia_cuda_arch_user_value(CACHE "${cache}" PREVIOUS "${previous}" ENV "${env}" OUT got)
  if(NOT "${got}" STREQUAL "${want}")
    message(FATAL_ERROR "cache='${cache}' previous='${previous}' env='${env}': got '${got}'")
  endif()
endmacro()

user_value("${release}" "${release}" "" "")    # reconfigure_keeps_auto
user_value("75" "${release}" "" "75")           # reconfigure_user_changed
user_value("" "" "90" "90")                      # env_cudaarchs
user_value("" "" "" "")                          # nothing set
user_value("native" "native" "" "")              # Ostia chose native last time
