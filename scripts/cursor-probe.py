"""Live check of the pointer-cursor session: warp the cursor via Sway IPC, compare with logged positions,
and confirm the cursor image arrives. Moves the pointer for ~2 s (no clicks) and leaves it at the last target.

    uv run scripts/cursor-probe.py            # against $SWAYSOCK / $WAYLAND_DISPLAY
"""
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from demovid.rec.clock import Clock
from demovid.rec.cursor import CursorSession, available
from demovid.rec.events import EventLog


def swaymsg(*args: str):
    out = subprocess.run(["swaymsg", "-r", *args], capture_output=True, text=True, check=True).stdout
    return json.loads(out) if out.strip() else {}


def main() -> int:
    if not available():
        print("ext-image-copy-capture not advertised", file=sys.stderr)
        return 2
    out = next(o for o in swaymsg("-t", "get_outputs") if o["active"])
    w, h = out["current_mode"]["width"], out["current_mode"]["height"]
    targets = [(100, 100), (w // 2, h // 2), (w - 120, h - 80), (5, h - 5), (w - 5, 5), (w // 3, h // 3)]
    with tempfile.TemporaryDirectory() as td:
        clock = Clock.start()
        log = EventLog(Path(td) / "events.jsonl", clock)
        sess = CursorSession(log, clock, out["name"], scale=out.get("scale", 1.0), shapes_dir=Path(td) / "cursors")
        sess.start()
        time.sleep(0.4)
        seen = []
        for x, y in targets:
            swaymsg("seat", "seat0", "cursor", "set", str(x), str(y))
            time.sleep(0.25)
            seen.append(sess.position)
        time.sleep(0.3)
        sess.stop()
        log.close()
        events = [json.loads(line) for line in open(Path(td) / "events.jsonl")]
        shapes = sorted(p.name for p in (Path(td) / "cursors").glob("*.png"))
    errs = []
    for (tx, ty), pos in zip(targets, seen):
        if pos is None:
            print(f"target {tx:4d},{ty:4d}  NO POSITION")
            errs.append(None)
            continue
        dx, dy = pos[0] - tx, pos[1] - ty
        errs.append((dx, dy))
        print(f"target {tx:4d},{ty:4d}  logged {pos[0]:4d},{pos[1]:4d}  err {dx:+d},{dy:+d}")
    kinds = {}
    for e in events:
        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
    print(f"events {kinds}  hotspot {sess.hotspot}  shapes {shapes}  session error {sess.error}")
    ok = all(e is not None and abs(e[0]) <= 1 and abs(e[1]) <= 1 for e in errs) and bool(shapes)
    print("OK" if ok else ("MISMATCH" if any(errs) else "NO HARDWARE CURSOR (WLR_NO_HARDWARE_CURSORS set, or an "
                                                          "overlay_cursor screencopy client is running)"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
