# Only the rdma-core headers are needed: ostia-topo-capture dlopens libibverbs.so.1 and
# never links it (RFC-0003 §1), so this module finds no library.
include_guard(GLOBAL)
include(FindPackageHandleStandardArgs)

# Debian and Ubuntu are multiarch, so the header can sit under include/<triplet>; find_path
# searches those directories through the compiler's implicit include paths.
find_path(ibverbs_INCLUDE_DIR infiniband/verbs.h)
mark_as_advanced(ibverbs_INCLUDE_DIR)
find_package_handle_standard_args(ibverbs REQUIRED_VARS ibverbs_INCLUDE_DIR)
if(ibverbs_FOUND AND NOT TARGET ibverbs::headers)
  add_library(ibverbs::headers INTERFACE IMPORTED)
  set_target_properties(
    ibverbs::headers
    PROPERTIES INTERFACE_INCLUDE_DIRECTORIES "${ibverbs_INCLUDE_DIR}"
  )
endif()
