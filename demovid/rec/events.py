import json
import threading
from pathlib import Path
from typing import Any

from .clock import Clock


class EventLog:
    """Append-only events.jsonl writer (FORMAT.md). Thread-safe; one line per call, flushed."""

    def __init__(self, path: Path, clock: Clock):
        self.path = path
        self.clock = clock
        self._lock = threading.Lock()
        self._fh = open(path, "a", buffering=1)
        self.count = 0

    def emit(self, kind: str, t: float | None = None, **fields: Any) -> None:
        if t is None:
            t = self.clock.now()
        rec = {"t": round(t, 6), "kind": kind, **fields}
        line = json.dumps(rec, separators=(",", ":"), ensure_ascii=False)
        with self._lock:
            self._fh.write(line + "\n")
            self.count += 1

    def close(self) -> None:
        with self._lock:
            self._fh.close()
