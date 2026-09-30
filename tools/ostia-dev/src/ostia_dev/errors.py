"""Exit codes (RFC-0005 §1.4, §3.5) and the exception that carries them to the entry point.

Code raises an OstiaError whose message is already in contract form (ostia_dev.contract);
ostia_dev.cli.OstiaApp prints it to stderr and exits with its code.
"""

OK = 0
FAILED = 1  # the checked thing failed: tests, lint, a check
USAGE = 2  # usage or configuration error; nothing was created
INFRA = 3  # infrastructure failure: the test result is unknown
TEARDOWN = 4  # tests passed, teardown not verified
INTERRUPTED = 130  # Ctrl-C


class OstiaError(Exception):
    code = USAGE

    def __init__(self, message: str, code: int | None = None, *, step: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.step = step  # the pipeline step it happened in, for summary.json
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
