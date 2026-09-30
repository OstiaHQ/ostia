"""`remote k8s init | verify | cleanup | profiles | usage` (RFC-0005 §4.1, §4.3, §4.6, §4.8)."""

import datetime
import json
import sys
from pathlib import Path

from ostia_dev import prompts
from ostia_dev.clock import Clock
from ostia_dev.config import Config
from ostia_dev.contract import violation
from ostia_dev.errors import OstiaError, UsageError
from ostia_dev.remote import core, suites
from ostia_dev.remote.k8s import manifests
from ostia_dev.remote.k8s.preflight import Target, apply_guardrails, fit_check
from ostia_dev.remote.profiles import parse_duration, resolve
from ostia_dev.remote.tarball import Tarball

# The probe runs with the run's exact pod spec (§4.6). The image has no curl, so it comes
# from conda-forge through pixi; if even that fails, every network check is unknown.
PROBE = r"""set -u
mkdir -p "$HOME"
say() { echo "$1 $2 $3"; }
c() { pixi exec --spec curl curl -sS -o /dev/null --max-time 5 "$@" 2>/dev/null; }
if ! pixi exec --spec curl curl --version >/dev/null 2>&1; then
  say FAIL curl "pixi exec curl failed: HTTPS to conda-forge is blocked, so no network check ran"
  echo "[ostia] probe done"; exit 1
fi
say PASS curl "pixi exec curl works"
if c http://169.254.169.254/; then say FAIL metadata "the cloud metadata server answered"
else say PASS metadata "unreachable (expected)"; fi
if [ -e /var/run/secrets/kubernetes.io/serviceaccount/token ]; then
  say FAIL token "a service-account token is mounted"
else say PASS token "none mounted"; fi
if c -k https://kubernetes.default.svc/; then say FAIL apiserver "kubernetes.default:443 answered"
else say PASS apiserver "unreachable"; fi
if c -k "https://${OSTIA_NODE_IP}:10250/" || c -k "https://${OSTIA_NODE_IP}:443/"; then
  say FAIL node "the node ${OSTIA_NODE_IP} answered (kubelet or 443)"
else say PASS node "${OSTIA_NODE_IP} unreachable"; fi
if getent hosts github.com >/dev/null; then say PASS dns "github.com resolves"
else say FAIL dns "github.com does not resolve"; fi
if c https://github.com/; then say PASS https "github.com answers"
else say FAIL https "no answer from https://github.com"; fi
if [ "${OSTIA_PROBE_RDMA:-0}" = 1 ]; then
  l=$(ulimit -l)
  if [ "$l" = unlimited ]; then say PASS memlock "ulimit -l unlimited"
  else say FAIL memlock "ulimit -l is $l; RDMA needs unlimited (LimitMEMLOCK)"; fi
fi
echo "[ostia] probe done"
"""


def init(target: Target, kube, *, cfg: Config, privileged: bool = False) -> list[str]:
    lines = apply_guardrails(target, kube, cfg=cfg, privileged=privileged)
    ns, role = target.namespace, manifests.ROLE
    lines += [
        "",
        "Grant each developer the access a run needs (RFC-0005 §4.3):",
        f"  kubectl --context {target.context} --namespace {ns} create rolebinding {role} "
        f"--role {role} --user <you@example.com>",
        f"  kubectl --context {target.context} create clusterrolebinding {role}-{ns} "
        f"--clusterrole {role}-{ns} --user <you@example.com>",
        f"Then check the isolation: ostia-dev remote k8s verify --context {target.context} "
        f"--namespace {ns}",
    ]
    return lines


CHECKS = ("curl", "metadata", "token", "apiserver", "node", "dns", "https")


