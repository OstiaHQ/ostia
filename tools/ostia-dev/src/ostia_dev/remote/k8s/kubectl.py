"""The pinned official kubectl (RFC-0005 §4.1): conda-forge's is outside the version-skew
policy for current servers, so ostia-dev downloads the release binary and checks its sha256.
"""

import hashlib
import os
import platform
import tomllib
import urllib.request
from importlib import resources
from pathlib import Path

from ostia_dev.contract import violation
from ostia_dev.errors import UsageError

BASE = "https://dl.k8s.io/release"
PLATFORMS = ("linux-amd64", "linux-arm64", "darwin-arm64")
CHUNK = 1024 * 1024
HEADER = (
    "# The official kubectl that remote k8s runs use (RFC-0005 §4.1), checked against these\n"
    "# sha256 sums before first use. Renovate bumps the version; its post-upgrade task\n"
    "# `pixi run ostia-dev remote k8s kubectl --update-shas` refills the sums.\n"
)


def _table() -> dict:
    return tomllib.loads((resources.files(__package__) / "kubectl.toml").read_text())


VERSION: str = _table()["version"]


def _fetch(url: str):
    return urllib.request.urlopen(url, timeout=60)


def _bad(problem: str, details: list[str], fix: str) -> UsageError:
    return UsageError(
        violation(
            problem,
            details,
            "remote k8s runs use the pinned official kubectl",
            fix,
            "RFC-0005 §4.1",
        )
    )


def platform_key() -> str:
    system = platform.system().lower()
    machine = {"x86_64": "amd64", "amd64": "amd64", "arm64": "arm64", "aarch64": "arm64"}.get(
        platform.machine().lower(), platform.machine().lower()
    )
    key = f"{system}-{machine}"
    if key not in PLATFORMS:
        raise _bad(
            f"no pinned kubectl for this platform ({key})",
            [f"pinned: {', '.join(PLATFORMS)}"],
            'pass --kubectl PATH, or set kubectl = "<path>" for the context in the config',
        )
    return key


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    return (Path(base) if base else Path.home() / ".cache") / "ostia" / "kubectl"


def _given(path: str, what: str) -> Path:
    p = Path(path).expanduser()
    if not p.is_file():
        raise _bad(
            f"{what} {path} does not exist",
            [],
            'check the path passed to --kubectl or set as kubectl = "…" in the config',
        )
    return p


def ensure(
    *,
    flag: str | None = None,
    configured: str | None = None,
    cache_root: Path | None = None,
    fetch=_fetch,
) -> Path:
    """--kubectl, else the context's `kubectl =`, else the verified pinned download."""
    if flag:
        return _given(flag, "--kubectl")
    if configured:
        return _given(configured, "the configured kubectl")
    table = _table()
    key = platform_key()
    want = table["sha256"][key]
    target = (cache_root or cache_dir()) / table["version"] / "kubectl"
    if target.is_file() and _sha256(target) == want:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".kubectl.{os.getpid()}.tmp")
    os_name, arch = key.split("-")
    url = f"{BASE}/{table['version']}/bin/{os_name}/{arch}/kubectl"
    h = hashlib.sha256()
    try:
        src = fetch(url)
        with tmp.open("wb") as out:
            while chunk := src.read(CHUNK):
                h.update(chunk)
                out.write(chunk)
    except (OSError, ValueError) as e:
        tmp.unlink(missing_ok=True)
        raise _bad(
            f"could not download kubectl {table['version']}",
            [f"{url}: {e}"],
            "check the network, or pass --kubectl PATH",
        ) from e
    got = h.hexdigest()
    if got != want:
        tmp.unlink(missing_ok=True)
        raise _bad(
            f"the downloaded kubectl {table['version']} does not match its pinned sha256",
            [f"url: {url}", f"expected: {want}", f"got: {got}"],
            "retry; if it repeats, the download is being tampered with or the pin is wrong",
        )
    tmp.chmod(0o755)
    os.replace(tmp, target)
    return target


def update_shas(path: Path | None = None, fetch=_fetch) -> dict[str, str]:
    """Refill the sums for the pinned version (Renovate's post-upgrade task)."""
    path = path or Path(str(resources.files(__package__) / "kubectl.toml"))
    version = tomllib.loads(path.read_text())["version"]
    shas = {}
    for key in PLATFORMS:
        os_name, arch = key.split("-")
        text = fetch(f"{BASE}/{version}/bin/{os_name}/{arch}/kubectl.sha256").read().decode()
        shas[key] = text.split()[0]
    body = "".join(f'{k} = "{v}"\n' for k, v in shas.items())
    path.write_text(f'{HEADER}version = "{version}"\n\n[sha256]\n{body}')
    return shas
