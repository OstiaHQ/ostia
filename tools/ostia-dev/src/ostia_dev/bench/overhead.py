#!/usr/bin/env python3
"""The telemetry overhead mechanism (RFC-0001 §6.6).

    ostia-dev bench overhead --off <cmd> --level <cmd>     # the gate: level against off, same box
    ostia-dev bench overhead --aa <cmd>                    # A/A noise floor: off against off
    ostia-dev bench overhead --self-test <cmd>             # 3% injected must fail, 0% must pass

Each command prints one duration in seconds as its last output line. Runs are paired
and interleaved (off, level, then level, off, ...), at least 20 pairs. For each pair the
overhead is r = t_level / t_off - 1, and a 95% bootstrap interval of the mean r decides:
pass if its upper bound is below the limit (2%), fail if its lower bound is above it,
otherwise 10 more pairs, up to 100, after which it fails. An A/A run must show a noise
floor (interval half-width) of at most 0.5%, or the gate moves to a quiet rented box
(--target, run with RFC-0004's tooling). The acceptance gate itself activates with
RFC-0002's implementation (Rollout PR 8); PR 5a lands this mechanism.
"""

import argparse
import os
import random
import statistics
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass

LIMIT = 0.02
NOISE_FLOOR = 0.005
MIN_PAIRS, MAX_PAIRS, STEP = 20, 100, 10
RESAMPLES = 10_000
SEED = 20260926
INJECT_ENV = "OSTIA_BENCH_INJECT_SLOWDOWN"


@dataclass
class Decision:
    outcome: str  # pass | fail | undecided
    mean: float
    lo: float
    hi: float
    pairs: int


def _interval(values: list[float]) -> tuple[float, float]:
    rng = random.Random(SEED)
    boot = sorted(statistics.fmean(rng.choices(values, k=len(values))) for _ in range(RESAMPLES))
    return boot[int(0.025 * RESAMPLES)], boot[int(0.975 * RESAMPLES) - 1]


def decide(pairs: list[tuple[float, float]], limit: float = LIMIT) -> Decision:
    r = [level / off - 1 for off, level in pairs]
    lo, hi = _interval(r)
    outcome = "pass" if hi < limit else "fail" if lo > limit else "undecided"
    return Decision(outcome, statistics.fmean(r), lo, hi, len(pairs))


def noise_floor(pairs: list[tuple[float, float]]) -> float:
    """Half-width of the interval of the mean r for an A/A run."""
    lo, hi = _interval([b / a - 1 for a, b in pairs])
    return (hi - lo) / 2


def measure(
    run_off: Callable[[], float],
    run_level: Callable[[], float],
    min_pairs: int = MIN_PAIRS,
    max_pairs: int = MAX_PAIRS,
    limit: float = LIMIT,
) -> Decision:
    pairs: list[tuple[float, float]] = []

    def one_pair(i: int) -> tuple[float, float]:
        if i % 2 == 0:  # alternate the order so drift does not bias one side
            off = run_off()
            return off, run_level()
        level = run_level()
        return run_off(), level

    while len(pairs) < min_pairs:
        pairs.append(one_pair(len(pairs)))
    d = decide(pairs, limit)
    while d.outcome == "undecided" and len(pairs) < max_pairs:
        for _ in range(min(STEP, max_pairs - len(pairs))):
            pairs.append(one_pair(len(pairs)))
        d = decide(pairs, limit)
    if d.outcome == "undecided":
        d.outcome = "fail"  # still undecided at the cap
    return d


def self_test(inject: float, run: Callable[[float], float] | None = None) -> Decision:
    """Runs the mechanism with `inject` slowdown on the level side. Without `run`, a
    synthetic workload with 0.2% noise stands in (for unit tests)."""
    if run is None:
        rng = random.Random(7)

        def run(extra: float) -> float:
            return (1.0 + rng.gauss(0, 0.002)) * (1 + extra)

    return measure(lambda: run(0.0), lambda: run(inject))


def _command(cmd: str, inject: float = 0.0) -> Callable[[], float]:
    env = dict(os.environ, **{INJECT_ENV: str(inject)})

    def run() -> float:
        out = subprocess.run(cmd, shell=True, capture_output=True, text=True, env=env)
        if out.returncode != 0:
            sys.stderr.write(out.stderr)
            print(f"error: {cmd} exited {out.returncode}", file=sys.stderr)
            raise SystemExit(1)
        return float(out.stdout.strip().splitlines()[-1])

    return run


def _report(label: str, d: Decision) -> None:
    print(
        f"{label}: {d.outcome} (mean r {d.mean:+.3%}, 95% [{d.lo:+.3%}, {d.hi:+.3%}], "
        f"{d.pairs} pairs, limit {LIMIT:.0%})"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ostia-dev bench overhead", description=__doc__.splitlines()[0]
    )
    parser.add_argument("--off")
    parser.add_argument("--level")
    parser.add_argument("--aa", metavar="CMD")
    parser.add_argument("--self-test", metavar="CMD")
    parser.add_argument(
        "--target",
        default="local",
        help="where it ran: local, l4 (a GPU node, with ostia-dev remote), or rented",
    )
    args = parser.parse_args(argv)
    print(f"target: {args.target}")
    if args.self_test:
        bad = measure(_command(args.self_test), _command(args.self_test, 0.03))
        good = measure(_command(args.self_test), _command(args.self_test, 0.0))
        _report("self-test, 3% injected", bad)
        _report("self-test, 0% injected", good)
        return 0 if (bad.outcome, good.outcome) == ("fail", "pass") else 1
    if args.aa:
        run = _command(args.aa)
        pairs = [(run(), run()) for _ in range(MIN_PAIRS * 2)]
        floor = noise_floor(pairs)
        print(f"A/A noise floor: ±{floor:.3%} (must be ≤ {NOISE_FLOOR:.1%})")
        if floor > NOISE_FLOOR:
            print(
                "error: this box is too noisy for the 2% overhead gate\n"
                "  fix: run the gate on a quiet rented box (--target rented, RFC-0004)\n"
                "  see: RFC-0001 §6.6"
            )
            return 1
        return 0
    if not (args.off and args.level):
        parser.error("give --off and --level, --aa, or --self-test")
    d = measure(_command(args.off), _command(args.level))
    _report("overhead", d)
    return 0 if d.outcome == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
