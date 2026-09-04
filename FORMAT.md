# Recording directory format (v1)

`demovid rec` writes it, `demovid import-screenix` synthesises it, `demovid render` consumes it. This file is the contract between them — change it here first, bump `version`, and keep `render` reading every version ever written.

```
~/Videos/demovid/<YYYY-MM-DD-HH-mm-ss>/
  manifest.json
  screen.mp4     full output, NVENC h264, 60 fps
  cam.mp4        webcam, NVENC h264, 720p30 or 1080p30 (absent if no camera)
  mic.flac       mono, 48 kHz (absent if no mic)
  events.jsonl   one JSON object per line, ascending t
```

## Clock

`t = 0` is the moment `rec` started (`time.monotonic_ns()` recorded in the manifest). Every event `t` and every stream offset is **seconds since t = 0, float**. Streams start late by a few hundred ms each; `rec` measures each stream's first-frame time and writes it as `offset_s`, and `render` shifts by it. Nothing else may assume the streams begin at 0.

## manifest.json

```json
{
  "version": 1,
  "started_at": "2026-09-04T18:50:12.345+01:00",
  "stopped_at": "2026-09-04T18:51:13.522+01:00",
  "duration_s": 61.18,
  "monotonic_ns": 812345678901234,
  "output": {"name": "HDMI-A-1", "x": 0, "y": 0, "width": 1920, "height": 1080, "scale": 1, "refresh_hz": 144},
  "streams": {
    "screen": {"file": "screen.mp4", "fps": 60, "offset_s": 0.31},
    "cam":    {"file": "cam.mp4", "fps": 30, "width": 1280, "height": 720, "offset_s": 0.42,
               "preview_rect": [1520, 800, 384, 216]},
    "mic":    {"file": "mic.flac", "sample_rate": 48000, "channels": 1, "offset_s": 0.05}
  },
  "cursor": {"theme": "Adwaita", "size": 24, "hidden_during_rec": true},
  "cursor_source": "ext-image-copy-capture",
  "tools": {"sway": "1.10.1", "wf-recorder": "0.4.1", "ffmpeg": "6.1.1"},
  "source": "demovid"
}
```

- `output.x/y` are the output's position in Sway's global layout; all event coordinates are already output-relative (see below), these exist only for debugging.
- `streams.cam.preview_rect` is `[x, y, w, h]` of the live preview window in output px — `render` paints the clean PiP over exactly that region. Omit if no preview was shown.
- `cursor_source` is one of `ext-image-copy-capture`, `layer-shell-anchor`, `evdev-dead-reckoning`, `screenix`, `none`. `render` uses it to decide how much to trust `cursor` events (e.g. dead-reckoning may need drift correction).
- `source` is `demovid` or `screenix` (imported). Imported manifests fill what they can; missing streams are omitted, not nulled.

## events.jsonl

Every line has `t` (float seconds) and `kind`. Coordinates are **output pixels, origin top-left of the recorded output** — identical to `screen.mp4` pixel coordinates at scale 1. `rec` subtracts `output.x/y` from Sway's global rects before writing.

| kind | fields | notes |
|---|---|---|
| `cursor` | `x, y` | pointer position; ~120 Hz from the cursor session, whatever the source gives otherwise |
| `button` | `button, state, x, y` | `button` ∈ `left, right, middle, side, extra`; `state` ∈ `down, up`; `x, y` = last known cursor position or `null` |
| `key` | `key, state, mods` | `key` = evdev name lowercased without `KEY_` (`a`, `enter`, `leftshift`); `mods` = subset of `["ctrl","alt","shift","super"]` held at the time |
| `focus` | `con_id, app_id, title, rect` | Sway focus changed to this window; `rect` = `[x, y, w, h]` output px; `app_id` may be `null` for Xwayland (then `class` is present) |
| `window` | `change, con_id, rect` | `change` ∈ `new, close, move, resize, fullscreen, floating` for the focused window only |
| `mark` | `label` | manual marker (`clap`, `chapter`, …) — used for sync checks and trims |
| `stream` | `stream, event` | `event` ∈ `first_frame, stopped` — the raw evidence behind `offset_s`; render ignores these |

Rules:
- `render` must tolerate unknown `kind`s (skip them) and missing optional fields.
- The zoom planner is a pure function `list[event] → list[keyframe]`; it never opens a video.
- Keyframe shape (planner output, not on disk): `{t, cx, cy, zoom}` — centre in output px, `zoom ≥ 1`. The renderer interpolates between keyframes; the planner decides easing by emitting enough of them.

## Screenix import

`~/Videos/screenix/recording_<ts>/` has `cursor.json` (`[{timestamp, x, y}]` px), `*.mp4.meta.json` (clicks normalised 0–1), a screen mp4 and a camera mp4. `import-screenix` writes a v1 dir with `source: "screenix"`, `cursor_source: "screenix"`, remuxed (not re-encoded) streams, `offset_s: 0`, and `cursor`/`button` events only. No `key`/`focus` events exist for imports — the planner must still produce something sensible from clicks alone.
