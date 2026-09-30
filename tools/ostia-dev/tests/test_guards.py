"""The pipeline guards, evaluated CLI-side on the collected results (RFC-0005 §3.2; D11)."""

from pathlib import Path

import pytest

from ostia_dev.remote import guards

JUNIT = Path(__file__).parent / "fixtures" / "junit"


def _steps(*steps, state="done", code_wait="ok"):
    return {
        "schema": 1,
        "state": state,
        "code_wait": code_wait,
        "exit": 0,
        "steps": [
            {"name": n, "kind": k, "code": c, "seconds": 1, "result": r}
            for n, k, c, r in steps
        ],
    }


OK = [("install", "install", 0, "ok"), ("build", "build", 0, "ok"), ("command", "command", 0, "ok")]
PLAN = [name for name, *_ in OK]


def _eval(doc, junits=(), gpu=False, plan=PLAN, **kw):
    files = {name: (JUNIT / name).read_text() for name in junits}
    return guards.evaluate(doc, files, gpu_profile=gpu, planned=plan, **kw)


def test_all_passed():
    v = _eval(_steps(*OK), ["pass.xml"])
    assert (v.test_code, v.infra, v.failing_step) == (0, False, None)


def test_failure_ctest_found_no_tests():
    v = _eval(_steps(*OK), ["zero-tests.xml"])
    assert v.test_code == 1 and not v.infra
    assert "no tests" in v.message


def test_failure_tests_fail():
    steps = [*OK[:2], ("command", "command", 8, "failed")]
    v = _eval(_steps(*steps), ["failed.xml"])
    assert v.test_code == 1 and v.failing_step == "command"


def test_failure_gpu_tests_skipped_on_a_gpu_profile():
    v = _eval(_steps(*OK), ["gpu-skipped.xml"], gpu=True)
    assert v.infra and v.failing_step == "command"
    assert "gpu.skipped" in v.message


def test_skipped_gpu_tests_are_fine_on_a_cpu_profile():
    v = _eval(_steps(*OK), ["gpu-skipped.xml"], gpu=False)
    assert v.test_code == 0 and not v.infra


def test_failure_build_fails():
    v = _eval(_steps(OK[0], ("build", "build", 1, "failed")))
    assert v.test_code == 1 and not v.infra and v.failing_step == "build"


@pytest.mark.parametrize("kind", ["preflight", "install"])
def test_failure_preflight_or_install_fails(kind):
    v = _eval(_steps(("x", kind, 1, "failed")), plan=["x", "command"])
    assert v.infra and v.failing_step == "x"


def test_failure_code_never_arrives():
    v = _eval(_steps(state="code_wait_timeout", code_wait="timeout"))
    assert v.infra and "code" in v.message


def test_failure_deadline_reached():
    v = _eval(_steps(OK[0], ("build", "build", 143, "timeout"), state="timeout"))
    assert v.infra and v.failing_step == "build" and "timeout" in v.message


def test_terminated_is_infra():
    v = _eval(_steps(("build", "build", 143, "terminated"), state="terminated"))
    assert v.infra and v.failing_step == "build"


def test_failure_out_of_memory_kill():
    v = _eval(_steps(OK[0], ("build", "build", 137, "failed")), oom=True, memory="24Gi", profile="l4")
    assert v.infra and v.failing_step == "build"
    assert "24Gi" in v.message and "profiles.l4" in v.message and "memory" in v.message


def test_a_failing_report_step_never_fails_the_run():
    steps = [*OK, ("aa", "report", 1, "failed")]
    v = _eval(_steps(*steps), ["pass.xml"], plan=[*PLAN, "aa"])
    assert v.test_code == 0 and not v.infra
    assert v.reports == {"aa": 1}


def test_steps_missing_without_a_failure_is_infra():
    v = _eval(_steps(OK[0]))  # the supervisor stopped without saying why
    assert v.infra


def test_no_test_runs_need_no_junit():
    v = _eval(_steps(*OK[:2]), plan=PLAN[:2])
    assert v.test_code == 0 and not v.infra


def test_expected_summary_line_missing_is_infra():
    v = _eval(_steps(*OK), ["pass.xml"], gpu=True,
              expect_summary="architectures: native (dev preset, GPU detected (8.9))",
              summary_text="architectures: 80-real;90-real;100 (dev preset, no GPU detected: release list)\n")
    assert v.infra and "native" in v.message


def test_expected_summary_line_present():
    line = "architectures: native (dev preset, GPU detected (8.9))"
    v = _eval(_steps(*OK), ["pass.xml"], gpu=True, expect_summary=line, summary_text=f"x\n{line}\n")
    assert v.test_code == 0 and not v.infra


def test_junit_labels_are_parsed():
    cases = guards.parse_junit((JUNIT / "gpu-skipped.xml").read_text())
    assert [(c.name, c.labels, c.skipped) for c in cases] == [
        ("gpu.skipped", ("gpu", "multiprocess"), True),
        ("gpu.pass", ("gpu",), False),
    ]


def test_unreadable_junit_is_a_failure():
    v = guards.evaluate(_steps(*OK), {"junit.xml": "<not xml"}, gpu_profile=False, planned=PLAN)
    assert v.test_code == 1 and "junit.xml" in v.message
