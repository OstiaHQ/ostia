# Benchmark baselines

One file per reference setup, `bench/baselines/<setup>.json` (RFC-0001 §6.3). Each holds
schema-1 records whose samples are pooled from at least two machines of that setup when
they exist, so comparisons include machine-to-machine variation on rented hardware.

- Produce one with `pixi run compare --write-baseline bench/baselines/<setup>.json --setup <setup> <run>.jsonl ...`.
- Updating a baseline is its own pull request, with the reason in its description.
- Baselines recorded before topology fixtures land (Rollout PR 6) carry `"topology": null`
  and are re-recorded once they do.

Results of individual runs go to `bench/results/`, which git ignores.
