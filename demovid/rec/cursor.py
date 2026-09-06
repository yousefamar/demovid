import hashlib
import mmap
import os
import select
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np
from pywayland.client import Display
from pywayland.protocol.ext_image_capture_source_v1 import ExtOutputImageCaptureSourceManagerV1
from pywayland.protocol.ext_image_copy_capture_v1 import ExtImageCopyCaptureManagerV1
from pywayland.protocol.wayland import WlOutput, WlSeat, WlShm

from . import xcursor
from .clock import Clock
from .events import EventLog

SOURCE_NAME = "ext-image-copy-capture"
MIN_INTERVAL_S = 1 / 240
SHAPES_DIR = "cursors"
FAILED_BUFFER_CONSTRAINTS = 1
FAILED_STOPPED = 2


def advertised_globals() -> set[str]:
    display = Display()
    try:
        display.connect()
    except Exception:
        return set()
    names: set[str] = set()
    registry = display.get_registry()
    registry.dispatcher["global"] = lambda reg, name, iface, ver: names.add(iface)
    display.roundtrip()
    display.disconnect()
    return names


def available() -> bool:
    return {ExtImageCopyCaptureManagerV1.name, ExtOutputImageCaptureSourceManagerV1.name} <= advertised_globals()


def shape_id(bgra: np.ndarray) -> str:
    return hashlib.sha1(bgra.tobytes()).hexdigest()[:10]


def unpremultiply(bgra: np.ndarray) -> np.ndarray:
    """wl_shm ARGB8888 is premultiplied; PNGs want straight alpha."""
    out = bgra.astype(np.float32)
    a = out[..., 3:4]
    out[..., :3] = np.where(a > 0, np.clip(out[..., :3] * 255.0 / np.maximum(a, 1), 0, 255), 0)
    return np.rint(out).astype(np.uint8)


