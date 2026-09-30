"""The shared remote-run pipeline (RFC-0005 §3): run IDs, argument rules, exit codes, drive().

A backend implements the seven operations of §3.1. drive() runs them once per --env:

    prepare (host: pixi lock check, tarball, step plan; then the backend's own preflight)
    gc -> start -> upload -> stream -> collect, and teardown in `finally`

then evaluates the guards on the collected results (guards.py), writes summary.json and
prints the summary line. Each environment is its own run with its own run ID; the worst
exit code wins.
"""

import datetime
import fnmatch
import secrets
import shutil
import signal
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ostia_dev import errors, proc
from ostia_dev.config import Config
from ostia_dev.contract import violation
from ostia_dev.errors import InfraError, UsageError
from ostia_dev.remote import guards, results, suites, tarball
from ostia_dev.remote.profiles import Profile, parse_duration, resolve

SECRET_KEYS = ("*TOKEN*", "*SECRET*", "*KEY*", "*PASSWORD*")


@dataclass
class RunSpec:
    """Every §3.1 flag, plus the backend's own under `extra`."""

    backend: str
    profile: str
    envs: list[str]
    results: Path
    preset: str | None = None
    suite: str | None = None
    command: list[str] | None = None
    no_build: bool = False
    no_test: bool = False
    timeout: str | None = None
    ref: str | None = None
    env_vars: list[str] = field(default_factory=list)
    allow_secret: bool = False
    yes: bool = False
    verbose: bool = False
    provider: str | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class Run:
    """One run: one environment of a RunSpec."""

    spec: RunSpec
    env: str
    run_id: str
    profile: Profile
    plan: suites.Plan
    tarball: tarball.Tarball
    results_dir: Path
    repo: Path
    git_sha: str
    env_vars: dict[str, str]
    windows: dict[str, int]
    image: str
    phases: dict[str, int] = field(default_factory=dict)
    oom: bool = False
    node: str | None = None
    state: dict = field(default_factory=dict)  # the backend's own (container name, Job UID)

    def supervisor_env(self) -> dict[str, str]:
        """The environment the supervisor gets, identical for every backend (§4.5)."""
        env = {
            "OSTIA_WORK": suites.WORK,
            "OSTIA_PLAN": suites.encode(self.plan.steps),
            "OSTIA_ENV": self.env,
            "OSTIA_PRESET": self.plan.preset,
            "OSTIA_BUILD_DIR": self.plan.build_dir,
            "OSTIA_CODE_WAIT": str(self.windows["code_wait"]),
            "OSTIA_COLLECT_WINDOW": str(self.windows["collect"]),
            "OSTIA_TIMEOUT": str(self.windows["timeout"]),
            "HOME": f"{suites.WORK}/home",
            "CMAKE_BUILD_PARALLEL_LEVEL": str(self.plan.build_jobs),
            "CTEST_PARALLEL_LEVEL": str(self.plan.test_jobs),
            "CTEST_NO_TESTS_ACTION": "error",
        }
        if self.profile.is_gpu:
            env["OSTIA_REQUIRE_GPU"] = "1"
        env.update(self.profile.env)
        env.update(self.env_vars)
        return env


class Backend(Protocol):
    name: str

    def prepare(self, run: Run) -> None: ...
    def gc(self, run: Run) -> list[str]: ...
    def start(self, run: Run) -> None: ...
    def upload(self, run: Run) -> None: ...
    def stream(self, run: Run) -> None: ...
    def collect(self, run: Run, workdir: Path) -> tuple[Path, Path]: ...
    def teardown(self, run: Run) -> bool: ...
    def describe(self, run: Run) -> dict: ...


def run_id(backend: str, profile: str, now: datetime.datetime, rand: str | None = None) -> str:
    return f"{backend}-{profile}-{now:%Y%m%d-%H%M%S}-{rand or secrets.token_hex(3)}"


def check_env_vars(pairs: list[str], *, allow_secret: bool) -> dict[str, str]:
    out = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise UsageError(
                violation(
                    f"--env-var {pair!r} is not KEY=VALUE",
                    [],
                    "--env-var passes one variable",
                    "write it as --env-var KEY=VALUE",
                    "RFC-0005 §4.10",
                )
            )
        if not allow_secret and any(fnmatch.fnmatchcase(key.upper(), p) for p in SECRET_KEYS):
            raise UsageError(
                violation(
                    f"--env-var {key} looks like a secret",
                    [f"keys matching {', '.join(SECRET_KEYS)} are refused"],
                    "no secret reaches a pod unless you say so; its key goes into summary.json",
                    f"rerun with --allow-secret if {key} must reach the run",
                    "RFC-0005 §4.10",
                )
            )
        out[key] = value
    return out


