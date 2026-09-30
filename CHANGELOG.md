# Changelog

All notable changes to Ostia are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and Ostia uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Before 1.0.0, minor versions may contain breaking changes; they are always listed under **Changed** or **Removed**.

## [Unreleased]

### Added

- Build system (RFC-0001 §1–§3): CMake 4.1+ with presets `dev`, `release` and `gpu-ci`; the `ostia_add_component` helper; skeleton `telemetry` and `fabric` components with placeholders for `exchange`, `runtime` and `query`; `ostia::Result<T>`.
- pixi environments `default`, `cuda-12` and `cuda-13`, with tasks `build`, `test`, `check`, `lint`, `fmt`, `py-dev`, `doctor` and `clean`.
- Layering enforcement: configure-time `DEPENDS` check, link walk, resolved-graph check and include scan, all reading `cmake/layering.json`.
- Telemetry build levels (RFC-0001 §5): `OSTIA_TELEMETRY=off|metrics|trace|debug`, `level-*` presets, the instrumentation macros, catalog handles from `telemetry.toml`, the flavour check and the macro-argument lint; tasks `test-preset`, `test-levels`, `check-macros`.
- Benchmark harness (RFC-0001 §6.1–§6.3, §6.6): result schema 1, the `ostia-bench` driver over nvbench, `compare.py` with bootstrap intervals and pooled baselines, the telemetry overhead mechanism with an A/A noise floor and self-test; tasks `bench` and `compare`; guide `docs/guides/benchmarks.md`.
- M0 gate workload programs (RFC-0001 §6.4) in `fabric/bench/`: `p2p_copy`, `pipelining`, `batching`, `dual_link`, `rdma_put`, `gdr_stream` and `tcp_put`, each with `--smoke` and checksums; oracles, bounds, transport evidence and the capability profiles in `infra/setups/`.
- CPU CI (RFC-0001 §4.1), a nightly all-levels run, and self-hosted Renovate for pixi and CPM pins (§2.4).
- Python packages `ostia-telemetry` and `ostia-fabric` in the shared `ostia` namespace (PEP 420).
- Guides `docs/guides/building.md` and `docs/guides/adding-a-component.md`; `THIRD_PARTY_NOTICES`.
- Product requirements document, design-docs process (RFCs, ADRs, generated index) and documentation tooling.
- Community files: Code of Conduct, contributing guide, security policy, support, governance, maintainers, Contributor License Agreements and issue templates.