class CursorSession(threading.Thread):
    """Streams pointer position + cursor image over ext-image-copy-capture-v1's pointer cursor session.

    wlroots only reports a *hardware* cursor here (types/ext_image_capture_source_v1/output.c), so the
    compositor must run without WLR_NO_HARDWARE_CURSORS and no other client may hold a screencopy with
    overlay_cursor (that forces a software cursor and we get `leave`). `hw_cursor_seen` tells the caller
    whether anything ever arrived.
    """

    def __init__(self, log: EventLog, clock: Clock, output_name: str, scale: float = 1.0,
                 shapes_dir: Path | None = None, theme: str | None = None, theme_size: int = 24):
        super().__init__(name="cursor-session", daemon=True)
        self.log = log
        self.clock = clock
        self.output_name = output_name
        self.scale = scale
        self.shapes_dir = shapes_dir
        self.theme = theme
        self.theme_size = theme_size
        self._theme_index: dict[tuple[int, int], str] = {}
        self.position: tuple[int, int] | None = None
        self.hotspot: tuple[int, int] = (0, 0)
        self.hw_cursor_seen = False
        self.entered = False
        self.shape: str | None = None
        self.shapes_written = 0
        self.error: str | None = None
        self.stop_event = threading.Event()
        self._last_emit = -1.0
        self._pending_hotspot: tuple[int, int] | None = None
        self._outputs: dict = {}
        self._copy_mgr = None
        self._src_mgr = None
        self._seat = None
        self._shm = None
        self._capture = None
        self._frame = None
        self._buf = None
        self._pool = None
        self._mm: mmap.mmap | None = None
        self._backing = None
        self._size: tuple[int, int] = (0, 0)
        self._buf_size: tuple[int, int] = (0, 0)
        self._format: int | None = None
        self._want_frame = False
        self._retry_at = 0.0

    def _on_global(self, registry, name, interface, version):
        if interface == ExtImageCopyCaptureManagerV1.name:
            self._copy_mgr = registry.bind(name, ExtImageCopyCaptureManagerV1, 1)
        elif interface == ExtOutputImageCaptureSourceManagerV1.name:
            self._src_mgr = registry.bind(name, ExtOutputImageCaptureSourceManagerV1, 1)
        elif interface == WlSeat.name and self._seat is None:
            self._seat = registry.bind(name, WlSeat, min(version, 7))
        elif interface == WlShm.name:
            self._shm = registry.bind(name, WlShm, 1)
        elif interface == WlOutput.name:
            out = registry.bind(name, WlOutput, min(version, 4))
            self._outputs[out] = None
            if version >= 4:
                out.dispatcher["name"] = lambda o, n: self._outputs.__setitem__(o, n)

    def start(self) -> None:
        self.display = Display()
        if self.theme is not None and self.shapes_dir is not None:
            try:
                self._theme_index = xcursor.index_theme(self.theme, self.theme_size)
            except OSError:
                self._theme_index = {}
        self.display.connect()
        registry = self.display.get_registry()
        registry.dispatcher["global"] = self._on_global
        self.display.roundtrip()
        self.display.roundtrip()
        if not (self._copy_mgr and self._src_mgr):
            self.display.disconnect()
            raise RuntimeError("compositor lacks ext-image-copy-capture-v1 (needs Sway >= 1.11)")
        if not self._seat:
            raise RuntimeError("no wl_seat")
        output = next((o for o, n in self._outputs.items() if n == self.output_name), None)
        if output is None:
            if len(self._outputs) != 1:
                raise RuntimeError(f"output {self.output_name!r} not found among {list(self._outputs.values())}")
            output = next(iter(self._outputs))
        self._pointer = self._seat.get_pointer()
        self._source = self._src_mgr.create_source(output)
        self._session = self._copy_mgr.create_pointer_cursor_session(self._source, self._pointer)
        self._session.dispatcher["enter"] = self._on_enter
        self._session.dispatcher["leave"] = self._on_leave
        self._session.dispatcher["position"] = self._on_position
        self._session.dispatcher["hotspot"] = self._on_hotspot
        if self.shapes_dir is not None and self._shm is not None:
            self._capture = self._session.get_capture_session()
            self._capture.dispatcher["buffer_size"] = self._on_buffer_size
            self._capture.dispatcher["shm_format"] = self._on_shm_format
            self._capture.dispatcher["dmabuf_device"] = lambda s, dev: None
            self._capture.dispatcher["dmabuf_format"] = lambda s, fmt, mods: None
            self._capture.dispatcher["done"] = self._on_done
            self._capture.dispatcher["stopped"] = self._on_stopped
        self.display.flush()
        super().start()

    # --- pointer session -------------------------------------------------------------------------

    def _on_enter(self, session):
        self.entered = True
        self.hw_cursor_seen = True
        self.log.emit("cursor_visible", self.clock.now(), visible=True)

    def _on_leave(self, session):
        self.entered = False
        self.position = None
        self.log.emit("cursor_visible", self.clock.now(), visible=False)

    def _on_hotspot(self, session, x, y):
        # effective at the next frame `ready`; until then the previous image + hotspot pair stands
        self._pending_hotspot = (x, y)
        self.hotspot = (x, y)
        self._resolve_theme_shape(x, y)

    def _resolve_theme_shape(self, hx: int, hy: int) -> None:
        """Name the shape from its hotspot and save the theme's art for it.

        Only used when the compositor cannot hand us the picture (software cursor -> no cursor swapchain
        -> `buffer_size 0x0`), which on this machine is always. See demovid/rec/xcursor.py."""
        if self.theme is None or self.shapes_dir is None or self._buf_size != (0, 0):
            return
        key = (round(hx / self.scale), round(hy / self.scale))
        name = self._theme_index.get(key)
        if name is None:
            return
        art = xcursor.load_shape(self.theme, name, self.theme_size)
        if art is None:
            return
        w, h, xhot, yhot, bgra = art
        sid = shape_id(bgra)
        if sid == self.shape:
            return
        self.shape = sid
        path = self.shapes_dir / f"{sid}.png"
        if not path.exists():
            import cv2

            self.shapes_dir.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(path), unpremultiply(bgra))
            self.shapes_written += 1
        self.hotspot = (hx, hy)
        self.log.emit("cursor_shape", self.clock.now(), id=sid, w=w, h=h, hotspot=[xhot, yhot],
                      scale=1.0, name=name, source="theme")

    def _on_position(self, session, x, y):
        t = self.clock.now()
        self.hw_cursor_seen = True
        pos = (round(x / self.scale), round(y / self.scale))
        self.position = pos
        if t - self._last_emit >= MIN_INTERVAL_S:
            self._last_emit = t
            self.log.emit("cursor", t, x=pos[0], y=pos[1])

    # --- cursor image capture --------------------------------------------------------------------

    def _on_buffer_size(self, session, width, height):
        self._size = (width, height)

    def _on_shm_format(self, session, fmt):
        if fmt in (WlShm.format.argb8888.value, WlShm.format.xrgb8888.value) and self._format is None:
            self._format = fmt

    def _on_done(self, session):
        if self._size[0] <= 0 or self._size[1] <= 0 or self._format is None:
            return
        if self._size != self._buf_size:
            self._alloc_buffer(*self._size)
        self._want_frame = True

    def _on_stopped(self, session):
        self._capture = None
        self._want_frame = False

    def _alloc_buffer(self, w: int, h: int) -> None:
        self._release_buffer()
        stride = w * 4
        size = stride * h
        # not os.memfd_create: uv's python-build-standalone targets a glibc without it
        self._backing = tempfile.TemporaryFile(prefix="demovid-cursor-", dir="/dev/shm" if os.path.isdir("/dev/shm") else None)
        fd = self._backing.fileno()
        os.ftruncate(fd, size)
        self._mm = mmap.mmap(fd, size)
        self._pool = self._shm.create_pool(fd, size)
        self._buf = self._pool.create_buffer(0, w, h, stride, self._format)
        self._buf_size = (w, h)

    def _release_buffer(self) -> None:
        for obj in (self._buf, self._pool):
            if obj is not None:
                obj.destroy()
        self._buf = self._pool = None
        if self._mm is not None:
            self._mm.close()
            self._mm = None
        if self._backing is not None:
            self._backing.close()
            self._backing = None
        self._buf_size = (0, 0)

    def _request_frame(self) -> None:
        self._want_frame = False
        w, h = self._buf_size
        self._frame = self._capture.create_frame()
        self._frame.dispatcher["transform"] = lambda f, tr: None
        self._frame.dispatcher["damage"] = lambda f, x, y, w_, h_: None
        self._frame.dispatcher["presentation_time"] = lambda f, hi, lo, ns: None
        self._frame.dispatcher["ready"] = self._on_ready
        self._frame.dispatcher["failed"] = self._on_failed
        self._frame.attach_buffer(self._buf)
        self._frame.damage_buffer(0, 0, w, h)
        self._frame.capture()

    def _on_ready(self, frame):
        t = self.clock.now()
        w, h = self._buf_size
        self._mm.seek(0)
        bgra = np.frombuffer(self._mm.read(w * h * 4), np.uint8).reshape(h, w, 4).copy()
        if self._pending_hotspot is not None:
            self.hotspot = self._pending_hotspot
        sid = shape_id(bgra)
        if sid != self.shape:
            self.shape = sid
            path = self.shapes_dir / f"{sid}.png"
            if not path.exists():
                import cv2

                self.shapes_dir.mkdir(exist_ok=True)
                cv2.imwrite(str(path), unpremultiply(bgra))
                self.shapes_written += 1
            self.log.emit("cursor_shape", t, id=sid, w=w, h=h, hotspot=list(self.hotspot), scale=self.scale)
        frame.destroy()
        self._frame = None
        self._want_frame = True

    def _on_failed(self, frame, reason):
        frame.destroy()
        self._frame = None
        if reason == FAILED_STOPPED:
            self._capture = None
        elif reason == FAILED_BUFFER_CONSTRAINTS:
            self._buf_size = (0, 0)  # next `done` reallocates
        else:
            self._retry_at = time.monotonic() + 0.5
            self._want_frame = True

    # --- loop ------------------------------------------------------------------------------------

    def run(self) -> None:
        fd = self.display.get_fd()
        try:
            while not self.stop_event.is_set():
                if self._want_frame and self._capture is not None and self._frame is None \
                        and self._buf is not None and time.monotonic() >= self._retry_at:
                    self._request_frame()
                self.display.flush()
                ready, _, _ = select.select([fd], [], [], 0.25)
                if ready:
                    self.display.dispatch(block=True)
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
            print(f"cursor session died: {self.error}", file=sys.stderr, flush=True)
        finally:
            try:
                if self._frame is not None:
                    self._frame.destroy()
                if self._capture is not None:
                    self._capture.destroy()
                self._release_buffer()
                self._session.destroy()
                self.display.flush()
                self.display.disconnect()
            except Exception:
                pass

    def stop(self) -> None:
        self.stop_event.set()
        self.join(timeout=2)
