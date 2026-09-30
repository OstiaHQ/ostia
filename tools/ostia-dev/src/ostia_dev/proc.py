"""External commands (kubectl, podman, docker, git, pixi), echoed under -v (RFC-0005 §1.3)."""

import shlex
import subprocess
import sys
from subprocess import CalledProcessError, CompletedProcess

__all__ = ["CalledProcessError", "CompletedProcess", "run", "set_verbose", "stream", "verbose"]

_verbose = False


def set_verbose(on: bool) -> None:
    global _verbose
    _verbose = on


def verbose() -> bool:
    return _verbose


def _echo(cmd: list[str], on: bool | None) -> None:
    if _verbose if on is None else on:
        print(f"+ {shlex.join(str(c) for c in cmd)}", file=sys.stderr, flush=True)


def run(
    cmd: list[str],
    *,
    input: str | bytes | None = None,
    capture: bool = True,
    check: bool = False,
    verbose: bool | None = None,
    **kwargs,
) -> CompletedProcess:
    """Run a command to completion; text mode unless `input` is bytes."""
    _echo(cmd, verbose)
    text = not isinstance(input, bytes)
    return subprocess.run(
        cmd,
        input=input,
        capture_output=capture,
        check=check,
        text=text,
        errors="replace" if text else None,  # tool output isn't always UTF-8
        **kwargs,
    )


def stream(cmd: list[str], *, verbose: bool | None = None, **kwargs) -> subprocess.Popen:
    """Start a command whose stdout (with stderr merged) the caller reads line by line."""
    _echo(cmd, verbose)
    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",  # compiler and test output isn't always UTF-8
        bufsize=1,
        **kwargs,
    )
