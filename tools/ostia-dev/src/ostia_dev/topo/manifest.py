"""The consumer side of the capture manifest contract (RFC-0003 §4).

A capture directory is accepted only when its manifest.json has a supported schema version,
validates against manifest.schema.json, says the capture is sanitized and passed the leak check,
and every listed file is a regular file whose SHA-256 matches. Every rejection reason is
values-free: it names a field, a file of the contract or a count, never a value read from the
machine. `ostia-dev topo import` uses it, and so do the remote runner's capture fetches.
"""

import functools
import hashlib
import json
import os
import re
import stat
from pathlib import Path

from ostia_dev import errors, paths
from ostia_dev.contract import violation

SCHEMA = paths.ROOT / "fabric/tools/topo-capture/schemas/manifest.schema.json"
SUPPORTED = 1
FILES = ("hwloc.xml", "nvml.json", "nics.json", "links.json", "meta.json")
# Written next to the data files and never listed in `files` (RFC-0003 §4); status.json is the
# remote runner's fetch result, next to a one-pod capture (RFC-0005 §3.4).
UNLISTED = ("manifest.json", "diagnostics.txt", "status.json")
# Every capture that did not fail has these; nvml.json depends on the machine.
ALWAYS = ("hwloc.xml", "nics.json", "meta.json")
# A manifest is a few hundred bytes; the cap keeps a hostile one from being read whole.
MAX_MANIFEST_BYTES = 1024 * 1024