def check_ref_rules(ref: str | None, *, cache: bool, env_vars: dict) -> None:
    if not (ref and ref.startswith("pr/")):
        return
    what = [flag for flag, on in (("--cache", cache), ("--env-var", bool(env_vars))) if on]
    if what:
        raise UsageError(
            violation(
                f"--ref {ref} runs contributor code, which may not use {' or '.join(what)}",
                [],
                "a pull request's code can't reach a shared cache or passed variables",
                f"drop {' and '.join(what)}",
                "RFC-0005 §4.10",
            )
        )


def final_exit(
    test_code: int, infra: bool, verified: bool, interrupted: bool, *, usage: bool = False
) -> int:
    """§3.5: Ctrl-C, then usage, then infrastructure, then the test result, then teardown."""
    if interrupted:
        return errors.INTERRUPTED
    if usage:
        return errors.USAGE
    if infra:
        return errors.INFRA
    if test_code:
        return errors.FAILED
    return errors.OK if verified else errors.TEARDOWN


_RANK = {0: 0, 4: 1, 1: 2, 2: 3, 3: 4, 130: 5}


def worst(codes: list[int]) -> int:
    return max(codes, key=lambda c: _RANK.get(c, 3)) if codes else 0


def check_lock(root: Path) -> None:
    """A stale pixi.lock is exit 2 on the host, before anything exists remotely (R16).

    --dry-run: plain `pixi lock --check` rewrites a stale lock as it reports it.
    """
    r = proc.run(["pixi", "lock", "--check", "--dry-run"], cwd=root)
    if r.returncode != 0:
        raise UsageError(
            violation(
                "pixi.lock is out of date with pixi.toml",
                [line for line in (r.stderr or r.stdout).strip().splitlines()[-3:]],
                "remote runs install exactly the locked environment (pixi install --locked)",
                "pixi lock, then rerun",
                "RFC-0005 §3.2",
            )
        )


def _windows(cfg: Config, spec: RunSpec) -> dict[str, int]:
    w = {k: parse_duration(v) for k, v in cfg.windows.items()}
    if spec.timeout:
        w["timeout"] = parse_duration(spec.timeout)
    return w


def prepare(spec: RunSpec, env: str, cfg: Config, repo: Path, workdir: Path) -> Run:
    """The host side of prepare: nothing remote exists yet, so every error here is exit 2."""
    env_vars = check_env_vars(spec.env_vars, allow_secret=spec.allow_secret)
    check_ref_rules(spec.ref, cache=bool(spec.extra.get("cache")), env_vars=env_vars)
    profile = resolve(spec.profile, spec.provider, cfg)
    rid = run_id(spec.backend, profile.name, datetime.datetime.now(datetime.UTC))
    plan = suites.build_plan(
        cfg,
        profile,
        env,
        suite=spec.suite,
        command=spec.command,
        preset=spec.preset,
        no_build=spec.no_build,
        no_test=spec.no_test,
        run_id=rid,
    )
    suites.encode(plan.steps)  # refuses newlines and tabs before anything is created
    if not spec.ref:
        check_lock(repo)
    tb = tarball.build(repo, ref=spec.ref, out_dir=workdir)
    sha = tb.ref_sha[:12] if tb.ref_sha else tarball.worktree_sha(repo)
    return Run(
        spec=spec,
        env=env,
        run_id=rid,
        profile=profile,
        plan=plan,
        tarball=tb,
        results_dir=Path(spec.results) / rid,
        repo=repo,
        git_sha=sha,
        env_vars=env_vars,
        windows=_windows(cfg, spec),
        image=cfg.image,
    )


def _fingerprint_gpu(text: str) -> str | None:
    """'NVIDIA L4, 580.95.05, 8.9, 23034 MiB' -> 'L4 (8.9) driver 580.95'."""
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 3 and parts[0] != "name" and parts[2].replace(".", "").isdigit():
            name = parts[0].removeprefix("NVIDIA ")
            driver = ".".join(parts[1].split(".")[:2])
            return f"{name} ({parts[2]}) driver {driver}"
    return None


def _result(code: int) -> str:
    return {0: "passed", 1: "failed", 2: "refused", 3: "infrastructure failure",
            4: "passed, teardown unverified", 130: "interrupted"}.get(code, "failed")  # fmt: skip


def _on_sigterm(signum, frame):
    raise KeyboardInterrupt


