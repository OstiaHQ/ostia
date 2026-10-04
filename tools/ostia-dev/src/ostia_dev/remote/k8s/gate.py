"""`ostia-dev remote gate <setup>`: a setup's gate workloads on a k8s machine (RFC-0005 §6).

Partial (A3, Decision 10): capability check, machine mapping, the RDMA profile rule, the
evidence-counter probe, the workloads through the bench driver with --evidence, and the
evidence check. A setup whose also_run has topo_capture adds a capture step, which never fails
the run; the backend fetches each pod's capture manifest-first (RFC-0003 §4). A two-pod gate
whose two captures are accepted and complete gets capture/pair.json and its records stamped
with the pair id before compare (RFC-0003 §5, §7); otherwise they keep topology null, which
no baseline with a pair id matches. RFC-0004 §1.1's active probes and rent's fallback handover
land with RFC-0004 PR 7.
"""

import json
import sys
from collections.abc import Callable
from pathlib import Path

from ostia_dev.config import Config
from ostia_dev.contract import violation
from ostia_dev.errors import OstiaError, UsageError
from ostia_dev.remote import capture, suites
from ostia_dev.remote.core import RunSpec
from ostia_dev.remote.profiles import Profile, parallelism, resolve

ENV = "cuda-12"
PENDING = (
    "RFC-0004 §1.1's active capability probes (RFC-0004 PR 7)",
    "rent's fallback handover and its second-active-run refusal (RFC-0004 PR 7)",
)
# workload -> (fabric/bench program, its arguments); dual_link's mode follows the pods
PROGRAMS: dict[str, tuple[str, list[str]]] = {
    "p2p_copy": ("p2p_copy", []),
    "pipelining": ("pipelining", []),
    "batching": ("batching", []),
    "dual_link": ("dual_link", []),
    "rdma_put": ("rdma_put", ["--mem", "cuda"]),
    "gdr_stream": ("gdr_stream", ["--mem", "cuda"]),
    "tcp_put": ("tcp_put", []),
}
NVLINK_WORKLOADS = ("p2p_copy", "pipelining", "batching")
BINARY = "ostia_fabric_bench_"
CAPTURE = "topo_capture"


def _bench():
    from ostia_dev.bench import capabilities, evidence

    return capabilities, evidence


def _compare_main() -> Callable[[list[str]], int]:
    from ostia_dev.bench import compare

    return compare.main


def _pair_id(directory: Path) -> str:
    from ostia_dev.topo import cli

    return cli.pair_id(directory)


def _bad(problem: str, details: list[str], rule: str, fix: str, see: str) -> UsageError:
    return UsageError(violation(problem, details, rule, fix, see))


def _rdma(workload: str, pods: int) -> bool:
    return workload in ("rdma_put", "gdr_stream") or (workload == "dual_link" and pods == 2)


def check_machine(setup: dict, which: str, cfg: Config) -> tuple[dict, dict, Profile]:
    """The chosen k8s machine, its user-config mapping and its profile; exit 2 on any gap."""
    capabilities, _ = _bench()
    machine = setup["machines"][which]
    if machine["backend"] != "k8s":
        raise _bad(
            f"the {which} machine of {setup['name']} is a {machine['backend']} machine",
            [],
            "remote gate drives k8s machines; rented ones belong to ostia-dev rent",
            "use --fallback for a k8s fallback, or run it with ostia-dev rent (RFC-0004 PR 7)",
            "RFC-0005 §6",
        )
    name = machine["machine"]
    mapping = cfg.machine(name)
    missing = [k for k in ("context", "namespace", "profile") if not mapping.get(k)]
    if missing:
        raise _bad(
            f"k8s machine {name} has no mapping in {cfg.path}",
            [f"missing: {', '.join(missing)}"],
            "the repository never names a cluster; the user config maps the logical machine",
            f'add [remote.k8s.machines.{name}] with context, namespace and profile = "<name>"',
            "RFC-0005 §6",
        )
    provider = cfg.context(mapping["context"]).get("provider")
    profile = resolve(mapping["profile"], provider, cfg)
    wanted = capabilities._gpus(machine)
    if profile.gpus < wanted:
        raise _bad(
            f"profile {profile.name} has {profile.gpus} GPUs; machine {name} declares {wanted}",
            [f"accelerators: {machine['accelerators']}"],
            "the profile must give each pod the GPUs the setup file declares",
            f"raise gpus in [remote.k8s.profiles.{profile.name}], or map another profile",
            "RFC-0005 §6",
        )
    rdma = [w for w in setup["gate"] if _rdma(w, machine["pods"])]
    if rdma and profile.kind != "rdma":
        raise _bad(
            f"RDMA gate workloads {', '.join(rdma)} would run on profile {profile.name} "
            f"(kind {profile.kind})",
            [f"machine: {name}"],
            "the counters that prove RDMA traffic are only visible with RDMA devices in the pod",
            f'map machine {name} to a profile of kind = "rdma" (RFC-0005 §4.12)',
            "RFC-0005 §6",
        )
    return machine, mapping, profile


