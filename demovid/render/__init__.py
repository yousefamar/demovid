"""demovid render <dir>: events -> zoom plan -> composited mp4."""

import argparse
import json
import time
from pathlib import Path


def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("dir", help="recording dir (manifest.json + events.jsonl + streams)")
    parser.add_argument("-o", "--out", help="output mp4 (default <dir>/render.mp4, or preview.mp4)")
    parser.add_argument("--preview", action="store_true", help="half size, 30 fps, fast encode")
    parser.add_argument("--from", dest="t_from", type=parse_time, help="start (s or mm:ss) on the recording clock")
    parser.add_argument("--to", dest="t_to", type=parse_time, help="end (s or mm:ss)")
    parser.add_argument("--fps", type=float, help="output fps (default: screen fps, 30 for --preview)")
    parser.add_argument("--still", action="append", type=parse_time, default=[],
                        help="render a single frame at this time to PNG instead of a video (repeatable)")
    zoom = parser.add_argument_group("zoom")
    zoom.add_argument("--no-zoom", action="store_true")
    zoom.add_argument("--zoom", type=float, default=1.8, help="zoom level for clicks/typing (default 1.8)")
    zoom.add_argument("--zoom-hold", type=float, default=2.0, help="seconds to stay zoomed after activity")
    zoom.add_argument("--no-focus-zoom", action="store_true", help="ignore Sway focus rects")
    zoom.add_argument("--plan-json", help="also write the keyframes here")
    cur = parser.add_argument_group("cursor")
    cur.add_argument("--no-cursor", action="store_true")
    cur.add_argument("--cursor-scale", type=float, default=2.5, help="x the 24 px system cursor (default 2.5)")
    cur.add_argument("--cursor-style", choices=["dark", "light"], default="dark",
                     help="dark: black arrow with white edge (macOS/Screen Studio); light: white with black edge")
    cur.add_argument("--cursor-smooth", type=float, default=0.05, help="gaussian sigma in seconds (default 0.05)")
    cur.add_argument("--no-ripple", action="store_true")
    pip = parser.add_argument_group("webcam")
    pip.add_argument("--no-pip", action="store_true")
    pip.add_argument("--pip-mode", choices=["fixed", "scene"], default="fixed",
                     help="fixed: output corner; scene: painted over preview_rect in the recording, zooms with it")
    pip.add_argument("--pip-size", type=float, default=0.15, help="fraction of output width (default 0.15)")
    pip.add_argument("--pip-pos", choices=["br", "bl", "tr", "tl"], default="br")
    aud = parser.add_argument_group("audio")
    aud.add_argument("--no-audio", action="store_true")
    aud.add_argument("--no-denoise", action="store_true", help="skip afftdn before loudnorm")
    enc = parser.add_argument_group("encode")
    enc.add_argument("--encoder", choices=["h264_nvenc", "libx264"], default="h264_nvenc")
    enc.add_argument("--quality", type=int, default=20, help="NVENC cq / x264 crf (default 20)")
    enc.add_argument("--cubic", action="store_true", help="bicubic zoom sampling (slower, sharper)")


def parse_time(s: str) -> float:
    if ":" in s:
        parts = [float(p) for p in s.split(":")]
        return sum(p * 60 ** i for i, p in enumerate(reversed(parts)))
    return float(s)


