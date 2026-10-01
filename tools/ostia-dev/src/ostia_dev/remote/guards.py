"""The pipeline guards (RFC-0005 §3.2), evaluated CLI-side on the collected results."""

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from ostia_dev.contract import violation


@dataclass
class Verdict:
    test_code: int = 0
    infra: bool = False
    failing_step: str | None = None
    message: str = ""
    reports: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class Case:
    name: str
    labels: tuple[str, ...]
    skipped: bool
    failed: bool


def parse_junit(text: str) -> list[Case]:
    """ctest's --output-junit: labels in <property name="cmake_labels" value="a;b"/>."""
    root = ET.fromstring(text)
    cases = []
    for tc in root.iter("testcase"):
        labels = ()
        for prop in tc.iter("property"):
            if prop.get("name") == "cmake_labels":
                labels = tuple(sorted(filter(None, (prop.get("value") or "").split(";"))))
        skipped = tc.get("status") in ("notrun", "disabled") or tc.find("skipped") is not None
        failed = tc.get("status") == "fail" or tc.find("failure") is not None
        cases.append(Case(tc.get("name", "?"), labels, skipped, failed))
    return cases


def _infra(step: str | None, msg: str) -> Verdict:
    return Verdict(test_code=0, infra=True, failing_step=step, message=msg)


def evaluate(
    steps: dict,
    junits: dict[str, str],
    *,
    gpu_profile: bool,
    planned: list[str],
    oom: bool = False,
    memory: str = "",
    profile: str = "",
    expect_summary: str | None = None,
    summary_text: str | None = None,
) -> Verdict:
    ran = steps.get("steps", [])
    reports = {s["name"]: s["code"] for s in ran if s["kind"] == "report"}
    last = ran[-1]["name"] if ran else None

    def done(v: Verdict) -> Verdict:
        v.reports = reports
        return v

    if oom:
        return done(_infra(last, violation(
            f"step {last} was killed for running out of memory",
            [f"memory limit: {memory}"],
            "a pod gets the memory its profile requests; an OOM kill is an infrastructure "
            "failure, not a test result",
            f"raise memory in [remote.k8s.profiles.{profile}], or lower the build parallelism",
            "RFC-0005 §3.2",
        )))  # fmt: skip
    state = steps.get("state")
    if state == "code_wait_timeout" or steps.get("code_wait") == "timeout":
        return done(_infra(None, "the code never arrived in the pod within the code wait"))
    if state == "timeout":
        return done(_infra(last, f"the pipeline hit its --timeout in step {last} (timeout)"))
    if state == "terminated":
        return done(_infra(last, f"the run was terminated during step {last}"))
    for s in ran:
        if s["code"] == 0 or s["kind"] == "report":
            continue
        if s["kind"] in ("preflight", "install"):
            return done(
                _infra(s["name"], f"{s['kind']} step {s['name']} failed (exit {s['code']})")
            )
        return done(Verdict(1, False, s["name"], f"step {s['name']} failed (exit {s['code']})"))
    if [s["name"] for s in ran] != planned:
        missing = [n for n in planned if n not in {s["name"] for s in ran}]
        return done(_infra(last, f"the supervisor stopped before steps {', '.join(missing)}"))
    if expect_summary and expect_summary not in (summary_text or "").splitlines():
        return done(_infra("configure", violation(
            "the configure summary does not show the GPU architecture the suite expects",
            [f"expected: {expect_summary}"],
            "the dev preset picks native when it detects the GPU (RFC-0005 §4.9)",
            "check the fingerprint and the profile's env (PATH, LD_LIBRARY_PATH)",
            "RFC-0005 §4.9",
        )))  # fmt: skip
    command = next((s["name"] for s in reversed(ran) if s["kind"] == "command"), last)
    for name, text in sorted(junits.items()):
        try:
            cases = parse_junit(text)
        except ET.ParseError as e:
            return done(Verdict(1, False, command, f"{name} is not valid junit: {e}"))
        if not cases:
            return done(Verdict(1, False, command, violation(
                f"ctest found no tests ({name} holds zero testcases)",
                [],
                "a run that tested nothing fails (RFC-0005 §3.2)",
                "check the ctest arguments (-L, -R) and that the build directory has tests",
                "RFC-0005 §3.2",
            )))  # fmt: skip
        if gpu_profile:
            skipped = [c.name for c in cases if c.skipped and "gpu" in c.labels]
            if skipped:
                return done(_infra(command, violation(
                    f"gpu-labelled tests were skipped or not run on a GPU profile ({name})",
                    [f"tests: {', '.join(skipped)}"],
                    "on a GPU node a skipped GPU test means the GPU wasn't usable (RFC-0005 §3.2)",
                    "read the test output in log.txt and the fingerprint",
                    "RFC-0005 §3.2",
                )))  # fmt: skip
    return done(Verdict())


def combine(verdicts: list[tuple[str, Verdict]]) -> Verdict:
    """One verdict for a run from each rank's (§4.11: either pod failing fails the run).

    Infrastructure wins over a test failure; the failing step names its rank directory.
    """
    if len(verdicts) == 1:
        return verdicts[0][1]

    def tagged(sub: str, v: Verdict) -> Verdict:
        step = f"{sub}/{v.failing_step}" if v.failing_step else None
        return Verdict(v.test_code, v.infra, step, f"{sub}: {v.message}" if v.message else "")

    infra = [tagged(s, v) for s, v in verdicts if v.infra]
    failed = [tagged(s, v) for s, v in verdicts if v.test_code]
    out = infra[0] if infra else max(failed, key=lambda v: v.test_code) if failed else Verdict()
    out.reports = {f"{s}/{k}": c for s, v in verdicts for k, c in v.reports.items()}
    return out
