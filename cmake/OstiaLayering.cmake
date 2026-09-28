# The component dependency table (RFC-0001 §3.3). cmake/layering.json is the single
# source; tools/ci/check_layering.py and check_graph.py read the same file.
include_guard(GLOBAL)
# Modules also run under `cmake -P`, where no project sets policies.
cmake_policy(VERSION 4.1)
include(${CMAKE_CURRENT_LIST_DIR}/OstiaMessages.cmake)

file(READ "${CMAKE_CURRENT_LIST_DIR}/layering.json" _ostia_json)
set_property(GLOBAL PROPERTY OSTIA_LAYERING_JSON "${_ostia_json}")

# _ostia_json_list(<out> <json path...>): a JSON array as a CMake list.
function(_ostia_json_list out)
  get_property(json GLOBAL PROPERTY OSTIA_LAYERING_JSON)
  set(result "")
  string(JSON arr ERROR_VARIABLE err GET "${json}" ${ARGN})
  if(NOT err)
    string(JSON n LENGTH "${arr}")
    if(n GREATER 0)
      math(EXPR last "${n} - 1")
      foreach(i RANGE ${last})
        string(JSON item GET "${arr}" ${i})
        list(APPEND result ${item})
      endforeach()
    endif()
  endif()
  set(${out} "${result}" PARENT_SCOPE)
endfunction()

function(ostia_layering_rank name out)
  get_property(json GLOBAL PROPERTY OSTIA_LAYERING_JSON)
  string(JSON r ERROR_VARIABLE err GET "${json}" components ${name} rank)
  if(err)
    set(r NOTFOUND)
  endif()
  set(${out} "${r}" PARENT_SCOPE)
endfunction()

function(ostia_layering_rfc name out)
  get_property(json GLOBAL PROPERTY OSTIA_LAYERING_JSON)
  string(JSON r ERROR_VARIABLE err GET "${json}" components ${name} rfc)
  if(err)
    set(r NOTFOUND)
  endif()
  set(${out} "${r}" PARENT_SCOPE)
endfunction()

function(ostia_layering_allowed name out)
  _ostia_json_list(result components ${name} depends)
  set(${out} "${result}" PARENT_SCOPE)
endfunction()

function(ostia_layering_internal_targets out)
  _ostia_json_list(result internal_targets)
  set(${out} "${result}" PARENT_SCOPE)
endfunction()

# ostia_layering_components(<out>): every component, in rank order.
function(ostia_layering_components out)
  get_property(json GLOBAL PROPERTY OSTIA_LAYERING_JSON)
  string(JSON n LENGTH "${json}" components)
  math(EXPR last "${n} - 1")
  set(by_rank "")
  foreach(i RANGE ${last})
    string(JSON name MEMBER "${json}" components ${i})
    string(JSON r GET "${json}" components ${name} rank)
    list(APPEND by_rank "${r}:${name}")
  endforeach()
  list(SORT by_rank COMPARE NATURAL)
  list(TRANSFORM by_rank REPLACE "^[0-9]+:" "")
  set(${out} "${by_rank}" PARENT_SCOPE)
endfunction()

# Part 2 of RFC-0001 §3.3: walk each component's own direct link edges, after all
# targets exist. Catches raw target_link_libraries calls that bypass ostia_add_component.
# Generator expressions are scanned for ostia names but cannot be evaluated here; CI's
# resolved-graph check (tools/ci/check_graph.py) covers what they expand to.
function(ostia_check_layering)
  if(OSTIA_SKIP_LINK_WALK)
    return() # fixtures only: lets the graph_genex fixture reach generation
  endif()
  get_property(components GLOBAL PROPERTY OSTIA_COMPONENTS)
  ostia_layering_internal_targets(internal)
  foreach(c IN LISTS components)
    ostia_layering_allowed(${c} allowed)
    list(JOIN allowed ", " allowed_text)
    if(allowed_text STREQUAL "")
      set(allowed_text "nothing")
    endif()
    foreach(prop LINK_LIBRARIES INTERFACE_LINK_LIBRARIES)
      get_target_property(libs ostia_${c} ${prop})
      if(NOT libs)
        continue()
      endif()
      foreach(entry IN LISTS libs)
        if(entry MATCHES "^::@")
          continue() # directory-scope markers CMake inserts
        endif()
        # ostia::X and ostia_X anywhere, including inside generator expressions.
        string(REGEX MATCHALL "ostia(::|_)[a-z][a-z_]*" refs "${entry}")
        foreach(ref IN LISTS refs)
          string(REGEX REPLACE "^ostia(::|_)" "" dep "${ref}")
          if(dep STREQUAL c OR dep IN_LIST internal OR dep IN_LIST allowed)
            continue()
          endif()
          if(NOT dep IN_LIST components)
            list(JOIN components ", " components_text)
            list(JOIN internal ", " internal_text)
            ostia_fail(
              PROBLEM "ostia_${c} ${prop} references ${ref}, which is not a component"
              DETAILS "components: ${components_text}" "internal targets: ${internal_text}"
              RULE "components link only table components or listed internal targets"
              FIX "fix the name, or add an internal target to internal_targets in cmake/layering.json"
              SEE "RFC-0001 §3.3"
            )
          endif()
          ostia_fail(
            PROBLEM "ostia_${c} ${prop} contains ${entry}"
            DETAILS "${c} may depend on: ${allowed_text}"
            RULE "a component links only the entries in its row of the dependency table"
            FIX "remove the link, or change the dependency table through an RFC"
            SEE "RFC-0001 §3.3"
          )
        endforeach()
      endforeach()
    endforeach()
  endforeach()
endfunction()
