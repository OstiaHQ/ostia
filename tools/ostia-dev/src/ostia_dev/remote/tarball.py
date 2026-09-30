"""The upload tarball, built on the host before anything exists remotely (RFC-0005 §4.10).

The file set is `git ls-files -co --exclude-standard` minus `git ls-files -d`: tracked and
untracked files, without tracked files deleted locally and without git-ignored ones. It
works in a linked worktree, because git runs on the host. Untracked files that look like
secrets are skipped with a warning (tracked ones are already in git). The tar is
deterministic (sorted, mtime 0, uid/gid 0, fixed modes), so its sha256 is the tree hash.

--ref <sha> and --ref pr/<n> fetch the commit from origin and archive it instead.
"""

import fnmatch
import hashlib
import io
import os
import sys
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from ostia_dev import proc
from ostia_dev.contract import violation
from ostia_dev.errors import UsageError

WARN_BYTES = 10 * 1024**2  # an untracked file this large gets a warning
MAX_BYTES = 500 * 1024**2  # a bigger upload is refused
SECRET_PATTERNS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "id_rsa*",
    "id_ed25519*",
    ".netrc",
    ".pypirc",
    ".npmrc",
    "*kubeconfig*",
)
SECRET_DIRS = (".kube",)


@dataclass
class Tarball:
    path: Path
    size: int
    tree_hash: str
    files: list[str]
    file_count: int  # regular files, what `find /w -type f` counts after unpacking
    skipped: list[str] = field(default_factory=list)
    ref_sha: str | None = None


def _git_error(root: Path, what: str, err: str) -> UsageError:
    return UsageError(
        violation(
            f"git {what} failed in {root}",
            err.strip().splitlines()[:5],
            "the upload is built from git on the host (RFC-0005 §4.10)",
            "run it from an Ostia checkout, or check the ref with git fetch origin <ref>",
            "RFC-0005 §4.10",
        )
    )


def _git(root: Path, *args: str) -> str:
    r = proc.run(["git", "-C", str(root), *args])
    if r.returncode != 0:
        raise _git_error(root, args[0], r.stderr)
    return r.stdout


def _zlist(out: str) -> list[str]:
    return [f for f in out.split("\0") if f]


def looks_secret(path: str) -> bool:
    p = PurePosixPath(path)
    if any(part in SECRET_DIRS for part in p.parts[:-1]):
        return True
    return any(fnmatch.fnmatchcase(p.name, pat) for pat in SECRET_PATTERNS)


def _info(name: str, *, size: int = 0, executable: bool = False) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = size
    info.mtime = 0
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mode = 0o755 if executable else 0o644
    return info


def _write(entries: list[tuple[tarfile.TarInfo, bytes | None]], out_dir: Path | None) -> Path:
    d = Path(out_dir or tempfile.mkdtemp(prefix="ostia-upload-"))
    d.mkdir(parents=True, exist_ok=True)
    path = d / "upload.tar"
    with tarfile.open(path, "w", format=tarfile.PAX_FORMAT) as t:
        for info, data in sorted(entries, key=lambda e: e[0].name):
            t.addfile(info, io.BytesIO(data) if data is not None else None)
    return path


def _refuse_size(sizes: dict[str, int], total: int) -> None:
    largest = sorted(sizes.items(), key=lambda kv: -kv[1])[:10]
    raise UsageError(
        violation(
            f"the upload would be {total / 1024**2:.0f} MB, over the "
            f"{MAX_BYTES // 1024**2} MB limit",
            ["largest files:", *(f"  {n / 1024**2:8.1f} MB  {f}" for f, n in largest)],
            "remote runs upload the working tree, which must stay small (RFC-0005 §4.10)",
            "delete or git-ignore the large untracked files, or run a pushed commit with --ref",
            "RFC-0005 §4.10",
        )
    )


