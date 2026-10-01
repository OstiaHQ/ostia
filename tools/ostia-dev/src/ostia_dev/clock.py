"""Time, behind one seam so the k8s tests can run a 20-minute wait in milliseconds."""

import datetime
import time


class Clock:
    def monotonic(self) -> float:
        return time.monotonic()

    def now(self) -> datetime.datetime:
        return datetime.datetime.now(datetime.UTC)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)
