# Building and testing Ostia

How to build Ostia from source, run the tests, and work on the C++ and Python code. The design behind all of this is [RFC-0001 §1–§3](../rfcs/0001-m0-foundations.md). If something fails, start with `pixi run ostia-dev doctor` and the [troubleshooting](#troubleshooting) table.

## Prerequisites

- **[pixi](https://pixi.sh) 0.81 or newer.** Install it with `brew install pixi` or `curl -fsSL https://pixi.sh/install.sh | bash`. pixi supplies every other tool: CMake, Ninja, the compilers, Python, and the linters.
- **macOS 14 or newer on Apple silicon**, with the Xcode Command Line Tools (`xcode-select --install`). The macOS build is host-only: no CUDA.
- **Linux x86_64 or aarch64.** No system packages are needed.
- Intel Macs and native Windows are not supported. On Windows, use WSL2, which is the `linux-64` platform.
- **Disk:** about 1.0 GB for the `default` environment and about 70 MB for the fetched sources (30 MB) and a build (35 MB).

You don't need a GPU or a cloud account for anything on this page.

## Supported platforms

| Tier | Meaning | Platforms | What backs it |
| --- | --- | --- | --- |
| 1 | Built and tested on every PR; GPU-tested before merging GPU-affecting PRs; gate benchmarks run here | Ubuntu 24.04 x86_64 | `linux-x64-*`; `ostia-dev remote k8s --suite gpu` on an L4 node ([ADR-0014](../adr/0014-on-demand-remote-test-runs.md) rule 2) |
| 1 | Built and tested on every PR | macOS 15 arm64, host-only (no CUDA) | `macos-arm64-host` |
| 2 | Built on every PR, tested where possible | Ubuntu 24.04 aarch64; Ubuntu 22.04 and Rocky 9 x86_64 without pixi | `linux-arm64-*`, `container-ubuntu2204`, `container-rocky9` |
| 3 | Best effort | Other Linux distributions; building without pixi elsewhere | none |

CPU jobs are in `.github/workflows/ci.yml`. There is no automated GPU CI ([ADR-0014](../adr/0014-on-demand-remote-test-runs.md)): before a pull request that touches GPU code merges, a maintainer runs its GPU tests on a GPU node with `ostia-dev remote` ([remote-runs.md](remote-runs.md), [RFC-0005](../rfcs/0005-dev-cli-remote-runner.md)).

Tests labelled `gpu` skip with a reason when no CUDA device is present. With `OSTIA_REQUIRE_GPU=1` set, as on a remote run's GPU node, they fail instead.

## Quick start

From a fresh clone:

<!-- docs-as-test:start -->
```bash
pixi install
pixi run ostia-dev build
pixi run ostia-dev test
```
<!-- docs-as-test:end -->

The three commands took 48 seconds on an M-series Mac with pixi's package cache already warm. A first-ever install also downloads about 1 GB, so how long it takes depends on your connection. Later runs rebuild only what changed.

- `pixi run ostia-dev test` runs the C++ and CMake tests (ctest) and the Python tests (pytest).
- Before it runs pytest, it installs the Python packages; see [Python development](#python-development).

See a captured machine's topology, on any laptop and without a GPU:

<!-- docs-as-test:start -->
```bash
pixi run ostia-dev topo show fabric/tests/fixtures/topology/synthetic/nvswitch-hidden
```
<!-- docs-as-test:end -->

## Environments

| Environment | Platforms | What it adds | Use it for |
| --- | --- | --- | --- |
| `default` | macOS arm64, Linux x86_64 and aarch64 | Host toolchain: conda-forge Clang 19 + libc++ on macOS, GCC 14 on Linux, and hwloc. No CUDA, UCX or rdma-core | Everything on this page. About 1.0 GB |
| `cuda-12` | Linux x86_64 and aarch64 | CUDA 12.8, GCC 11, CUDA-enabled UCX and rdma-core | The CUDA floor; compile-only without a GPU |
| `cuda-13` | Linux x86_64 and aarch64 | CUDA 13.4, GCC 14, CUDA-enabled UCX and rdma-core | The newest supported CUDA |

- Run a command in another environment with `-e`, for example `pixi run -e cuda-12 ostia-dev build`.
- `OSTIA_ENABLE_CUDA` is set per environment: `OFF` in `default`, `ON` in the CUDA environments. A plain CMake build outside pixi defaults to `AUTO`.
- `OSTIA_BUILD_TOOLS` defaults to `ON` and builds `ostia-topo` (`ostia-topo-capture` joins it on Linux once the capture tool lands). With `OFF`, the configure summary says `tools: OFF (topology golden tests skipped)`.
- Ostia's own C and C++ targets build with `-Wall -Wextra -Wpedantic`. `OSTIA_WARNINGS_AS_ERRORS` adds `-Werror`; it defaults to `ON` when the `CI` environment variable is set (GitHub Actions sets it) and `OFF` otherwise, so reproduce a CI warnings failure with `CI=1 pixi run ostia-dev build` in a fresh build directory. Dependencies keep their own flags.
- Each environment builds into its own directory, `build/<env>/<preset>`, so switching environments never reuses a cache made with another compiler.
- CI also uses Linux-only environments `gcc11`, `clang`, `ucx` (UCX over TCP for the multi-process tests) and `gcc15` (only for a configure test).
- On linux/arm64 the environments take about 1.9 GB (`default`), 2.1 GB (`cuda-12`) and 2.3 GB (`cuda-13`) on disk; on macOS `default` is about 1.0 GB.

## Presets and CUDA architectures

| Preset | Build type | CUDA architectures |
| --- | --- | --- |
| `dev` | Debug (`-O0`) | `native` when a GPU is detected, otherwise the release list |
| `release` | RelWithDebInfo | The release list: SASS for `sm_80`, `sm_90`, `sm_100`, and PTX for `sm_100` |

- A `-DCMAKE_CUDA_ARCHITECTURES=...` you pass always wins, and so does `CUDAARCHS` in a plain CMake build. The presets clear `CUDAARCHS`, because conda's `cuda-nvcc` activation exports its own default list; with a preset, pass `-DCMAKE_CUDA_ARCHITECTURES` instead. The configure summary prints the choice and the reason.
- Architectures outside the list, such as `sm_120`, run through PTX JIT. Set `CUDA_CACHE_PATH` to a persistent directory so the JIT cost is paid once.

## Telemetry levels

The build compiles one of four telemetry levels (RFC-0001 §5). `OSTIA_TELEMETRY` selects it.

| Level | Compiles in |
| --- | --- |
| `off` | No telemetry: the instrumentation macros compile to nothing |
| `metrics` | Counters and histograms (the default for release builds) |
| `trace` | Plus trace events |
| `debug` | Plus debug checks (the default for the `dev` preset) |

- Presets `level-off`, `level-metrics`, `level-trace` and `level-debug` build a release at each level. `pixi run ostia-dev test --preset level-off` builds and tests one; `pixi run ostia-dev test --levels` runs all four.
- The macros (`OSTIA_COUNT`, `OSTIA_TRACE_EVENT`, `OSTIA_DEBUG_CHECK`) type-check their arguments at every level, but evaluate them only at their own. Arguments must not have side effects: `pixi run ostia-dev check macros` rejects calls, assignments and `++`/`--` in them.
- A telemetry flavour applies to the whole installed stack. A Python extension that finds a `libostia-telemetry` built at another level raises `ImportError`, naming both levels. `pixi run ostia-dev doctor` shows the installed flavour.
- To install another level into the environment, run `pixi run ostia-dev py-dev --preset level-off --build-native --force`. `pixi run ostia-dev py-dev` switches back to `dev`.
- Each component declares its metrics and trace events in `<component>/telemetry.toml` (RFC-0002 §1). The build turns the catalog into storage-free handles.

## Everyday commands

| Command | What it does |
| --- | --- |
| `pixi run ostia-dev build` | Configure (when needed) and build the `dev` preset |
| `pixi run ostia-dev test` | All tests (`test cpp` and `test py`) |
| `pixi run ostia-dev test cpp` | C++ and CMake tests; extra arguments go to ctest |
| `pixi run ostia-dev test py` | Python tests; extra arguments go to pytest |
| `pixi run ostia-dev test rebuild` | Slow tests: a native change is visible from Python after a rebuild |
| `pixi run ostia-dev py-dev` | Build, install native code into the environment, install the Python editables |
| `pixi run ostia-dev lint` | Fast checks: clang-format, ruff, gersemi, include layering, CPM pins, docs index |
| `pixi run ostia-dev fmt` | Apply clang-format, ruff and gersemi |
| `pixi run ostia-dev check` | Everything CI requires: `lint`, `check graph`, `check macros` and `test` |
| `pixi run ostia-dev check macros` | Telemetry macro arguments must not change state (parses the sources with libclang) |
| `pixi run ostia-dev test --preset <preset>` | Configure, build and test one preset, e.g. `level-off` |
| `pixi run ostia-dev test --levels` | Build and test all four telemetry levels |
| `pixi run ostia-dev check graph` | Check the resolved link graph against the layering table (reconfigures) |
| `pixi run ostia-dev check tidy` | clang-tidy over the compile database (slow) |
| `pixi run ostia-dev doctor` | Print the environment and diagnose common problems |
| `pixi run ostia-dev clean` | Remove build output and everything `py-dev` installed |
| `pixi run ostia-dev hooks` | Install the git pre-commit hook, which runs `pixi run ostia-dev lint` |
| `pixi run ostia-dev remote container --env cuda-12 --env cuda-13 --suite cuda-compile` | Compile the CUDA code with nvcc in a `linux/arm64` container (needs podman or docker) |
| `pixi run -e clang ostia-dev test --sanitize asan-ubsan` | Build and test with AddressSanitizer and UBSan (Linux); `--sanitize tsan` for ThreadSanitizer |
| `pixi run -e clang ostia-dev fuzz --time 120` | Fuzz the topology fixture JSON path with libFuzzer, ASan and UBSan for 120 seconds (Linux); extra arguments go to libFuzzer |
| `pixi run -e clang ostia-dev coverage` | C++ tests with Clang source-based coverage, then the Python tests with coverage.py (Linux); writes `cpp.lcov`, `python.xml` and a summary under `build/clang/coverage`, which CI uploads to [Codecov](https://codecov.io/gh/OstiaHQ/ostia) |
| `pixi run -e ucx ostia-dev test -L multiprocess` | Multi-process tests over UCX TCP loopback (Linux) |
| `pixi run -e cuda-12 ostia-dev bench run --bench <binary>` | Run a benchmark and record schema-1 results ([benchmarks.md](benchmarks.md)) |
| `pixi run ostia-dev bench compare --baseline <file> --candidate <file>` | Compare benchmark results with a baseline |
| `pixi run ostia-dev check docs-as-test` | Run this guide's quick start verbatim, as CI does on a fresh runner |
| `pixi run ostia-dev docs index` | Regenerate the RFC/ADR index in `docs/README.md` |

`pixi run ostia-dev --help` shows every command; each one's `--help` shows its options.

## Running one test

```bash
pixi run ostia-dev test cpp -R Result                         # ctest by name
pixi run pytest tests/python/test_namespace.py -k telemetry   # pytest, no rebuild
pixi shell                                                    # a shell with the environment, for repeated commands
```

## Python development

- Each implemented component has a Python package in `<component>/python/` that installs into `ostia/<component>/`. They share the `ostia` import namespace (PEP 420), and no package ships `ostia/__init__.py`.
- `pixi run ostia-dev py-dev` does three things:
  - builds the native libraries;
  - installs them into the pixi environment (`.pixi/envs/<env>/lib`);
  - installs each Python package as a scikit-build-core editable.
- Every extension loads the one installed `libostia-*` through a relative RPATH. `pixi run ostia-dev clean` undoes all of this.
- **After a native change, rebuild with `pixi run ostia-dev py-dev`.** Python-only changes need no rebuild.
- Don't run `pip install -e` on a component directly: it cannot find the native libraries, and its error says so.

## IDE setup

- The repository ships a `.clangd` that points at `build/default/dev/compile_commands.json`, the database of the `default` environment's `dev` preset. For another environment, change its `CompilationDatabase` line locally.
- In VS Code:
  - set `clangd.path` to `.pixi/envs/default/bin/clangd`;
  - add `--query-driver=${workspaceFolder}/.pixi/envs/*/bin/*` to `clangd.arguments`;
  - set `cmake.cmakePath` to `.pixi/envs/default/bin/cmake`;
  - select the Python interpreter `.pixi/envs/default/bin/python`.
- Alternatively, start your editor from `pixi shell`, so it inherits the environment.
- Don't point an IDE at a system CMake older than 4.1: the build stops with an error naming the fix.

## ccache

- The build uses ccache when it finds it (`OSTIA_USE_CCACHE`, on by default); pixi provides it. `ccache -s` shows the hit rate.
- The cache lives in ccache's default directory unless you set `CCACHE_DIR`.
- A `CMAKE_CXX_COMPILER_LAUNCHER` you set yourself takes precedence.

## Offline

- The first build fetches the pinned source dependencies (GoogleTest, nanobind) into `.cache/cpm`, the `CPM_SOURCE_CACHE`. After that, builds work offline. If you set `CPM_SOURCE_CACHE` yourself, pixi keeps your value.
- A failed fetch names the package, tag and commit SHA it wanted. To use a local copy instead, pass `-DCPM_<package>_SOURCE=<dir>`, for example `-DCPM_googletest_SOURCE=$HOME/src/googletest`.
- Packagers can use `CPM_USE_LOCAL_PACKAGES=ON`, or `CPM_LOCAL_PACKAGES_ONLY=ON` to forbid downloads entirely (RFC-0001 §2.1).

## Checking CUDA code on a Mac

The Mac has no CUDA toolkit. `pixi run ostia-dev remote container --env cuda-12 --env cuda-13 --suite cuda-compile` compiles the CUDA code with nvcc in a `linux/arm64` container, for both `cuda-12` and `cuda-13` (`--env cuda-13` alone for one). The container runs natively on Apple silicon, and nvcc needs no GPU. It needs [podman](https://podman.io) (`brew install podman && podman machine init && podman machine start`) or Docker. Run it before opening a pull request that changes CUDA code.

## Building without pixi

This is tier 2 or 3: supported on Ubuntu 22.04 and Rocky 9 through CI, best effort elsewhere.

- Install CMake 4.1 or newer from [Kitware](https://cmake.org/download/), Ninja, and a supported compiler: GCC 11 or newer, or Clang 17 or newer.
- Install hwloc 2.4 or newer with its headers: `libhwloc-dev` on Debian and Ubuntu, `hwloc-devel` on Rocky and RHEL (enable the CRB repository first).
- Configure and build:
  ```bash
  cmake --preset release -DOSTIA_ENABLE_CUDA=OFF
  cmake --build --preset release
  ctest --preset release
  ```
  Without pixi the build directory is `build/<preset>`, and `OSTIA_ENABLE_CUDA` defaults to `AUTO`.
- Python bindings need the pixi workflow, or a native install into the Python prefix followed by `pip install <component>/python`.

## Update your checkout

- **After `pixi.lock` changes** (for example after `git pull`), run `pixi install`.
  - To resolve a merge conflict in `pixi.lock`, run `git checkout --theirs pixi.lock && pixi lock`.
  - If a `pixi run` changed the lock when you didn't edit `pixi.toml`, run `git checkout pixi.lock`.
- **After changes to `CMakePresets.json` or `cmake/`**, reconfigure from scratch with `rm -rf build/<env>/dev`, then `pixi run ostia-dev build`.
- **To reset the build and the Python install**, run `pixi run ostia-dev clean`.
- **For a full reset**, run `pixi clean && rm -rf build`. This keeps `.cache/cpm`, so nothing is downloaded again.
- Renamed commands or presets are listed in `CHANGELOG.md` with their replacement.

## Troubleshooting

Every Ostia error follows one contract: the problem, the offending item, the rule that was broken, the exact fix, and the RFC section. `pixi run ostia-dev doctor` prints the environment (compiler, CUDA and why, architectures, components, dependencies) and checks for the problems below.

| Symptom | Cause | Fix |
| --- | --- | --- |
| `CMake 3.x ... is too old`, or `Unrecognized "version" field` | A system CMake (or an IDE) is used instead of pixi's | Run through pixi (`pixi run ostia-dev build`), or point the IDE at `.pixi/envs/default/bin/cmake` |
| Warning `AppleClang is best effort` | Configured with Xcode's compiler outside pixi | Build through pixi, which uses conda-forge Clang 19 |
| Odd configure errors after switching branches | Stale build directory | `rm -rf build/<env>/dev`, then `pixi run ostia-dev build` |
| `No module named 'ostia'` | The Python packages are not installed | `pixi run ostia-dev py-dev` |
| `ostia.telemetry native extension failed to load` | Native libraries removed or out of date | `pixi run ostia-dev py-dev` |
| `pixi.lock` shows up as modified | A `pixi run` re-solved the lock after `pixi.toml` changed | Commit it if you changed `pixi.toml`, otherwise `git checkout pixi.lock` |
| `xcrun: error` or missing SDK headers on macOS | Command Line Tools missing | `xcode-select --install` |
| `ostia: <package> ... offline or failing?` before a fetch error | No network, or a pinned tag removed upstream | See [Offline](#offline); a removed tag is fixed by a pin-bump PR |
