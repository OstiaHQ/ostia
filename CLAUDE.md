# Working on Ostia

Guidance for AI coding agents and human contributors working in this repository. Read it before making changes.

## Start here

- [`docs/product/prd.md`](docs/product/prd.md): what Ostia is, its four layers, the roadmap (M0–M5) and the founding decisions D1–D12. It is the source of truth for scope.
- [`docs/README.md`](docs/README.md): how design docs work (RFCs, ADRs, guides), when an RFC is required, and the document index.
- [`CONTRIBUTING.md`](CONTRIBUTING.md): pull request expectations, the commit convention and the CLA.

Ostia is in **M0 Foundations** ([RFC-0001](docs/rfcs/0001-m0-foundations.md), Accepted). The build skeleton, layering enforcement and Python namespace packages exist; telemetry and fabric have only placeholder APIs until their RFCs land. Build and test instructions: [`docs/guides/building.md`](docs/guides/building.md).

## Layers and components

| Component | Layer | Folder (when it exists) | Depends on |
| --- | --- | --- | --- |
| ostia-fabric | 1a | `fabric/` | telemetry |
| ostia-exchange | 1b | `exchange/` | fabric, telemetry |
| ostia-runtime | 2 | `runtime/` | exchange, fabric, telemetry |
| ostia-query | 3 | `query/` | runtime, exchange, telemetry |
| ostia-telemetry | all | `telemetry/` | nothing |

Dependencies only point down. A lower layer never includes or links a higher one; `cmake/layering.json` is the table, and the build and `pixi run ostia-dev lint` enforce it (RFC-0001 §3.3).

Layer 1 is C++20 + CUDA with a stable C ABI and Python bindings (D7). Python packages share the `ostia` import namespace (PEP 420): each component installs into `ostia/<component>/`, and no package ships `ostia/__init__.py`.

## Workflow

- `main` is protected by a ruleset: every change goes through a pull request, squash merge only, and the `cla` check must pass. Direct pushes and force pushes are blocked.
- One logical change per pull request. Branch names: `<type>/<short-topic>`, for example `docs/rfc-0001-m0` or `feat/fabric-topology`.
- Follow the RFC rule in `docs/README.md`: new components, public API/ABI or wire-protocol changes, cross-layer contract changes, new dependencies and hot-path designs need an accepted RFC before code.
- Record architectural decisions as ADRs in `docs/adr/`, numbered from 0013.

## Comments

Follow [ADR-0015](docs/adr/0015-code-comments.md). A comment says why the code is this way, states an invariant, unit or hazard, or cites an RFC or ADR section. Don't narrate steps, restate the next line, leave commented-out code or banners, or write notes about your edit ("added", "as requested"); those go in the commit message. Match the comment density of the surrounding code. `pixi run ostia-dev lint` (pre-commit and CI) runs `tools/ostia-dev/src/ostia_dev/ci/check_comments.py` on every file, and a `PostToolUse` hook (`.claude/settings.json`) runs it on each edit and returns the findings; revise the comments, or mark a real false positive with `comment-ok`.

## Commits and pull requests

- Commit messages and pull request titles use gitmoji + Conventional Commits: `<emoji> <type>(<scope>): <summary>`, for example `✨ feat(fabric): add UCX backend`. The full table is in `CONTRIBUTING.md#commit-messages`. The squash commit takes the pull request title, so the title must follow the convention.
- The author is Alireza Shateri <alirezashateri7@gmail.com> (set in this repository's local git config).
- **Do not add AI attribution:** no `Co-Authored-By:` trailers for AI assistants in commits, and no "Generated with …" lines in pull request descriptions.
- Public contact address for security, conduct and CLA matters: alirezashateri7@gmail.com.

## Tools

```bash
pixi run ostia-dev build                # configure and build (dev preset)
pixi run ostia-dev test                 # C++, CMake and Python tests
pixi run ostia-dev check                # everything CI requires: lint, graph check, tests
pixi run ostia-dev py-dev               # rebuild native code and reinstall the Python editables
pixi run ostia-dev doctor               # print the environment and diagnose problems
pixi run ostia-dev clean                # remove build output and what py-dev installed
pixi run ostia-dev docs index           # regenerate the RFC/ADR index in docs/README.md
pixi run ostia-dev docs index --check   # fails if the index is stale (run before opening a docs PR)
pixi run ostia-dev docs figures         # re-render PRD figures (needs bun)
pixi run ostia-dev --help               # every other command: test variants, check <name>, bench, remote
```

New documents use Mermaid for diagrams. Hand-drawn figures keep their JSX source next to the rendered SVG.

## Environment notes

- The maintainer's workstation is macOS on Apple silicon with no CUDA toolchain. CUDA builds and GPU tests run on Linux: everyday CI plus rented GPU machines for gate benchmarks (D9). Keep the build configurable so non-CUDA parts (docs, tools, host-side logic, unit tests with mocks) work on macOS.
- Third-party GitHub Actions are pinned by commit SHA with a comment naming the version. Dependabot keeps them current.
