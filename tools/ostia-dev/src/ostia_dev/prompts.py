"""Prompts only on a TTY; without one, a question no flag answers is exit 2 (RFC-0005 §1.5)."""

import sys

import typer

from ostia_dev.contract import violation
from ostia_dev.errors import UsageError


def is_tty() -> bool:
    return sys.stdin.isatty()


def _no_tty(question: str, flag: str) -> UsageError:
    return UsageError(
        violation(
            f"ostia-dev needs an answer to {question!r}, and there is no terminal to ask",
            [],
            "without a TTY, every question needs its flag",
            f"pass {flag}",
            "RFC-0005 §1.5",
        )
    )


def ask(question: str, default: str, *, flag: str) -> str:
    if not is_tty():
        raise _no_tty(question, flag)
    return typer.prompt(question, default=default)


def confirm(question: str, *, yes: bool, default: bool = False, flag: str = "--yes") -> bool:
    if yes:
        return True
    if not is_tty():
        raise _no_tty(question, flag)
    return typer.confirm(question, default=default)
