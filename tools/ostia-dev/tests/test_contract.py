"""The error-message contract is a copy of tools/ci/_contract.py until PR B (RFC-0005 §2.3)."""

import pytest
from ostia_dev.contract import violation

from tools.ci import _contract

CASES = [
    ("namespace ostia-test has no ResourceQuota", ["context: gke_p_z_c"], "r", "f", "RFC-0005"),
    ("x", [], "rule", "fix: a\n     or b", "RFC-0005 §4.3"),
    ("multi", ["a: 1", "b: 2", "c: 3"], "", "", ""),
]


@pytest.mark.parametrize("args", CASES)
def test_violation_matches_the_tools_ci_copy(args):
    assert violation(*args) == _contract.violation(*args)


def test_violation_shape():
    msg = violation("p", ["item: x"], "the rule", "the fix", "RFC-0005 §1.3")
    assert msg.splitlines() == [
        "error: p",
        "  item: x",
        "  rule: the rule",
        "  fix: the fix",
        "  see: RFC-0005 §1.3",
    ]