def main(ns: argparse.Namespace) -> int:
    import cv2

    from demovid.render.compositor import Compositor, Look
    from demovid.render.cursor import CursorTrack
    from demovid.render.media import AsyncEncoder, Encoder, FrameReader, Prefetcher, audio_chain, probe_duration
    from demovid.render.planner import Keyframe, PlanConfig, plan

    rec = Path(ns.dir).expanduser()
    manifest = json.loads((rec / "manifest.json").read_text())
    events = load_events(rec / "events.jsonl")
    src_w, src_h = int(manifest["output"]["width"]), int(manifest["output"]["height"])
    streams = manifest["streams"]
    screen = streams["screen"]
    screen_path = rec / screen["file"]
    screen_offset = float(screen.get("offset_s", 0.0))
    screen_fps = float(screen.get("fps") or 60)

    out_scale = 0.5 if ns.preview else 1.0
    out_w, out_h = even(src_w * out_scale), even(src_h * out_scale)
    fps = ns.fps or (30.0 if ns.preview else screen_fps)
    t_from = max(ns.t_from if ns.t_from is not None else 0.0, screen_offset)
    t_end_default = screen_offset + probe_duration(screen_path)
    t_to = min(ns.t_to, t_end_default) if ns.t_to is not None else t_end_default
    if t_to <= t_from:
        raise SystemExit(f"empty range {t_from}..{t_to}")

    if ns.no_zoom:
        keyframes = [Keyframe(0.0, src_w / 2, src_h / 2, 1.0)]
    else:
        cfg = PlanConfig(width=src_w, height=src_h, zoom=ns.zoom, hold_s=ns.zoom_hold, focus_zoom=not ns.no_focus_zoom)
        keyframes = plan(events, cfg)
    if ns.plan_json:
        Path(ns.plan_json).write_text(json.dumps([k.as_dict() for k in keyframes], indent=1))

    look = Look(out_w=out_w, out_h=out_h, out_scale=out_scale, cursor=not ns.no_cursor, cursor_scale=ns.cursor_scale,
                cursor_style=ns.cursor_style,
                ripple=not ns.no_ripple, pip=not ns.no_pip, pip_mode=ns.pip_mode, pip_size=ns.pip_size,
                pip_pos=ns.pip_pos, zoom_level=ns.zoom,
                interpolation=cv2.INTER_CUBIC if ns.cubic else cv2.INTER_LINEAR)
    clicks = [(float(e["t"]), float(e["x"]), float(e["y"])) for e in events
              if e.get("kind") == "button" and e.get("state") == "down" and e.get("x") is not None]
    cam = streams.get("cam") if look.pip else None
    preview_rect = cam.get("preview_rect") if cam else None
    if look.pip_mode == "scene" and not preview_rect:
        print("[pip] no preview_rect in the manifest; falling back to fixed corner")
        look.pip_mode = "fixed"
    cam_h = int(round(look.pip_size * out_w)) if look.pip_mode == "fixed" else int(round(preview_rect[3]))

    def cam_reader(seek_t: float, duration: float | None):
        if cam is None:
            return None
        cam_offset = float(cam.get("offset_s", 0.0))
        return CamFeed(FrameReader(rec / cam["file"], float(cam.get("fps") or 30), max(seek_t - cam_offset, 0.0),
                                   duration, height=cam_h),
                       t0=max(seek_t, cam_offset), fps=float(cam.get("fps") or 30))

    if ns.still:
        for t in ns.still:
            if not (t_from <= t <= t_to):
                print(f"[still] {t} outside {t_from:.2f}..{t_to:.2f}, skipped")
                continue
            win = 0.5
            track = CursorTrack(events, t - win, fps, int(2 * win * fps) + 1, ns.cursor_smooth)
            comp = Compositor(keyframes, src_w, src_h, look, track, clicks, preview_rect)
            reader = FrameReader(screen_path, fps, t - screen_offset, 1.5 / fps)
            feed = cam_reader(t, 1.5 / (cam.get("fps") or 30) if cam else None)
            frame = reader.read()
            reader.close()
            if frame is None:
                raise SystemExit(f"no screen frame at {t}")
            out = comp.compose(frame, t, int(win * fps), feed.at(t) if feed else None)
            if feed:
                feed.close()
            path = Path(ns.out).with_suffix("") if ns.out else rec / "still"
            png = Path(f"{path}-{t:.3f}.png")
            cv2.imwrite(str(png), out)
            print(png)
        return 0

    out = Path(ns.out).expanduser() if ns.out else rec / ("preview.mp4" if ns.preview else "render.mp4")
    n_frames = int(round((t_to - t_from) * fps))
    track = CursorTrack(events, t_from, fps, n_frames, ns.cursor_smooth)
    comp = Compositor(keyframes, src_w, src_h, look, track, clicks, preview_rect)

    audio = streams.get("mic") if not ns.no_audio else None
    audio_args = {}
    if audio:
        filt, seek, dur = audio_chain(rec / audio["file"], float(audio.get("offset_s", 0.0)), t_from, t_to, not ns.no_denoise)
        audio_args = {"audio": rec / audio["file"], "audio_filter": filt, "audio_offset_s": seek, "audio_duration_s": dur}

    print(f"[render] {rec.name}: {t_from:.2f}..{t_to:.2f}s, {out_w}x{out_h}@{fps:g}, {n_frames} frames, "
          f"{len(keyframes)} keyframes, cursor={'yes' if track.present and look.cursor else 'no'}, "
          f"pip={look.pip_mode if cam else 'no'}, audio={'yes' if audio else 'no'} -> {out}", flush=True)
    reader = Prefetcher(FrameReader(screen_path, fps, t_from - screen_offset, t_to - t_from))
    feed = cam_reader(t_from, t_to - t_from)
    enc = AsyncEncoder(Encoder(out, out_w, out_h, fps, ns.encoder, ns.quality, **audio_args))
    started = time.time()
    i = 0
    try:
        for i in range(n_frames):
            t = t_from + i / fps
            frame = reader.read()
            if frame is None:
                break
            enc.write(comp.compose(frame, t, i, feed.at(t) if feed else None))
            if i and i % 600 == 0:
                el = time.time() - started
                print(f"  {i}/{n_frames} frames, {i / el:.0f} fps, eta {(n_frames - i) / (i / el):.0f}s", flush=True)
    finally:
        reader.close()
        if feed:
            feed.close()
        rc = enc.close()
    el = time.time() - started
    print(f"[render] wrote {i + 1} frames in {el:.0f}s ({(i + 1) / max(el, 1e-6):.0f} fps) -> {out}")
    return rc


class CamFeed:
    """Hands out the camera frame current at time t; holds the first frame before the stream starts."""

    def __init__(self, reader, t0: float, fps: float):
        self.reader, self.t0, self.fps = reader, t0, fps
        self.index = -1
        self.frame = None
        self.exhausted = False

    def at(self, t: float):
        want = int((t - self.t0) * self.fps)
        while not self.exhausted and self.index < want:
            nxt = self.reader.read()
            if nxt is None:
                self.exhausted = True
                break
            self.frame, self.index = nxt, self.index + 1
        return self.frame

    def close(self):
        self.reader.close()


def load_events(path: Path) -> list[dict]:
    events = []
    if not path.exists():
        return events
    with path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def even(v: float) -> int:
    return int(round(v / 2)) * 2
