# Adding or growing a component

How a component is laid out, how the build enforces layering, and what it takes to add one or to turn a placeholder into a real component. The design is [RFC-0001 §3](../rfcs/0001-m0-foundations.md#3-repository-layout-and-targets).

## Before you start: an RFC

A new component, or a placeholder (exchange, runtime, query) becoming real, needs an accepted RFC first ([docs/README.md](../README.md#when-you-need-an-rfc)). That RFC is also the only way to change the dependency table.

## The dependency table

`cmake/layering.json` is the single source of the layering rules. CMake, `check_layering.py` and `check_graph.py` all read it.

| Component | Rank | May depend on |
| --- | --- | --- |
| telemetry | 0 | nothing |
| fabric | 1 | telemetry |
| exchange | 2 | fabric, telemetry |
| runtime | 3 | exchange, fabric, telemetry |
| query | 4 | runtime, exchange, telemetry |

A new component gets a row with its rank, its allowed dependencies and the RFC that defines it.

## Folder shape

```text
<component>/
  CMakeLists.txt            ostia_add_component(...) and the component's sources
  README.md                 what it is, with links to its RFCs
  include/ostia/<component>/  public, installed: *.h = C ABI (ostia_<component>_*), *.hpp = C++
  src/                      private sources and headers, never installed
  tests/                    C++ tests; ctest labels cpu, gpu, multiprocess
  bench/                    benchmarks (RFC-0001 §6)
  python/                   the Python package (optional)
```

## The CMake helper

```cmake
ostia_add_component(NAME fabric RANK 1 DEPENDS telemetry)
target_sources(ostia_fabric PRIVATE src/fabric.cpp)
```

`ostia_add_component` does the following:
- creates `ostia_<name>`, with the alias `ostia::<name>`, building `libostia-<name>`;
- makes `include/` PUBLIC and `src/` PRIVATE;
- generates `<ostia/<name>/export.h`, which provides the `OSTIA_<NAME>_EXPORT` macro;
- hides every symbol that isn't marked with that macro;
- installs and exports the target.

The top-level `CMakeLists.txt` adds components in rank order, each behind an `OSTIA_BUILD_<NAME>` option.

A placeholder is declared with `ostia_add_component(NAME exchange RANK 2 PLACEHOLDER)`. It becomes an empty INTERFACE target that is never installed or exported, and `find_package(ostia COMPONENTS exchange)` fails with a pointer to the component's RFC.

## Turning a placeholder into a real component

1. Replace `PLACEHOLDER` with the component's `DEPENDS` (from its table row), and add its sources.
2. Add `include/ostia/<component>/`, `src/` and `tests/`.
3. Update the component's README to link its RFC.

## Python packages

- A component with bindings has `python/pyproject.toml` (scikit-build-core), `python/CMakeLists.txt`, a nanobind module in `python/src/_native.cpp`, and `python/src/ostia/<component>/__init__.py`. Copy `telemetry/python/` as a starting point.
- **Never add `ostia/__init__.py`.** The packages share the `ostia` namespace (PEP 420).
- The extension links the installed `ostia::<component>` and never builds or bundles native libraries.
- `pixi run py-dev` picks up the new package automatically, in rank order.

## How layering is enforced

The three checks report a component's own direct edges. A dependency that arrives through another component's public headers is allowed.

1. **Configure time:** `ostia_add_component` rejects a `DEPENDS` entry that is not in the table, a wrong `RANK`, an unknown component, and a disabled dependency.
2. **After all targets exist:** a link walk over each component's `LINK_LIBRARIES` and `INTERFACE_LINK_LIBRARIES` rejects edges not in the table, including `ostia` names inside generator expressions. `pixi run check-graph` then checks the graph CMake resolved (`cmake --graphviz`), which catches what the generator expressions expand to.
3. **Includes:** `tools/ci/check_layering.py` (part of `pixi run lint`) rejects three kinds of include:
   - an `<ostia/X/...>` include of a component outside the row;
   - an include that reaches into another component's `src/`;
   - a relative include that leaves the component's folder.

Every failure names the file and line, the component's allowed dependencies, and the fix. For example:

```text
error: query/src/plan.cpp:12 includes <ostia/fabric/topology.hpp>
  query may depend on: runtime, exchange, telemetry
  rule: a component includes only its own headers and those of its table row
  fix: use an allowed component's API, or change the dependency table through an RFC
  see: RFC-0001 §3.3
```

The tests for these checks live in `tests/cmake/layering/` (fixture projects) and `tools/ci/tests/`.