def plan_gate(setup: dict, machine: dict, profile: Profile, env: str, run_id: str) -> suites.Plan:
    pods = machine["pods"]
    build_dir = f"{suites.WORK}/build/{env}/release"
    steps = suites.gpu_preflight(profile)
    steps.append(suites.Step("install", "install", ("pixi", "install", "--locked", "-e", env)))
    configure = ["cmake", "--preset", "release", "-DOSTIA_BUILD_BENCH=ON"]
    if profile.cc:
        configure.append(f"-DCMAKE_CUDA_ARCHITECTURES={profile.cc}-real")
    steps.append(suites.Step("configure", "build", suites._pixi(env, configure)))
    steps.append(
        suites.Step(
            "build", "build", suites._pixi(env, ["cmake", "--build", "--preset", "release"])
        )
    )
    probes = []
    if any(_rdma(w, pods) for w in setup["gate"]):
        probes += ["--probe", "ib"]
    if any(w in NVLINK_WORKLOADS or (w == "dual_link" and pods == 1) for w in setup["gate"]):
        probes += ["--probe", "nvlink"]
    if probes:
        probe = ["ostia-dev", "bench", "evidence", *probes]
        steps.append(suites.Step("evidence-probe", "command", suites._pixi(env, probe)))
    for w in setup["gate"]:
        if w not in PROGRAMS:
            raise _bad(
                f"gate workload {w} has no fabric/bench program",
                [],
                "remote gate runs the gate workloads through the bench driver",
                "move it to also_run",
                "RFC-0005 §6",
            )
        program, args = PROGRAMS[w]
        if w == "dual_link" and pods == 2:
            if not profile.rdma_nics:
                raise _bad(
                    f"profile {profile.name} has no rdma_nics for dual_link's two rails",
                    [],
                    "dual_link --mode rails runs one transfer on each of two NICs",
                    f'add rdma_nics = "mlx5_0:1,mlx5_1:1" (your node\'s two NIC ports) to '
                    f"[remote.k8s.profiles.{profile.name}]",
                    "RFC-0005 §6",
                )
            args = ["--mode", "rails", "--nics", profile.rdma_nics]
        elif w == "dual_link":
            args = ["--mode", "nvlink"]
        remote = ["--remote"] if pods == 2 else []
        argv = [
            "ostia-dev", "bench", "run", "--format", "ostia", "--evidence",
            *remote, "--needs-gpu", "--bench", f"{build_dir}/fabric/bench/{BINARY}{program}",
            "--build-dir", build_dir, "--out", f"{suites.WORK}/bench/results", "--run-id", run_id,
            "--", *args,
        ]  # fmt: skip
        steps.append(suites.Step(f"bench-{w}", "command", suites._pixi(env, argv)))
    if CAPTURE in setup.get("also_run", []):
        capture_argv = ["ostia-dev", "topo", "capture", "--build-dir", build_dir,
                        "--out", capture.CAPTURE_DIR]  # fmt: skip
        steps.append(suites.Step("capture", "report", suites._pixi(env, capture_argv)))
    build_jobs, test_jobs = parallelism(profile, env)
    return suites.Plan(
        steps=steps,
        env=env,
        preset="release",
        build_dir=build_dir,
        suite="gate",
        build_jobs=build_jobs,
        test_jobs=test_jobs,
    )


def _missing_records(workloads: list[str], results: Path) -> list[str]:
    """compare.py only judges the cases it is given, so a workload without records would
    pass silently."""
    text = results.read_text() if results.exists() else ""
    benches = {json.loads(line).get("bench") for line in text.splitlines() if line.strip()}
    return [
        f"error: no benchmark records for {w} in {results}" for w in workloads if w not in benches
    ]


def _evidence_problems(workloads: list[str], ev_dir: Path) -> list[str]:
    _, evidence = _bench()
    lines = []
    for w in workloads:
        path = ev_dir / f"{w}.json"
        if not path.exists():
            lines.append(f"error: no evidence for {w} ({path})")
            continue
        problems = evidence.check(json.loads(path.read_text()))
        if problems:
            lines.append(f"error: {w}: the run does not show the transport it tests")
            lines += [f"  {p}" for p in problems]
    if lines:
        lines.append(
            "  rule: a gate run that cannot show its transport fails\n  see: RFC-0001 §6.4"
        )
    return lines


