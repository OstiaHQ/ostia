"""Manifest-first capture fetching, gate pair files and pair-id stamping (RFC-0003 §4, §5, §7).

A run whose pod wrote /w/capture/manifest.json has its capture fetched before teardown:

    read_tar(["manifest.json"]) -> check_manifest -> read_tar(manifest + listed files)
      -> same manifest bytes? -> only regular members, each requested once -> verify_dir
      -> accepted (complete | partial)            any failure -> rejected, diagnostics.txt only

`read_tar(names, limit)` is the backend's transport: it returns a local tar of those names from
the capture directory, raising CaptureAbsent, CaptureTooLarge or CaptureTransportError. Nothing
here raises InfraError: a capture never decides a run's exit code (RFC-0005 §6).
"""

import json
import os
import re
import shutil
import statistics
import tarfile
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from ostia_dev.errors import OstiaError
from ostia_dev.topo import manifest
from ostia_dev.topo.manifest import CaptureRejected

CAPTURE_DIR = "/w/capture"
# Every fetched member counts; a capture is a few MB, so more means stray files.
LIMIT = 64 * 1024**2
DIAGNOSTICS = "diagnostics.txt"
NODE_DIRS = ("node-0", "node-1")
LEAK_STOPLIST = frozenset(
    ("cluster", "clusters", "arn", "aws", "eks", "gke", "gcp", "azure", "aks", "default", "context")
)
# Region and zone names (us-central1-a, us-west-2) are shared by every account.
REGION = re.compile(r"^[a-z]{2,}-[a-z]+-?\d{1,2}(-?[a-z])?$")
MIN_IDENTIFIER = 6
LEAK_CHECK = "docs/guides/fixtures.md#leak-check"


class CaptureAbsent(Exception):
    """The pod has no manifest.json in its capture directory."""


class CaptureTransportError(Exception):
    """kubectl or the remote tar failed; the capture may be fine."""


class CaptureTooLarge(Exception):
    """The requested files add up to more than the limit."""


class CaptureNotRegular(Exception):
    """A requested name is missing, a symlink or not a regular file; retrying cannot help."""


class PairError(Exception):
    """pair.json cannot be written from these captures; the message is values-free."""


@dataclass(frozen=True)
class Accepted:
    status: str
    missing: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Rejected:
    reason: str
    fix: str


@dataclass(frozen=True)
class Absent:
    pass


def probe_script(capture_dir: str = CAPTURE_DIR) -> str:
    return f"test -f {capture_dir}/manifest.json || exit 3"


def sizes_script(capture_dir: str = CAPTURE_DIR) -> str:
    # wc would follow a symlink and block on a FIFO; only regular files reach it (exit 4 else).
    return (
        f'cd {capture_dir} && for f; do [ -f "$f" ] && [ ! -h "$f" ] || exit 4; done; wc -c -- "$@"'
    )


def tar_script(capture_dir: str = CAPTURE_DIR) -> str:
    return f'cd {capture_dir} && tar -cf - -- "$@"'


def parse_sizes(text: str, names: list[str]) -> int:
    """The total of `wc -c` output for `names`: one line per file in order, then a total line
    when there are several."""
    lines = text.splitlines()
    if len(lines) < len(names):
        raise CaptureTransportError("wc -c printed fewer lines than files")
    try:
        return sum(int(line.split()[0]) for line in lines[: len(names)])
    except (IndexError, ValueError) as e:
        raise CaptureTransportError("wc -c printed a line without a size") from e


def tar_bound(limit: int, members: int) -> int:
    """The largest tar stream `members` files of `limit` bytes in total can make: a 512-byte
    header and up to 511 bytes of padding per member, the 1 KiB end marker, and padding to
    tar's 10 KiB record."""
    return limit + 1024 * members + 1024 + 10240


_TRANSPORT_FIX = "rerun; the pod or the connection went away during the fetch"
_OVERSIZE_FIX = (
    f"only the capture tool may write to {CAPTURE_DIR}; remove the stray files the run put there"
)
_INTEGRITY_FIX = (
    f"rerun; if it repeats, keep the pod with --keep-on-failure and look at {CAPTURE_DIR}"
)


