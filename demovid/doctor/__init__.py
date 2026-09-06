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


def _cursor_tracking(timeout_s: float = 1.0) -> tuple[bool, str]:
    """Does the compositor actually report a hardware cursor? Nothing else can give exact positions."""
    import tempfile
    import time

    from ..rec import sway
    from ..rec.clock import Clock
    from ..rec.cursor import CursorSession
    from ..rec.events import EventLog

    conn = sway.connect()
    output = sway.pick_output(conn, None)
    with tempfile.TemporaryDirectory() as td:
        clock = Clock.start()
        log = EventLog(Path(td) / "events.jsonl", clock)
        session = CursorSession(log, clock, output.name, output.scale)
        session.start()
        deadline = time.monotonic() + timeout_s
        try:
            while time.monotonic() < deadline and not session.hw_cursor_seen:
                conn.command("nop demovid doctor cursor probe")
                time.sleep(0.05)
        finally:
            session.stop()
            log.close()
    if session.hw_cursor_seen:
        return True, f"hardware cursor tracked at {session.position}, hotspot {session.hotspot}"
    return False, ("no hardware cursor reported: unset WLR_NO_HARDWARE_CURSORS in ~/.local/bin/sway-nvidia and "
                   "re-login, or stop the screencopy client that is overlaying the cursor")


WLROOTS_LIB = Path("/usr/local/lib/x86_64-linux-gnu/libwlroots-0.19.so")
WLROOTS_PATCHED_BUILD = Path("~/src/wlroots-0.19.3/build2/libwlroots-0.19.so").expanduser()
WLROOTS_UNPATCHED_GLOB = ("~/src", "libwlroots-0.19.so.unpatched-*")


