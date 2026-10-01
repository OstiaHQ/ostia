"""The error-message contract (RFC-0005 §1.3)."""

from ostia_dev.contract import violation


def test_violation_shape():
    msg = violation("p", ["item: x"], "the rule", "the fix", "RFC-0005 §1.3")
    assert msg.splitlines() == [
        "error: p",
        "  item: x",
        "  rule: the rule",
        "  fix: the fix",
        "  see: RFC-0005 §1.3",
    ]