def _read(read_tar: Callable[[list[str], int], Path], names: list[str], limit: int) -> Path:
    try:
        path = read_tar(names, limit)
    except CaptureTransportError:
        path = read_tar(names, limit)
    if path.stat().st_size > tar_bound(limit, len(names)):
        raise CaptureTooLarge("the tar stream is larger than its files allow")
    return path


def _extract(tar_path: Path, names: list[str], limit: int, into: Path) -> None:
    try:
        with tarfile.open(tar_path, mode="r:") as t:
            members = t.getmembers()
            seen = [m.name for m in members]
            if len(set(seen)) != len(seen):
                raise CaptureRejected("the tar stream repeats a member")
            outside = sum(name not in names for name in seen)
            if outside:
                # Names are not printed: a stray member's name can be anything.
                raise CaptureRejected(f"the tar stream holds {outside} unrequested members")
            for m in members:
                if not m.isreg():
                    raise CaptureRejected(f"{m.name} is not a regular file in the tar stream")
            if sum(m.size for m in members) > limit:
                raise CaptureTooLarge("the tar stream's files are larger than the limit")
            for name in names:
                if name not in seen:
                    raise CaptureRejected(f"{name} is missing from the tar stream")
            t.extractall(into, members=members, filter="data")
    except tarfile.TarError as e:
        raise CaptureRejected("the tar stream is not a valid tar") from e


class _ManifestRejected(CaptureRejected):
    """The manifest itself says the capture must not be used: schema, status, leak check."""


def _reason(value: object) -> str:
    from ostia_dev.topo.cli import REASON_CODE

    return value if isinstance(value, str) and REASON_CODE.fullmatch(value) else "non-code reason"


def _missing(doc: dict) -> list[str]:
    return [
        f"{m.get('file')} missing ({_reason(m.get('reason'))})"
        for m in doc.get("missing", [])
        if isinstance(m, dict) and m.get("file") in manifest.FILES
    ]


def _diagnostics(read_tar: Callable[[list[str], int], Path], dest: Path) -> None:
    """Best effort: diagnostics.txt is values-free by construction (RFC-0003 §4)."""
    staging = Path(tempfile.mkdtemp(prefix=".diagnostics-", dir=dest))
    try:
        _extract(_read(read_tar, [DIAGNOSTICS], LIMIT), [DIAGNOSTICS], LIMIT, staging)
        os.replace(staging / DIAGNOSTICS, dest / DIAGNOSTICS)
    except (
        CaptureAbsent,
        CaptureTransportError,
        CaptureTooLarge,
        CaptureNotRegular,
        CaptureRejected,
        OSError,
    ):
        pass
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _accept(read_tar: Callable[[list[str], int], Path], staging: Path) -> dict:
    first = staging / "first"
    first.mkdir()
    _extract(
        _read(read_tar, ["manifest.json"], manifest.MAX_MANIFEST_BYTES),
        ["manifest.json"],
        manifest.MAX_MANIFEST_BYTES,
        first,
    )
    raw = (first / "manifest.json").read_bytes()
    try:
        doc = manifest.check_manifest(raw)
    except CaptureRejected as e:
        raise _ManifestRejected(e.reason) from e
    except OstiaError as e:
        raise CaptureRejected("manifest.json could not be checked (jsonschema missing)") from e
    names = ["manifest.json", *sorted(doc["files"])]
    files = staging / "files"
    files.mkdir()
    try:
        _extract(_read(read_tar, names, LIMIT), names, LIMIT, files)
    except CaptureAbsent as e:
        raise CaptureRejected("manifest.json disappeared during the fetch") from e
    # The tool writes the manifest last, so a changed one means the files may have changed too.
    if (files / "manifest.json").read_bytes() != raw:
        raise CaptureRejected("manifest.json changed during the fetch")
    manifest.verify_dir(files, doc)
    return doc


