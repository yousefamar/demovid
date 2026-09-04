import time
from dataclasses import dataclass


@dataclass(frozen=True)
class Clock:
    """t = 0 of a recording: one monotonic reading and the wall clock read at the same instant."""

    monotonic_ns: int
    epoch_s: float

    @classmethod
    def start(cls) -> "Clock":
        return cls(monotonic_ns=time.monotonic_ns(), epoch_s=time.time())

    def now(self) -> float:
        return (time.monotonic_ns() - self.monotonic_ns) / 1e9

    def from_monotonic(self, seconds: float) -> float:
        return seconds - self.monotonic_ns / 1e9

    def from_epoch(self, seconds: float) -> float:
        return seconds - self.epoch_s
