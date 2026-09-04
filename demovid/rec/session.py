import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import audio, blankcursor, cursor, sway
from .clock import Clock
from .events import EventLog
from .inputs import InputLogger
from .state import clear_state, log_path, write_state
from .streams import (Preview, Stream, cam_stream, mic_stream, screen_stream, tool_versions, unsuspend_source,
                      wait_first_frames, wf_supports_no_cursor)

FIRST_FRAME_TIMEOUT_S = 8.0
MIC_WAKE_S = 0.4


@dataclass
class RecOptions:
    out_root: Path = Path("~/Videos/demovid").expanduser()
    output: str | None = None
    fps: int = 60
    cq: int = 20
    cam_device: str | None = "/dev/video0"
    cam_size: tuple[int, int] = (1280, 720)
    cam_fps: int = 30
    mic_source: str | None = "default"
    preview: bool = True
    preview_size: tuple[int, int] = (384, 216)
    preview_margin: int = 24
    hide_cursor: bool = False
    log_keys: bool = True
    notify: bool = True


@dataclass
class Session:
    opts: RecOptions
    stop_event: threading.Event = field(default_factory=threading.Event)
    error: str | None = None

    def log(self, msg: str) -> None:
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)

    def notify(self, title: str, body: str = "", ms: int = 2500) -> None:
        if not self.opts.notify or not shutil.which("notify-send"):
            return
        subprocess.Popen(
            ["notify-send", "-a", "demovid", "-t", str(ms), "-h", "string:x-canonical-private-synchronous:demovid",
             title, body],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    def run(self) -> Path:
        o = self.opts
        self.clock = Clock.start()
        started = datetime.fromtimestamp(self.clock.epoch_s).astimezone()
        self.dir = o.out_root / started.strftime("%Y-%m-%d-%H-%M-%S")
        self.dir.mkdir(parents=True, exist_ok=False)
        self.events = EventLog(self.dir / "events.jsonl", self.clock)

        self.conn = sway.connect()
        self.output = sway.pick_output(self.conn, o.output)
        self.sway_version = self.conn.get_version().human_readable

        # The cursor session only reports a HARDWARE cursor, and wf-recorder's cursor overlay forces a
        # software one — so exact positions and a baked-in cursor are mutually exclusive. Start the
        # session first: if it tracks, record without the overlay and let render draw the cursor.
        self.cursor_session: cursor.CursorSession | None = None
        self.cursor_source = "none"
        self.shapes_dir = self.dir / cursor.SHAPES_DIR
        try:
            cs = cursor.CursorSession(self.events, self.clock, self.output.name, self.output.scale,
                                      shapes_dir=self.shapes_dir)
            cs.start()
            self.cursor_session = cs
        except Exception as e:
            self.log(f"cursor session unavailable ({e}); cursor_source=none")
        self.tracking = self._wait_for_cursor()
        if self.tracking:
            self.cursor_source = cursor.SOURCE_NAME
        overlay_cursor = not self.tracking
        if self.tracking and not wf_supports_no_cursor():
            self.log("wf-recorder has no --no-cursor (unpatched build): recording the cursor overlay, which "
                     "forces a software cursor and will stop position tracking")
            overlay_cursor = True

        self.streams: list[Stream] = [
            screen_stream(self.dir, self.output.name, o.fps, cq=o.cq, overlay_cursor=overlay_cursor)]
        cam_dev = o.cam_device if o.cam_device and Path(o.cam_device).exists() else None
        if o.cam_device and not cam_dev:
            self.log(f"camera {o.cam_device} not present, skipping cam")
        if cam_dev:
            self.streams.append(cam_stream(
                self.dir, self.clock, cam_dev, *o.cam_size, o.cam_fps, o.preview_size if o.preview else None,
            ))
        self.mic_source = None
        if o.mic_source:
            self.mic_source = audio.default_source() if o.mic_source == "default" else o.mic_source
            if self.mic_source:
                unsuspend_source(self.mic_source)
                self.streams.append(mic_stream(self.dir, self.clock, self.mic_source))
            else:
                self.log("no default audio source, skipping mic")

        self.preview_rect: list[int] | None = None
        self._preview_placed = False
        self.preview: Preview | None = None
        self.sway_logger = sway.SwayLogger(self.conn, self.events, self.output, on_new_window=self._place_preview)
        self.sway_logger.log_initial_focus()
        self.sway_logger.start()

        for s in self.streams:
            if s.name == "mic":
                time.sleep(max(0.0, MIC_WAKE_S - self.clock.now()))
            s.start(self.clock)
            self.log(f"{s.name}: pid {s.pid}: {' '.join(s.cmd)}")
            if s.name == "cam" and o.preview:
                self._start_preview(s)

        self.inputs = InputLogger(self.events, self.clock, self._cursor_pos, log_keys=o.log_keys,
                                  window_at=self._window_at)
        self.inputs.start()
        self.log(f"evdev: {', '.join(self.inputs.device_names())}")

        self.theme, self.theme_size = sway.current_xcursor_theme()
        self.cursor_hidden = False
        if o.hide_cursor:
            sway.set_xcursor_theme(self.conn, blankcursor.ensure_theme(self.theme_size), self.theme_size)
            self.cursor_hidden = True

        self._write_state()

        missing = wait_first_frames(self.streams, FIRST_FRAME_TIMEOUT_S)
        for s in self.streams:
            if s.offset_s is not None:
                self.log(f"{s.name}: first frame at t={s.offset_s:.3f}s")
        dead = [s.name for s in self.streams if not s.alive()]
        if "screen" in dead:
            self._finish(aborted=True)
            raise RuntimeError("screen capture died at start:\n" + "\n".join(self.streams[0].log))
        for name in dead:
            self.log(f"{name} died at start; continuing without it")
        for name in missing:
            if name not in dead:
                self.log(f"{name}: no first-frame marker after {FIRST_FRAME_TIMEOUT_S}s; offset_s unknown")
        self.write_manifest()

        warn = audio.mic_warning(self.mic_source) if self.mic_source else None
        if warn:
            self.log("WARNING: " + warn)
        self.notify("● REC", warn or f"{self.output.name} {o.fps}fps → {self.dir.name}")

        while not self.stop_event.wait(0.25):
            if not self.streams[0].alive():
                self.error = "screen capture died mid-recording"
                self.log(self.error)
                break
        return self._finish(aborted=False)

    def _cursor_pos(self) -> tuple[int, int] | None:
        return self.cursor_session.position if self.cursor_session else None

    def _wait_for_cursor(self, timeout_s: float = 1.0) -> bool:
        """True once the compositor reports a hardware cursor. It only does so on an output commit,
        so nudge one per poll rather than waiting for the desktop to repaint on its own."""
        cs = self.cursor_session
        if cs is None:
            return False
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if cs.hw_cursor_seen:
                return True
            self.conn.command("nop demovid cursor probe")
            time.sleep(0.05)
        self.log("no hardware cursor reported: WLR_NO_HARDWARE_CURSORS is set (see ~/.local/bin/sway-nvidia) "
                 "or another screencopy client is overlaying the cursor; positions unavailable")
        return False

    def _window_at(self, pos: tuple[int, int] | None) -> dict | None:
        """Smallest window containing the (output-relative) point, for `button.window`."""
        if pos is None:
            return None
        x, y = pos[0] + self.output.x, pos[1] + self.output.y
        best = None
        try:
            for con in self.conn.get_tree().descendants():
                if con.type not in ("con", "floating_con") or not (con.app_id or con.window_class):
                    continue
                r = con.rect
                if r.x <= x < r.x + r.width and r.y <= y < r.y + r.height:
                    if best is None or r.width * r.height < best.rect.width * best.rect.height:
                        best = con
        except Exception:
            return None
        if best is None:
            return None
        return {"con_id": best.id, "app_id": best.app_id,
                **({"class": best.window_class} if best.app_id is None and best.window_class else {}),
                "rect": self.output.relative(best.rect)}

    def _preview_geometry(self) -> tuple[int, int, int, int]:
        pw, ph = self.opts.preview_size
        x = self.output.x + self.output.width - pw - self.opts.preview_margin
        y = self.output.y + self.output.height - ph - self.opts.preview_margin
        return x, y, pw, ph

    def _start_preview(self, cam: Stream) -> None:
        x, y, pw, ph = self._preview_geometry()
        try:
            self.preview = Preview(cam, self.opts.preview_size, self.opts.cam_fps)
            pid = self.preview.spawn()
        except Exception as e:
            self.preview = None
            self.log(f"preview unavailable ({e}); recording without it")
            return
        # the window maps on the first frame; this rule places it before it can ever be tiled
        self.conn.command(
            f"for_window [pid={pid}] floating enable, sticky enable, border none, "
            f"resize set {pw} px {ph} px, move absolute position {x} px {y} px, inhibit_idle visible"
        )
        self.preview.run()

    def _place_preview(self, con) -> None:
        if self._preview_placed or not self.preview or con.pid != self.preview.pid:
            return
        self._preview_placed = True
        self.sway_logger.ignore_ids.add(con.id)
        prev = self.sway_logger.focused_id
        x, y, pw, ph = self._preview_geometry()
        self.conn.command(
            f"[con_id={con.id}] floating enable, sticky enable, border none, "
            f"resize set {pw} px {ph} px, move absolute position {x} px {y} px, inhibit_idle visible"
        )
        if prev:
            self.conn.command(f"[con_id={prev}] focus")
        placed = self.conn.get_tree().find_by_id(con.id)
        if placed:
            self.preview_rect = self.output.relative(placed.rect)
            self.log(f"preview window placed at {self.preview_rect}")

    def _write_state(self) -> None:
        write_state({
            "pid": os.getpid(),
            "dir": str(self.dir),
            "started_at": self.clock.epoch_s,
            "children": {s.name: s.pid for s in self.streams} | (
                {"preview": self.preview.pid} if self.preview and self.preview.pid else {}),
            "restore": {"xcursor_theme": [self.theme, self.theme_size]} if self.cursor_hidden else None,
        })

    def manifest(self, stopped_at: float | None = None) -> dict:
        streams: dict[str, dict] = {}
        for s in self.streams:
            entry: dict = {"file": s.file, "offset_s": None if s.offset_s is None else round(s.offset_s, 6)}
            if s.name == "screen":
                entry["fps"] = self.opts.fps
            elif s.name == "cam":
                entry.update(fps=self.opts.cam_fps, width=self.opts.cam_size[0], height=self.opts.cam_size[1])
                if self.preview_rect:
                    entry["preview_rect"] = self.preview_rect
            elif s.name == "mic":
                entry.update(sample_rate=48000, channels=1, source=self.mic_source,
                             volume_pct=audio.source_volume_pct(self.mic_source) if self.mic_source else None)
            streams[s.name] = entry
        started = datetime.fromtimestamp(self.clock.epoch_s).astimezone()
        m = {
            "version": 1,
            "started_at": started.isoformat(timespec="milliseconds"),
            "stopped_at": None,
            "duration_s": None,
            "monotonic_ns": self.clock.monotonic_ns,
            "output": self.output.as_dict(),
            "streams": streams,
            "cursor": {"theme": self.theme, "size": self.theme_size,
                       "hidden_during_rec": self.cursor_hidden or self.tracking,
                       "shapes_dir": cursor.SHAPES_DIR if self.shapes_dir.is_dir() else None},
            "cursor_source": self.cursor_source,
            "input_devices": self.inputs.device_names(),
            "tools": {"sway": self.sway_version, **tool_versions()},
            "source": "demovid",
        }
        if stopped_at is not None:
            m["stopped_at"] = datetime.fromtimestamp(self.clock.epoch_s + stopped_at).astimezone().isoformat(
                timespec="milliseconds")
            m["duration_s"] = round(stopped_at, 3)
        return m

    def write_manifest(self, stopped_at: float | None = None) -> None:
        (self.dir / "manifest.json").write_text(json.dumps(self.manifest(stopped_at), indent=1) + "\n")

    def _finish(self, aborted: bool) -> Path:
        t_stop = self.clock.now()
        for s in self.streams:
            s.interrupt()
        for s in self.streams:
            if s.offset_s is not None:
                self.events.emit("stream", s.offset_s, stream=s.name, event="first_frame")
            if s.alive():
                self.events.emit("stream", t_stop, stream=s.name, event="stopped")
        for s in self.streams:
            rc = s.wait()
            self.log(f"{s.name}: exit {rc}")
            if rc not in (0, None, -2, 255):
                s.write_log(self.dir / f"{s.name}.log")
        if self.preview:
            self.preview.stop()
        if self.cursor_session:
            self.cursor_session.stop()
        self.inputs.stop()
        self.sway_logger.stop()
        if self.cursor_hidden:
            try:
                sway.set_xcursor_theme(self.conn, self.theme, self.theme_size)
            except Exception as e:
                self.log(f"failed to restore cursor theme: {e}")
        self.events.close()
        if not aborted:
            self.write_manifest(stopped_at=t_stop)
        clear_state()
        try:
            shutil.copy(log_path(), self.dir / "rec.log")
        except OSError:
            pass
        if not aborted:
            self.notify("■ Saved" if not self.error else "■ Stopped (error)", str(self.dir), ms=6000)
        return self.dir