def _stamp_pair(captures: Path, profile: Profile, records: Path) -> bool:
    """pair.json, the pair id and the stamp, only when both captures are accepted and complete
    (RFC-0003 §7); otherwise one warning naming the node, and the records keep topology null."""
    try:
        status = json.loads((captures / "status.json").read_text())
    except (OSError, ValueError):
        status = {}
    unusable = []
    for node in capture.NODE_DIRS:
        entry = status.get(node) if isinstance(status, dict) else None
        if not isinstance(entry, dict):
            unusable.append(f"{node} absent")
        elif entry.get("result") != "accepted" or entry.get("status") != "complete":
            detail = entry.get("status") or entry.get("reason")
            unusable.append(f"{node} {entry.get('result')} ({detail})")
    why = None
    if unusable:
        why = f"capture {', '.join(unusable)}"
    else:
        try:
            capture.write_pair_json(captures, profile, records)
            pair = _pair_id(captures)
            capture.stamp(records, pair)
        except (capture.PairError, OstiaError, OSError, ValueError, KeyError) as e:
            # pair.json next to unstamped records would claim a pair the records don't carry
            (captures / "pair.json").unlink(missing_ok=True)
            why = f"no pair id ({e.message.splitlines()[0] if isinstance(e, OstiaError) else e})"
    if why:
        print(
            f"warning: the records keep topology null: {why}\n"
            "  rule: a two-pod gate stamps the pair id only when both captures are accepted and "
            "complete; no baseline with a pair id matches these records\n"
            f"  see: {captures / 'status.json'}, RFC-0003 §7",
            file=sys.stderr,
        )
        return False
    print(f"gate: records stamped with pair id {pair}")
    return True


def _baseline_topologies(baseline: Path) -> set:
    from ostia_dev.bench import compare

    try:
        return {r["compat"].get("topology") for r in compare.load_baseline(baseline)}
    except (OSError, ValueError, KeyError, TypeError):
        return set()  # compare reports an unreadable baseline itself


def gate(
    setup_path: Path,
    *,
    cfg: Config,
    repo: Path,
    run,
    fallback: bool = False,
    baseline: Path | None = None,
    results: Path | None = None,
    yes: bool = False,
    kubectl: str | None = None,
    verbose: bool = False,
) -> int:
    capabilities, _ = _bench()
    try:
        setup = capabilities.load_setup(setup_path)
    except capabilities.SetupError as e:
        raise _bad(
            "the setup file is not valid", [str(e)], "setup files follow RFC-0004 §1",
            "fix the file", "RFC-0005 §6",
        ) from e  # fmt: skip
    which = "fallback" if fallback else "primary"
    unsupported = [r for r in capabilities.evaluate(setup)[which] if r.required and not r.supported]
    for r in unsupported:
        print(
            f"error: {setup['name']}/{which} cannot run gate workload {r.workload} ({r.reason})\n"
            "  rule: a gate workload the machine cannot support fails the gate\n"
            "  see: RFC-0001 §6.4"
        )
    if unsupported:
        return 1
    machine, mapping, profile = check_machine(setup, which, cfg)
    for item in PENDING:
        print(f"note: not checked yet: {item}", file=sys.stderr)
    run_ids: list[str] = []

    def plan(p: Profile, env: str, run_id: str) -> suites.Plan:
        run_ids.append(run_id)
        return plan_gate(setup, machine, p, env, run_id)

    spec = RunSpec(
        backend="k8s",
        profile=mapping["profile"],
        envs=[ENV],
        results=results or repo / "build" / "remote",
        yes=yes,
        verbose=verbose,
        extra={
            "context": mapping["context"],
            "namespace": mapping["namespace"],
            "kubectl": kubectl,
            "pods": machine["pods"],
            "plan": plan,
        },
    )
    code = run(spec, cfg, repo)
    if code != 0:
        return code
    out = repo / "bench" / "results" / run_ids[-1]
    stamped = None
    if machine["pods"] == 2 and CAPTURE in setup.get("also_run", []):
        captures = Path(spec.results) / run_ids[-1] / "capture"
        stamped = _stamp_pair(captures, profile, out / "results.jsonl")
    problems = _missing_records(setup["gate"], out / "results.jsonl")
    problems += _evidence_problems(setup["gate"], out / "evidence")
    if problems:
        print("\n".join(problems))
        return 1
    print(f"gate {setup['name']}/{which}: transport evidence ok for {', '.join(setup['gate'])}")
    if baseline is None:
        return 0
    argv = ["--baseline", str(baseline), "--candidate", str(out / "results.jsonl"),
            "--evidence-dir", str(out / "evidence"), "--require-pass"]  # fmt: skip
    code = _compare_main()(argv)
    # compare skips cases whose topology differs, and a skipped case does not fail it; a gate
    # whose records could not be stamped has not shown it ran on the baseline's pair.
    if code == 0 and stamped is False and _baseline_topologies(baseline) - {None}:
        status = Path(spec.results) / run_ids[-1] / "capture" / "status.json"
        print(
            "error: the baseline has a pair id and these records have none\n"
            "  rule: a two-pod gate compares only records stamped with the pair id (RFC-0003 §7)\n"
            f"  fix: rerun the gate; {status} says why a capture was not used\n"
            "  see: RFC-0005 §6"
        )
        return 1
    return code
