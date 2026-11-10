import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .state import (LOCK_ENV, clear_state, lock_holder, log_path, pid_alive, read_state, set_lock_pid,
                    try_lock)

STOP_TIMEOUT_S = 25.0
START_TIMEOUT_S = 60.0    # a cold-cache start has taken 22 s; past this the child is stopped, never abandoned
MAX_HOURS = 6.0


def _size(text: str) -> tuple[int, int]:
    w, h = text.lower().split("x")
    return int(w), int(h)


def add_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--stop", action="store_true", help="stop only; exit 1 if nothing is recording")
    p.add_argument("--pause", action="store_true", help="pause/resume the live recording; exit 1 if idle")
    p.add_argument("--status", action="store_true", help="print recording state as JSON")
    p.add_argument("--waybar", action="store_true", help="print a waybar custom-module JSON line")
    p.add_argument("--fg", action="store_true", help="record in the foreground (Ctrl-C stops)")
    p.add_argument("--audio-only", action="store_true",
                   help="a meeting: record the mic and PC audio only, no screen/camera/cursor/keys")
    p.add_argument("--daemon", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--out-root", type=Path, default=Path("~/Videos/demovid").expanduser())
    p.add_argument("--output", help="Sway output name (default: the focused one)")
    p.add_argument("--fps", type=int, default=60)
    p.add_argument("--cq", type=int, default=20, help="NVENC constant quality for the screen")
    # --cam/--mic/--preview/--keys exist as explicit positives so a pref (see demovid/prefs.py) can be
    # overridden either way on the command line; the --no-* forms stay for compatibility.
    p.add_argument("--cam", dest="cam", action="store_true", default=None)
    p.add_argument("--no-cam", dest="cam", action="store_false")
    p.add_argument("--cam-device", default="/dev/video0")
    p.add_argument("--cam-size", type=_size, default=(1280, 720))
    p.add_argument("--cam-fps", type=int, default=30)
    p.add_argument("--mic", dest="mic", action="store_true", default=None)
    p.add_argument("--no-mic", dest="mic", action="store_false")
    p.add_argument("--mic-source", default="default", help="PulseAudio/PipeWire source name")
    p.add_argument("--system-audio", dest="system_audio", action="store_true", default=None,
                   help="also record what the PC plays (the far side of a call) to system.flac")
    p.add_argument("--no-system-audio", dest="system_audio", action="store_false")
    p.add_argument("--system-source", default="default",
                   help="monitor source to read PC audio from (default: the default sink's .monitor)")
    p.add_argument("--preview", dest="preview", action="store_true", default=None,
                   help="show the webcam preview window")
    p.add_argument("--no-preview", dest="preview", action="store_false", help="don't show the webcam preview window")
    p.add_argument("--preview-size", type=_size, default=None,
                   help="WxH of the preview window (default: the square demovid.layout prescribes)")
    p.add_argument("--hide-cursor", dest="hide_cursor", action="store_true", default=None,
                   help="swap in a blank cursor theme while recording (you won't see your cursor either)")
    p.add_argument("--show-cursor", dest="hide_cursor", action="store_false", help="keep the real cursor visible")
    p.add_argument("--keys", dest="keys", action="store_true", default=None, help="log keystrokes to events.jsonl")
    p.add_argument("--no-keys", dest="keys", action="store_false",
                   help="don't log keystrokes (passwords!) to events.jsonl")
    p.add_argument("--no-notify", action="store_true")
    p.add_argument("--max-hours", type=float, default=MAX_HOURS,
                   help=f"stop on its own after this long (default {MAX_HOURS:g}; 0 = never). It also stops "
                        "when the disk is nearly full")


def _options(ns: argparse.Namespace):
    from demovid import prefs

    from .session import RecOptions

    # unset flags fall back to the sticky prefs the waybar menu writes
    saved = prefs.load()
    cam = saved["cam"] if ns.cam is None else ns.cam
    mic = saved["mic"] if ns.mic is None else ns.mic
    # audio-only is the meeting mode: PC audio is on unless --no-system-audio says otherwise
    system_audio = (saved["system_audio"] if ns.system_audio is None else ns.system_audio) or (
        ns.audio_only and ns.system_audio is None)
    preview = saved["preview"] if ns.preview is None else ns.preview
    keys = saved["keys"] if ns.keys is None else ns.keys
    hide_cursor = saved["hide_cursor"] if ns.hide_cursor is None else ns.hide_cursor
    return RecOptions(
        out_root=ns.out_root, output=ns.output, fps=ns.fps, cq=ns.cq,
        cam_device=ns.cam_device if cam and not ns.audio_only else None, cam_size=ns.cam_size, cam_fps=ns.cam_fps,
        mic_source=ns.mic_source if mic else None,
        system_source=ns.system_source if system_audio else None,
        preview=preview and cam and not ns.audio_only, preview_size=ns.preview_size,
        hide_cursor=hide_cursor and not ns.audio_only, log_keys=keys and not ns.audio_only,
        notify=not ns.no_notify, audio_only=ns.audio_only, max_s=max(0.0, ns.max_hours) * 3600,
    )


def status() -> dict:
    st = read_state()
    if st and pid_alive(st["pid"]):
        now = time.time()
        paused_since = st.get("paused_since")
        paused = float(st.get("paused_total_s") or 0.0) + ((now - paused_since) if paused_since else 0.0)
        return {"recording": True, "dir": st["dir"], "pid": st["pid"],
                "paused": paused_since is not None, "audio_only": bool(st.get("audio_only")),
                "elapsed_s": round(now - st["started_at"] - paused, 1)}
    holder = lock_holder()
    if holder:   # started, streams not up yet: report it, so a second click stops it instead of starting another
        return {"recording": True, "starting": True, "pid": holder, "dir": None, "paused": False,
                "audio_only": False, "elapsed_s": 0.0}
    return {"recording": False, "paused": False, "stale": bool(st)}


def toggle_pause(st: dict) -> int:
    os.kill(st["pid"], signal.SIGUSR1)
    return 0


def stop_recording(st: dict) -> int:
    pid = st["pid"]
    os.kill(pid, signal.SIGINT)
    deadline = time.monotonic() + STOP_TIMEOUT_S
    while time.monotonic() < deadline:
        cur = read_state()
        if not pid_alive(pid) or (st.get("dir") and not (cur and cur.get("pid") == pid)):
            print(st.get("dir") or f"stopped recorder pid {pid} (it was still starting)")
            return 0
        time.sleep(0.1)
    print(f"recorder pid {pid} did not exit in {STOP_TIMEOUT_S}s; run: demovid doctor --restore", file=sys.stderr)
    return 1


def run_foreground(ns: argparse.Namespace) -> int:
    # Catch stop signals before the slow imports (seconds on a cold cache): a stop sent while starting must
    # not be lost, and a child started from a shell's `&` inherits SIGINT as ignored.
    import threading

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    signal.signal(signal.SIGHUP, signal.SIG_IGN)

    inherited = os.environ.pop(LOCK_ENV, None)   # the spawner took the lock and handed us its fd
    if inherited is None:
        lock_fd = try_lock()
        if lock_fd is None:
            print(f"another recorder is running (pid {lock_holder()})", file=sys.stderr)
            return 1
        set_lock_pid(lock_fd, os.getpid())
    # the fd stays open (unreferenced) until this process exits; the kernel releases the lock then

    from .session import Session

    if stop.is_set():
        print("stopped before recording started", file=sys.stderr)
        return 0
    session = Session(_options(ns), stop_event=stop)
    signal.signal(signal.SIGUSR1, lambda *_: session.toggle_pause())
    try:
        out = session.run()
    except Exception as e:
        clear_state(owner=os.getpid())
        print(f"demovid rec: {e}", file=sys.stderr)
        return 1
    print(out)
    return 1 if session.error else 0


def spawn_daemon(argv: list[str]) -> int:
    lock_fd = try_lock()
    if lock_fd is None:
        print("another recorder is starting", file=sys.stderr)
        return 1
    log = open(log_path(), "w")   # only under the lock: truncating it under a live recorder lost its log
    child = subprocess.Popen(
        [sys.executable, "-m", "demovid.cli", "rec", "--daemon", *argv],
        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        pass_fds=(lock_fd,), env={**os.environ, LOCK_ENV: str(lock_fd)},
    )
    set_lock_pid(lock_fd, child.pid)
    os.close(lock_fd)   # the child's copy keeps the lock
    deadline = time.monotonic() + START_TIMEOUT_S
    while time.monotonic() < deadline:
        st = read_state()
        if st and st.get("pid") == child.pid:
            print(st["dir"])
            return 0
        if child.poll() is not None:
            break
        time.sleep(0.1)
    log.close()
    if child.poll() is None:
        # never leave a half-started recorder behind: one did, and recorded unseen for 5 days
        child.send_signal(signal.SIGINT)
        try:
            child.wait(STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            pass
        print(f"recorder did not start in {START_TIMEOUT_S:.0f}s; stopped it (exit {child.poll()}); "
              f"log: {log_path()}", file=sys.stderr)
        return 1
    if child.returncode == 0:   # a toggle stopped it before the streams came up
        print("recorder stopped before recording started")
        return 0
    print(f"recorder failed to start (exit {child.poll()}); log: {log_path()}", file=sys.stderr)
    try:
        print(log_path().read_text()[-3000:], file=sys.stderr)
    except OSError:
        pass
    return 1


def main(ns: argparse.Namespace) -> int:
    if ns.status:
        print(json.dumps(status()))
        return 0
    if ns.waybar:
        s = status()
        if s.get("starting"):
            out = {"text": "\u25cb", "class": "starting", "tooltip": "demovid: starting"}
        elif s["recording"]:
            m, sec = divmod(int(s["elapsed_s"]), 60)
            icon, cls = ("\u23f8", "paused") if s["paused"] else ("\uf130" if s["audio_only"] else "\u25cf", "recording")
            out = {"text": f"{icon} {m}:{sec:02d}", "class": cls, "tooltip": s["dir"]}
        else:
            out = {"text": "", "class": "idle", "tooltip": "demovid: idle"}
        print(json.dumps(out))
        return 0

    if ns.daemon:   # the spawner already decided to start, and holds the lock for us
        return run_foreground(ns)
    st = read_state()
    if ns.pause:
        if st and pid_alive(st["pid"]):
            return toggle_pause(st)
        print("not recording" if not lock_holder() else "still starting", file=sys.stderr)
        return 1
    if st and pid_alive(st["pid"]):
        return stop_recording(st)
    holder = lock_holder()
    if holder and holder > 0 and pid_alive(holder):
        return stop_recording({"pid": holder, "dir": None})   # a toggle while it starts stops it
    if holder:
        print("a recorder is starting; try again in a moment", file=sys.stderr)
        return 1
    if st:
        from ..doctor import restore

        print("stale recorder state found; restoring", file=sys.stderr)
        restore(st)
    if ns.stop:
        print("not recording", file=sys.stderr)
        return 1
    if ns.fg:
        return run_foreground(ns)
    passthrough = [a for a in sys.argv[2:] if a not in ("--fg", "--daemon")]
    return spawn_daemon(passthrough)
