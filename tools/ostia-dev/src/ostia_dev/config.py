"""Config (RFC-0005 §1.2): the built-in profiles.toml, with the user's file merged over it.

The user file is $OSTIA_CONFIG, else $XDG_CONFIG_HOME/ostia/config.toml, else
~/.config/ostia/config.toml. Its `[remote.k8s.profiles]` and `[remote.windows]` tables
merge per key over the built-in `[profiles]` and `[windows]`. The CLI writes to the file
only after asking (§4.3), through append_table().
"""

import copy
import os
import re
import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from ostia_dev.contract import violation
from ostia_dev.errors import UsageError

SCHEMAS = (1,)
_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def user_path() -> Path:
    if explicit := os.environ.get("OSTIA_CONFIG"):
        return Path(explicit)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "ostia" / "config.toml"


def builtin_defaults() -> dict:
    """ostia_dev/remote/profiles.toml, shipped as package data."""
    return tomllib.loads((resources.files("ostia_dev.remote") / "profiles.toml").read_text())


@dataclass
class Config:
    path: Path
    user: dict = field(default_factory=dict)
    profiles: dict = field(default_factory=dict)
    suites: dict = field(default_factory=dict)
    windows: dict = field(default_factory=dict)
    image: str = ""

    def remote(self, *keys: str) -> dict:
        """A table under the user's [remote], e.g. remote("k8s", "machines")."""
        node = self.user.get("remote", {})
        for k in keys:
            node = node.get(k, {})
        return node

    def context(self, name: str) -> dict:
        return self.remote("k8s", "contexts").get(name, {})


def merge(base: dict, over: dict) -> dict:
    """Per-key merge: tables merge recursively, anything else (lists too) is replaced."""
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _parse(path: Path, text: str) -> dict:
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise UsageError(
            violation(
                f"config {path} is not valid TOML",
                [str(e)],
                "the config file is TOML (RFC-0005 §1.2)",
                f"fix the file, or move it away to use the built-in defaults: {path}",
                "RFC-0005 §1.2",
            )
        ) from e
    if doc.get("schema") not in SCHEMAS:
        raise UsageError(
            violation(
                f"config {path} has schema {doc.get('schema', '(none)')!r}",
                [f"supported schema versions: {', '.join(map(str, SCHEMAS))}"],
                "every config file carries a schema version this ostia-dev knows",
                "set schema = 1 at the top of the file",
                "RFC-0005 §1.2",
            )
        )
    return doc


def load(path: Path | None = None, *, builtins: dict | None = None) -> Config:
    path = path or user_path()
    base = builtin_defaults() if builtins is None else builtins
    user = _parse(path, path.read_text()) if path.exists() else {}
    remote = user.get("remote", {})
    return Config(
        path=path,
        user=user,
        profiles=merge(base.get("profiles", {}), remote.get("k8s", {}).get("profiles", {})),
        suites=copy.deepcopy(base.get("suites", {})),
        windows=merge(base.get("windows", {}), remote.get("windows", {})),
        image=remote.get("image", base.get("image", "")),
    )


def _basic(s: str) -> str:
    """A TOML basic string: quotes, backslashes and control characters escaped."""
    out = []
    for ch in s:
        if ch in ('"', "\\"):
            out.append("\\" + ch)
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _value(v: object) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int | float):
        return repr(v)
    if isinstance(v, str):
        return _basic(v)
    raise ValueError(f"append_table writes scalar values only, not {type(v).__name__}")


def _header_path(line: str) -> tuple[str, ...] | None:
    """The key path of a `[table]` header line, or None for anything else."""
    stripped = line.strip()
    if not stripped.startswith("[") or stripped.startswith("[["):
        return None
    try:
        node = tomllib.loads(stripped)
    except tomllib.TOMLDecodeError:
        return None
    keys = []
    while isinstance(node, dict) and len(node) == 1:
        ((k, node),) = node.items()
        keys.append(k)
    return tuple(keys) if node == {} else None


def append_table(path: Path, table: tuple[str, ...], values: dict[str, object]) -> None:
    """Add `values` to `[table]` in the user config, keeping everything else byte for byte.

    Every table-key segment and string value is a TOML basic string, because kube context
    names aren't bare keys (EKS contexts are ARNs). The result is re-parsed before it
    replaces the file; if it doesn't parse, the file is left as it was.
    """
    for k in values:
        if not _BARE_KEY.match(k):
            raise ValueError(f"not a bare TOML key: {k!r}")
    lines = [f"{k} = {_value(v)}\n" for k, v in values.items()]
    text = path.read_text() if path.exists() else "schema = 1\n"
    rows = text.splitlines(keepends=True)
    start = next((i for i, r in enumerate(rows) if _header_path(r) == tuple(table)), None)
    if start is None:
        if rows and not rows[-1].endswith("\n"):
            rows[-1] += "\n"
        header = "[" + ".".join(_basic(s) for s in table) + "]\n"
        rows += ["\n", header, *lines]
    else:
        end = next(
            (i for i in range(start + 1, len(rows)) if rows[i].lstrip().startswith("[")),
            len(rows),
        )
        while end > start + 1 and not rows[end - 1].strip():
            end -= 1  # insert before the blank lines that separate the next table
        if not rows[end - 1].endswith("\n"):
            rows[end - 1] += "\n"
        rows[end:end] = lines
    new = "".join(rows)
    try:
        doc = tomllib.loads(new)
    except tomllib.TOMLDecodeError as e:
        raise UsageError(
            violation(
                f"could not save {'.'.join(table)} to {path}",
                [str(e), "the file was left unchanged"],
                "ostia-dev writes the config only when the result is valid TOML",
                f"add these lines under [{'.'.join(table)}] by hand: {''.join(lines).strip()}",
                "RFC-0005 §1.2",
            )
        ) from e
    node = doc
    for k in table:
        node = node[k]
    assert all(node.get(k) == v for k, v in values.items())
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(new)
    tmp.replace(path)
