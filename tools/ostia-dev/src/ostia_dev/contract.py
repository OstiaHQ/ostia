"""The error-message contract (RFC-0005 §1.3). A copy of tools/ci/_contract.py until
Rollout PR B; tests/test_contract.py pins the two together.
"""


def violation(problem: str, details: list[str], rule: str, fix: str, see: str) -> str:
    lines = [f"error: {problem}"]
    lines += [f"  {d}" for d in details]
    lines += [f"  rule: {rule}", f"  fix: {fix}", f"  see: {see}"]
    return "\n".join(lines)
