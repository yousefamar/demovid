"""demovid render <dir>: events -> zoom plan -> composited mp4."""

import argparse
import json
import time
from pathlib import Path

DEFAULTS: dict = {}   # argparse dest -> default, captured by add_args for presets
OPTIONS: dict = {}    # argparse dest -> its flag strings, so a preset never overrides an explicit flag


def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("dir", nargs="?", help="recording dir (manifest.json + events.jsonl + streams)")
    parser.add_argument("-o", "--out", help="output mp4 (default <dir>/render.mp4, or preview.mp4)")
    parser.add_argument("--preview", action="store_true", help="half size, 30 fps, fast encode")
    parser.add_argument("--from", dest="t_from", type=parse_time, help="start (s or mm:ss) on the recording clock")
    parser.add_argument("--to", dest="t_to", type=parse_time, help="end (s or mm:ss)")
    parser.add_argument("--fps", type=float, help="output fps (default: screen fps, 30 for --preview)")
    parser.add_argument("--still", action="append", type=parse_time, default=[],
                        help="render a single frame at this time to PNG instead of a video (repeatable)")
    parser.add_argument("--preset", help="apply a named flag bundle (see --list-presets); explicit flags win")
    parser.add_argument("--list-presets", action="store_true")
    region = parser.add_argument_group("region")
    region.add_argument("--crop", type=parse_rect, metavar="X,Y,W,H", help="render only this source rectangle")
    region.add_argument("--window", metavar="NAME", help="render only the window whose app_id/title contains NAME "
                        "(rect from the recording's focus events)")
    zoom = parser.add_argument_group("zoom")
    zoom.add_argument("--no-zoom", action="store_true")
    zoom.add_argument("--zoom", type=float, default=1.8, help="zoom level for clicks/typing (default 1.8)")
    zoom.add_argument("--zoom-hold", type=float, default=2.0, help="seconds to stay zoomed after activity")
    zoom.add_argument("--no-focus-zoom", action="store_true", help="ignore Sway focus rects")
    zoom.add_argument("--plan-json", help="also write the keyframes here")
    idle = parser.add_argument_group("idle speed-up")
    idle.add_argument("--idle-speed", type=float, default=0.0,
                      help="fast-forward stretches with no input and no speech by this factor (0 = off)")
    idle.add_argument("--idle-min", type=float, default=2.0, help="shortest quiet stretch to speed up (s)")
    idle.add_argument("--idle-max", type=float, default=1.5, help="a sped-up stretch never lasts longer than this (s)")
    idle.add_argument("--idle-ignore-speech", action="store_true", help="speed up even while the mic hears talking")
    idle.add_argument("--keep-pauses", action="store_true", help="do not cut the spans where rec was paused")
    frame = parser.add_argument_group("background frame")
    frame.add_argument("--pad", type=float, default=0.0, help="inset the screen on a background, fraction of height (0 = off)")
    frame.add_argument("--bg", default="#141414", help="'#rrggbb', '#rrggbb,#rrggbb' gradient, or an image path")
    frame.add_argument("--radius", type=float, default=0.018, help="corner radius of the inset screen, fraction of height")
    frame.add_argument("--no-shadow", action="store_true")
    cur = parser.add_argument_group("cursor")
    cur.add_argument("--no-cursor", action="store_true")
    cur.add_argument("--cursor-scale", type=float, default=2.5, help="x the 24 px system cursor (default 2.5)")
    cur.add_argument("--cursor-style", choices=["dark", "light"], default="dark",
                     help="dark: black arrow with white edge (macOS/Screen Studio); light: white with black edge")
    cur.add_argument("--cursor-smooth", type=float, default=None,
                     help="gaussian sigma in seconds (default: 0 when the recording has exact positions, else 0.05)")
    cur.add_argument("--cursor-lag", type=float, default=None, metavar="S",
                     help="constant delay between cursor samples and screen frames (default 0)")
    cur.add_argument("--no-cursor-snap", action="store_true",
                     help="don't snap the drawn cursor onto the real one frame by frame (exact recordings only)")
    cur.add_argument("--no-ripple", action="store_true")
    cur.add_argument("--synthetic-cursor", action="store_true",
                     help="always draw the stylised arrow, even when the recording captured real cursor shapes")
    chip = parser.add_argument_group("keystroke chips")
    chip.add_argument("--no-chips", action="store_true")
    chip.add_argument("--chip-hold", type=float, default=1.1, help="seconds a chip stays up (default 1.1)")
    chip.add_argument("--all-keys", action="store_true",
                      help="chip every key, not just shortcuts and editing keys")
    cap = parser.add_argument_group("captions")
    cap.add_argument("--captions", action="store_true",
                     help="transcribe the mic with whisper (cached in <dir>/captions.json), burn in + write .srt")
    cap.add_argument("--captions-lang", metavar="ISO", help="force the language (default: auto)")
    pip = parser.add_argument_group("webcam")
    pip.add_argument("--no-pip", action="store_true")
    pip.add_argument("--pip-mode", choices=["auto", "fixed", "scene"], default="auto",
                     help="auto: cover the recorded preview window while it is in view, fixed corner otherwise; "
                          "fixed: always the output corner; scene: always over the preview, zooms with the content")
    pip.add_argument("--pip-shape", choices=["squircle", "rounded", "circle"], default="squircle")
    pip.add_argument("--pip-size", type=float, default=0.15, help="fraction of output width (default 0.15)")
    pip.add_argument("--pip-pos", choices=["br", "bl", "tr", "tl"], default="br")
    aud = parser.add_argument_group("audio")
    aud.add_argument("--no-audio", action="store_true")
    aud.add_argument("--no-denoise", action="store_true", help="skip afftdn before loudnorm")
    enc = parser.add_argument_group("encode")
    enc.add_argument("--encoder", choices=["h264_nvenc", "libx264"], default="h264_nvenc")
    enc.add_argument("--quality", type=int, default=20, help="NVENC cq / x264 crf (default 20)")
    enc.add_argument("--cubic", action="store_true", help="bicubic zoom sampling (slower, sharper)")
    DEFAULTS.update({a.dest: a.default for a in parser._actions if a.dest != "help"})
    OPTIONS.update({a.dest: list(a.option_strings) for a in parser._actions if a.dest != "help"})


