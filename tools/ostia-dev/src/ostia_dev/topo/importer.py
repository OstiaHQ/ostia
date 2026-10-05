"""Turn an accepted capture into a fixture folder (RFC-0003 §9).

The fixture is `captured/<provider>-<instance type>/`, named from meta.json unless the caller
names it. Only the files the manifest lists and manifest.json are copied: diagnostics.txt is
for debugging and is never committed (RFC-0003 §4).
"""

import json
import re
import shutil
from pathlib import Path

from ostia_dev import errors
from ostia_dev.contract import violation
from ostia_dev.topo import manifest

GROUP = "captured"
NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _no_name(problem: str) -> errors.UsageError:
    return errors.UsageError(
        violation(
            problem,
            [],
            "a captured fixture is named <provider>-<instance type> (RFC-0003 §9)",
            "pass --name <provider>-<instance-type>, such as --name gcp-g2-standard-16",
            "RFC-0003 §9",
        )
    )


def fixture_name(capture: Path) -> str:
    """`<provider>-<instance type>` from the capture's meta.json, lowercased, with every run of
    other characters turned into one `-` (g6.4xlarge becomes g6-4xlarge)."""
    try:
        meta = json.loads((capture / "meta.json").read_text(encoding="utf-8"))
        provider, instance = meta["provider"], meta["instance_type"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise _no_name("meta.json does not name the provider and instance type") from e
    if not isinstance(provider, str) or not isinstance(instance, str):
        raise _no_name("meta.json does not name the provider and instance type")
    if "unknown" in (provider, instance):
        raise _no_name("meta.json records the provider or instance type as unknown")
    name = f"{_slug(provider)}-{_slug(instance)}"
    if not NAME.fullmatch(name):
        raise _no_name("meta.json's provider and instance type make no fixture name")
    return name


def check_name(name: str) -> str:
    if not NAME.fullmatch(name):
        raise _no_name("--name must be lowercase letters and digits joined by single hyphens")
    return name


def copy_capture(capture: Path, accepted: dict, target: Path) -> None:
    """Copy the listed files and manifest.json into `target`, which must not exist, then verify
    the copy against the manifest as well."""
    if target.exists() or target.is_symlink():
        raise errors.UsageError(
            violation(
                f"{target.parent.name}/{target.name} already exists",
                [],
                "an import never overwrites a committed fixture",
                "pass another --name, or remove the old folder first and review the change",
                "RFC-0003 §9",
            )
        )
    target.mkdir(parents=True)
    try:
        for name in [*sorted(accepted["files"]), "manifest.json"]:
            shutil.copyfile(capture / name, target / name, follow_symlinks=False)
        manifest.verify_dir(target, accepted)
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise


def checklist(shown: str) -> str:
    return "\n".join(
        [
            f"review before you commit {shown}:",
            "  [ ] meta.json names the provider and instance type you rented, and its driver "
            "and CUDA versions are the ones you expect",
            f"  [ ] pixi run ostia-dev topo show {shown} matches the machine: GPUs, NVLinks, "
            "NICs and PCIe links",
            "  [ ] expected.json is reviewed like code",
            "  [ ] no site name, asset tag or account name appears in the files; the leak check "
            "knows only the identifiers it collected or was given",
            "  [ ] diagnostics.txt stays out of the fixture (it was not copied)",
        ]
    )
