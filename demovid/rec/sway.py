import glob
import os
import socket
import threading
import time
from dataclasses import dataclass
from typing import Callable

import i3ipc
from i3ipc import Event

from .events import EventLog


def find_socket() -> str | None:
    """SWAYSOCK from the environment if it still answers, else the newest live sway IPC socket."""
    candidates = [p for p in [os.environ.get("SWAYSOCK"), os.environ.get("I3SOCK")] if p]
    run_dir = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    candidates += sorted(glob.glob(f"{run_dir}/sway-ipc.*.sock"), key=os.path.getmtime, reverse=True)
    for path in candidates:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.settimeout(0.5)
            s.connect(path)
            return path
        except OSError:
            continue
        finally:
            s.close()
    return None


def connect() -> i3ipc.Connection:
    path = find_socket()
    if not path:
        raise RuntimeError("no live sway IPC socket found")
    return i3ipc.Connection(socket_path=path)


@dataclass
class Output:
    name: str
    x: int
    y: int
    width: int
    height: int
    scale: float
    refresh_hz: float
    workarea: list[int] | None = None  # [x, y, w, h] output-relative, minus bars/panels

    @classmethod
    def from_ipc(cls, o, conn: i3ipc.Connection | None = None) -> "Output":
        r = o.rect
        mode = o.ipc_data.get("current_mode") or {}
        out = cls(o.name, r.x, r.y, r.width, r.height, float(o.ipc_data.get("scale", 1.0)),
                  round((mode.get("refresh") or 0) / 1000, 3))
        if conn is not None:
            # a visible workspace's rect is the output minus layer-shell exclusive zones (waybar)
            for ws in conn.get_workspaces():
                if ws.output == o.name and ws.visible:
                    out.workarea = out.relative(ws.rect)
                    break
        return out

    def relative(self, rect) -> list[int]:
        return [rect.x - self.x, rect.y - self.y, rect.width, rect.height]

    @property
    def bottom_reserved(self) -> int:
        """Pixels taken by a bar along the bottom edge (0 when unknown or none)."""
        if not self.workarea:
            return 0
        return max(0, self.height - (self.workarea[1] + self.workarea[3]))

    @property
    def right_reserved(self) -> int:
        if not self.workarea:
            return 0
        return max(0, self.width - (self.workarea[0] + self.workarea[2]))

    def as_dict(self) -> dict:
        d = {"name": self.name, "x": self.x, "y": self.y, "width": self.width, "height": self.height,
             "scale": self.scale, "refresh_hz": self.refresh_hz}
        if self.workarea:
            d["workarea"] = self.workarea
        return d


def pick_output(conn: i3ipc.Connection, name: str | None) -> Output:
    outputs = [o for o in conn.get_outputs() if o.active]
    if name:
        match = [o for o in outputs if o.name == name]
        if not match:
            raise RuntimeError(f"output {name!r} not found; have {[o.name for o in outputs]}")
        return Output.from_ipc(match[0], conn)
    focused = [o for o in outputs if o.focused] or outputs
    if not focused:
        raise RuntimeError("no active outputs")
    return Output.from_ipc(focused[0], conn)


def wake_output(conn: i3ipc.Connection, name: str) -> bool:
    """Power a DPMS-blanked output back on. screencopy has nothing to copy while it is off, so
    capture fails outright (swayidle here blanks after 600 s)."""
    match = [o for o in conn.get_outputs() if o.name == name]
    if not match or getattr(match[0], "power", True) is not False:
        return False
    if not all(r.success for r in conn.command(f"output {name} power on")):
        raise RuntimeError(f"could not power on {name}")
    time.sleep(0.5)
    return True


def set_xcursor_theme(conn: i3ipc.Connection, theme: str, size: int) -> None:
    reply = conn.command(f"seat * xcursor_theme {theme} {size}")
    if not all(r.success for r in reply):
        raise RuntimeError(f"xcursor_theme failed: {[r.error for r in reply]}")


def current_xcursor_theme() -> tuple[str, int]:
    """Sway doesn't expose the seat theme over IPC; read the config line, then the env, else Adwaita 24."""
    cfg = os.path.expanduser("~/.config/sway/config")
    try:
        for line in open(cfg):
            parts = line.split()
            if len(parts) >= 5 and parts[0] == "seat" and parts[2] == "xcursor_theme":
                return parts[3], int(parts[4])
    except (OSError, ValueError):
        pass
    return os.environ.get("XCURSOR_THEME", "Adwaita"), int(os.environ.get("XCURSOR_SIZE", "24"))


def _con_fields(con) -> dict:
    return {
        "con_id": con.id,
        "app_id": con.app_id,
        **({"class": con.window_class} if con.app_id is None and con.window_class else {}),
        "title": con.name,
    }


class SwayLogger(threading.Thread):
    """Logs focus/window events (FORMAT.md) and calls `on_new_window(con)` for windows we spawn."""

    WINDOW_CHANGES = {"new": "new", "close": "close", "move": "move", "floating": "floating",
                      "fullscreen_mode": "fullscreen"}

    def __init__(self, conn: i3ipc.Connection, log: EventLog, output: Output,
                 on_new_window: Callable[[i3ipc.Con], None] | None = None):
        super().__init__(name="sway-ipc", daemon=True)
        self.conn = conn
        self.log = log
        self.output = output
        self.on_new_window = on_new_window
        self.ignore_ids: set[int] = set()
        self.focused_id: int | None = None
        self.conn.on(Event.WINDOW, self._on_window)

    def log_initial_focus(self) -> None:
        focused = self.conn.get_tree().find_focused()
        if focused and focused.type in ("con", "floating_con") and (focused.app_id or focused.window_class):
            self.focused_id = focused.id
            self.log.emit("focus", 0.0, **_con_fields(focused), rect=self.output.relative(focused.rect))

    def _on_window(self, conn, ev) -> None:
        con = ev.container
        if ev.change in ("new", "title") and self.on_new_window:
            self.on_new_window(con)
        if con.id in self.ignore_ids:
            return
        if ev.change == "focus":
            self.focused_id = con.id
            self.log.emit("focus", **_con_fields(con), rect=self.output.relative(con.rect))
            return
        change = self.WINDOW_CHANGES.get(ev.change)
        if change is None:
            return
        if change in ("move", "floating", "fullscreen") and con.id != self.focused_id:
            return
        self.log.emit("window", change=change, con_id=con.id, rect=self.output.relative(con.rect))

    def run(self) -> None:
        try:
            self.conn.main()
        except Exception:
            pass

    def stop(self) -> None:
        self.conn.main_quit()
        self.join(timeout=2)
