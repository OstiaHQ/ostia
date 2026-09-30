"""Exit codes (RFC-0005 §1.4, §3.5) and OstiaError, which carries one to the entry point."""

OK = 0
FAILED = 1
USAGE = 2
INFRA = 3
TEARDOWN = 4
INTERRUPTED = 130


class OstiaError(Exception):
    code = USAGE

    def __init__(self, message: str, code: int | None = None, *, step: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.step = step
        if code is not None:
            self.code = code


class CheckFailed(OstiaError):
    code = FAILED


class UsageError(OstiaError):
    code = USAGE


class InfraError(OstiaError):
    code = INFRA


class TeardownUnverified(OstiaError):
    code = TEARDOWN
