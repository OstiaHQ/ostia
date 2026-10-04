# Benchmark baselines

One file per reference setup, `bench/baselines/<setup>.json` (RFC-0001 §6.3). Each holds
schema-1 records whose samples are pooled from at least two machines of that setup when
they exist, so comparisons include machine-to-machine variation on rented hardware.

- Produce one with `pixi run ostia-dev bench compare --write-baseline bench/baselines/<setup>.json --setup <setup> <run>.jsonl ...`.
- Updating a baseline is its own pull request, with the reason in its description.
- Every record carries the machine's `topo1` id in `compat.topology`, taken by the driver from
  `ostia-topo-capture --print-id`, and a baseline compares only with records of the same id.
  Record baselines with `--require-topology` so a missing id fails the run instead of
  leaving `null`; the optional top-level `devices` field (measured GPU bus IDs) is
  informational and never part of the comparison key.
- Each baseline setup needs a committed captured fixture whose id matches its records'
  `compat.topology`. Capture it with `pixi run ostia-dev topo capture`, and find the fixture
  behind an id with `pixi run ostia-dev topo which <id>`.
- Baselines recorded before topology ids existed carry `"topology": null` and are
  re-recorded.

Results of individual runs go to `bench/results/`, which git ignores.