def _probe_run(cfg: Config, profile: str, provider: str) -> core.Run:
    p = resolve(profile, provider, cfg)
    plan = suites.build_plan(cfg, p, "default", no_build=True, no_test=True, run_id="probe")
    now = datetime.datetime.now(datetime.UTC)
    rid = core.run_id("probe", p.name, now)
    spec = core.RunSpec(backend="k8s", profile=profile, envs=["default"], results=Path("."))
    windows = {k: parse_duration(v) for k, v in cfg.windows.items()}
    tb = Tarball(path=Path("/dev/null"), size=0, tree_hash="", files=[], file_count=0)
    return core.Run(
        spec=spec,
        env="default",
        run_id=rid,
        profile=p,
        plan=plan,
        tarball=tb,
        results_dir=Path("."),
        repo=Path("."),
        git_sha="",
        env_vars={},
        windows=windows,
        image=cfg.image,
    )


def verify(
    target: Target,
    kube,
    *,
    cfg: Config,
    profile: str = "cpu",
    clock: Clock | None = None,
    timeout: int = 600,
) -> int:
    clock = clock or Clock()
    run = _probe_run(cfg, profile, target.provider)
    job = manifests.job(
        run, namespace=target.namespace, script=PROBE, owner=core.owner_id(), now=clock.now()
    )
    job["spec"]["suspend"] = False
    job["metadata"]["labels"]["ostia.dev/probe"] = "true"
    container = job["spec"]["template"]["spec"]["containers"][0]
    container["env"].append(
        {"name": "OSTIA_NODE_IP", "valueFrom": {"fieldRef": {"fieldPath": "status.hostIP"}}}
    )
    if run.profile.kind == "rdma":
        container["env"].append({"name": "OSTIA_PROBE_RDMA", "value": "1"})
    name = job["metadata"]["name"]
    kube.apply(job)
    results: list[tuple[str, str, str]] = []
    done = False
    try:
        t0 = clock.monotonic()
        pod = None
        while clock.monotonic() - t0 < timeout:
            pods = [i for i in kube.run_status(run.run_id).get("items", []) if i["kind"] == "Pod"]
            if pods and pods[-1]["status"].get("phase") in ("Running", "Succeeded", "Failed"):
                pod = pods[-1]["metadata"]["name"]
                break
            clock.sleep(3)
        if pod is None:
            print(f"FAIL probe: the probe pod did not start within {timeout}s", flush=True)
            return 1
        for raw in kube.logs_follow(pod):
            line = raw.partition(" ")[2]
            word, _, rest = line.partition(" ")
            if word in ("PASS", "FAIL"):
                check, _, detail = rest.partition(" ")
                results.append((word, check, detail))
                print(f"{word} {check}: {detail}", flush=True)
            if line.startswith("[ostia] probe done"):
                done = True
                break
    finally:
        kube.delete("job", name)
    expected = set(CHECKS) | ({"memlock"} if run.profile.kind == "rdma" else set())
    missing = sorted(expected - {r[1] for r in results})
    failed = [r for r in results if r[0] == "FAIL"]
    if failed and failed[0][1] == "curl":
        missing = []  # the probe stops by design when it has no curl
    if not done or missing:
        # a cut-off log must never read as a pass (the checks that didn't run proved nothing)
        print(f"FAIL probe: incomplete; no result for {', '.join(missing) or 'the end marker'}")
        failed.append(("FAIL", "probe", "incomplete"))
    passed = len(results) - len(failed)
    print(
        f"verify: {passed} checks passed, {len(failed)} failed (context {target.context}, "
        f"namespace {target.namespace})"
    )
    return 1 if failed or not results else 0


