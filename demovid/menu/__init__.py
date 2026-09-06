"""demovid menu: the waybar button's right-click menu (fuzzel --dmenu).

Start/stop, toggle what the next recording captures, and the follow-up actions (render, upload,
open, doctor). Toggles write demovid/prefs.py, so they stick until changed.
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

KITTY = Path("~/.local/kitty.app/bin/kitty").expanduser()
# Nerd Font (Font Awesome 4) glyphs as \u escapes on purpose: literal PUA characters get silently
# stripped by some tooling, which then looks like a missing font glyph. Keep this file pure ASCII.
ICONS = {"start": "\uf04b", "stop": "\uf04d", "cam": "\uf03d", "preview": "\uf030", "mic": "\uf130",
         "keys": "\uf11c", "hide_cursor": "\uf245", "render": "\uf008", "upload": "\uf093",
         "open": "\uf07b", "doctor": "\uf0f1"}
LABELS = {"cam": "Camera", "preview": "Preview window", "mic": "Microphone", "keys": "Keystrokes",
          "hide_cursor": "Hide real cursor"}


def add_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--print", action="store_true", help="print the menu instead of showing it (for testing)")
    p.add_argument("--pick", metavar="LABEL", help="run the action with this exact label, no menu")
    p.add_argument("--lines", type=int, default=11, help="menu height")


def entries() -> list[tuple[str, str]]:
    """(label, action) in menu order. Action is an id resolved by `run`."""
    from demovid import prefs
    from demovid.rec import status

    st = status()
    out: list[tuple[str, str]] = []
    if st["recording"]:
        m, s = divmod(int(st["elapsed_s"]), 60)
        out.append((f"{ICONS['stop']}  Stop recording  {m}:{s:02d}", "toggle-rec"))
    else:
        out.append((f"{ICONS['start']}  Start recording", "toggle-rec"))
    suffix = "  (next recording)" if st["recording"] else ""
    values = prefs.load()
    for name in ("cam", "preview", "mic", "keys", "hide_cursor"):
        state = "on" if values[name] else "off"
        out.append((f"{ICONS[name]}  {LABELS[name]}: {state}{suffix}", f"toggle:{name}"))
    last = latest()
    if last:
        out.append((f"{ICONS['render']}  Render {last.name}", "render"))
        if newest_render(last):
            out.append((f"{ICONS['upload']}  Upload {newest_render(last).name}", "upload"))
        out.append((f"{ICONS['open']}  Open {last.name}", "open"))
    out.append((f"{ICONS['doctor']}  Run doctor", "doctor"))
    return out


def latest() -> Path | None:
    from demovid.island import recordings

    recs = recordings(limit=1)
    return Path(recs[0]["path"]) if recs else None


def newest_render(rec: Path) -> Path | None:
    from demovid.island import STREAM_FILES

    mp4s = [p for p in rec.glob("*.mp4") if p.name not in STREAM_FILES]
    return max(mp4s, key=lambda p: p.stat().st_mtime) if mp4s else None


def notify(body: str) -> None:
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", "-a", "demovid", "-t", "2000",
                        "-h", "string:x-canonical-private-synchronous:demovid", "demovid", body])


def in_terminal(args: list[str]) -> None:
    """Run a long job where Yousef can watch it; the window stays open when it finishes."""
    cli = [sys.executable, "-m", "demovid.cli", *args]
    if KITTY.exists():
        subprocess.Popen([str(KITTY), "--hold", "-e", *cli], start_new_session=True)
    else:
        subprocess.Popen(cli, start_new_session=True)


def run(action: str) -> int:
    from demovid import prefs
    from demovid.rec.state import poke_waybar

    if action == "toggle-rec":
        return subprocess.run([sys.executable, "-m", "demovid.cli", "rec"]).returncode
    if action.startswith("toggle:"):
        name = action.split(":", 1)[1]
        value = prefs.toggle(name)
        poke_waybar()
        notify(f"{LABELS[name]}: {'on' if value else 'off'}"
               + (" (from the next recording)" if name != "hide_cursor" else ""))
        return 0
    last = latest()
    if action == "render" and last:
        in_terminal(["render", str(last), "--preset", "studio"])
        return 0
    if action == "upload" and last:
        target = newest_render(last)
        if not target:
            notify("nothing rendered yet")
            return 1
        in_terminal(["upload", str(target)])
        return 0
    if action == "open" and last:
        subprocess.Popen(["xdg-open", str(last)], start_new_session=True)
        return 0
    if action == "doctor":
        in_terminal(["doctor"])
        return 0
    notify(f"no recording yet ({action})")
    return 1


def main(ns: argparse.Namespace) -> int:
    items = entries()
    if ns.print:
        for label, action in items:
            print(f"{action}\t{label}")
        return 0
    if ns.pick:
        for label, action in items:
            if label == ns.pick or action == ns.pick:
                return run(action)
        print(f"no menu entry {ns.pick!r}", file=sys.stderr)
        return 1
    if not shutil.which("fuzzel"):
        print("fuzzel is not installed", file=sys.stderr)
        return 1
    chosen = subprocess.run(["fuzzel", "--dmenu", "--lines", str(ns.lines), "--width", "34",
                             "--prompt", "demovid  "],
                            input="\n".join(label for label, _ in items),
                            capture_output=True, text=True)
    picked = chosen.stdout.strip()
    if not picked:
        return 0
    for label, action in items:
        if label == picked:
            return run(action)
    return 0