def _from_worktree(root: Path, out_dir: Path | None) -> Tarball:
    listed = set(_zlist(_git(root, "ls-files", "-co", "--exclude-standard", "-z")))
    deleted = set(_zlist(_git(root, "ls-files", "-d", "-z")))
    untracked = set(_zlist(_git(root, "ls-files", "-o", "--exclude-standard", "-z")))
    skipped = sorted(f for f in untracked if looks_secret(f))
    files = sorted(listed - deleted - set(skipped))
    if skipped:
        print(
            "warning: not uploading untracked files that look like secrets "
            f"(RFC-0005 §4.10): {', '.join(skipped)}",
            file=sys.stderr,
        )
    entries, sizes, count = [], {}, 0
    for f in files:
        p = root / f
        if p.is_symlink():
            info = _info(f)
            info.type, info.linkname, info.mode = tarfile.SYMTYPE, os.readlink(p), 0o777
            entries.append((info, None))
            continue
        if not p.is_file():
            continue  # a submodule or a directory
        data = p.read_bytes()
        sizes[f] = len(data)
        count += 1
        if f in untracked and len(data) > WARN_BYTES:
            print(
                f"warning: uploading a large untracked file: {f} ({len(data) / 1024**2:.0f} MB)",
                file=sys.stderr,
            )
        entries.append((_info(f, size=len(data), executable=os.access(p, os.X_OK)), data))
    total = sum(sizes.values())
    if total > MAX_BYTES:
        _refuse_size(sizes, total)
    kept = [e[0].name for e in entries]
    return _finish(_write(entries, out_dir), kept, count, skipped, None)


def _from_ref(root: Path, ref: str, out_dir: Path | None) -> Tarball:
    spec = f"pull/{ref.removeprefix('pr/')}/head" if ref.startswith("pr/") else ref
    r = proc.run(["git", "-C", str(root), "fetch", "-q", "origin", spec])
    if r.returncode != 0:
        raise UsageError(
            violation(
                f"could not fetch {ref} from origin",
                [f"git fetch origin {spec}: {r.stderr.strip()}"],
                "--ref runs a pushed commit or a pull request head (RFC-0005 §4.10)",
                "push the commit first, or check the SHA or pull request number",
                "RFC-0005 §4.10",
            )
        )
    sha = _git(root, "rev-parse", "FETCH_HEAD").strip()
    archive = proc.run(["git", "-C", str(root), "archive", "--format=tar", sha], input=b"")
    if archive.returncode != 0:
        raise _git_error(root, "archive", archive.stderr.decode(errors="replace"))
    entries, sizes, count = [], {}, 0
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as t:
        for m in t.getmembers():
            if m.isdir() or m.type == tarfile.XGLTYPE:
                continue
            if m.issym():
                info = _info(m.name)
                info.type, info.linkname, info.mode = tarfile.SYMTYPE, m.linkname, 0o777
                entries.append((info, None))
                continue
            data = t.extractfile(m).read()
            sizes[m.name] = len(data)
            count += 1
            entries.append((_info(m.name, size=len(data), executable=bool(m.mode & 0o111)), data))
    total = sum(sizes.values())
    if total > MAX_BYTES:
        _refuse_size(sizes, total)
    kept = sorted(e[0].name for e in entries)
    return _finish(_write(entries, out_dir), kept, count, [], sha)


def _finish(path: Path, files, count, skipped, sha) -> Tarball:
    data = path.read_bytes()
    return Tarball(
        path=path,
        size=len(data),
        tree_hash=hashlib.sha256(data).hexdigest(),
        files=files,
        file_count=count,
        skipped=skipped,
        ref_sha=sha,
    )


def build(root: Path, *, ref: str | None = None, out_dir: Path | None = None) -> Tarball:
    root = Path(root)
    return _from_ref(root, ref, out_dir) if ref else _from_worktree(root, out_dir)


def worktree_sha(root: Path) -> str:
    """The short HEAD SHA, with +dirty when the working tree differs from it."""
    sha = _git(root, "rev-parse", "--short", "HEAD").strip()
    dirty = proc.run(["git", "-C", str(root), "status", "--porcelain"]).stdout.strip()
    return f"{sha}+dirty" if dirty else sha
