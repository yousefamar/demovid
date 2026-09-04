import json
import os
from pathlib import Path
from typing import Any


def runtime_dir() -> Path:
    base = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    d = base / "demovid"
    d.mkdir(parents=True, exist_ok=True)
    return d


def state_path() -> Path:
    return runtime_dir() / "rec.json"


def log_path() -> Path:
    return runtime_dir() / "rec.log"


def read_state() -> dict[str, Any] | None:
    try:
        return json.loads(state_path().read_text())
    except (OSError, ValueError):
        return None


def write_state(state: dict[str, Any]) -> None:
    tmp = state_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    os.replace(tmp, state_path())


def clear_state() -> None:
    try:
        state_path().unlink()
    except FileNotFoundError:
        pass


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().split(")")[-1].split()[0] != "Z"
    except OSError:
        return False
