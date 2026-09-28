---
number: 13
title: C++ and Python code style
status: Accepted
authors: [ShAlireza]
components: [build]
created: 2026-09-27
updated: 2026-09-27
supersedes: []
superseded_by: []
discussion: https://github.com/OstiaHQ/ostia/pull/13
---

# ADR-0013: C++ and Python code style

## Context

M0 adds the first code: the CMake helpers, the telemetry and fabric skeletons, Python bindings and tools ([RFC-0001](../rfcs/0001-m0-foundations.md) Rollout PR 1). That PR also adds lint to CI and to pre-commit, so formatting and naming need to be settled before the first line lands. Otherwise every early review argues about them.

What we already have:

- `.editorconfig` sets a 4-space indent for code, and 2 spaces for CMake, YAML and JSON.
- The API sketches in the [PRD](../product/prd.md) already use snake_case functions and PascalCase types (`register_memory`, `on_complete`, `Topology`, `Status`), and a C ABI prefix of `ostia_<component>_*`.
- [docs/README.md](../README.md) asks for an ADR when a choice such as a naming rule deserves a record.

## Decision

Formatting is enforced by tools; naming is checked by clang-tidy and by review.

**C and C++ (`.h`, `.hpp`, `.c`, `.cpp`, `.cu`, `.cuh`)**

- clang-format settings: `BasedOnStyle: LLVM`, `IndentWidth: 4`, `ColumnLimit: 100`, `PointerAlignment: Left`.
- Includes are regrouped in three blocks, in this order: standard and third-party headers, then `<ostia/...>`, then local headers.
- Names:
  - types: `PascalCase`;
  - functions and variables: `snake_case`;
  - private data members: `snake_case_`, with a trailing underscore;
  - constants: `kPascalCase`;
  - macros: `OSTIA_UPPER_CASE`;
  - namespaces: lower case, under `ostia::`.
- C ABI functions are named `ostia_<component>_<verb>`, and C ABI types `ostia_<component>_<noun>`.
- clang-tidy's `readability-identifier-naming` check encodes the naming rules.

**CMake:** formatted by gersemi with a 2-space indent and a line length of 100. Functions defined by Ostia are named `ostia_<verb>`.

**Python:** formatted and linted by ruff with a line length of 100, targeting Python 3.11.

## Consequences

- `pixi run fmt` applies every formatter. `pixi run lint` checks formatting in CI and in the pre-commit hook, and reports `fix: pixi run fmt` when a file is off.
- Code taken from other projects keeps its own style only if it is vendored unchanged, like `cmake/CPM.cmake`. Anything else is reformatted.
- Changing a rule takes a new ADR that supersedes this one, plus a separate PR that reformats the tree.
