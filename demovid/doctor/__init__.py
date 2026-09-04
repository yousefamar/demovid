import argparse
import grp
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from ..rec import audio
from ..rec.state import clear_state, pid_alive, read_state

OK, WARN, FAIL = "ok", "WARN", "FAIL"


def add_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--restore", action="store_true",
                   help="undo a crashed recording: kill its capture processes, restore the cursor theme")
    p.add_argument("--out-root", type=Path, default=Path("~/Videos/demovid").expanduser())


def _run(cmd: list[str], timeout: float = 10) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout + r.stderr
    except (subprocess.SubprocessError, OSError) as e:
        return f"<{e}>"


def restore(st: dict | None = None) -> list[str]:
    from ..rec import sway

    st = st or read_state() or {}
    done: list[str] = []
    for name, pid in (st.get("children") or {}).items():
        if pid and pid_alive(pid):
            os.kill(pid, signal.SIGINT)
            for _ in range(50):
                if not pid_alive(pid):
                    break
                time.sleep(0.1)
            else:
                os.kill(pid, signal.SIGKILL)
            done.append(f"stopped {name} (pid {pid})")
    pid = st.get("pid")
    if pid and pid_alive(pid):
        os.kill(pid, signal.SIGKILL)
        done.append(f"killed recorder pid {pid}")
    try:
        conn = sway.connect()
        theme = (st.get("restore") or {}).get("xcursor_theme")
        if theme:
            sway.set_xcursor_theme(conn, theme[0], int(theme[1]))
            done.append(f"cursor theme restored to {theme[0]} {theme[1]}")
        preview_pid = (st.get("children") or {}).get("preview")
        if preview_pid and any(r.success for r in conn.command(f"[pid={preview_pid}] kill")):
            done.append("closed preview window")
    except Exception as e:
        done.append(f"sway unreachable ({e}); cursor theme not touched")
    clear_state()
    done.append("state cleared")
    return done


def checks(out_root: Path) -> list[tuple[str, str, str]]:
    from ..rec import cursor, sway
    from ..rec.inputs import input_devices

    res: list[tuple[str, str, str]] = []

    try:
        conn = sway.connect()
        ver = conn.get_version().human_readable
        outs = [f"{o.name} {o.rect.width}x{o.rect.height}" for o in conn.get_outputs() if o.active]
        res.append((OK, "sway", f"{ver}, outputs: {', '.join(outs)}"))
    except Exception as e:
        res.append((FAIL, "sway", f"IPC unreachable: {e}"))

    try:
        res.append((OK if cursor.available() else WARN, "cursor",
                    "ext-image-copy-capture-v1 advertised" if cursor.available()
                    else "no ext-image-copy-capture-v1 (needs Sway >= 1.11): cursor positions won't be logged"))
    except Exception as e:
        res.append((WARN, "cursor", f"wayland probe failed: {e}"))

    for tool in ("wf-recorder", "ffmpeg", "ffprobe", "pactl", "notify-send"):
        res.append((OK if shutil.which(tool) else FAIL, tool, shutil.which(tool) or "not on PATH"))

    enc = _run(["ffmpeg", "-hide_banner", "-encoders"])
    res.append((OK if "h264_nvenc" in enc else FAIL, "nvenc", "h264_nvenc encoder listed" if "h264_nvenc" in enc
                else "ffmpeg has no h264_nvenc"))
    dev = _run(["ffmpeg", "-hide_banner", "-devices"])
    for name in ("sdl", "pulse", "video4linux2"):
        res.append((OK if name in dev else (WARN if name == "sdl" else FAIL), f"ffmpeg {name}",
                    "available" if name in dev else "missing"))
    test = _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=60",
                 "-frames:v", "30", "-c:v", "h264_nvenc", "-preset", "p4", "-f", "null", "-"], timeout=30)
    res.append((OK if not test.strip() else FAIL, "nvenc encode", "30-frame test encode ok" if not test.strip()
                else test.strip().splitlines()[-1]))

    groups = {grp.getgrgid(g).gr_name for g in os.getgroups()}
    res.append((OK if "input" in groups else FAIL, "input group", "member" if "input" in groups
                else "not in `input`: sudo usermod -aG input $USER, re-login"))
    devs = input_devices()
    names = [d.name for d in devs]
    for d in devs:
        d.close()
    res.append((OK if names else FAIL, "evdev", ", ".join(names) or "no readable key/button devices"))

    cam = Path("/dev/video0")
    if cam.exists() and os.access(cam, os.R_OK):
        fmts = _run(["ffmpeg", "-hide_banner", "-f", "v4l2", "-list_formats", "all", "-i", str(cam)])
        has = "mjpeg" in fmts.lower() and "1280x720" in fmts
        res.append((OK if has else WARN, "camera", "mjpeg 1280x720 available" if has else fmts.strip()[-200:]))
    else:
        res.append((WARN, "camera", f"{cam} missing or unreadable (recording continues without cam)"))

    src = audio.default_source()
    if src:
        vol = audio.source_volume_pct(src)
        warn = audio.mic_warning(src)
        res.append((WARN if warn else OK, "mic", warn or f"{src} at {vol}%"))
    else:
        res.append((WARN, "mic", "no default source"))

    try:
        st = shutil.disk_usage(out_root if out_root.exists() else out_root.parent)
        free_gb = st.free / 1e9
        res.append((OK if free_gb > 5 else WARN, "disk", f"{free_gb:.1f} GB free under {out_root}"))
    except OSError as e:
        res.append((WARN, "disk", str(e)))

    state = read_state()
    if state and pid_alive(state["pid"]):
        res.append((OK, "state", f"recording in progress: {state['dir']}"))
    elif state:
        res.append((WARN, "state", "stale recorder state: run `demovid doctor --restore`"))
    else:
        res.append((OK, "state", "idle"))
    return res


def main(ns: argparse.Namespace) -> int:
    if ns.restore:
        for line in restore():
            print(line)
        return 0
    results = checks(ns.out_root)
    width = max(len(name) for _, name, _ in results)
    for level, name, detail in results:
        print(f"{level:>4}  {name:<{width}}  {detail}")
    return 1 if any(level == FAIL for level, _, _ in results) else 0