def explicit_dests(options: dict) -> set[str]:
    """Which dests the user actually typed. Comparing to the default cannot tell `--idle-speed 0`
    (same as the default) from not passing it at all, and a preset must never win over a typed flag."""
    import sys

    typed = set(sys.argv[1:])
    return {dest for dest, flags in options.items()
            if any(a == f or a.startswith(f + "=") for a in typed for f in flags)}


def parse_time(s: str) -> float:
    if ":" in s:
        parts = [float(p) for p in s.split(":")]
        return sum(p * 60 ** i for i, p in enumerate(reversed(parts)))
    return float(s)


def parse_rect(s: str) -> tuple[int, int, int, int]:
    parts = [int(float(p)) for p in s.replace("x", ",").split(",")]
    if len(parts) != 4 or parts[2] <= 0 or parts[3] <= 0:
        raise argparse.ArgumentTypeError("expected X,Y,W,H")
    return tuple(parts)  # type: ignore[return-value]


def main(ns: argparse.Namespace) -> int:
    import cv2

    from demovid.render import presets
    from demovid.render.chips import chips_from_events
    from demovid.render.compositor import Compositor, Look
    from demovid.render.cursor import CursorRefiner, CursorTrack, ShapeTrack
    from demovid.render.media import (AsyncEncoder, Encoder, FrameReader, Prefetcher, audio_chain, probe_duration,
                                      silences)
    from demovid.render.planner import Keyframe, PlanConfig, plan
    from demovid.render.timing import IdleConfig, TimeMap, compress, cuts, idle_intervals, paused_intervals, subtract

    if ns.list_presets:
        print(presets.describe())
        return 0
    if not ns.dir:
        raise SystemExit("render: a recording dir is required")
    if ns.preset:
        changed = presets.apply(ns, DEFAULTS, ns.preset, explicit_dests(OPTIONS))
        print(f"[preset] {ns.preset}: {' '.join(changed) or 'nothing to change'}")

    rec = Path(ns.dir).expanduser()
    manifest = json.loads((rec / "manifest.json").read_text())
    events = load_events(rec / "events.jsonl")
    src_w, src_h = int(manifest["output"]["width"]), int(manifest["output"]["height"])
    streams = manifest["streams"]
    screen = streams["screen"]
    screen_path = rec / screen["file"]
    screen_offset = float(screen.get("offset_s", 0.0))
    screen_fps = float(screen.get("fps") or 60)
    cam = streams.get("cam") if not ns.no_pip else None
    preview_rect = cam.get("preview_rect") if cam else None

    crop = ns.crop or (window_rect(events, ns.window) if ns.window else None)
    if crop:
        crop = clip_rect(crop, src_w, src_h)
        events = crop_events(events, crop)
        preview_rect = crop_rect(preview_rect, crop) if preview_rect else None
        src_w, src_h = crop[2], crop[3]
        print(f"[region] rendering {crop[2]}x{crop[3]} at {crop[0]},{crop[1]}")

    out_scale = 0.5 if ns.preview else 1.0
    out_w, out_h = even(src_w * out_scale), even(src_h * out_scale)
    fps = ns.fps or (30.0 if ns.preview else screen_fps)
    t_from = max(ns.t_from if ns.t_from is not None else 0.0, screen_offset)
    t_end_default = screen_offset + probe_duration(screen_path)
    t_to = min(ns.t_to, t_end_default) if ns.t_to is not None else t_end_default
    if t_to <= t_from:
        raise SystemExit(f"empty range {t_from}..{t_to}")

    # spans where rec was paused are cut from the output; nothing that happened inside them may drive
    # the zoom, ripples or chips either (a click while paused must not leave the render zoomed in)
    paused = [] if (ns.keep_pauses or ns.still) else paused_intervals(events, t_from, t_to)
    live_events = [e for e in events if not any(a <= float(e.get("t", 0.0)) < b for a, b in paused)] if paused else events

    if ns.no_zoom:
        keyframes = [Keyframe(0.0, src_w / 2, src_h / 2, 1.0)]
    else:
        cfg = PlanConfig(width=src_w, height=src_h, zoom=ns.zoom, hold_s=ns.zoom_hold, focus_zoom=not ns.no_focus_zoom)
        keyframes = plan(live_events, cfg)
    if ns.plan_json:
        Path(ns.plan_json).write_text(json.dumps([k.as_dict() for k in keyframes], indent=1))

    audio = streams.get("mic") if not ns.no_audio else None
    mic_offset = float(audio.get("offset_s") or 0.0) if audio else 0.0

    segments = cuts(paused)
    if paused:
        print(f"[pause] {len(paused)} paused span(s) cut, {sum(b - a for a, b in paused):.1f}s")
    if ns.idle_speed and ns.idle_speed > 1.0 and not ns.still:
        icfg = IdleConfig(min_idle_s=ns.idle_min, speed=ns.idle_speed, max_out_s=ns.idle_max)
        quiet = None if (ns.idle_ignore_speech or not audio) else silences(rec / audio["file"], mic_offset)
        idle = subtract(idle_intervals(events, t_from, t_to, icfg, quiet), paused)
        segments += compress(idle, icfg)
        print(f"[idle] {len(idle)} stretches sped up"
              + ("" if quiet is not None else " (speech not checked)"))
    tm = TimeMap(t_from, t_to, segments)
    if segments:
        print(f"[time] {t_to - t_from:.1f}s of recording -> {tm.duration:.1f}s of video")

    look = Look(out_w=out_w, out_h=out_h, out_scale=out_scale, cursor=not ns.no_cursor, cursor_scale=ns.cursor_scale,
                cursor_style=ns.cursor_style, cursor_real=not ns.synthetic_cursor, chips=not ns.no_chips,
                ripple=not ns.no_ripple, pip=not ns.no_pip, pip_mode=ns.pip_mode, pip_size=ns.pip_size,
                pip_pos=ns.pip_pos, pip_shape=ns.pip_shape, zoom_level=ns.zoom,
                interpolation=cv2.INTER_CUBIC if ns.cubic else cv2.INTER_LINEAR,
                pad=max(ns.pad, 0.0), bg=ns.bg, radius=ns.radius, shadow=not ns.no_shadow)
    clicks = [(float(e["t"]), float(e["x"]), float(e["y"])) for e in live_events
              if e.get("kind") == "button" and e.get("state") == "down" and e.get("x") is not None]
    shapes_name = (manifest.get("cursor") or {}).get("shapes_dir")
    shapes = ShapeTrack(events, rec / shapes_name if shapes_name else None)
    exact_cursor = manifest.get("cursor_source") == "ext-image-copy-capture"
    cursor_smooth = ns.cursor_smooth if ns.cursor_smooth is not None else (0.0 if exact_cursor else 0.05)
    cursor_lag = ns.cursor_lag or 0.0
    nominal_px = int((manifest.get("cursor") or {}).get("size") or 24)
    refiner = (CursorRefiner(shapes, nominal_px) if exact_cursor and shapes.present and not ns.no_cursor
               and not ns.no_cursor_snap else None)
    chips = [] if ns.no_chips else chips_from_events(live_events, ns.chip_hold, shortcuts_only=not ns.all_keys)
    # the PiP square in source px: over the recorded preview window when there is one (so it hides it),
    # else a work-area corner from the same layout rules; imports without either use the old corner+margin
    from demovid import layout
    pip_box_src = None
    if cam:
        workarea = manifest["output"].get("workarea")
        if preview_rect:
            pip_box_src = layout.clamp_square(layout.pip_for_preview(preview_rect), src_w, src_h)
        elif workarea and not crop:
            pip_box_src = layout.pip_square(src_w, src_h, workarea, ns.pip_pos)
        if look.pip_mode == "scene" and not preview_rect:
            print("[pip] no preview_rect in the manifest; falling back to fixed corner")
            look.pip_mode = "fixed"
    # decode the cam big enough for the in-source cover at the planner's zoom, not just the corner size
    pip_side_out = int(round((pip_box_src[2] * out_scale) if pip_box_src else look.pip_size * out_w))
    cam_h = int(round(pip_side_out * (ns.zoom if look.pip_mode != "fixed" else 1.0)))

    def cam_reader(seek_t: float, duration: float | None):
        if cam is None:
            return None
        cam_offset = float(cam.get("offset_s") or 0.0)
        return CamFeed(FrameReader(rec / cam["file"], float(cam.get("fps") or 30), max(seek_t - cam_offset, 0.0),
                                   duration, height=cam_h),
                       t0=max(seek_t, cam_offset), fps=float(cam.get("fps") or 30))

    def source_frame(frame):
        if crop:
            x, y, w, h = crop
            return frame[y:y + h, x:x + w]
        return frame

    if ns.still:
        for t in ns.still:
            if not (t_from <= t <= t_to):
                print(f"[still] {t} outside {t_from:.2f}..{t_to:.2f}, skipped")
                continue
            win = 0.5
            track = CursorTrack(events, t - win, fps, int(2 * win * fps) + 1, cursor_smooth, cursor_lag)
            comp = Compositor(keyframes, src_w, src_h, look, track, clicks, preview_rect, shapes, chips, refiner, pip_box_src)
            reader = FrameReader(screen_path, fps, t - screen_offset, 1.5 / fps)
            feed = cam_reader(t, 1.5 / (cam.get("fps") or 30) if cam else None)
            frame = reader.read()
            reader.close()
            if frame is None:
                raise SystemExit(f"no screen frame at {t}")
            out = comp.compose(source_frame(frame), t, int(win * fps), feed.at(t) if feed else None)
            if feed:
                feed.close()
            path = Path(ns.out).with_suffix("") if ns.out else rec / "still"
            png = Path(f"{path}-{t:.3f}.png")
            cv2.imwrite(str(png), out)
            print(png)
        return 0

    out = Path(ns.out).expanduser() if ns.out else rec / ("preview.mp4" if ns.preview else "render.mp4")
    n_src_frames = int(round((t_to - t_from) * fps))
    n_frames = int(round(tm.duration * fps))
    track = CursorTrack(events, t_from, fps, n_src_frames, cursor_smooth, cursor_lag)
    comp = Compositor(keyframes, src_w, src_h, look, track, clicks, preview_rect, shapes, chips, refiner, pip_box_src)

    audio_args = {}
    if audio:
        keep = tm.audio_keep() if tm.segments else None
        filt, seek, dur = audio_chain(rec / audio["file"], mic_offset, t_from, t_to, not ns.no_denoise, keep)
        audio_args = {"audio": rec / audio["file"], "audio_filter": filt, "audio_offset_s": seek, "audio_duration_s": dur}

    video_filter = ""
    if ns.captions:
        from demovid.render.captions import subtitles_filter, to_srt, transcribe
        if not audio:
            raise SystemExit("--captions needs the mic track (drop --no-audio)")
        segments = transcribe(rec / audio["file"], rec / "captions.json", ns.captions_lang)
        srt_path = out.with_suffix(".srt")
        srt_path.write_text(to_srt(segments, mic_offset, tm))
        side = (pip_side_out / out_w + 0.03) if cam else 0.06
        video_filter = subtitles_filter(srt_path, out_h, above_chips=bool(chips), side_margin_frac=side)
        print(f"[captions] {len(segments)} segments -> {srt_path}")

    print(f"[render] {rec.name}: {t_from:.2f}..{t_to:.2f}s -> {tm.duration:.1f}s, {out_w}x{out_h}@{fps:g}, {n_frames} frames, "
          f"{len(keyframes)} keyframes, cursor={'yes' if track.present and look.cursor else 'no'}, "
          f"pip={look.pip_mode if cam else 'no'}, audio={'yes' if audio else 'no'}, "
          f"shapes={'yes' if shapes.present and look.cursor_real else 'no'}, chips={len(chips)}, "
          f"pad={look.pad:g} -> {out}", flush=True)
    reader = Prefetcher(FrameReader(screen_path, fps, t_from - screen_offset, t_to - t_from))
    feed = cam_reader(t_from, t_to - t_from)
    enc = AsyncEncoder(Encoder(out, out_w, out_h, fps, ns.encoder, ns.quality, video_filter=video_filter, **audio_args))
    started = time.time()
    i = 0
    k_read = -1  # index of the last source frame pulled from the decoder
    frame = None
    try:
        for i in range(n_frames):
            t_src = tm.src(i / fps)
            k = min(int(round((t_src - t_from) * fps)), n_src_frames - 1)
            while k_read < k:
                nxt = reader.read()
                if nxt is None:
                    break
                frame, k_read = nxt, k_read + 1
            if frame is None or k_read < k:
                break
            enc.write(comp.compose(source_frame(frame), t_src, k, feed.at(t_src) if feed else None))
            if i and i % 600 == 0:
                el = time.time() - started
                print(f"  {i}/{n_frames} frames, {i / el:.0f} fps, eta {(n_frames - i) / (i / el):.0f}s", flush=True)
    finally:
        reader.close()
        if feed:
            feed.close()
        rc = enc.close()
    el = time.time() - started
    print(f"[render] wrote {i + 1} frames in {el:.0f}s ({(i + 1) / max(el, 1e-6):.0f} fps) -> {out}"
          + (f"; cursor {refiner.summary()}" if refiner else ""))
    if rc == 0:
        try:
            from demovid.island import refresh
            refresh()
        except Exception as e:  # the dashboard is a convenience, never a render failure
            print(f"[island] not updated: {e}")
    return rc


