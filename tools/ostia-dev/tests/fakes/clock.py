"""A clock that moves only when slept, firing the hooks FakeKube's timeline hangs on."""

import datetime

from ostia_dev.clock import Clock

START = datetime.datetime(2026, 10, 2, 14, 15, 1, tzinfo=datetime.UTC)


class FakeClock(Clock):
    def __init__(self) -> None:
        self.t = 0.0
        self.hooks = []

    def monotonic(self) -> float:
        return self.t

    def now(self) -> datetime.datetime:
        return START + datetime.timedelta(seconds=self.t)

    def sleep(self, seconds: float) -> None:
        end = self.t + seconds
        while self.t < end:
            self.t = min(end, self.t + 1)
            for hook in list(self.hooks):
                hook(self.t)

    def on_advance(self, hook) -> None:
        self.hooks.append(hook)
