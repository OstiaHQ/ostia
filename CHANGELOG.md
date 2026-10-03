# Changelog

All notable changes to Ostia are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and Ostia uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Before 1.0.0, minor versions may contain breaking changes; they are always listed under **Changed** or **Removed**.

## [Unreleased]

### Added

- Topology model and fixture replay (RFC-0003 §5–§6, §9, Rollout PR 6a): the internal `ostia_fabric_topology` library, the `topo1` structural identity, closed fixture schemas, synthetic fixtures with golden tests, `ostia-topo` and `ostia-dev topo show|golden`, the `fixture-leaks` lint check, and the `OSTIA_BUILD_TOOLS` option. New dependencies: hwloc ≥ 2.4 (pixi and distro), nlohmann/json 3.12.0 (CPM).
- Build system (RFC-0001 §1–§3): CMake 4.1+ with presets `dev`, `release`, `asan-ubsan`, `tsan` and `level-*`; the `ostia_add_component` helper; skeleton `telemetry` and `fabric` components with placeholders for `exchange`, `runtime` and `query`; `ostia::Result<T>`.
- pixi environments `default`, `cuda-12` and `cuda-13`, and the `ostia-dev` contributor CLI (RFC-0005) with `build`, `test`, `check`, `lint`, `fmt`, `py-dev`, `doctor`, `clean` and `remote`.
- Layering enforcement: configure-time `DEPENDS` check, link walk, resolved-graph check and include scan, all reading `cmake/layering.json`.
- Telemetry build levels (RFC-0001 §5): `OSTIA_TELEMETRY=off|metrics|trace|debug`, `level-*` presets, the instrumentation macros, catalog handles from `telemetry.toml`, the flavour check and the macro-argument lint; `ostia-dev test --preset`, `test --levels` and `check macros`.
- Benchmark harness (RFC-0001 §6.1–§6.3, §6.6): result schema 1, the benchmark driver over nvbench, comparison with bootstrap intervals and pooled baselines, the telemetry overhead mechanism with an A/A noise floor and self-test; `ostia-dev bench run|compare|overhead`; guide `docs/guides/benchmarks.md`.
- M0 gate workload programs (RFC-0001 §6.4) in `fabric/bench/`: `p2p_copy`, `pipelining`, `batching`, `dual_link`, `rdma_put`, `gdr_stream` and `tcp_put`, each with `--smoke` and checksums; oracles, bounds, transport evidence and the capability profiles in `infra/setups/`.
- CPU CI (RFC-0001 §4.1), a nightly all-levels run, and self-hosted Renovate for pixi and CPM pins (§2.4).
- Python packages `ostia-telemetry` and `ostia-fabric` in the shared `ostia` namespace (PEP 420).
- Guides `docs/guides/building.md` and `docs/guides/adding-a-component.md`; `THIRD_PARTY_NOTICES`.
- Product requirements document, design-docs process (RFCs, ADRs, generated index) and documentation tooling.
- Community files: Code of Conduct, contributing guide, security policy, support, governance, maintainers, Contributor License Agreements and issue templates.

### Changed