def _sha256(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _wlroots_patch() -> tuple[str, str]:
    """Is the installed wlroots the software-cursor-capture build? Compared by checksum against the
    known patched build and the saved unpatched copy (see CLAUDE.md)."""
    if not WLROOTS_LIB.exists():
        return FAIL, f"{WLROOTS_LIB} missing"
    installed = _sha256(WLROOTS_LIB)
    if WLROOTS_PATCHED_BUILD.exists() and _sha256(WLROOTS_PATCHED_BUILD) == installed:
        return OK, "installed lib is the patched build2 (software cursor reported to capture clients)"
    for backup in Path(WLROOTS_UNPATCHED_GLOB[0]).expanduser().glob(WLROOTS_UNPATCHED_GLOB[1]):
        if _sha256(backup) == installed:
            return FAIL, f"installed lib matches the UNPATCHED backup {backup.name}: cursor tracking is dead — reinstall build2"
    return WARN, "installed lib matches neither the patched build nor the unpatched backup (rebuilt? re-apply the patch)"


def _sway_environ(pid: int) -> dict[str, str]:
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except OSError:
        return {}
    return dict(kv.decode(errors="replace").split("=", 1) for kv in raw if b"=" in kv)


def _sync_fds(pid: int) -> int:
    n = 0
    try:
        for fd in Path(f"/proc/{pid}/fd").iterdir():
            try:
                if "sync_file" in os.readlink(fd):
                    n += 1
            except OSError:
                pass
    except OSError:
        return -1
    return n


def _screencopy_probe() -> tuple[str, str]:
    """grim on a tiny region: the one-line functional test that screencopy works at all."""
    import tempfile

    from ..rec import sway

    if not shutil.which("grim"):
        return WARN, "grim not installed; cannot probe screencopy"
    with tempfile.TemporaryDirectory() as td:
        out = subprocess.run(["grim", "-g", "0,0 16x16", f"{td}/probe.png"], capture_output=True, text=True, timeout=10)
        ok = out.returncode == 0 and Path(f"{td}/probe.png").exists()
    if ok:
        return OK, "grim copied a 16x16 region"
    err = (out.stderr or out.stdout).strip()[-160:]
    try:
        asleep = [o.name for o in sway.connect().get_outputs() if o.active and getattr(o, "power", True) is False]
    except Exception:
        asleep = []
    if asleep:
        return WARN, (f"output {', '.join(asleep)} is powered off (swayidle), so there is nothing to copy — "
                      "wake the display and re-run; `rec` wakes it itself")
    return FAIL, f"grim failed: {err}"


def checks(out_root: Path) -> list[tuple[str, str, str]]:
    from ..rec import cursor, sway
    from ..rec.inputs import input_devices
    from ..rec.streams import wf_supports_no_cursor

    res: list[tuple[str, str, str]] = []
    sway_pid = None
    try:
        sway_pid = int(subprocess.run(["pgrep", "-x", "sway"], capture_output=True, text=True).stdout.split()[0])
    except (IndexError, ValueError, OSError):
        pass

    if sway_pid:
        env = _sway_environ(sway_pid)
        fds = _sync_fds(sway_pid)
        has = env.get("WLR_RENDER_NO_EXPLICIT_SYNC") == "1"
        res.append((OK if has else WARN, "explicit sync",
                    f"WLR_RENDER_NO_EXPLICIT_SYNC=1 in sway's environment; {fds} sync_file fds now (steady ~1750 is normal, "
                    "a count that climbs between runs is the fence leak)" if has
                    else f"WLR_RENDER_NO_EXPLICIT_SYNC not set for sway (pid {sway_pid}); {fds} sync_file fds — screencopy "
                         "breaks on Vulkan + NVIDIA 580 without it (see CLAUDE.md)"))
    level, detail = _screencopy_probe()
    res.append((level, "screencopy", detail))
    level, detail = _wlroots_patch()
    res.append((level, "wlroots patch", detail))
    wf = shutil.which("wf-recorder") or ""
    res.append((OK if wf.startswith("/usr/local/") else WARN, "wf-recorder build",
                f"{wf} (patched 0.5.0 shadows the distro build)" if wf.startswith("/usr/local/")
                else f"{wf or 'missing'}: the distro build has no --no-cursor; ninja -C ~/src/wf-recorder-0.5.0/build install"))

    try:
        conn = sway.connect()
        ver = conn.get_version().human_readable
        outs = [f"{o.name} {o.rect.width}x{o.rect.height}" for o in conn.get_outputs() if o.active]
        res.append((OK, "sway", f"{ver}, outputs: {', '.join(outs)}"))
    except Exception as e:
        res.append((FAIL, "sway", f"IPC unreachable: {e}"))

    try:
        res.append((OK if cursor.available() else WARN, "cursor protocol",
                    "ext-image-copy-capture-v1 advertised" if cursor.available()
                    else "no ext-image-copy-capture-v1 (needs Sway >= 1.11): cursor positions won't be logged"))
        if cursor.available():
            tracking, why = _cursor_tracking()
            res.append((OK if tracking else FAIL, "cursor tracking", why))
    except Exception as e:
        res.append((WARN, "cursor protocol", f"wayland probe failed: {e}"))

    res.append((OK if wf_supports_no_cursor() else WARN, "wf-recorder --no-cursor",
                "supported: screencopy skips the cursor overlay so position tracking keeps working "
                "(the software cursor is still painted into the frames)" if wf_supports_no_cursor()
                else "unpatched build: the cursor overlay is forced, which disables cursor tracking "
                     "(see CLAUDE.md for the ~/src/wf-recorder-0.5.0 patch)"))

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

    # `--captions` only: sway-launched renders never read ~/.zshrc, so report which source has the key
    from demovid.render.captions import KEY_FILE, openai_key
    if os.environ.get("OPENAI_API_KEY", "").strip():
        res.append((OK, "openai key", "OPENAI_API_KEY set (captions available in this shell)"))
    elif openai_key():
        res.append((OK, "openai key", f"{KEY_FILE} (captions available everywhere)"))
    else:
        res.append((WARN, "openai key", f"no key: --captions will fail. export OPENAI_API_KEY or write {KEY_FILE}"))

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