_LOCAL_IO_FIX = "check the free space and permissions of the results directory, then rerun"


def fetch(read_tar: Callable[[list[str], int], Path], dest: Path) -> Accepted | Rejected | Absent:
    """Fetch one pod's capture into `dest`, which must not exist yet (RFC-0003 §4). A rejected
    capture leaves only diagnostics.txt in `dest`."""
    try:
        return _fetch(read_tar, dest)
    except OSError:
        return Rejected("local_io: the capture could not be written on this machine", _LOCAL_IO_FIX)


def _fetch(read_tar: Callable[[list[str], int], Path], dest: Path) -> Accepted | Rejected | Absent:
    dest.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".capture-", dir=dest.parent))
    try:
        try:
            doc = _accept(read_tar, staging)
        except CaptureAbsent:
            return Absent()
        except CaptureTransportError:
            outcome = Rejected("the fetch failed twice (kubectl or tar)", _TRANSPORT_FIX)
        except CaptureTooLarge:
            outcome = Rejected(f"the capture is larger than {LIMIT // 1024**2} MiB", _OVERSIZE_FIX)
        except CaptureNotRegular:
            outcome = Rejected(
                f"not_regular: a requested file in {CAPTURE_DIR} is missing, a symlink or not a "
                "regular file",
                _INTEGRITY_FIX,
            )
        except _ManifestRejected as e:
            outcome = Rejected(e.reason, f"see {LEAK_CHECK}")
        except CaptureRejected as e:
            outcome = Rejected(e.reason, _INTEGRITY_FIX)
        else:
            os.replace(staging / "files", dest)
            _diagnostics(read_tar, dest)
            return Accepted(doc["status"], _missing(doc))
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    dest.mkdir(exist_ok=True)
    _diagnostics(read_tar, dest)
    return outcome


def status_entry(outcome: Accepted | Rejected | Absent) -> dict:
    """One node's line of capture/status.json."""
    if isinstance(outcome, Accepted):
        fix = ", ".join(outcome.missing) if outcome.status == "partial" else None
        return {"result": "accepted", "reason": None, "status": outcome.status, "fix": fix}
    if isinstance(outcome, Rejected):
        return {"result": "rejected", "reason": outcome.reason, "status": None, "fix": outcome.fix}
    return {
        "result": "absent",
        "reason": f"no {CAPTURE_DIR}/manifest.json",
        "status": None,
        "fix": "the capture step did not run or wrote no manifest; see log.txt",
    }


def describe(node: str, entry: dict) -> str:
    """One values-free line for a node's capture."""
    if entry["result"] == "accepted":
        text = f"capture {node}: accepted ({entry['status']})"
    else:
        text = f"capture {node}: {entry['result']}: {entry['reason']}"
    return f"{text}; fix: {entry['fix']}" if entry.get("fix") else text


def leak_identifiers(values: Iterable[str | None]) -> list[str]:
    """The identifiers a pod's capture must not contain, from the kube context and the
    kubeconfig cluster name: each whole value, then the segments a `_`, `:` or `/` splits it
    into. Only segments are filtered: short ones, provider words and region names are shared by
    every account, so matching them would fail clean captures (RFC-0003 §3)."""
    out: list[str] = []
    for value in values:
        whole = (value or "").strip()
        if not whole:
            continue
        parts = [p.strip() for p in re.split(r"[_:/]", whole) if p.strip()]
        kept = [
            p
            for p in (parts if len(parts) > 1 else [])
            if len(p) >= MIN_IDENTIFIER
            and p.lower() not in LEAK_STOPLIST
            and not REGION.match(p.lower())
        ]
        for item in (whole, *kept):
            if item not in out:
                out.append(item)
    return out


def _rails(rdma_nics: str) -> list[tuple[str, int]]:
    rails = []
    for item in filter(None, (s.strip() for s in rdma_nics.split(","))):
        device, _, port = item.partition(":")
        try:
            rails.append((device, int(port or "1")))
        except ValueError as e:
            raise PairError("rdma_nics names a port that is not a number") from e
    return rails


