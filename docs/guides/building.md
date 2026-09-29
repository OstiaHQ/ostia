# Building and testing Ostia

How to build Ostia from source, run the tests, and work on the C++ and Python code. The design behind all of this is [RFC-0001 §1–§3](../rfcs/0001-m0-foundations.md). If something fails, start with `pixi run doctor` and the [troubleshooting](#troubleshooting) table.

## Prerequisites

- **[pixi](https://pixi.sh) 0.81 or newer.** Install it with `brew install pixi` or `curl -fsSL https://pixi.sh/install.sh | bash`. pixi supplies every other tool: CMake, Ninja, the compilers, Python, and the linters.
- **macOS 14 or newer on Apple silicon**, with the Xcode Command Line Tools (`xcode-select --install`). The macOS build is host-only: no CUDA.
- **Linux x86_64 or aarch64.** No system packages are needed.
- Intel Macs and native Windows are not supported. On Windows, use WSL2, which is the `linux-64` platform.
- **Disk:** about 1.0 GB for the `default` environment and about 70 MB for the fetched sources (30 MB) and a build (35 MB).

You don't need a GPU or a cloud account for anything on this page.

## Supported platforms

| Tier | Meaning | Platforms | CI job that backs it |
| --- | --- | --- | --- |
| 1 | Built and tested on every PR; gate benchmarks run here | Ubuntu 24.04 x86_64 | `linux-x64-*`, `gpu-l4` |
| 1 | Built and tested on every PR | macOS 15 arm64, host-only (no CUDA) | `macos-arm64-host` |
| 2 | Built on every PR, tested where possible | Ubuntu 24.04 aarch64; Ubuntu 22.04 and Rocky 9 x86_64 without pixi | `linux-arm64-*`, `container-ubuntu2204`, `container-rocky9` |
| 3 | Best effort | Other Linux distributions; building without pixi elsewhere | none |

CPU jobs are in `.github/workflows/ci.yml`; GPU jobs arrive with RFC-0001 Rollout PR 4.

## Quick start

From a fresh clone:

<!-- docs-as-test:start -->
```bash
pixi install
pixi run build
pixi run test
```
<!-- docs-as-test:end -->

The three commands took 48 seconds on an M-series Mac with pixi's package cache already warm. A first-ever install also downloads about 1 GB, so how long it takes depends on your connection. Later runs rebuild only what changed.

- `pixi run test` runs the C++ and CMake tests (ctest) and the Python tests (pytest).
- Before it runs pytest, it installs the Python packages; see [Python development](#python-development).

## Environments

| Environment | Platforms | What it adds | Use it for |
| --- | --- | --- | --- |
| `default` | macOS arm64, Linux x86_64 and aarch64 | Host toolchain: conda-forge Clang 19 + libc++ on macOS, GCC 14 on Linux. No CUDA, UCX or rdma-core | Everything on this page. About 1.0 GB |
| `cuda-12` | Linux x86_64 and aarch64 | CUDA 12.8, GCC 11, CUDA-enabled UCX and rdma-core | The CUDA floor; compile-only without a GPU |
| `cuda-13` | Linux x86_64 and aarch64 | CUDA 13.4, GCC 14, CUDA-enabled UCX and rdma-core | The newest supported CUDA |

- Run a task in another environment with `-e`, for example `pixi run -e cuda-12 build`.
- `OSTIA_ENABLE_CUDA` is set per environment: `OFF` in `default`, `ON` in the CUDA environments. A plain CMake build outside pixi defaults to `AUTO`.
- Each environment builds into its own directory, `build/<env>/<preset>`, so switching environments never reuses a cache made with another compiler.
- CI also uses Linux-only environments `gcc11`, `clang`, `ucx` (UCX over TCP for the multi-process tests) and `gcc15` (only for a configure test).
- On linux/arm64 the environments take about 1.9 GB (`default`), 2.1 GB (`cuda-12`) and 2.3 GB (`cuda-13`) on disk; on macOS `default` is about 1.0 GB.

## Presets and CUDA architectures

| Preset | Build type | CUDA architectures |
| --- | --- | --- |
| `dev` | Debug (`-O0`) | `native` when a GPU is detected, otherwise the release list |
| `release` | RelWithDebInfo | The release list: SASS for `sm_80`, `sm_90`, `sm_100`, and PTX for `sm_100` |
| `gpu-ci` | RelWithDebInfo | `sm_89` only (the L4 CI runner) |

- A `-DCMAKE_CUDA_ARCHITECTURES=...` you pass always wins, and so does `CUDAARCHS` in a plain CMake build. The presets clear `CUDAARCHS`, because conda's `cuda-nvcc` activation exports its own default list; with a preset, pass `-DCMAKE_CUDA_ARCHITECTURES` instead. The configure summary prints the choice and the reason.
- Architectures outside the list, such as `sm_120`, run through PTX JIT. Set `CUDA_CACHE_PATH` to a persistent directory so the JIT cost is paid once.

## Everyday tasks

| Task | What it does |
| --- | --- |
| `pixi run build` | Configure (when needed) and build the `dev` preset |
| `pixi run test` | All tests (`test-cpp` and `test-py`) |
| `pixi run test-cpp` | C++ and CMake tests; extra arguments go to ctest |
| `pixi run test-py` | Python tests; extra arguments go to pytest |
| `pixi run test-rebuild` | Slow tests: a native change is visible from Python after a rebuild |
| `pixi run py-dev` | Build, install native code into the environment, install the Python editables |
| `pixi run lint` | Fast checks: clang-format, ruff, gersemi, include layering, CPM pins, docs index |
| `pixi run fmt` | Apply clang-format, ruff and gersemi |
| `pixi run check` | Everything CI requires: `lint`, `check-graph` and `test` |
| `pixi run check-graph` | Check the resolved link graph against the layering table (reconfigures) |
| `pixi run tidy` | clang-tidy over the compile database (slow) |
| `pixi run doctor` | Print the environment and diagnose common problems |
| `pixi run clean` | Remove build output and everything `py-dev` installed |
| `pixi run hooks` | Install the git pre-commit hook, which runs `pixi run lint` |
| `pixi run check-cuda` | Compile the CUDA code with nvcc in a `linux/arm64` container (needs podman or docker) |
| `pixi run -e clang sanitize-asan` | Build and test with AddressSanitizer and UBSan (Linux); `sanitize-tsan` for ThreadSanitizer |
| `pixi run -e ucx test-multiprocess` | Multi-process tests over UCX TCP loopback (Linux) |
| `pixi run docs-as-test` | Run this guide's quick start verbatim, as CI does on a fresh runner |
| `pixi run docs-index` | Regenerate the RFC/ADR index in `docs/README.md` |

`pixi task list` shows the same list.

## Running one test

```bash
pixi run test-cpp -R Result                                   # ctest by name
pixi run pytest tests/python/test_namespace.py -k telemetry   # pytest, no rebuild
pixi shell                                                    # a shell with the environment, for repeated commands
```

## Python development

- Each implemented component has a Python package in `<component>/python/` that installs into `ostia/<component>/`. They share the `ostia` import namespace (PEP 420), and no package ships `ostia/__init__.py`.
- `pixi run py-dev` does three things:
  - builds the native libraries;
  - installs them into the pixi environment (`.pixi/envs/<env>/lib`);
  - installs each Python package as a scikit-build-core editable.
- Every extension loads the one installed `libostia-*` through a relative RPATH. `pixi run clean` undoes all of this.
- **After a native change, rebuild with `pixi run py-dev`.** Python-only changes need no rebuild.
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

The Mac has no CUDA toolkit. `pixi run check-cuda` compiles the CUDA code with nvcc in a `linux/arm64` container, for both `cuda-12` and `cuda-13` (`pixi run check-cuda cuda-13` for one). The container runs natively on Apple silicon, and nvcc needs no GPU. It needs [podman](https://podman.io) (`brew install podman && podman machine init && podman machine start`) or Docker. Run it before asking for the `ci:gpu` label.

## Building without pixi

This is tier 2 or 3: supported on Ubuntu 22.04 and Rocky 9 through CI, best effort elsewhere.

- Install CMake 4.1 or newer from [Kitware](https://cmake.org/download/), Ninja, and a supported compiler: GCC 11 or newer, or Clang 17 or newer.
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
- **After changes to `CMakePresets.json` or `cmake/`**, reconfigure from scratch with `rm -rf build/<env>/dev`, then `pixi run build`.
- **To reset the build and the Python install**, run `pixi run clean`.
- **For a full reset**, run `pixi clean && rm -rf build`. This keeps `.cache/cpm`, so nothing is downloaded again.
- Renamed tasks or presets are listed in `CHANGELOG.md` with their replacement.

## Troubleshooting

Every Ostia error follows one contract: the problem, the offending item, the rule that was broken, the exact fix, and the RFC section. `pixi run doctor` prints the environment (compiler, CUDA and why, architectures, components, dependencies) and checks for the problems below.

| Symptom | Cause | Fix |
| --- | --- | --- |
| `CMake 3.x ... is too old`, or `Unrecognized "version" field` | A system CMake (or an IDE) is used instead of pixi's | Run through pixi (`pixi run build`), or point the IDE at `.pixi/envs/default/bin/cmake` |
| Warning `AppleClang is best effort` | Configured with Xcode's compiler outside pixi | Build through pixi, which uses conda-forge Clang 19 |
| Odd configure errors after switching branches | Stale build directory | `rm -rf build/<env>/dev`, then `pixi run build` |
| `No module named 'ostia'` | The Python packages are not installed | `pixi run py-dev` |
| `ostia.telemetry native extension failed to load` | Native libraries removed or out of date | `pixi run py-dev` |
| `pixi.lock` shows up as modified | A `pixi run` re-solved the lock after `pixi.toml` changed | Commit it if you changed `pixi.toml`, otherwise `git checkout pixi.lock` |
| `xcrun: error` or missing SDK headers on macOS | Command Line Tools missing | `xcode-select --install` |
| `ostia: <package> ... offline or failing?` before a fetch error | No network, or a pinned tag removed upstream | See [Offline](#offline); a removed tag is fixed by a pin-bump PR |