def window_rect(events: list[dict], needle: str) -> tuple[int, int, int, int]:
    needle = needle.lower()
    for e in events:
        if e.get("kind") != "focus" or not e.get("rect"):
            continue
        hay = " ".join(str(e.get(k) or "") for k in ("app_id", "class", "title")).lower()
        if needle in hay:
            x, y, w, h = (int(round(float(v))) for v in e["rect"])
            return x, y, w, h
    raise SystemExit(f"--window: no focus event matches {needle!r}")


def clip_rect(r: tuple, w: int, h: int) -> tuple[int, int, int, int]:
    x, y, rw, rh = r
    nx, ny = max(0, min(x, w)), max(0, min(y, h))
    rw, rh = even(min(x + rw, w) - nx), even(min(y + rh, h) - ny)
    if rw < 16 or rh < 16:
        raise SystemExit(f"crop {x},{y},{r[2]},{r[3]} barely overlaps the {w}x{h} recording")
    return nx, ny, rw, rh


def crop_rect(rect: list, crop: tuple) -> list | None:
    x, y, w, h = crop
    rx, ry, rw, rh = rect
    nx, ny = rx - x, ry - y
    if nx + rw <= 0 or ny + rh <= 0 or nx >= w or ny >= h:
        return None
    return [nx, ny, rw, rh]


def crop_events(events: list[dict], crop: tuple) -> list[dict]:
    """Shift coordinates into the crop; drop pointer events that fall outside it."""
    x0, y0, w, h = crop
    out = []
    for e in events:
        e = dict(e)
        if e.get("x") is not None and e.get("y") is not None:
            nx, ny = float(e["x"]) - x0, float(e["y"]) - y0
            if not (0 <= nx < w and 0 <= ny < h):
                continue
            e["x"], e["y"] = nx, ny
        if e.get("rect") and len(e["rect"]) == 4:
            rx, ry, rw, rh = (float(v) for v in e["rect"])
            e["rect"] = [rx - x0, ry - y0, rw, rh]
        out.append(e)
    return out


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
