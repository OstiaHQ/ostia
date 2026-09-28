"""Tests for tools/bench/overhead.py (RFC-0001 §6.6)."""

import random

from tools.bench.overhead import decide, measure, noise_floor, self_test


def pairs(overhead, n=40, sd=0.002, seed=3):
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        base = 1.0 + rng.gauss(0, sd)
        out.append((base, base * (1 + overhead) + rng.gauss(0, sd)))
    return out


def test_no_overhead_passes():
    assert decide(pairs(0.0)).outcome == "pass"


def test_three_percent_fails():
    assert decide(pairs(0.03)).outcome == "fail"


def test_near_the_limit_is_undecided():
    # Overheads alternate 1% and 3%: the mean is exactly the limit.
    straddling = [(1.0, 1.01 if i % 2 else 1.03) for i in range(40)]
    assert decide(straddling).outcome == "undecided"


def test_measure_interleaves_and_alternates_order():
    calls = []

    def run(level):
        calls.append(level)
        return 1.0

    measure(lambda: run("off"), lambda: run("level"), min_pairs=4, max_pairs=4)
    assert calls == ["off", "level", "level", "off", "off", "level", "level", "off"]


def test_measure_extends_until_decided_then_fails_at_the_cap():
    rng = random.Random(5)
    result = measure(
        lambda: 1.0 + rng.gauss(0, 0.05),
        lambda: 1.02 + rng.gauss(0, 0.05),
        min_pairs=20,
        max_pairs=100,
    )
    assert result.pairs == 100 and result.outcome == "fail"


def test_noise_floor_reports_the_half_width():
    assert noise_floor(pairs(0.0, sd=0.0005)) < 0.005
    assert noise_floor(pairs(0.0, sd=0.05)) > 0.005


def test_self_test_detects_three_percent_and_passes_zero():
    assert self_test(0.03).outcome == "fail"
    assert self_test(0.0).outcome == "pass"