def cleanup(
    target: Target,
    kube,
    *,
    run_id: str | None = None,
    all_: bool = False,
    delete_namespace: bool = False,
    cache: bool = False,
    yes: bool = False,
) -> list[str]:
    lines = []
    if run_id:
        selector = f"ostia.dev/managed=true,ostia.dev/run-id={run_id}"
    elif all_:
        if not prompts.confirm(f"delete every ostia run in {target.namespace}?", yes=yes):
            raise UsageError("error: cleanup --all was not confirmed")
        selector = "ostia.dev/managed=true"
    else:
        selector = f"ostia.dev/managed=true,ostia.dev/owner={core.owner_id()}"
    for job in kube.list("job", selector=selector):
        kube.delete("job", job["metadata"]["name"])
        lines.append(f"deleted job/{job['metadata']['name']}")
    if cache:
        kube.delete("persistentvolumeclaim", manifests.CACHE_PVC)
        lines.append(f"deleted persistentvolumeclaim/{manifests.CACHE_PVC}")
    if delete_namespace:
        ns = kube.get("namespace", target.namespace, namespaced=False)
        if ns and ns["metadata"].get("labels", {}).get("ostia.dev/managed") != "true":
            raise UsageError(
                violation(
                    f"namespace {target.namespace} was not created by ostia-dev",
                    ["it has no ostia.dev/managed=true label"],
                    "cleanup deletes only a namespace ostia-dev created (RFC-0005 §4.3)",
                    "delete it yourself if you mean to",
                    "RFC-0005 §4.3",
                )
            )
        if ns and prompts.confirm(f"delete namespace {target.namespace}?", yes=yes):
            kube.delete("namespace", target.namespace, namespaced=False)
            lines.append(f"deleted namespace/{target.namespace}")
    return lines or ["nothing to delete"]


def profiles(cfg: Config, *, target: Target | None = None, kube=None) -> str:
    out = []
    nodes = []
    if target and kube:
        try:
            nodes = kube.list("node", namespaced=False)
        except OstiaError:
            out.append("note: no access to nodes, so no fit is shown")
    for name, raw in sorted(cfg.profiles.items()):
        providers = [target.provider] if target else [p for p in ("gke", "eks", "aks") if p in raw]
        base = raw
        out.append(
            f"{name}: {base.get('kind', 'gpu')}, cpu {base.get('cpu')}, memory "
            f"{base.get('memory')}, storage {base.get('ephemeral_storage')}, "
            f"gpus {base.get('gpus', 0)}"
        )
        for provider in providers or [None]:
            try:
                p = resolve(name, provider, cfg)
            except UsageError:
                out.append(f"  {name} ({provider}): no mapping")
                continue
            line = f"  {name} ({provider or 'any'}): selector {p.node_selector or '{}'}"
            if nodes:
                try:
                    note = fit_check(nodes, p)
                    fits = [
                        n["metadata"]["name"]
                        for n in nodes
                        if all(
                            n["metadata"].get("labels", {}).get(k) == v
                            for k, v in p.node_selector.items()
                        )
                    ]
                    line += f"; {note}" if note else f"; fits {', '.join(fits)}"
                except UsageError as e:
                    line += f"; does not fit: {e.message.splitlines()[0]}"
            out.append(line)
    return "\n".join(out)


def usage(results_root: Path) -> str:
    totals: dict[tuple[str, str], list] = {}
    for path in sorted(results_root.glob("*/summary.json")):
        try:
            s = json.loads(path.read_text())
        except (OSError, ValueError):
            print(f"warning: skipping unreadable {path}", file=sys.stderr)
            continue
        if s.get("backend") != "k8s":
            continue
        t = totals.setdefault((s.get("context", "?"), s.get("profile", "?")), [0, 0.0, None])
        t[0] += 1
        t[1] += s.get("node_hours") or 0
        if s.get("cost_estimate") is not None:
            t[2] = (t[2] or 0) + s["cost_estimate"]
    if not totals:
        return f"no k8s runs under {results_root}"
    lines = []
    for (ctx, prof), (n, hours, cost) in sorted(totals.items()):
        money = f"${cost:.2f}" if cost is not None else "(no price)"
        lines.append(f"{ctx}  {prof}  {n} runs  {hours:.2f} node-hours  {money}")
    return "\n".join(lines)