def _rail_end(nics: dict, device: str, port: int, node: str, rail: int) -> tuple[int, str]:
    """The NIC's index in `nics`'s bus-ordered list, and its link class (RFC-0003 §7)."""
    ends = [r for r in nics.get("rdma", []) if r.get("device") == device and r.get("port") == port]
    if len(ends) != 1:
        raise PairError(f"rail {rail}: {node}'s nics.json lists its RDMA port {len(ends)} times")
    bus = ends[0].get("bus_id")
    index = [i for i, n in enumerate(nics.get("nics", [])) if n.get("bus_id") == bus]
    if len(index) != 1:
        raise PairError(f"rail {rail}: {node}'s nics.json has no NIC for the rail's RDMA port")
    nic = nics["nics"][index[0]]
    if nic.get("driver") == "efa":
        return index[0], "efa"
    layer = ends[0].get("link_layer")
    if layer not in ("infiniband", "ethernet"):
        raise PairError(f"rail {rail}: {node}'s RDMA port has an unknown link layer")
    return index[0], "infiniband" if layer == "infiniband" else "roce"


def _measured(records: Path, rails: int) -> list[dict]:
    """Rank 1 is the source of every two-pod workload, so each record measures 1->0. A dual_link
    path names its rail; a single-NIC workload is attributed only when there is one rail."""
    out = []
    text = records.read_text(encoding="utf-8") if records.exists() else ""
    for line in text.splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("unit") != "GB/s" or not r.get("samples"):
            continue
        params = r.get("params", {})
        if r["bench"] == "dual_link" and params.get("mode") == "rails":
            rail = {"a": 0, "b": 1}.get(params.get("path"))
        elif r["bench"] in ("rdma_put", "gdr_stream", "tcp_put") and rails == 1:
            rail = 0
        else:
            rail = None
        if rail is None or rail >= rails:
            continue
        median = r.get("median")
        if median is None:
            median = statistics.median(r["samples"])
        out.append(
            {"rail": rail, "direction": "1->0", "bw_mbps": round(median * 1000), "test": r["bench"]}
        )
    return sorted(out, key=lambda m: (m["rail"], m["test"]))


def write_pair_json(capture: Path, profile, records: Path) -> None:
    """capture/pair.json from the profile's rails, each node's nics.json and the rank-1 records
    (RFC-0003 §7). Without rdma_nics the pair has no rails and is a tcp pair."""
    rails = _rails(profile.rdma_nics or "")
    nics = {}
    for node in NODE_DIRS:
        try:
            nics[node] = json.loads((capture / node / "nics.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise PairError(f"{node}'s nics.json cannot be read") from e
    entries, classes = [], set()
    for i, (device, port) in enumerate(rails):
        entry = {}
        for node in NODE_DIRS:
            index, cls = _rail_end(nics[node], device, port, node, i)
            entry[node] = {"nic_index": index}
            classes.add(cls)
        entries.append(entry)
    if len(classes) > 1:
        raise PairError("the rails' NICs have different link classes")
    try:
        measured = _measured(records, len(entries))
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise PairError("the run's results.jsonl cannot be read") from e
    doc = {
        "schema": 1,
        "nodes": list(NODE_DIRS),
        "link_class": classes.pop() if classes else "tcp",
        "rails": entries,
        "measured": measured,
    }
    (capture / "pair.json").write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")


def stamp(results_jsonl: Path, pair: str) -> None:
    """Every record gets the pair id as its topology; only the gate sets topology_source."""
    lines = []
    for line in results_jsonl.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        r["compat"]["topology"] = pair
        r["provenance"]["topology_source"] = "gate"
        lines.append(json.dumps(r) + "\n")
    tmp = results_jsonl.with_name(results_jsonl.name + ".tmp")
    tmp.write_text("".join(lines), encoding="utf-8")
    os.replace(tmp, results_jsonl)
