# Demovid — CLI screen recorder for product demos (Linux / Sway / NVIDIA)

Screen Studio, but for Yousef's machine: record the whole output at OS level, then render auto-zoom on clicks/focus, an enlarged smoothed cursor, a webcam PiP, and loudness-normalised audio. CLI only, single user. Project page + working docs live in the vault: `~/sync/brain/root/projects/demovid/` (`repo` symlinks back here; `index.md` = public brief, `research.md` = tool/lib survey with verified facts, `roadmap.md` = milestones + open decisions, `board.md` = kanban). Read `research.md` before touching capture code — most of the hard constraints are there.

## Git: commit to `main`, never branch
Trunk-based like Console. No feature branches, no worktree branches that outlive the work — fold into `main` and delete. Ticket forks land on `main` too.

## Architecture (decided)
**Record raw + an event log; render afterwards.** Live effects are a dead end (bake decisions in, and the compositor gives a live client nothing to work with).

```
demovid rec      → ~/Videos/demovid/<ts>/{manifest.json, screen.mp4, cam.mp4, mic.flac, events.jsonl}
demovid render   → events → zoom plan (pure fn) → composite → NVENC mp4
demovid upload   → public URL on the clipboard (wl-copy)
```
All artefacts share one monotonic start clock from `manifest.json`; every event and stream is `t`-relative to it. Never write raw video intermediates — `/home` sits at ~93 %; encode (NVENC) at capture time.

## Hard constraints of this machine (verified 2026-09-03, re-verify if the Sway build changes)
- **Sway 1.10.1 + wlroots 0.18.2, built from tarballs in `~/src/`**, run with `--unsupported-gpu` on a GTX 1060 (driver 580). Only `zwlr_screencopy_v1` is available. `ext-image-copy-capture-v1` (the protocol that streams cursor position via a pointer-cursor session) needs **wlroots ≥ 0.19 / Sway ≥ 1.11** — an upgrade Yousef may do (roadmap M0). Check what's live before assuming either: `strings /usr/local/lib/x86_64-linux-gnu/libwlroots-*.so | grep -c ext_image_copy_capture`.
- **The cursor is software-composited** → it is in every screencopy frame regardless of `overlay_cursor`, and the portal's "hidden" mode can't remove it. To record without it, swap the seat's xcursor theme for a blank one (`swaymsg seat seat0 xcursor_theme <blank> 24`) and restore on stop. Current theme: `Adwaita 24`.
- **No global pointer position from Wayland.** Options, best first: (1) `ext-image-copy-capture` cursor session (post-upgrade); (2) libinput/evdev deltas re-anchored by a transparent layer-shell surface's `wl_pointer.enter` (framepipe's `gayland` technique — GPL-3, copy the idea only); (3) pure evdev dead-reckoning (what Screenix does; drifts — needs `accel_profile flat`, a centre warp at start, and still breaks). Clicks and keys are trivially reliable via evdev either way: `amar` is in `input`, mouse = `/dev/input/event2`.
- **NVIDIA encode works through ffmpeg NVENC**; `wf-recorder -c h264_nvenc -p preset=p4 -p rc=vbr -p cq=20 -r 60 -f out.mp4` is the verified capture command (1080p60 clean). No VAAPI here (`wl-screenrec` is useless), `av1_nvenc` is listed but the 1060 has no AV1 encoder. gpu-screen-recorder (KMS capture + CUDA/NVENC) is the upgrade if screencopy drops frames — not packaged for Ubuntu 24.04, flatpak/source only.
- **xdg-desktop-portal-wlr rejects cursor mode METADATA** → PipeWire/portal streams can never be the position source on wlroots.
- Single output `HDMI-A-1` 1920×1080@144, scale 1. Webcam = Logitech C615 `/dev/video0` (720p/1080p). Default mic = the C615's mono capture. It sat at **53 % (hardware gain 6/18, +15 dB)** until 2026-09-03 — most of the "too quiet" complaint; now **85 % (hw 15/18, +28.5 dB)**, calibrated on his voice 2026-09-03 (peaks ≈ −6 dBFS, raw ≈ −27 LUFS — `loudnorm` at render does the rest), persisted by WirePlumber in `~/.local/state/wireplumber/default-routes`; if it drifts back down suspect a browser WebRTC AGC (`--disable-features=WebRtcAllowInputVolumeAdjustment` for Brave). Render still applies `loudnorm`.
- Sway IPC is a superpower the Mac tools lack: `window` focus events + `get_tree` give exact window rects → zoom-to-focused-window, not a blind box around the pointer.

## Conventions
- Language/toolchain: see roadmap decision 2 (Python 3.12 + `uv` recommended; not yet chosen). Whatever is chosen: ffmpeg/wf-recorder/pw-record are subprocesses, never re-implemented.
- The zoom planner is a pure function `events → keyframes`; keep it that way and unit-test it. The renderer consumes keyframes, never raw events.
- Screenix import: `~/Videos/screenix/recording_<ts>/` has `cursor.json` (`{timestamp,x,y}` px @~120 Hz), `*.mp4.meta.json` (click events normalised 0–1), screen + camera mp4s — a free regression corpus; keep `demovid import-screenix` working.
- Never test against Yousef's live session in a way that mutates it. Recording his screen for a few seconds to `/tmp` is fine; changing seat/input settings must always be paired with a restore, and a crashed `rec` must leave a `demovid doctor --restore` path (cursor theme, mic gain, accel profile).
- Verify by artefact, not by claim: `ffprobe` the outputs (codec, fps, frame count), diff `events.jsonl` timestamps against the clip, frame-step a click in the render.

## Reference projects (see research.md for the full table)
- framepipe (Rust, Wayland-only, GPL-3) — tracking technique + GStreamer pipeline; no NVENC.
- screenstudio-alt-skill (Python, MIT) — headless events→render pipeline closest to ours; Mac logger, portable renderer.
- Screenix — the incumbent; its data layout is our import format.
- openscreen / Beam / Cap — editor/effect references; none track the cursor on Linux, Cap has no Linux at all.
