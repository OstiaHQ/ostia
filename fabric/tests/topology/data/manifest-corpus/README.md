# Manifest validator corpus

Cases that both manifest validators must judge the same way (RFC-0003 §4): the C++ validator in
`ostia_fabric_topology` (`validate("manifest", ...)`, tested by `manifest_test.cpp`) and the
Python consumer in `ostia-dev`, which uses `jsonschema` against `manifest.schema.json`.

## Format

One case per file, `<expect>-<what>.json`:

```json
{"expect": "valid", "manifest": {...}}
```

`expect` is `valid` or `invalid`. A case that JSON cannot hold as a value carries the manifest as
raw text in `manifest_text` instead of `manifest`; each side parses that string with its own JSON
parser and validates the result.

## Cases that need care

- `valid-duplicate-key.json`: `status` appears twice, `"bogus"` then `"complete"`. nlohmann/json
  and Python's `json` both keep the last occurrence, so both validators see `"complete"` and accept
  the manifest. The C++ writer never emits a duplicate key, so the Python consumer's
  `check_manifest` is stricter than the schema: its parser rejects any repeated key before it
  validates, and the corpus test checks that too.
- `valid-schema-float.json`: `"schema": 1.0`. JSON Schema compares numbers by value, so `const: 1`
  accepts `1.0` in `jsonschema`, and nlohmann/json compares `1.0` equal to `1`. The manifest schema
  has no `"type": "integer"` field; for one, the C++ validator rejects `1.0` while JSON Schema
  2020-12 accepts it.
- `invalid-topology-id-newline.json`: the topology id followed by `\n`. ECMAScript `$` matches
  only at the end of the string, so the C++ validator rejects it. Python's `re.search` lets `$`
  match before a trailing newline, so plain `jsonschema` would accept it; the Python consumer must
  check patterns with full-string matching for the two sides to agree.