class CaptureRejected(Exception):
    """A capture that must not be used; `reason` is values-free."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _no_duplicates(items: list[tuple[str, object]]) -> dict:
    # The C++ writer never repeats a key, and JSON parsers disagree on which copy wins.
    if len({k for k, _ in items}) != len(items):
        raise CaptureRejected("manifest.json repeats a key")
    return dict(items)


def _no_constant(name: str) -> object:
    raise CaptureRejected("manifest.json holds NaN or Infinity, which JSON does not allow")


def _parse(raw: bytes) -> object:
    try:
        return json.loads(
            raw.decode("utf-8"), object_pairs_hook=_no_duplicates, parse_constant=_no_constant
        )
    except (ValueError, RecursionError) as e:
        raise CaptureRejected("manifest.json is not UTF-8 JSON") from e


@functools.cache
def _ecma(pattern: str) -> re.Pattern[str]:
    """`pattern` with ECMAScript's `$`, which matches only at the end of the string, as the C++
    validator's std::regex does; Python's `$` also matches before a trailing newline."""
    out, i, in_class = [], 0, False
    while i < len(pattern):
        c = pattern[i]
        if c == "\\":
            out.append(pattern[i : i + 2])
            i += 2
            continue
        if in_class:
            in_class = c != "]"
        elif c == "[":
            in_class = True
        elif c == "$":
            c = r"\Z"
        out.append(c)
        i += 1
    # ECMAScript's \d and \w are ASCII.
    return re.compile("".join(out), re.ASCII)


def _validator():
    try:
        from jsonschema import Draft202012Validator, validators
        from jsonschema.exceptions import ValidationError
    except ImportError as e:
        raise errors.InfraError(
            violation(
                "jsonschema is not installed in this environment",
                [f"needed by: {Path(__file__).name}"],
                "the capture consumer validates manifest.json with jsonschema (RFC-0003 §4)",
                "pixi install",
                "RFC-0005 §2.4",
            )
        ) from e

    def pattern(validator, regex, instance, schema):
        if validator.is_type(instance, "string") and not _ecma(regex).search(instance):
            yield ValidationError("does not match the schema's pattern")

    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    return validators.extend(Draft202012Validator, {"pattern": pattern})(schema)


def _check_version(doc: object) -> None:
    if not isinstance(doc, dict):
        raise CaptureRejected("manifest.json is not a JSON object")
    version = doc.get("schema")
    # A non-number is left to the schema, which names the field.
    if isinstance(version, int | float) and not isinstance(version, bool) and version != SUPPORTED:
        raise CaptureRejected(
            f"manifest.json has unsupported schema {version} (supported: {SUPPORTED})"
        )


def validate(doc: object) -> None:
    """Raise CaptureRejected unless `doc` is a manifest of a supported schema version. The C++
    validator judges fabric/tests/topology/data/manifest-corpus/ the same way."""
    _check_version(doc)
    found = sorted(_validator().iter_errors(doc), key=lambda e: e.json_path)
    if found:
        first = found[0]
        raise CaptureRejected(
            f"manifest.json does not match its schema at {first.json_path} ({first.validator})"
        )


def check_manifest(raw: bytes) -> dict:
    """The manifest in `raw`, if a consumer may use the capture it describes (RFC-0003 §4).
    Its status is complete or partial; the caller decides whether partial is enough."""
    doc = _parse(raw)
    _check_version(doc)
    files = doc.get("files")
    if isinstance(files, dict) and any(name not in FILES for name in files):
        raise CaptureRejected(f"manifest.json lists a file outside {', '.join(FILES)}")
    validate(doc)
    if doc["status"] == "failed":
        raise CaptureRejected("the capture failed (status failed); its diagnostics.txt says why")
    if not doc["sanitized"]:
        raise CaptureRejected("the capture is not sanitized (sanitized false)")
    if doc["leak_check"] != "passed":
        raise CaptureRejected(f"the leak check did not pass (leak_check {doc['leak_check']})")
    if doc["topology_id"] is None:
        raise CaptureRejected("manifest.json has no topology_id")
    for name in ALWAYS:
        if name not in doc["files"]:
            raise CaptureRejected(f"manifest.json does not list {name}")
    return doc


def _read_regular(path: Path, limit: int | None = None) -> bytes:
    """The bytes of `path`, refusing a symlink or anything but a regular file at open time, so
    the check and the read see the same file."""
    try:
        fd = os.open(
            path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        )
    except FileNotFoundError as e:
        raise CaptureRejected(f"{path.name} is missing") from e
    except OSError as e:
        raise CaptureRejected(f"{path.name} is a symlink or cannot be opened") from e
    with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            raise CaptureRejected(f"{path.name} is not a regular file")
        data = f.read() if limit is None else f.read(limit + 1)
    if limit is not None and len(data) > limit:
        raise CaptureRejected(f"{path.name} is larger than {limit} bytes")
    return data


def verify_dir(d: Path, manifest: dict) -> None:
    """Raise CaptureRejected unless `d` holds exactly the files `manifest` lists, each a regular
    file with its listed SHA-256, plus at most manifest.json and diagnostics.txt."""
    if d.is_symlink() or not d.is_dir():
        raise CaptureRejected("the capture is not a directory")
    listed = manifest["files"]
    allowed = set(listed) | set(UNLISTED)
    with os.scandir(d) as entries:
        found = list(entries)
    unexpected = sum(e.name not in allowed for e in found)
    if unexpected:
        # Names are not printed: an unexpected file's name can be anything, a hostname included.
        raise CaptureRejected(f"the capture holds {unexpected} unexpected entries")
    for entry in found:
        if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
            raise CaptureRejected(f"{entry.name} is not a regular file")
    for name, digest in sorted(listed.items()):
        actual = "sha256:" + hashlib.sha256(_read_regular(d / name)).hexdigest()
        if actual != digest:
            raise CaptureRejected(f"{name} does not match its sha256 in manifest.json")


def accept(d: Path) -> dict:
    """The manifest of the capture in `d`, after every consumer check of RFC-0003 §4."""
    if d.is_symlink() or not d.is_dir():
        raise CaptureRejected("the capture is not a directory")
    manifest = check_manifest(_read_regular(d / "manifest.json", MAX_MANIFEST_BYTES))
    verify_dir(d, manifest)
    return manifest
