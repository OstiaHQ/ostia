"""The error-message contract shared by every ostia-dev command (RFC-0001, Failure handling;
RFC-0005 §1.3).

Every message names the problem, the offending item, the rule that was broken, the exact
fix and the RFC section, in the same shape as cmake/OstiaMessages.cmake's ostia_fail().
"""


def violation(problem: str, details: list[str], rule: str, fix: str, see: str) -> str:
    lines = [f"error: {problem}"]
    lines += [f"  {d}" for d in details]
    lines += [f"  rule: {rule}", f"  fix: {fix}", f"  see: {see}"]
    return "\n".join(lines)
