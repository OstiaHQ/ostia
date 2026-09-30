"""The error-message contract (RFC-0005 §1.3, RFC-0001 Failure handling).

A copy of tools/ci/_contract.py::violation() until Rollout PR B moves the tools into this
package (RFC-0005 §2.3); tests/test_contract.py pins the two to the same output.
"""


def violation(problem: str, details: list[str], rule: str, fix: str, see: str) -> str:
    lines = [f"error: {problem}"]
    lines += [f"  {d}" for d in details]
    lines += [f"  rule: {rule}", f"  fix: {fix}", f"  see: {see}"]
    return "\n".join(lines)