- The `topo1` canonical search now prunes with automorphisms found from equal leaf certificates, so symmetric shapes that are not twins finish in milliseconds instead of minutes, and some models that hit `leaf_cap` before now get an id. No `topo1` id changed: the 43 pinned shapes and all golden fixtures match the previous algorithm (RFC-0003 §5, #33).
- Every contributor command is now an `ostia-dev` command (RFC-0005 §2, Rollout PR B), run as `pixi run ostia-dev …` or as `ostia-dev …` inside `pixi shell`. The old pixi tasks and script paths are gone, with no aliases; `pixi run ostia-dev --help` lists everything. The tools themselves moved into `tools/ostia-dev/src/ostia_dev/{ci,dev,docs,bench}/`; their behaviour, output and exit codes are unchanged apart from the names.

| Before | After |
| --- | --- |
| `pixi run build` | `pixi run ostia-dev build` |
| `pixi run test` | `pixi run ostia-dev test` |
| `pixi run test-cpp [ctest args]` | `pixi run ostia-dev test cpp [ctest args]` |
| `pixi run test-py [pytest args]` | `pixi run ostia-dev test py [pytest args]` |
| `pixi run test-rebuild` | `pixi run ostia-dev test rebuild` |
| `pixi run test-multiprocess` | `pixi run ostia-dev test -L multiprocess` |
| `pixi run test-preset <preset>` | `pixi run ostia-dev test --preset <preset>` |
| `pixi run test-levels` | `pixi run ostia-dev test --levels` |
| `pixi run sanitize-asan` | `pixi run ostia-dev test --sanitize asan-ubsan` |
| `pixi run sanitize-tsan` | `pixi run ostia-dev test --sanitize tsan` |
| `pixi run install-native` | `pixi run ostia-dev py-dev` |
| `pixi run py-dev` | `pixi run ostia-dev py-dev` |
| `pixi run lint` | `pixi run ostia-dev lint` |
| `pixi run fmt` | `pixi run ostia-dev fmt` |
| `pixi run check` | `pixi run ostia-dev check` |
| `pixi run check-graph` | `pixi run ostia-dev check graph` |
| `pixi run check-macros` | `pixi run ostia-dev check macros` |
| `pixi run tidy` | `pixi run ostia-dev check tidy` |
| `pixi run doctor` | `pixi run ostia-dev doctor` |
| `pixi run clean` | `pixi run ostia-dev clean` |
| `pixi run hooks` | `pixi run ostia-dev hooks` |
| `pixi run docs-index` | `pixi run ostia-dev docs index` |
| `pixi run docs-as-test` | `pixi run ostia-dev check docs-as-test` |
| `pixi run check-cuda` | `pixi run ostia-dev remote container --env cuda-12 --env cuda-13 --suite cuda-compile` |
| `pixi run bench run|convert|median-seconds …` | `pixi run ostia-dev bench run|convert|median-seconds …` |
| `pixi run compare …` | `pixi run ostia-dev bench compare …` |
| `python tools/ci/lint.py lint\|fmt` | `pixi run ostia-dev lint\|fmt` |
| `python tools/ci/check_layering.py` | `pixi run ostia-dev check layering` |
| `python tools/ci/check_cpm_pins.py` | `pixi run ostia-dev check cpm-pins` |
| `python tools/ci/check_exports.py` | `pixi run ostia-dev check exports` |
| `python tools/ci/check_graph.py` | `pixi run ostia-dev check graph` |
| `python tools/ci/check_telemetry_macros.py` | `pixi run ostia-dev check macros` |
| `python tools/ci/check_comments.py` | `pixi run ostia-dev check comments` |
| `python tools/ci/docs_as_test.py` | `pixi run ostia-dev check docs-as-test` |
| `tools/ci/_contract.py` | `tools/ostia-dev/src/ostia_dev/contract.py` (library) |
| `python tools/dev/py_dev.py` | `pixi run ostia-dev py-dev` |
| `python tools/dev/doctor.py` | `pixi run ostia-dev doctor` |
| `python tools/dev/clean.py` | `pixi run ostia-dev clean` |
| `tools/dev/_paths.py` | `tools/ostia-dev/src/ostia_dev/paths.py` (library) |
| `python tools/dev/check_cuda.py` | `pixi run ostia-dev remote container --env cuda-12 --env cuda-13 --suite cuda-compile` |
| `python tools/docs/gen_index.py` | `pixi run ostia-dev docs index` |
| `bun tools/docs/render-figures.js` | `pixi run ostia-dev docs figures` |
| `python tools/bench/ostia_bench.py` | `pixi run ostia-dev bench` |
| `python tools/bench/compare.py` | `pixi run ostia-dev bench compare` |
| `python tools/bench/overhead.py` | `pixi run ostia-dev bench overhead` |
| `python tools/bench/oracles.py` | `pixi run ostia-dev bench oracles` |
| `python tools/bench/bounds.py` | `pixi run ostia-dev bench bounds` |
| `python tools/bench/capabilities.py` | `pixi run ostia-dev bench capabilities` |
| `python tools/bench/evidence.py` | `pixi run ostia-dev bench evidence` |
| `tools/bench/schema.py` | `tools/ostia-dev/src/ostia_dev/bench/schema.py` (library) |
| `tools/bench/schema-v1.json` | `tools/ostia-dev/src/ostia_dev/bench/schema-v1.json` (library) |

### Removed

- The `gpu-ci` CMake preset (sm_89 only). Remote GPU runs pass `-DCMAKE_CUDA_ARCHITECTURES=<cc>-real` from the node profile instead (RFC-0005 §4.9).
- The `check-cuda` task and `tools/dev/check_cuda.py`. `pixi run ostia-dev remote container --env cuda-12 --env cuda-13 --suite cuda-compile` replaces them and also compiles the benchmarks, as `check-cuda` did.
- Every pixi task except the internal `_configure`, and the `tools/ci`, `tools/dev`, `tools/docs` and `tools/bench` script paths (see the table above). `tools/ci/install_cmake.sh` and `tools/ci/gpu_preflight.sh` stay, because they run before pixi exists.
