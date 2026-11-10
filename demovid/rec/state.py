import fcntl
import json
import os
import time
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
    # a tmp file per writer: two recorders once shared "rec.tmp", and the loser's os.replace raised
    tmp = state_path().with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state, indent=1))
    os.replace(tmp, state_path())


def clear_state(owner: int | None = None) -> None:
    """Remove the state file; with `owner`, only if that pid wrote it (a dying recorder must never wipe a
    live one's state: that is how a recorder ran unseen for 5 days in Nov 2026)."""
    if owner is not None:
        st = read_state()
        if st and st.get("pid") != owner:
            return
    try:
        state_path().unlink()
    except FileNotFoundError:
        pass


# One recorder at a time. rec.json is written only once the streams are up, seconds after the process
# starts (22 s on a cold cache), so it cannot tell "idle" from "starting". The lock can: a recorder takes it
# before anything else (the spawner takes it and hands the fd down) and holds it until the kernel drops it
# at exit, crash or kill. The file's content is the holder's pid, for status and stop.
LOCK_ENV = "DEMOVID_LOCK_FD"


def lock_path() -> Path:
    return runtime_dir() / "rec.lock"


def try_lock(attempts: int = 5) -> int | None:
    """Take the recorder lock: an fd to keep open for the recorder's life, or None if one holds it.
    Retries briefly, because lock_holder()'s probe holds a shared lock for a few microseconds."""
    fd = os.open(lock_path(), os.O_RDWR | os.O_CREAT, 0o644)
    for i in range(attempts):
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if i < attempts - 1:
                time.sleep(0.05)
    os.close(fd)
    return None


def set_lock_pid(fd: int, pid: int) -> None:
    os.ftruncate(fd, 0)
    os.pwrite(fd, str(pid).encode(), 0)


def lock_holder() -> int | None:
    """pid of the recorder holding the lock (-1 if it is held but the pid is not written yet), None if free."""
    try:
        fd = os.open(lock_path(), os.O_RDONLY)
    except FileNotFoundError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            try:
                return int(os.pread(fd, 32, 0).decode().strip())
            except ValueError:
                return -1
        fcntl.flock(fd, fcntl.LOCK_UN)
        return None
    finally:
        os.close(fd)


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


WAYBAR_SIGNAL = 9  # waybar's custom/demovid listens on SIGRTMIN+9


def poke_waybar() -> None:
    """Nudge waybar to re-read the state file now instead of on its next tick."""
    import subprocess
    try:
        subprocess.run(["pkill", f"-RTMIN+{WAYBAR_SIGNAL}", "waybar"], capture_output=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        pass
