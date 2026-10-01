"""Collected results (RFC-0005 §3.4). The pod's tar is untrusted: artifacts pass
tarfile.data_filter, then Ostia's allowlist, link and size rules.
"""

import json
import os
import re
import shutil
import sys
import tarfile
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath

from ostia_dev.contract import violation
from ostia_dev.errors import InfraError

CONTROL = ("steps.json", "tests.json", "log.txt", "fingerprint.txt")
LOG_CAP_BYTES = 256 * 1024**2
CAP_BYTES = 2 * 1024**3
BUILD_ALLOW = ("ostia-summary.txt", "junit*.xml", "Testing/*")
BENCH = "bench/results/"


@dataclass
class Control:
    steps: dict
    log_truncated: bool


def control_tar_script(work: str = "/w") -> str:
    names = " ".join(CONTROL)
    return (
        f"cd {work}/.ostia && tar -cf - -T /dev/null "
        f'$(for f in {names}; do [ -e "$f" ] && echo "$f"; done)'
    )


def artifact_tar_script(build_rel: str, work: str = "/w") -> str:
    paths = (
        f"{build_rel}/ostia-summary.txt {build_rel}/junit*.xml {build_rel}/Testing bench/results"
    )
    return (
        f'cd {work} && tar -cf - -T /dev/null $(for p in {paths}; do [ -e "$p" ] && echo "$p"; '
        "done)"
    )


def _missing_steps(detail: str) -> InfraError:
    return InfraError(
        violation(
            "the run's steps.json is missing or invalid",
            [detail],
            "the exit code is decided from steps.json; without it the result is unknown",
            "read log.txt in the results directory; rerun, and check the pod's storage",
            "RFC-0005 §3.4",
        )
    )


def extract_control(tar_path: Path, dest: Path) -> Control:
    dest.mkdir(parents=True, exist_ok=True)
    truncated = False
    with tarfile.open(tar_path) as t:
        for m in t.getmembers():
            # `exec tar -c` gives bare names; `engine cp <c>:/w/.ostia -` prefixes .ostia/
            name = m.name.removeprefix("./").removeprefix(".ostia/")
            if name not in CONTROL or not m.isfile():
                continue
            src = t.extractfile(m)
            limit = LOG_CAP_BYTES if name == "log.txt" else None
            with (dest / name).open("wb") as f:
                if limit is not None and m.size > limit:
                    f.write(src.read(limit))
                    f.write(b"\n[ostia] log truncated at 256 MiB; see summary.json\n")
                    truncated = True
                else:
                    shutil.copyfileobj(src, f)
    path = dest / "steps.json"
    if not path.exists():
        raise _missing_steps("steps.json was not in the pod's .ostia directory")
    try:
        steps = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise _missing_steps(f"steps.json does not parse: {e}") from e
    if not isinstance(steps, dict) or "state" not in steps or "steps" not in steps:
        raise _missing_steps("steps.json has no state or steps")
    return Control(steps=steps, log_truncated=truncated)


def _target(name: str, build_rel: str) -> str | None:
    prefix = build_rel.rstrip("/") + "/"
    if name.startswith(prefix):
        rel = name[len(prefix) :]
        if any(fnmatch(rel, pat) for pat in BUILD_ALLOW) and "/" not in rel.split("Testing/", 1)[0]:
            return rel
        return None
    if name.startswith(BENCH) and name != BENCH:
        return name
    return None


