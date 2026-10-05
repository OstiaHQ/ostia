#!/usr/bin/env python3
"""Compare benchmark results with a baseline (RFC-0001 §6.3).

    ostia-dev bench compare --baseline bench/baselines/<setup>.json --candidate results.jsonl \\
        [--manifest cases.json] [--require-pass] [--evidence-dir <run>/evidence]
    ostia-dev bench compare --write-baseline bench/baselines/<setup>.json --setup <setup> \\
        run1.jsonl run2.jsonl

Outcomes per case:
- `pass`: the change is below the threshold with 95% confidence;
- `regression`: it is above the threshold with 95% confidence;
- `inconclusive`: neither, or fewer than 10 samples on a side;
- `invalid`: missing, duplicate, malformed or non-finite results, or missing files; with
  --evidence-dir, also a gate case without evidence of the transport it tests (§6.4);
- `skipped`: the compatibility fields differ, so the results are not comparable.

The change is d = (candidate median - baseline median) / baseline median, signed so that
positive means worse. Its 95% interval comes from a bootstrap with 10,000 resamples and a
fixed seed, so runs are reproducible. A required gate (--require-pass) never passes on
`inconclusive`. Baselines pool samples from several machines of one setup, so the
interval includes machine-to-machine variation.
"""

import argparse
import json
import random
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ostia_dev.bench import evidence
from ostia_dev.bench.schema import COMPAT, SchemaError, case_key, compat_key, load, validate

THRESHOLD = 0.05
MIN_SAMPLES = 10
RESAMPLES = 10_000
SEED = 20260925


@dataclass
class Outcome:
    outcome: str
    d: float = float("nan")
    lo: float = float("nan")
    hi: float = float("nan")


@dataclass
class CaseResult:
    bench: str
    params: str
    outcome: str
    detail: str = ""
    d: float = float("nan")
    lo: float = float("nan")
    hi: float = float("nan")


def _worse(base_median: float, cand_median: float, higher_is_better: bool) -> float:
    change = (cand_median - base_median) / base_median
    return -change if higher_is_better else change


def compare_case(
    baseline: list[float],
    candidate: list[float],
    higher_is_better: bool,
    threshold: float = THRESHOLD,
) -> Outcome:
    if len(baseline) < MIN_SAMPLES or len(candidate) < MIN_SAMPLES:
        return Outcome("inconclusive")
    rng = random.Random(SEED)
    d = _worse(statistics.median(baseline), statistics.median(candidate), higher_is_better)
    boot = sorted(
        _worse(
            statistics.median(rng.choices(baseline, k=len(baseline))),
            statistics.median(rng.choices(candidate, k=len(candidate))),
            higher_is_better,
        )
        for _ in range(RESAMPLES)
    )
    lo, hi = boot[int(0.025 * RESAMPLES)], boot[int(0.975 * RESAMPLES) - 1]
    if hi < threshold:
        outcome = "pass"
    elif lo > threshold:
        outcome = "regression"
    else:
        outcome = "inconclusive"
    return Outcome(outcome, d, lo, hi)


def _evidence_problem(bench: str, evidence_dir: Path) -> str:
    path = evidence_dir / f"{bench}.json"
    try:
        ev = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return f"no transport evidence ({path.name})"
    return "; ".join(evidence.check(ev))


def _short(topology_id: str | None) -> str:
    return "null" if topology_id is None else topology_id.rpartition(":")[2][:12]


def _topology_note(base: str | None, cand: str | None) -> str:
    if base is None or cand is None:
        return (
            f"topology is {_short(base)} in the baseline and {_short(cand)} in the candidate; "
            "re-record the baseline (or the run) with a topology id"
        )
    return (
        f"topology {_short(base)} (baseline) vs {_short(cand)} (candidate); "
        f"fix: pixi run ostia-dev topo which {_short(cand)}"
    )


