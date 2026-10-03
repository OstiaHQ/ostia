# Run with cmake -P at build time: writes one {"name", R"OSTIA(...)OSTIA"} entry per schema
# into OUT, so schema.cpp embeds the schemas without reading files at run time.
foreach(
  name
  nvml
  nics
  links
  meta
  manifest
  pair
)
  file(READ "${SCHEMA_DIR}/${name}.schema.json" body)
  string(FIND "${body}" ")OSTIA\"" clash)
  if(NOT clash EQUAL -1)
    message(FATAL_ERROR "${name}.schema.json contains the raw-string terminator )OSTIA\"")
  endif()
  string(APPEND out "{\"${name}\", R\"OSTIA(${body})OSTIA\"},\n")
endforeach()
file(WRITE "${OUT}" "${out}")
