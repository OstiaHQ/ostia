# ostia_add_component(): one target per component, with layering checks (RFC-0001 §3.2).
include_guard(GLOBAL)
cmake_policy(VERSION 4.1)
include(${CMAKE_CURRENT_LIST_DIR}/OstiaLayering.cmake)
include(GenerateExportHeader)
include(GNUInstallDirs)

# ostia_add_component(NAME <n> RANK <r> [DEPENDS <c>...] [PLACEHOLDER])
# Creates ostia_<n> (alias ostia::<n>, library libostia-<n>) with include/ PUBLIC and
# src/ PRIVATE. Placeholders are empty INTERFACE targets, never installed (§3.1).
function(ostia_add_component)
  cmake_parse_arguments(PARSE_ARGV 0 A "PLACEHOLDER" "NAME;RANK" "DEPENDS")
  set(name ${A_NAME})
  string(TOUPPER ${name} upper)
  ostia_relpath("${CMAKE_CURRENT_LIST_FILE}" where)

  # Part 1 of RFC-0001 §3.3: declared dependencies against the table.
  ostia_layering_rank(${name} rank)
  if(rank STREQUAL "NOTFOUND")
    ostia_fail(
      PROBLEM "${where}: unknown component '${name}'"
      RULE "every component is a row of cmake/layering.json"
      FIX "add the component through an RFC, which adds its row"
      SEE "RFC-0001 §3.3"
    )
  endif()
  if(NOT "${A_RANK}" STREQUAL "${rank}")
    ostia_fail(
      PROBLEM "${where}: ostia_add_component(NAME ${name}) declares RANK ${A_RANK}"
      DETAILS "the dependency table gives ${name} rank ${rank}"
      RULE "ranks come from cmake/layering.json"
      FIX "write RANK ${rank}"
      SEE "RFC-0001 §3.3"
    )
  endif()
  ostia_layering_allowed(${name} allowed)
  list(JOIN allowed ", " allowed_text)
  if(allowed_text STREQUAL "")
    set(allowed_text "nothing")
  endif()
  foreach(dep IN LISTS A_DEPENDS)
    if(NOT dep IN_LIST allowed)
      ostia_fail(
        PROBLEM "${where}: ostia_add_component(NAME ${name}) DEPENDS ${dep}"
        DETAILS "${name} may depend on: ${allowed_text}"
        RULE "components depend only on the entries in their row of the dependency table"
        FIX "remove ${dep} from DEPENDS, or change the dependency table through an RFC"
        SEE "RFC-0001 §3.3"
      )
    endif()
    if(NOT TARGET ostia_${dep})
      string(TOUPPER ${dep} dep_upper)
      ostia_fail(
        PROBLEM "ostia-${name} needs ostia-${dep}, which is disabled"
        DETAILS "OSTIA_BUILD_${dep_upper}=OFF while OSTIA_BUILD_${upper}=ON"
        RULE "an enabled component's dependencies must be enabled"
        FIX "configure with -DOSTIA_BUILD_${dep_upper}=ON or -DOSTIA_BUILD_${upper}=OFF"
        SEE "RFC-0001 §3.3"
      )
    endif()
  endforeach()
  set_property(GLOBAL APPEND PROPERTY OSTIA_COMPONENTS ${name})
  set_property(GLOBAL PROPERTY OSTIA_DEPENDS_${name} "${A_DEPENDS}")

  if(A_PLACEHOLDER)
    add_library(ostia_${name} INTERFACE)
    add_library(ostia::${name} ALIAS ostia_${name})
    set_property(GLOBAL APPEND PROPERTY OSTIA_PLACEHOLDERS ${name})
    return() # placeholders are neither installed nor exported (RFC-0001 §3.1)
  endif()

  add_library(ostia_${name} SHARED)
  add_library(ostia::${name} ALIAS ostia_${name})
  if(APPLE)
    set(origin "@loader_path")
  else()
    set(origin "$ORIGIN")
  endif()
  set_target_properties(
    ostia_${name}
    PROPERTIES
      OUTPUT_NAME ostia-${name}
      EXPORT_NAME ${name}
      CXX_VISIBILITY_PRESET hidden
      VISIBILITY_INLINES_HIDDEN ON
      INSTALL_RPATH "${origin}"
      INSTALL_REMOVE_ENVIRONMENT_RPATH ON
  )
  target_compile_features(ostia_${name} PUBLIC cxx_std_20)
  generate_export_header(
    ostia_${name}
    BASE_NAME OSTIA_${upper}
    EXPORT_FILE_NAME ${CMAKE_CURRENT_BINARY_DIR}/include/ostia/${name}/export.h
  )
  target_include_directories(
    ostia_${name}
    PUBLIC
      $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
      $<BUILD_INTERFACE:${CMAKE_CURRENT_BINARY_DIR}/include>
      $<INSTALL_INTERFACE:${CMAKE_INSTALL_INCLUDEDIR}>
    PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/src
  )
  foreach(dep IN LISTS A_DEPENDS)
    target_link_libraries(ostia_${name} PUBLIC ostia::${dep})
  endforeach()
  install(
    TARGETS ostia_${name}
    EXPORT ostia-${name}-targets
    LIBRARY DESTINATION ${CMAKE_INSTALL_LIBDIR}
    RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR}
  )
  install(DIRECTORY include/ ${CMAKE_CURRENT_BINARY_DIR}/include/ TYPE INCLUDE)
  install(
    EXPORT ostia-${name}-targets
    NAMESPACE ostia::
    DESTINATION ${CMAKE_INSTALL_LIBDIR}/cmake/ostia
  )
endfunction()