def compare_runs(
    baseline: list[dict],
    candidate: list[dict],
    manifest: list[dict] | None = None,
    evidence_dir: Path | None = None,
) -> list[CaseResult]:
    results: list[CaseResult] = []
    by_case: dict[tuple, list[dict]] = {}
    for r in candidate:
        by_case.setdefault(case_key(r), []).append(r)
    base_by_case = {case_key(r): r for r in baseline}
    expected = [(m["bench"], json.dumps(m["params"], sort_keys=True)) for m in manifest or []]
    for key in expected:
        if key not in by_case:
            results.append(CaseResult(*key, "invalid", "missing from the candidate results"))
    for key, records in by_case.items():
        if len(records) > 1:
            results.append(CaseResult(*key, "invalid", f"duplicate case ({len(records)} records)"))
            continue
        cand = records[0]
        if evidence_dir is not None and (problem := _evidence_problem(key[0], evidence_dir)):
            results.append(CaseResult(*key, "invalid", problem))
            continue
        base = base_by_case.get(key)
        if base is None:
            results.append(CaseResult(*key, "skipped", "no baseline for this case"))
            continue
        if compat_key(base) != compat_key(cand):
            diff = [f for f in COMPAT if base["compat"].get(f) != cand["compat"].get(f)]
            note = "compat fields differ: " + ", ".join(diff)
            if "topology" in diff:
                note += "; " + _topology_note(
                    base["compat"]["topology"], cand["compat"]["topology"]
                )
            results.append(CaseResult(*key, "skipped", note))
            continue
        o = compare_case(base["samples"], cand["samples"], cand["higher_is_better"])
        results.append(CaseResult(*key, o.outcome, "", o.d, o.lo, o.hi))
    return results


def pool_baseline(runs: list[list[dict]], setup: str) -> dict:
    """One baseline from runs on several machines of the same setup."""
    pooled: dict[tuple, dict] = {}
    for records in runs:
        for r in records:
            key = case_key(r)
            if key not in pooled:
                pooled[key] = dict(r, samples=list(r["samples"]), machines=1)
                continue
            p = pooled[key]
            if compat_key(p) != compat_key(r):
                raise ValueError(f"{r['bench']}: incompatible records cannot be pooled")
            p["samples"] += r["samples"]
            p["machines"] += 1
    for p in pooled.values():
        s = sorted(p["samples"])
        p["median"] = statistics.median(s)
        p["p5"], p["p95"] = s[int(0.05 * (len(s) - 1))], s[int(0.95 * (len(s) - 1))]
    return {"schema": 1, "setup": setup, "records": list(pooled.values())}


def load_baseline(path: Path) -> list[dict]:
    if path.suffix == ".json":
        data = json.loads(path.read_text())
        for r in data["records"]:
            validate(r)
        return data["records"]
    return load(path)


def _summary(results: list[CaseResult]) -> str:
    lines = ["| Case | Outcome | d | 95% interval | Detail |", "| --- | --- | --- | --- | --- |"]
    for r in results:
        interval = f"[{r.lo:+.2%}, {r.hi:+.2%}]" if r.lo == r.lo else ""
        d = f"{r.d:+.2%}" if r.d == r.d else ""
        lines.append(f"| {r.bench} {r.params} | {r.outcome} | {d} | {interval} | {r.detail} |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ostia-dev bench compare", description=__doc__.splitlines()[0]
    )
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--manifest", type=Path, help="JSON list of expected {bench, params}")
    parser.add_argument("--require-pass", action="store_true", help="inconclusive fails (gates)")
    parser.add_argument(
        "--evidence-dir", type=Path, help="require transport evidence for every case (gates)"
    )
    parser.add_argument("--write-baseline", type=Path)
    parser.add_argument("--setup")
    parser.add_argument("runs", nargs="*", type=Path, help="result files to pool into a baseline")
    args = parser.parse_args(argv)

    if args.write_baseline:
        baseline = pool_baseline(
            [load(p) for p in args.runs], args.setup or args.write_baseline.stem
        )
        args.write_baseline.parent.mkdir(parents=True, exist_ok=True)
        args.write_baseline.write_text(json.dumps(baseline, indent=2) + "\n")
        cases = len(baseline["records"])
        print(f"wrote {args.write_baseline}: {cases} cases from {len(args.runs)} runs")
        return 0

    try:
        baseline = load_baseline(args.baseline)
        if not args.candidate.exists() or not args.candidate.read_text().strip():
            raise SchemaError(f"{args.candidate}: missing or empty result file")
        candidate = load(args.candidate)
    except (SchemaError, OSError, json.JSONDecodeError) as e:
        print(f"invalid: {e}")
        return 1
    manifest = json.loads(args.manifest.read_text()) if args.manifest else None
    results = compare_runs(baseline, candidate, manifest, args.evidence_dir)
    print(_summary(results))
    bad = {"regression", "invalid"} | ({"inconclusive"} if args.require_pass else set())
    failed = [r for r in results if r.outcome in bad]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
