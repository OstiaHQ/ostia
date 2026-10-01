"""A stand-in for ostia_dev.dev.steps.RUNNER that records each command instead of running it."""

from pathlib import Path


class RecordingRunner:
    """Returns `code` for the call at index `fail_at` and 0 for the others; with
    `interrupt_at`, raises KeyboardInterrupt there, as a Ctrl-C in the child would."""

    def __init__(
        self, fail_at: int | None = None, code: int = 8, interrupt_at: int | None = None
    ) -> None:
        self.fail_at = fail_at
        self.code = code
        self.interrupt_at = interrupt_at
        self.calls: list[list[str]] = []
        self.cwds: list[Path] = []

    def __call__(self, argv: list[str], cwd: Path) -> int:
        index = len(self.calls)
        self.calls.append([str(a) for a in argv])
        self.cwds.append(Path(cwd))
        if index == self.interrupt_at:
            raise KeyboardInterrupt
        return self.code if index == self.fail_at else 0
