"""Prompts on a TTY, flags without one (RFC-0005 §1.5)."""

import io
import sys

import pytest
from ostia_dev import prompts
from ostia_dev.errors import UsageError


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture
def tty(monkeypatch):
    def answer(text: str) -> None:
        monkeypatch.setattr(sys, "stdin", _Tty(text))

    return answer


@pytest.fixture
def no_tty(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))  # isatty() is False


def test_ask_on_a_tty_returns_the_answer(tty):
    tty("ostia-ci\n")
    assert prompts.ask("namespace for context c1?", "ostia-test", flag="--namespace") == "ostia-ci"


def test_ask_on_a_tty_takes_the_default(tty):
    tty("\n")
    assert prompts.ask("namespace?", "ostia-test", flag="--namespace") == "ostia-test"


def test_ask_without_a_tty_is_exit_2_naming_the_flag(no_tty):
    with pytest.raises(UsageError) as e:
        prompts.ask("namespace for context c1?", "ostia-test", flag="--namespace")
    assert e.value.code == 2
    assert "fix: pass --namespace" in e.value.message
    assert "namespace for context c1?" in e.value.message


def test_confirm_with_yes_skips_the_question(no_tty):
    assert prompts.confirm("create namespace ostia-test?", yes=True) is True


def test_confirm_on_a_tty(tty):
    tty("y\n")
    assert prompts.confirm("create namespace ostia-test?", yes=False) is True
    tty("\n")
    assert prompts.confirm("create namespace ostia-test?", yes=False) is False
    tty("\n")
    assert prompts.confirm("save?", yes=False, default=True) is True


def test_confirm_without_a_tty_is_exit_2_naming_yes(no_tty):
    with pytest.raises(UsageError) as e:
        prompts.confirm("create namespace ostia-test?", yes=False)
    assert "fix: pass --yes" in e.value.message
