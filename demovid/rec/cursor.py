import select
import threading

from pywayland.client import Display
from pywayland.protocol.ext_image_capture_source_v1 import ExtOutputImageCaptureSourceManagerV1
from pywayland.protocol.ext_image_copy_capture_v1 import ExtImageCopyCaptureManagerV1
from pywayland.protocol.wayland import WlOutput, WlSeat

from .clock import Clock
from .events import EventLog

SOURCE_NAME = "ext-image-copy-capture"
MIN_INTERVAL_S = 1 / 240


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


class CursorSession(threading.Thread):
    """Streams the pointer position over ext-image-copy-capture-v1's pointer cursor session.

    Written against the protocol spec (wlroots >= 0.19 / Sway >= 1.11) using pywayland's bundled
    bindings; on older compositors `start()` raises and the recorder falls back to cursor_source "none".
    """

    def __init__(self, log: EventLog, clock: Clock, output_name: str, scale: float = 1.0):
        super().__init__(name="cursor-session", daemon=True)
        self.log = log
        self.clock = clock
        self.output_name = output_name
        self.scale = scale
        self.position: tuple[int, int] | None = None
        self.hotspot: tuple[int, int] = (0, 0)
        self.stop_event = threading.Event()
        self._last_emit = -1.0
        self._outputs: dict = {}
        self._copy_mgr = None
        self._src_mgr = None
        self._seat = None

    def _on_global(self, registry, name, interface, version):
        if interface == ExtImageCopyCaptureManagerV1.name:
            self._copy_mgr = registry.bind(name, ExtImageCopyCaptureManagerV1, 1)
        elif interface == ExtOutputImageCaptureSourceManagerV1.name:
            self._src_mgr = registry.bind(name, ExtOutputImageCaptureSourceManagerV1, 1)
        elif interface == WlSeat.name and self._seat is None:
            self._seat = registry.bind(name, WlSeat, min(version, 7))
        elif interface == WlOutput.name:
            out = registry.bind(name, WlOutput, min(version, 4))
            self._outputs[out] = None
            if version >= 4:
                out.dispatcher["name"] = lambda o, n: self._outputs.__setitem__(o, n)

    def start(self) -> None:
        self.display = Display()
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
        self._session.dispatcher["enter"] = lambda s: None
        self._session.dispatcher["leave"] = self._on_leave
        self._session.dispatcher["position"] = self._on_position
        self._session.dispatcher["hotspot"] = self._on_hotspot
        self.display.flush()
        super().start()

    def _on_leave(self, session):
        self.position = None

    def _on_hotspot(self, session, x, y):
        self.hotspot = (x, y)

    def _on_position(self, session, x, y):
        t = self.clock.now()
        pos = (round(x / self.scale), round(y / self.scale))
        self.position = pos
        if t - self._last_emit >= MIN_INTERVAL_S:
            self._last_emit = t
            self.log.emit("cursor", t, x=pos[0], y=pos[1])

    def run(self) -> None:
        fd = self.display.get_fd()
        try:
            while not self.stop_event.is_set():
                self.display.flush()
                ready, _, _ = select.select([fd], [], [], 0.25)
                if ready:
                    self.display.dispatch(block=True)
        except Exception:
            pass
        finally:
            try:
                self._session.destroy()
                self.display.flush()
                self.display.disconnect()
            except Exception:
                pass

    def stop(self) -> None:
        self.stop_event.set()
        self.join(timeout=2)
