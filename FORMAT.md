# Recording directory format (v1)

`demovid rec` writes it, `demovid import-screenix` synthesises it, `demovid render` consumes it. This file is the contract between them — change it here first, bump `version`, and keep `render` reading every version ever written.

```
~/Videos/demovid/<YYYY-MM-DD-HH-mm-ss>/
  manifest.json
  screen.mp4     full output, NVENC h264, 60 fps
  cam.mp4        webcam, NVENC h264, 720p30 or 1080p30 (absent if no camera)
  mic.flac       mono, 48 kHz (absent if no mic)
  cursors/       <id>.png per distinct cursor image (absent unless the cursor session captured shapes)
  events.jsonl   one JSON object per line, ascending t (except `stream` lines, appended at stop)
```

`render` also writes derived files into the same dir, all safe to delete: `render.mp4` / `preview.mp4`
(+ a sibling `.srt` when `--captions` burned captions in), `still-<t>.png`, and `captions.json` — the
cached whisper transcript, keyed on `mic.flac`'s size+mtime so re-renders never pay for it twice.

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
  "cursor": {"theme": "Adwaita", "size": 24, "hidden_during_rec": true, "shapes_dir": "cursors"},
  "cursor_source": "ext-image-copy-capture",
  "tools": {"sway": "1.10.1", "wf-recorder": "0.4.1", "ffmpeg": "6.1.1"},
  "source": "demovid"
}
```

- `output.x/y` are the output's position in Sway's global layout; all event coordinates are already output-relative (see below), these exist only for debugging.
- `output.workarea` is `[x, y, w, h]` (output-relative) of the output minus bars/panels — a visible workspace's rect in Sway. `demovid/layout.py` measures the camera's position from it, so the PiP floats above waybar instead of on it. Absent on imports and pre-2026-09-06 recordings (then the screen edge is used).
- `streams.cam.preview_rect` is `[x, y, w, h]` of the live preview window in output px. Since 2026-09-06 it is a SQUARE (side a multiple of 16, 0.80 of the PiP side, centred where the PiP goes — `layout.preview_rect`), and `render` hides it under a squircle PiP of `layout.pip_for_preview(preview_rect)` whenever that region is in view; when the zoom moves it out of view the PiP jumps to the same spot in the output corner (`--pip-mode auto`). Omit if no preview was shown.
- `cursor.shapes_dir` names the subdir of cursor PNGs (`null` when none were written). `cursor.hidden_during_rec` is true ONLY when the blank-theme swap (`rec --hide-cursor`) was used: a software cursor is painted into the output by the compositor, so `wf-recorder --no-cursor` suppresses only wf-recorder's own duplicate overlay, never the cursor itself. With `hidden_during_rec: false` the renderer must cover the recorded cursor, which it does by drawing the same shape enlarged about the same hotspot.
- Cursor positions are emitted per output commit, so they are dense (~48/s measured) while the pointer moves and absent while it is still — gaps in the `cursor` stream mean "did not move", not "not recorded".
- `cursor_source` is one of `ext-image-copy-capture`, `layer-shell-anchor`, `evdev-dead-reckoning`, `screenix`, `none`. `render` uses it to decide how much to trust `cursor` events (e.g. dead-reckoning may need drift correction).
- `source` is `demovid` or `screenix` (imported). Imported manifests fill what they can; missing streams are omitted, not nulled.
- `rec` also writes informational extras that `render` may ignore: `streams.mic.source` / `volume_pct` (the PipeWire source and its level at stop — decision 4 says warn, never force), `input_devices` (evdev node names read), `offset_s: null` when a stream never reported a first frame.

## events.jsonl

Every line has `t` (float seconds) and `kind`. Coordinates are **output pixels, origin top-left of the recorded output** — identical to `screen.mp4` pixel coordinates at scale 1. `rec` subtracts `output.x/y` from Sway's global rects before writing.

| kind | fields | notes |
|---|---|---|
| `cursor` | `x, y` | pointer position; ~120 Hz from the cursor session, whatever the source gives otherwise |
| `button` | `button, state, x, y, dev, window?` | `button` ∈ `left, right, middle, side, extra, forward, back, task`; `state` ∈ `down, up`; `x, y` = last known cursor position or `null`; `dev` = evdev device name (informational); `window` (down only, needs a known position) = `{con_id, app_id, class?, rect}` of the smallest window under the pointer per Sway `get_tree` |
| `key` | `key, state, mods, dev` | `key` = evdev name lowercased without `KEY_` (`a`, `enter`, `leftshift`); `mods` = subset of `["ctrl","alt","shift","super"]` held *before* this press (so `leftctrl` down has `mods: []`); key repeats are not logged; absent entirely with `rec --no-keys` |
| `focus` | `con_id, app_id, title, rect` | Sway focus changed to this window; `rect` = `[x, y, w, h]` output px (container rect incl. title bar); `app_id` may be `null` for Xwayland (then `class` is present). `rec` writes one at `t = 0` for the initially focused window and never logs its own preview window |
| `window` | `change, con_id, rect` | `change` ∈ `new, close` for any window; `move, floating, fullscreen` for the focused window only (Sway emits no resize events) |
| `cursor_shape` | `id, w, h, hotspot, scale, name?, source?` | the cursor image changed; `id` names `<shapes_dir>/<id>.png` (straight-alpha BGRA), `hotspot` is `[x, y]` in that image's pixels, `scale` the output scale it was captured at. `source` is `theme` when the picture came from the xcursor theme (identified by hotspot — see demovid/rec/xcursor.py) with `name` the cursor's name in it. The shape in force at time t is the last event at or before t |
| `cursor_visible` | `visible` | the cursor entered (`true`) or left (`false`) the captured output |
| `pause` / `resume` | — | `rec` was paused (the bar button) and resumed. Nothing else stops: every stream keeps running so the clocks stay trivially aligned, and the raw files still contain the span. `render` cuts `[pause, resume)` out (a `TimeMap` segment with speed = ∞, so video, audio and captions all skip it together; `--keep-pauses` disables) and ignores every event inside it when planning zooms. While paused `rec` writes NO `key` or `button` events (that is what pausing is for: passwords). An unterminated `pause` is closed by `rec` at stop |
| `mark` | `label` | manual marker (`clap`, `chapter`, …) — used for sync checks and trims |
| `stream` | `stream, event` | `event` ∈ `first_frame, stopped` — the raw evidence behind `offset_s`, appended when the recording stops (so out of `t` order); render ignores these |

Rules:
- `render` must tolerate unknown `kind`s (skip them) and missing optional fields.
- The zoom planner is a pure function `list[event] → list[keyframe]`; it never opens a video.
- Keyframe shape (planner output, not on disk): `{t, cx, cy, zoom}` — *desired* centre in output px, `zoom ≥ 1`. The renderer smoothsteps between consecutive keyframes (a hold is two equal keyframes) and clamps the centre per frame so the crop stays inside the output — so a zoom-out keeps its target centre and slides into place.

## Webcam PiP vs the live preview window

The preview window is in every frame of `screen.mp4`, so a camera drawn anywhere else shows two cameras. `render --pip-mode auto` (default) therefore paints the camera *into the source frame* over `pip_for_preview(preview_rect)` — a squircle slightly larger than the recorded square window — whenever that region intersects the current crop (always at zoom 1), and only when the zoom has moved it out of view does it draw the camera in the fixed output corner instead (same place, so the hand-over is a small pop, never a double). `--pip-mode scene` always paints into the source (the camera zooms with the content and can leave the frame); `--pip-mode fixed` always uses the corner (imports, or when there was no preview).

## Screenix import

`~/Videos/screenix/recording_<ts>/` has `<name>.cursor.json` (`[{timestamp, x, y}]` px @ ~120 Hz), `<name>.mp4.meta.json` (`click_events` normalised 0–1 + `key_events` with `label`/`pressed`), `<name>.mp4` (screen, with the mic as an AAC track), `<name>.camera.seekable.mp4` (Screenix's indexed re-encode; the raw `.camera.mp4` is 10× larger and unindexed). `import-screenix` writes a v1 dir with `source: "screenix"`, `cursor_source: "screenix"`, **hardlinked** `screen.mp4` / `cam.mp4` (zero extra disk; render only reads the video track of `screen.mp4`), `mic.flac` extracted from the screen file's audio, all `offset_s: 0`, and `cursor`/`button`/`key` events. No `focus` events exist for imports, so the planner's typing target falls back to the cursor position.
