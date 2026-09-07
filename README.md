# demovid

Screen Studio for Linux, as a CLI. Record your whole screen once, then render a product demo with auto-zoom on the things you click, an enlarged cursor, a webcam bubble, captions, and normalised audio. Built for one machine (Sway + wlroots + NVIDIA) by someone who could not get any of the existing tools to work on it. MIT.

```
demovid rec                       # toggle: start, and later stop (or bind it to a key / the bar button)
demovid render ~/Videos/demovid/2026-09-06-16-42-36
demovid upload ~/Videos/demovid/2026-09-06-16-42-36/render.mp4   # YouTube, URL on the clipboard
```

## What you get

- **Record raw, render later.** `rec` captures the full output at 60 fps (NVENC), the webcam, the mic, and an event log: every cursor position, click, keystroke, window focus change and cursor shape, all on one clock. Nothing is baked in at capture time, so any render setting can be changed afterwards for free.
- **Auto-zoom that knows the future.** The zoom starts half a second *before* a click, holds while you work, follows typing to the field you clicked into, fits the focused window when that reads better, and never bounces out and back in.
- **Exact cursor tracking on Wayland.** Positions come from the compositor's `ext-image-copy-capture` pointer-cursor session (pixel-exact, ~140 samples/s while moving), not from dead-reckoning mouse deltas. The real cursor shape (arrow, hand, text) is redrawn enlarged from the theme's own 96 px art, with a click ripple.
- **Webcam bubble** in a fixed corner, squircle-masked, sitting above your bar rather than on it. The live preview window you see while recording is erased from the frame at render time.
- **Pause** from the bar button. Paused spans are cut from the output; keys and clicks are not logged while paused.
- **Idle speed-up, captions (whisper), keystroke chips, padded background frame, region/window crops, presets.** All optional, all flags.
- **Upload** to YouTube with a resumable upload, URL copied with `wl-copy`.
- **`demovid doctor`** tells you what is missing or misconfigured before a recording fails.

## Requirements

This is a personal tool and the capture side is tied to a specific stack. It will not work out of the box elsewhere without reading `CLAUDE.md`, which documents every machine constraint and workaround.

- Linux, **Sway ≥ 1.11 / wlroots ≥ 0.19** (for the cursor-position protocol; on 1.10 it records without positions), waybar, fuzzel (for the menu)
- `wf-recorder` (patched build with `--no-cursor`, see `CLAUDE.md`), `ffmpeg` with NVENC (`h264_nvenc`), `gst-launch-1.0` with `waylandsink` (live preview), `pactl`, `notify-send`, `jq`
- Python 3.12 + [uv](https://docs.astral.sh/uv/)
- Your user in the `input` group (clicks and keys are read from evdev)
- Optional: an OpenAI key for `--captions` (`OPENAI_API_KEY` or `~/.config/demovid/openai_key`), a Google OAuth desktop client for `upload`

## Install

```
git clone https://github.com/yousefamar/demovid && cd demovid
uv sync --group dev
uv run pytest                       # 130 tests, no hardware needed
uv tool install -e .                # → ~/.local/bin/demovid
demovid doctor                      # every prerequisite, green or with the fix
```

Bar button: point a waybar `custom` module at `scripts/waybar-demovid` (`return-type: json`, `signal: 9`, `on-click: demovid menu --click`). A Sway keybind is just `bindsym $mod+Shift+r exec ~/.local/bin/demovid rec`.

## Using it

**Record.** `demovid rec` starts; run it again to stop. Output goes to `~/Videos/demovid/<timestamp>/` as `screen.mp4`, `cam.mp4`, `mic.flac`, `events.jsonl`, `manifest.json` (see [FORMAT.md](FORMAT.md)). From the bar: click to open the menu and start; while recording, one click pauses; while paused, the menu offers Resume or Stop. `--no-keys` if you will type a password.

**Render.** `demovid render <dir>` writes `render.mp4` next to the sources. Useful flags:

| | |
|---|---|
| `--preview` | half size, 30 fps, about 2× realtime, for checking |
| `--still 19.7` | one PNG at that second, the frame-step tool |
| `--spans`, `--keep 1`, `--drop 2` | list the takes between pauses and pick which to render |
| `--from 1:05 --to 2:30` | trim on the recording clock |
| `--zoom 1.8 --zoom-hold 2` | how far in, and how long to stay after the last action |
| `--no-zoom`, `--no-cursor`, `--no-pip`, `--no-chips`, `--no-audio` | switch effects off |
| `--pip-pos bl`, `--pip-shape rounded` | camera corner and shape |
| `--idle-speed 8` | compress stretches with no input and no speech |
| `--captions` | whisper transcript burnt in, SRT written beside the mp4 |
| `--pad 0.06 --bg '#1c1b33,#4b2a7a'` | padded gradient frame |
| `--crop X,Y,W,H`, `--window "Brave"` | render a region only |
| `--preset studio\|framed\|clean\|talk\|social` | flag bundles; `--list-presets`; your own in `~/.config/demovid/presets.toml` |

The bar menu has a **Render settings** submenu that cycles the common knobs (zoom, camera, captions, padding, ...) and remembers them in `~/.config/demovid/render.json`. Precedence is: typed flag > menu setting > preset > default.

**Upload.** `demovid upload --auth` once (browser consent), then `demovid upload <mp4> [--title ... --privacy unlisted]`. `--list`, `--delete <id|url>`. Note that Google locks uploads from unaudited API projects created after mid-2020 to private; `upload` exits 3 if the returned privacy differs from what you asked for.

**Import Screenix recordings.** `demovid import-screenix ~/Videos/screenix/recording_<ts>` converts an existing Screenix take into the same layout so it renders through the same pipeline.

## How it works

`rec` (`demovid/rec/`) runs wf-recorder, ffmpeg (v4l2 and pulse) as subprocesses, measures each stream's first-frame time against one monotonic clock, and logs events from evdev, Sway IPC and the Wayland cursor session. `render` (`demovid/render/`) is a pure planner (`events → keyframes`, unit-tested) feeding an OpenCV compositor (one sub-pixel `warpAffine` per frame, then cursor, ripples, chips, camera) between ffmpeg decode and NVENC encode pipes, with a `TimeMap` as the single source→output clock for frames, audio and captions. `layout.py` holds the one set of numbers that puts the live preview window and the rendered camera in the same place.

`CLAUDE.md` is the working notebook: architecture decisions, the hard constraints of this machine (software cursor, explicit-sync leak, the wlroots patch), and the traps found along the way. Read it before touching capture code.

## Status

Working daily on the author's machine as of September 2026. Six milestones shipped; open threads are listed at the bottom of `CLAUDE.md`. Issues and PRs are welcome but expect the capture side to need adapting to your compositor.