def run_one(backend: Backend, spec: RunSpec, env: str, cfg: Config, repo: Path) -> int:
    workdir = Path(tempfile.mkdtemp(prefix="ostia-run-"))
    try:
        run = prepare(spec, env, cfg, repo, workdir)
        backend.prepare(run)
        return _execute(backend, run, workdir)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _execute(backend: Backend, run: Run, workdir: Path) -> int:
    t0 = time.monotonic()
    removed = backend.gc(run)
    if removed:
        print(f"[ostia] removed expired runs: {', '.join(removed)}", file=sys.stderr)
    interrupted = usage = infra = False
    message = ""
    verdict = None
    ctl = None
    dropped: list[str] = []
    verified = False
    try:
        backend.start(run)
        backend.upload(run)
        backend.stream(run)
        control, artifacts = backend.collect(run, workdir)
        ctl = results.extract_control(control, run.results_dir)
        build_rel = run.plan.build_dir.removeprefix(suites.WORK + "/")
        dropped = results.extract_artifacts(artifacts, run.results_dir, build_rel)
        junits = {p.name: p.read_text() for p in sorted(run.results_dir.glob("junit*.xml"))}
        summary_file = run.results_dir / "ostia-summary.txt"
        verdict = guards.evaluate(
            ctl.steps,
            junits,
            gpu_profile=run.profile.is_gpu,
            planned=[s.name for s in run.plan.steps],
            oom=run.oom,
            memory=run.profile.memory,
            profile=run.profile.name,
            expect_summary=run.plan.expect_summary,
            summary_text=summary_file.read_text() if summary_file.exists() else None,
        )
        infra, message = verdict.infra, verdict.message
    except KeyboardInterrupt:
        interrupted = True
        message = "interrupted; tearing down"
        print(f"[ostia] {message}", file=sys.stderr)
    except InfraError as e:
        infra, message = True, e.message
    except UsageError as e:
        usage, message = True, e.message
    finally:
        previous = signal.signal(signal.SIGTERM, _on_sigterm)
        try:
            verified = backend.teardown(run)
        except KeyboardInterrupt:
            interrupted, verified = True, False
        finally:
            signal.signal(signal.SIGTERM, previous)
    test_code = verdict.test_code if verdict else 0
    code = final_exit(test_code, infra, verified, interrupted, usage=usage)
    _finish(run, ctl, verdict, code, verified, message, dropped, time.monotonic() - t0, backend)
    return code


def _finish(run, ctl, verdict, code, verified, message, dropped, seconds, backend) -> None:
    run.results_dir.mkdir(parents=True, exist_ok=True)
    log = run.results_dir / "log.txt"
    fingerprint = run.results_dir / "fingerprint.txt"
    reports = {}
    for name, rc in (verdict.reports if verdict else {}).items():
        floor = results.parse_noise_floor(log.read_text(errors="replace")) if log.exists() else None
        reports[name] = {"code": rc, "noise_floor": floor}
    bench = results.copy_bench_results(run.results_dir, run.repo, run.run_id)
    ref = run.spec.ref or ""
    summary = {
        "schema": 1,
        "run_id": run.run_id,
        "backend": run.spec.backend,
        "profile": run.profile.name,
        "env": run.env,
        "preset": run.plan.preset,
        "suite": run.plan.suite,
        "node": run.node,
        "gpu": _fingerprint_gpu(fingerprint.read_text()) if fingerprint.exists() else None,
        "git_sha": run.git_sha,
        "tree_hash": run.tarball.tree_hash,
        "upload_bytes": run.tarball.size,
        "upload_skipped": run.tarball.skipped,
        "steps": ctl.steps.get("steps", []) if ctl else [],
        "phases": run.phases,
        "seconds": round(seconds),
        "node_hours": round(seconds / 3600, 4),
        "exit_code": code,
        "result": _result(code),
        "failing_step": verdict.failing_step if verdict else None,
        "message": message,
        "teardown": "verified" if verified else "unverified",
        "env_var_keys": sorted(run.env_vars),
        "parallelism": {"build_jobs": run.plan.build_jobs, "test_jobs": run.plan.test_jobs},
        "code": "contributor" if ref.startswith("pr/") else "local",
        "pr": int(ref.removeprefix("pr/")) if ref.startswith("pr/") else None,
        "ref": run.spec.ref,
        "log_truncated": ctl.log_truncated if ctl else False,
        "dropped": dropped,
        "reports": reports,
        "bench_results": str(bench) if bench else None,
        **backend.describe(run),
    }
    results.write_summary(run.results_dir, summary)
    if message and code != errors.OK:
        print(message, file=sys.stderr)
    print("\n".join(results.summary_lines(summary)))
    print(f"results: {run.results_dir}")


def drive(backend: Backend, spec: RunSpec, *, cfg: Config, repo: Path) -> int:
    """Run the pipeline once per --env (RFC-0005 §3.1); the worst exit code wins."""
    codes = []
    for env in spec.envs:
        code = run_one(backend, spec, env, cfg, repo)
        codes.append(code)
        if code == errors.INTERRUPTED:
            break
    return worst(codes)
