"""The topo commands: fixtures, captures and topology ids (RFC-0003 §4, §8, §9; RFC-0005 §2.2)."""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Annotated

import typer

from ostia_dev import errors, paths
from ostia_dev.contract import violation
from ostia_dev.dev import steps
from ostia_dev.topo import importer, links, manifest

topo_app = typer.Typer(help="Topology fixtures (RFC-0003).", no_args_is_help=True)

FIXTURES = paths.ROOT / "fabric/tests/fixtures/topology"
INPUTS = ("hwloc.xml", "pair.json")


def _binary() -> Path:
    return Path(steps.build_root()) / "dev/fabric/tools/topo/ostia-topo"


def _topo(subcommand: str, target: Path, problem: str, rule: str) -> str:
    try:
        return subprocess.run(
            [str(_binary()), subcommand, str(target)],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout
    except subprocess.CalledProcessError as e:
        raise errors.InfraError(
            violation(
                problem,
                [f"exit code: {e.returncode}", *(e.stderr or "").strip().splitlines()],
                rule,
                f"pixi run ostia-dev topo show {_shown(target)}",
                "RFC-0003 §9",
            ),
            e.returncode,
        ) from e


def _model(fixture: Path) -> str:
    return _topo(
        "model",
        fixture,
        f"ostia-topo could not model {fixture.name}",
        "a fixture must be readable and replayable for its golden to be regenerated",
    )


def _id(directory: Path) -> str:
    return _topo(
        "id",
        directory,
        f"ostia-topo could not compute the topology id of {directory.name}",
        "a fixture or capture must replay into a model to have an id (RFC-0003 §5)",
    ).strip()


def pair_id(directory: Path) -> str:
    """The topo1 id of a machine or pair directory, from `ostia-topo id` (RFC-0003 §5, §7)."""
    env = steps.env_or_exit("topo id")
    code = steps.run(steps.build(env, "--target", "ostia-topo"))
    if code:
        raise errors.InfraError(
            violation(
                "ostia-topo did not build",
                [f"exit code: {code}"],
                "topology ids come from ostia-topo, built from this checkout",
                "pixi run ostia-dev build",
                "RFC-0003 §5",
            )
        )
    return _id(directory)


def _fixtures() -> list[Path]:
    found = {p.parent for name in INPUTS for p in FIXTURES.glob(f"*/*/{name}")}
    # node-0/ and node-1/ belong to their pair, which is the fixture (RFC-0003 §9).
    return sorted(d for d in found if not d.name.startswith("node-"))


def _is_fixture(d: Path) -> bool:
    return d.is_dir() and any((d / name).is_file() for name in INPUTS)


def _bad_fixture(arg: Path, why: str | None = None, matches: list[Path] | None = None):
    names = matches or _fixtures()
    return errors.UsageError(
        violation(
            why or f"{arg} is not a topology fixture",
            [f"available: {p.relative_to(FIXTURES).as_posix()}" for p in names],
            "a fixture is a directory holding hwloc.xml or pair.json (RFC-0003 §9)",
            "pass a fixture directory, <group>/<case>, or a unique <case> name",
            "RFC-0003 §9",
        )
    )


def _resolve(arg: Path) -> Path:
    """An absolute fixture directory from a path, `<group>/<case>`, or a unique case name."""
    for candidate in (arg, paths.ROOT / arg, FIXTURES / arg):
        if _is_fixture(candidate):
            return candidate.resolve()
    if len(arg.parts) == 1:
        matches = [f for f in _fixtures() if f.name == arg.name]
        if len(matches) == 1:
            return matches[0].resolve()
        if len(matches) > 1:
            raise _bad_fixture(arg, f"{arg} matches more than one fixture", matches)
    raise _bad_fixture(arg)


def _shown(path: Path) -> str:
    return path.relative_to(FIXTURES).as_posix() if path.is_relative_to(FIXTURES) else str(path)


@topo_app.command()
def show(
    fixture: Annotated[Path, typer.Argument(help="A fixture directory.")],
) -> None:
    """Print a fixture's discovered topology."""
    fixture = _resolve(fixture)
    env = steps.env_or_exit("topo show")
    plan = [*steps.build(env, "--target", "ostia-topo"), [str(_binary()), "show", str(fixture)]]
    raise typer.Exit(steps.run(plan))


@topo_app.command()
def golden(
    fixtures: Annotated[
        list[Path] | None, typer.Argument(help="Fixtures to check or update; default all.")
    ] = None,
    update: Annotated[
        bool, typer.Option("--update", help="Rewrite expected.json; review it like code.")
    ] = False,
) -> None:
    """Run the golden-model tests, or regenerate the goldens with --update."""
    chosen = [_resolve(f) for f in fixtures] if fixtures else _fixtures()
    env = steps.env_or_exit("topo golden")
    if not update:
        pattern = r"^fabric\.topo\.golden\."
        if fixtures:
            names = "|".join(re.escape(f.name) for f in chosen)
            pattern += f"({names})$"
        ctest = ["ctest", "--preset", "dev", "--no-tests=error", "-R", pattern]
        code = steps.run([*steps.build(env), ctest])
        raise typer.Exit(code)
    code = steps.run(steps.build(env, "--target", "ostia-topo"))
    if code:
        raise typer.Exit(code)
    models = {f: _model(f) for f in chosen}
    changed, same = [], []
    for f, new in models.items():
        target = f / "expected.json"
        if target.is_file() and target.read_text(encoding="utf-8") == new:
            same.append(target)
        else:
            target.write_text(new, encoding="utf-8")
            changed.append(target)
    for t in changed:
        typer.echo(f"updated {_shown(t)}")
    for t in same:
        typer.echo(f"unchanged {_shown(t)}")
    typer.echo(f"{len(changed)} changed, {len(same)} unchanged")


CAPTURE_TOOL = "ostia-topo-capture"
CAPTURE_IN_BUILD = "fabric/tools/topo-capture/ostia-topo-capture"
# A manifest older than the run belongs to an earlier capture. The slack covers file systems
# that store coarse modification times.
FRESH_SLACK_NS = 2_000_000_000


def _on_linux() -> bool:
    return sys.platform.startswith("linux")


def _capture_usage(problem: str, rule: str, fix: str) -> errors.UsageError:
    return errors.UsageError(violation(problem, [], rule, fix, "RFC-0003 §1"))


def _capture_tool(build_dir: Path | None) -> tuple[list[list[str]], str]:
    """The build plan and the tool path: $OSTIA_TOPO_CAPTURE, then --build-dir, then this
    checkout's dev build inside pixi, then PATH."""
    override = os.environ.get("OSTIA_TOPO_CAPTURE")
    if override:
        return [], _executable(Path(override), "$OSTIA_TOPO_CAPTURE")
    if build_dir is not None:
        tool = build_dir.resolve() / CAPTURE_IN_BUILD
        if not tool.is_file():
            raise _capture_usage(
                f"--build-dir holds no {CAPTURE_TOOL}",
                f"--build-dir names a configured build tree that built {CAPTURE_TOOL}",
                f"cmake --build {build_dir} --target {CAPTURE_TOOL}",
            )
        return [], str(tool)
    env = os.environ.get("PIXI_ENVIRONMENT_NAME")
    if env:
        tool = Path(steps.build_root()) / "dev" / CAPTURE_IN_BUILD
        return steps.build(env, "--target", CAPTURE_TOOL), str(tool)
    found = shutil.which(CAPTURE_TOOL)
    if found:
        return [], found
    raise _capture_usage(
        f"{CAPTURE_TOOL} was not found",
        f"the tool is $OSTIA_TOPO_CAPTURE, <--build-dir>/{CAPTURE_IN_BUILD}, or on PATH",
        "pixi run ostia-dev topo capture …, which builds it",
    )


def _executable(tool: Path, source: str) -> str:
    if not tool.is_file() or not os.access(tool, os.X_OK):
        raise _capture_usage(
            f"{source} names no executable {CAPTURE_TOOL}",
            f"topo capture runs {CAPTURE_TOOL} from $OSTIA_TOPO_CAPTURE, --build-dir, this "
            "checkout's build, or PATH",
            f"unset $OSTIA_TOPO_CAPTURE or point it at a built {CAPTURE_TOOL}; inside pixi, "
            "pixi run ostia-dev build",
        )
    return str(tool)


def _read_default(path: str | None) -> str | None:
    """The first line of a defaults file a remote runner wrote, or None. A missing or unreadable
    file only leaves the default unset: the tool then records "unknown"."""
    if not path:
        return None
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    value = lines[0].strip() if lines else ""
    return value or None


def _identifiers(extra: Path | None) -> list[str]:
    """The lines of the caller's --extra-identifiers file, then the values a remote pod is given:
    NODE_NAME and the lines of OSTIA_LEAK_IDENTIFIERS. Only newlines separate values, because the
    leak check matches a multi-word identifier as one anchored substring (RFC-0003 §3)."""
    values = []
    if extra is not None:
        try:
            values += extra.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as e:
            raise _capture_usage(
                "the --extra-identifiers file cannot be read",
                "a missing file would silently weaken the leak check",
                "check the path, or drop --extra-identifiers",
            ) from e
    env = [os.environ.get("NODE_NAME", ""), os.environ.get("OSTIA_LEAK_IDENTIFIERS", "")]
    return values + [v.strip() for v in "\n".join(env).splitlines() if v.strip()]


def _run_tool(argv: list[str]) -> int:
    try:
        proc = subprocess.Popen(argv)
    except OSError as e:
        raise _capture_usage(
            f"{CAPTURE_TOOL} could not be started",
            f"the tool must be an executable file ({e.strerror or type(e).__name__})",
            "pixi run ostia-dev build, or point $OSTIA_TOPO_CAPTURE at a built tool",
        ) from e
    try:
        return proc.wait()
    except KeyboardInterrupt:
        # The tool got the same SIGINT and is removing its scratch directory and partial files;
        # killing it now would leave them.
        return proc.wait()


def _fresh_manifest(out: Path, since_ns: int) -> dict | None:
    path = out / "manifest.json"
    try:
        if path.stat().st_mtime_ns < since_ns - FRESH_SLACK_NS:
            return None
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _leak_findings(diagnostics: Path) -> int:
    try:
        lines = diagnostics.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return 0
    return sum(line.startswith("leak: ") for line in lines)


# A manifest's missing[].reason is a code written by the tool; anything else is not echoed, since
# a hand-edited manifest could carry any text.
REASON_CODE = re.compile(r"[a-z][a-z0-9_]{0,40}")


def _reason(value: object) -> str:
    return value if isinstance(value, str) and REASON_CODE.fullmatch(value) else "non-code reason"


def _data_file(value: object) -> str:
    """A manifest's file name, echoed only when it is one of the data files a capture writes."""
    return value if isinstance(value, str) and value in manifest.FILES else "non-data file"


def _interpretation(code: int, out: Path | None, since_ns: int) -> str:
    """One line on what the tool's exit code means (RFC-0003 §4), from the manifest it wrote."""
    doc = _fresh_manifest(out, since_ns) if out is not None else None
    see = f"see {out / 'diagnostics.txt'}" if doc is not None else "see the error above"
    if code == 0:
        return f"capture complete: {out}" if out is not None else "capture complete"
    if code == 2:
        missing = [
            f"{_data_file(m.get('file'))} missing ({_reason(m.get('reason'))})"
            for m in (doc or {}).get("missing", [])
            if isinstance(m, dict)
        ]
        return f"capture partial: {', '.join(missing) or 'a required source failed'}, {see}"
    if code == 3:
        if doc is not None and doc.get("leak_check") == "failed":
            findings = _leak_findings(out / "diagnostics.txt")
            return f"capture rejected by the leak check: {findings} findings, {see}"
        return f"capture rejected by the leak check or the schema check, {see}"
    if code == 1:
        codes = [
            _reason(e.get("code")) for e in (doc or {}).get("errors", []) if isinstance(e, dict)
        ]
        return f"capture failed: {', '.join(codes) or 'no manifest written'}, {see}"
    return f"capture ended with exit code {code}"


@topo_app.command(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def capture(
    ctx: typer.Context,
    out: Annotated[Path | None, typer.Option(help="Write the capture into this directory.")] = None,
    print_id: Annotated[
        bool, typer.Option("--print-id", help="Print only the topology id; keep nothing.")
    ] = False,
    provider: Annotated[
        str | None, typer.Option(help="Recorded in meta.json; default $OSTIA_CAPTURE_PROVIDER.")
    ] = None,
    instance_type: Annotated[
        str | None,
        typer.Option(
            help="Recorded in meta.json; default $OSTIA_CAPTURE_INSTANCE_TYPE, else the file "
            "$OSTIA_CAPTURE_INSTANCE_TYPE_FILE names (remote k8s writes it from the pod's node)."
        ),
    ] = None,
    extra_identifiers: Annotated[
        Path | None, typer.Option(help="More identifiers for the leak check, one per line.")
    ] = None,
    links_file: Annotated[
        Path | None, typer.Option("--links", help="A links.json to include (topo links).")
    ] = None,
    build_dir: Annotated[
        Path | None, typer.Option(help=f"A build tree holding {CAPTURE_IN_BUILD}; no build.")
    ] = None,
) -> None:
    """Capture this Linux machine's topology with ostia-topo-capture (RFC-0003 §1-§4).

    Other options (--node-index, --sysfs-root, --no-verbs) go to the tool unchanged, and its
    exit code is this command's: 0 complete, 2 partial, 1 failed, 3 leak or schema violation.
    """
    if not _on_linux():
        raise _capture_usage(
            "topo capture runs on the Linux machine being captured",
            "the capture reads hwloc, NVML, ibverbs and sysfs of the machine it runs on",
            "pixi run ostia-dev remote k8s --profile l4 --suite topo-capture, "
            "or run it on a Linux machine",
        )
    plan, tool = _capture_tool(build_dir)
    if plan:
        if code := steps.run(plan):
            if code == errors.INTERRUPTED:
                raise typer.Exit(code)
            # The tool's own exit codes are this command's, so cmake's must not pass as one.
            raise errors.InfraError(
                violation(
                    f"{CAPTURE_TOOL} did not build",
                    [f"exit code: {code}"],
                    f"topo capture builds {CAPTURE_TOOL} from this checkout before it runs it",
                    "pixi run ostia-dev build, or pass --build-dir with a built tree",
                    "RFC-0003 §1",
                )
            )
        tool = _executable(Path(tool), "the dev build")
    provider = provider or os.environ.get("OSTIA_CAPTURE_PROVIDER") or None
    instance_type = (
        instance_type
        or os.environ.get("OSTIA_CAPTURE_INSTANCE_TYPE")
        or _read_default(os.environ.get("OSTIA_CAPTURE_INSTANCE_TYPE_FILE"))
    )
    # Absolute, so the interpretation line names the capture wherever it is read.
    out = out.absolute() if out is not None else None
    argv = [tool]
    if out is not None:
        argv += ["--out", str(out)]
    if print_id:
        argv.append("--print-id")
    if provider is not None:
        argv += ["--provider", provider]
    if instance_type is not None:
        argv += ["--instance-type", instance_type]
    if links_file is not None:
        argv += ["--links", str(links_file)]
    identifiers = _identifiers(extra_identifiers)
    handle = None
    try:
        if identifiers:
            # mkstemp creates the file 0600: the identifiers are what the capture must not publish.
            fd, handle = tempfile.mkstemp(prefix="ostia-identifiers-", suffix=".txt")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("\n".join(identifiers) + "\n")
            argv += ["--extra-identifiers", handle]
        argv += list(ctx.args)
        since = time.time_ns()
        code = _run_tool(argv)
    finally:
        if handle is not None:
            Path(handle).unlink(missing_ok=True)
    typer.echo(_interpretation(code, out, since), err=True)
    raise typer.Exit(code)


@topo_app.command("links")
def links_cmd(
    records: Annotated[list[Path], typer.Argument(help="Benchmark JSONL files.")],
    capture_dir: Annotated[
        Path, typer.Option("--capture", help="The measured machine's capture or fixture.")
    ],
    out: Annotated[Path | None, typer.Option(help="Write here instead of stdout.")] = None,
) -> None:
    """Convert unidirectional p2p_copy records into links.json (RFC-0003 §8)."""
    loaded = []
    for path in records:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as e:
            raise errors.UsageError(
                violation(
                    f"cannot read {path}",
                    [str(e.strerror)],
                    "records are the JSONL that bench run writes",
                    "pass the run's results.jsonl",
                    "RFC-0003 §8",
                )
            ) from e
        for number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError as e:
                raise errors.UsageError(
                    violation(
                        f"{path}:{number} is not JSON",
                        [],
                        "records are one JSON object per line",
                        "pass the run's results.jsonl",
                        "RFC-0003 §8",
                    )
                ) from e
            loaded.append(record)
    text = json.dumps(links.links_from_records(loaded, capture_dir), indent=2, sort_keys=True)
    if out is None:
        typer.echo(text)
    else:
        out.write_text(text + "\n", encoding="utf-8")
        typer.echo(f"wrote {out}", err=True)


@topo_app.command()
def diff(
    a: Annotated[Path, typer.Argument(help="A fixture or capture directory.")],
    b: Annotated[Path, typer.Argument(help="A fixture or capture directory.")],
) -> None:
    """Compare two topologies: exit 0 when their ids are equal, 1 when they differ."""
    first, second = _resolve(a), _resolve(b)
    env = steps.env_or_exit("topo diff")
    code = steps.run(steps.build(env, "--target", "ostia-topo"))
    if code:
        raise typer.Exit(code)
    code = steps.run([[str(_binary()), "diff", str(first), str(second)]])
    if code == errors.INTERRUPTED:
        raise typer.Exit(code)
    if code >= 2:
        raise errors.InfraError(
            violation(
                "ostia-topo diff could not compare the two topologies",
                [f"exit code: {code}"],
                "both sides must replay into a model (RFC-0003 §9)",
                f"pixi run ostia-dev topo show {_shown(first)}, then {_shown(second)}",
                "RFC-0003 §9",
            )
        )
    raise typer.Exit(code)


TOPOLOGY_ID = re.compile(r"(?:topo1:sha256:)?([0-9a-f]{1,64})")


@topo_app.command()
def which(
    topology: Annotated[str, typer.Argument(help="A topo1 id, or a prefix of its hex digest.")],
) -> None:
    """List the fixtures whose topology id is, or starts with, TOPOLOGY."""
    m = TOPOLOGY_ID.fullmatch(topology.strip().lower())
    if m is None:
        raise errors.UsageError(
            violation(
                "not a topology id",
                [],
                "a topology id is topo1:sha256:<hex>, and a short id is a prefix of it",
                "pass the id a compare or show line printed",
                "RFC-0003 §5",
            )
        )
    wanted = "topo1:sha256:" + m.group(1)
    env = steps.env_or_exit("topo which")
    code = steps.run(steps.build(env, "--target", "ostia-topo"))
    if code:
        raise typer.Exit(code)
    found = [(f, i) for f in _fixtures() if (i := _id(f)).startswith(wanted)]
    for fixture, topology_id in found:
        typer.echo(f"{_shown(fixture)}  {topology_id}")
    if not found:
        typer.echo(f"no fixture has topology {wanted}", err=True)
        raise typer.Exit(errors.FAILED)


@topo_app.command("import")
def import_(
    capture_dir: Annotated[Path, typer.Argument(help="An accepted, complete capture directory.")],
    name: Annotated[
        str | None, typer.Option(help="The fixture name; default <provider>-<instance type>.")
    ] = None,
) -> None:
    """Add a capture as fixture captured/<name>, with its golden, after the leak scan."""
    env = steps.env_or_exit("topo import")
    try:
        accepted = manifest.accept(capture_dir)
    except manifest.CaptureRejected as e:
        raise errors.CheckFailed(
            violation(
                "the capture was rejected",
                [e.reason],
                "only a sanitized capture that passed the leak check and its hashes is imported",
                "capture the machine again; see its diagnostics.txt",
                "RFC-0003 §4",
            )
        ) from e
    if accepted["status"] != "complete":
        missing = [
            f"missing: {_data_file(m.get('file'))} ({_reason(m.get('reason'))})"
            for m in accepted["missing"]
            if isinstance(m, dict)
        ]
        raise errors.CheckFailed(
            violation(
                f"the capture is {accepted['status']}, not complete",
                missing,
                "a fixture records the whole machine, so a partial capture is not imported",
                "fix the failed source and capture the machine again",
                "RFC-0003 §4",
            )
        )
    chosen = importer.check_name(name) if name else importer.fixture_name(capture_dir)
    target = FIXTURES / importer.GROUP / chosen
    shown = f"{importer.GROUP}/{chosen}"
    importer.copy_capture(capture_dir, accepted, target)
    try:
        code = steps.run(steps.build(env, "--target", "ostia-topo"))
        if code:
            raise typer.Exit(code)
        (target / "expected.json").write_text(_model(target), encoding="utf-8")
        files = sorted(str(p) for p in target.iterdir())
        code = steps.run([steps.py("ci.check_fixture_leaks", "--files", *files)])
        if code:
            # diagnostics.txt is never imported and fixture-leaks rejects it, so the fix names
            # only what was copied.
            copied = [*sorted(map(_data_file, accepted["files"])), "manifest.json"]
            rerun = " ".join(str(capture_dir / name) for name in copied)
            raise errors.CheckFailed(
                violation(
                    f"fixture-leaks found identifiers in {shown}; the folder was removed",
                    [f"capture: {capture_dir}"],
                    "a committed fixture holds no machine identifier (RFC-0003 §3)",
                    f"pixi run ostia-dev check fixture-leaks --files {rerun}",
                    "RFC-0003 §3",
                )
            )
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise
    typer.echo(f"imported {shown} ({accepted['topology_id']})")
    typer.echo(importer.checklist(shown))


def register(app: typer.Typer) -> None:
    app.add_typer(topo_app, name="topo")