def extract_artifacts(tar_path: Path, dest: Path, build_rel: str) -> list[str]:
    dest.mkdir(parents=True, exist_ok=True)
    root = os.path.realpath(dest) + os.sep
    dropped: list[str] = []
    total = 0

    def keep(member: tarfile.TarInfo, path: str) -> tarfile.TarInfo | None:
        nonlocal total
        name = member.name
        try:
            member = tarfile.data_filter(member, path)
        except tarfile.FilterError:
            dropped.append(name)
            return None
        if member.name != name:
            dropped.append(name)  # data_filter stripped a leading /
            return None
        if member.isdir():
            return None
        if member.issym() or member.islnk() or not member.isfile():
            dropped.append(name)
            return None
        target = _target(name, build_rel)
        # data_filter checked the original name; the remapped one must stay inside too
        if (
            target is None
            or ".." in PurePosixPath(target).parts
            or not os.path.realpath(os.path.join(path, target)).startswith(root)
            or total + member.size > CAP_BYTES
        ):
            dropped.append(name)
            return None
        total += member.size
        return member.replace(name=target, deep=False)

    with tarfile.open(tar_path) as t:
        t.extractall(dest, filter=keep)
    if dropped:
        print(
            "warning: results not copied back (outside the allowlist, links, unsafe, or past "
            f"the {CAP_BYTES // 1024**3} GiB cap; RFC-0005 §3.4): {', '.join(dropped)}",
            file=sys.stderr,
        )
    return dropped


def merge_bench_results(
    results_dir: Path, subdirs: list[str], run_id: str, repo: Path
) -> Path | None:
    """Copies each rank's bench/results/<run id>/ to the repo for compare.py (§3.4). Only the
    source rank writes records (§4.11), so the same file from two ranks is a bug, not a merge."""
    found: dict[Path, tuple[str, bytes]] = {}
    for sub in subdirs:
        src = results_dir / sub / "bench" / "results" / run_id
        for f in sorted(p for p in src.rglob("*") if p.is_file()) if src.is_dir() else []:
            rel, data = f.relative_to(src), f.read_bytes()
            if rel in found and found[rel][1] != data:
                raise InfraError(
                    f"error: {found[rel][0]} and {sub} both wrote bench/results/{run_id}/{rel}\n"
                    "  rule: only the source rank of a two-pod run writes benchmark records\n"
                    "  see: RFC-0005 §4.11"
                )
            found[rel] = (sub, data)
    if not found:
        return None
    dst = repo / "bench" / "results" / run_id
    for rel, (_, data) in found.items():
        (dst / rel).parent.mkdir(parents=True, exist_ok=True)
        (dst / rel).write_bytes(data)
    return dst


_FLOOR = re.compile(r"A/A noise floor: (±[0-9.]+%)")


def parse_noise_floor(log: str) -> str | None:
    m = _FLOOR.search(log)
    return m.group(1) if m else None


def write_summary(results_dir: Path, summary: dict) -> Path:
    path = results_dir / "summary.json"
    path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    return path


def duration(seconds: int) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


_GROUPS = (("preflight", "preflight"), ("install", "install"), ("build", "build"),
           ("command", "test"))  # fmt: skip


def summary_lines(s: dict) -> list[str]:
    where = s.get("gpu") or s["profile"]
    what = f"suite {s['suite']}" if s.get("suite") else f"env {s['env']}"
    code = "  code contributor pr/" + str(s["pr"]) if s.get("code") == "contributor" else ""
    first = (
        f"{s['run_id']}  {s['result']}  {where}  sha {s['git_sha']}(tree {s['tree_hash'][:4]}…)"
        f"  {what}{code}  {duration(s['seconds'])}"
    )
    parts = [f"{k} {duration(v)}" for k, v in s.get("phases", {}).items()]
    by_group: dict[str, int] = {}
    for step in s.get("steps", []):
        for kind, label in _GROUPS:
            if step["kind"] == kind:
                by_group[label] = by_group.get(label, 0) + step["seconds"]
    parts += [f"{label} {duration(by_group[label])}" for _, label in _GROUPS if label in by_group]
    lines = [first, "  " + " · ".join(parts)] if parts else [first]
    for name, rep in sorted(s.get("reports", {}).items()):
        floor = f"noise floor {rep['noise_floor']}" if rep.get("noise_floor") else "reported"
        lines.append(f"  report {name}: {floor} (exit {rep['code']}, never fails the run)")
    heavy = by_group.get("install", 0) + by_group.get("build", 0)
    if s["backend"] == "k8s" and s["seconds"] and heavy / s["seconds"] >= 0.5:
        lines.append(
            f"  tip: install and build took {round(100 * heavy / s['seconds'])}% of this run; "
            "--cache reuses downloads between runs (RFC-0005 §4.7)"
        )
    return lines
