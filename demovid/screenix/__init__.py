"""Convert a Screenix recording dir into the FORMAT.md v1 layout (hardlinks, no re-encode)."""

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from demovid import RECORDINGS_DIR

SCREENIX_DIR = Path("~/Videos/screenix").expanduser()
BUTTONS = {1: "left", 2: "middle", 3: "right", 8: "side", 9: "extra"}
MODS = {"Ctrl": "ctrl", "Alt": "alt", "Shift": "shift", "Super": "super", "Meta": "super"}


def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("src", help="Screenix recording dir (or its name under ~/Videos/screenix)")
    parser.add_argument("-o", "--out", help=f"destination dir (default {RECORDINGS_DIR}/<ts>)")
    parser.add_argument("--force", action="store_true", help="overwrite an existing destination")


def main(ns: argparse.Namespace) -> int:
    src = Path(ns.src).expanduser()
    if not src.is_dir() and (SCREENIX_DIR / ns.src).is_dir():
        src = SCREENIX_DIR / ns.src
    if not src.is_dir():
        raise SystemExit(f"not a directory: {src}")
    out = Path(ns.out).expanduser() if ns.out else Path(RECORDINGS_DIR).expanduser() / recording_stamp(src.name)
    if out.exists() and any(out.iterdir()):
        if not ns.force:
            raise SystemExit(f"{out} exists (use --force)")
        shutil.rmtree(out)
    convert(src, out)
    print(out)
    return 0


def recording_stamp(name: str) -> str:
    m = re.search(r"(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})", name)
    if not m:
        return name
    return "-".join(m.groups())


def convert(src: Path, out: Path) -> Path:
    base = src.name
    screen = src / f"{base}.mp4"
    meta_path = src / f"{base}.mp4.meta.json"
    cursor_path = src / f"{base}.cursor.json"
    if not screen.exists() or not meta_path.exists():
        raise SystemExit(f"{src}: missing {base}.mp4 or its .meta.json")
    meta = json.loads(meta_path.read_text())
    cursor = json.loads(cursor_path.read_text()) if cursor_path.exists() else []
    width, height = int(meta["width"]), int(meta["height"])

    out.mkdir(parents=True, exist_ok=True)
    screen_info = probe(screen)
    link_or_copy(screen, out / "screen.mp4")
    streams = {
        "screen": {"file": "screen.mp4", "fps": screen_info["fps"], "offset_s": 0.0},
    }

    cam = pick_camera(src, base)
    if cam is not None:
        cam_info = probe(cam)
        link_or_copy(cam, out / "cam.mp4")
        streams["cam"] = {
            "file": "cam.mp4",
            "fps": cam_info["fps"],
            "width": cam_info["width"],
            "height": cam_info["height"],
            "offset_s": 0.0,
        }

    if screen_info["has_audio"]:
        extract_audio(screen, out / "mic.flac")
        streams["mic"] = {"file": "mic.flac", "sample_rate": 48000, "channels": screen_info["channels"], "offset_s": 0.0}

    events = list(iter_events(meta, cursor, width, height))
    events.sort(key=lambda e: e["t"])
    with (out / "events.jsonl").open("w") as f:
        for e in events:
            f.write(json.dumps(e, separators=(",", ":")) + "\n")

    started = parse_stamp(base)
    duration = float(meta.get("duration") or screen_info["duration"])
    manifest = {
        "version": 1,
        "started_at": started.isoformat() if started else None,
        "stopped_at": (started + dt.timedelta(seconds=duration)).isoformat() if started else None,
        "duration_s": duration,
        "monotonic_ns": None,
        "output": {"name": None, "x": 0, "y": 0, "width": width, "height": height, "scale": 1, "refresh_hz": None},
        "streams": streams,
        "cursor": {"theme": None, "size": None, "hidden_during_rec": False},
        "cursor_source": "screenix",
        "tools": {"screenix": str(meta.get("version"))},
        "source": "screenix",
        "screenix_dir": str(src),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return out


def iter_events(meta: dict, cursor: list, width: int, height: int):
    last = None
    for c in cursor:
        last = (int(c["x"]), int(c["y"]))
        yield {"t": float(c["timestamp"]), "kind": "cursor", "x": last[0], "y": last[1]}
    for ce in meta.get("click_events", []):
        yield {
            "t": float(ce["timestamp"]),
            "kind": "button",
            "button": BUTTONS.get(int(ce.get("button", 1)), "left"),
            "state": "down" if ce.get("event_type") == "down" else "up",
            "x": round(float(ce["x"]) * width),
            "y": round(float(ce["y"]) * height),
        }
    held: set[str] = set()
    for ke in sorted(meta.get("key_events", []), key=lambda k: k["timestamp"]):
        label = str(ke.get("label", ""))
        mod = MODS.get(label)
        state = "down" if ke.get("pressed") else "up"
        if mod:
            (held.add if state == "down" else held.discard)(mod)
        yield {
            "t": float(ke["timestamp"]),
            "kind": "key",
            "key": mod or label.lower(),
            "state": state,
            "mods": sorted(held - {mod} if mod else held),
        }


def pick_camera(src: Path, base: str) -> Path | None:
    # The .seekable copy is Screenix's own re-encode with a proper index; the raw .camera.mp4 is 10x larger.
    for name in (f"{base}.camera.seekable.mp4", f"{base}.camera.mp4"):
        p = src / name
        if p.exists() and p.stat().st_size > 0:
            return p
    return None


def probe(path: Path) -> dict:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=codec_type,width,height,avg_frame_rate,channels:format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    info = json.loads(r.stdout)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    audio = [s for s in info["streams"] if s["codec_type"] == "audio"]
    num, den = video["avg_frame_rate"].split("/")
    return {
        "width": int(video["width"]),
        "height": int(video["height"]),
        "fps": round(int(num) / int(den), 3),
        "duration": float(info["format"]["duration"]),
        "has_audio": bool(audio),
        "channels": int(audio[0]["channels"]) if audio else 0,
    }


def extract_audio(src: Path, dst: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(src), "-vn", "-ar", "48000", "-c:a", "flac", str(dst)],
        check=True,
    )


def link_or_copy(src: Path, dst: Path) -> None:
    if dst.exists():
        dst.unlink()
    try:
        os.link(src, dst)
    except OSError:
        shutil.copyfile(src, dst)


def parse_stamp(name: str) -> dt.datetime | None:
    m = re.search(r"(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})", name)
    if not m:
        return None
    return dt.datetime(*map(int, m.groups())).astimezone()
