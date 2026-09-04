import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .state import clear_state, log_path, pid_alive, read_state, state_path

STOP_TIMEOUT_S = 25.0
START_TIMEOUT_S = 15.0


def _size(text: str) -> tuple[int, int]:
    w, h = text.lower().split("x")
    return int(w), int(h)


def add_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--stop", action="store_true", help="stop only; exit 1 if nothing is recording")
    p.add_argument("--status", action="store_true", help="print recording state as JSON")
    p.add_argument("--waybar", action="store_true", help="print a waybar custom-module JSON line")
    p.add_argument("--fg", action="store_true", help="record in the foreground (Ctrl-C stops)")
    p.add_argument("--daemon", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--out-root", type=Path, default=Path("~/Videos/demovid").expanduser())
    p.add_argument("--output", help="Sway output name (default: the focused one)")
    p.add_argument("--fps", type=int, default=60)
    p.add_argument("--cq", type=int, default=20, help="NVENC constant quality for the screen")
    p.add_argument("--no-cam", action="store_true")
    p.add_argument("--cam-device", default="/dev/video0")
    p.add_argument("--cam-size", type=_size, default=(1280, 720))
    p.add_argument("--cam-fps", type=int, default=30)
    p.add_argument("--no-mic", action="store_true")
    p.add_argument("--mic-source", default="default", help="PulseAudio/PipeWire source name")
    p.add_argument("--no-preview", action="store_true", help="don't show the webcam preview window")
    p.add_argument("--preview-size", type=_size, default=(384, 216))
    p.add_argument("--hide-cursor", action="store_true",
                   help="swap in a blank cursor theme while recording (you won't see your cursor either)")
    p.add_argument("--no-keys", action="store_true", help="don't log keystrokes (passwords!) to events.jsonl")
    p.add_argument("--no-notify", action="store_true")


def _options(ns: argparse.Namespace):
    from .session import RecOptions

    return RecOptions(
        out_root=ns.out_root, output=ns.output, fps=ns.fps, cq=ns.cq,
        cam_device=None if ns.no_cam else ns.cam_device, cam_size=ns.cam_size, cam_fps=ns.cam_fps,
        mic_source=None if ns.no_mic else ns.mic_source,
        preview=not ns.no_preview, preview_size=ns.preview_size,
        hide_cursor=ns.hide_cursor, log_keys=not ns.no_keys, notify=not ns.no_notify,
    )


def status() -> dict:
    st = read_state()
    if st and pid_alive(st["pid"]):
        return {"recording": True, "dir": st["dir"], "pid": st["pid"],
                "elapsed_s": round(time.time() - st["started_at"], 1)}
    return {"recording": False, "stale": bool(st)}


def stop_recording(st: dict) -> int:
    pid = st["pid"]
    os.kill(pid, signal.SIGINT)
    deadline = time.monotonic() + STOP_TIMEOUT_S
    while time.monotonic() < deadline:
        if not pid_alive(pid) or not state_path().exists():
            print(st["dir"])
            return 0
        time.sleep(0.1)
    print(f"recorder pid {pid} did not exit in {STOP_TIMEOUT_S}s; run: demovid doctor --restore", file=sys.stderr)
    return 1


def run_foreground(ns: argparse.Namespace) -> int:
    from .session import Session

    session = Session(_options(ns))
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: session.stop_event.set())
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        out = session.run()
    except Exception as e:
        clear_state()
        print(f"demovid rec: {e}", file=sys.stderr)
        return 1
    print(out)
    return 1 if session.error else 0


def spawn_daemon(argv: list[str]) -> int:
    log = open(log_path(), "w")
    child = subprocess.Popen(
        [sys.executable, "-m", "demovid.cli", "rec", "--daemon", *argv],
        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
    )
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
        if s["recording"]:
            m, sec = divmod(int(s["elapsed_s"]), 60)
            out = {"text": f"● {m}:{sec:02d}", "class": "recording", "tooltip": s["dir"]}
        else:
            out = {"text": "", "class": "idle", "tooltip": "demovid: idle"}
        print(json.dumps(out))
        return 0

    st = read_state()
    if st and pid_alive(st["pid"]):
        return stop_recording(st)
    if st:
        from ..doctor import restore

        print("stale recorder state found; restoring", file=sys.stderr)
        restore(st)
    if ns.stop:
        print("not recording", file=sys.stderr)
        return 1
    if ns.fg or ns.daemon:
        return run_foreground(ns)
    passthrough = [a for a in sys.argv[2:] if a not in ("--fg", "--daemon")]
    return spawn_daemon(passthrough)
