import fcntl
import re
import selectors
import struct
import threading
import time
from typing import Callable

import evdev
from evdev import ecodes

from .clock import Clock
from .events import EventLog

EVIOCSCLOCKID = 0x400445A0
_SKIP = re.compile(r"Power Button|Sleep Button|Video Bus|HDA |Lid Switch", re.I)

BUTTONS = {
    ecodes.BTN_LEFT: "left", ecodes.BTN_RIGHT: "right", ecodes.BTN_MIDDLE: "middle",
    ecodes.BTN_SIDE: "side", ecodes.BTN_EXTRA: "extra", ecodes.BTN_FORWARD: "forward",
    ecodes.BTN_BACK: "back", ecodes.BTN_TASK: "task",
}
MODS = {
    "leftctrl": "ctrl", "rightctrl": "ctrl", "leftalt": "alt", "rightalt": "alt",
    "leftshift": "shift", "rightshift": "shift", "leftmeta": "super", "rightmeta": "super",
}


def key_name(code: int) -> str:
    name = ecodes.KEY.get(code) or ecodes.BTN.get(code) or f"key_{code}"
    if isinstance(name, list):
        name = name[0]
    return name.split("_", 1)[-1].lower()


def input_devices() -> list[evdev.InputDevice]:
    """Every readable node that can emit keys or mouse buttons. Grabbed nodes (keyd's physical
    keyboards) stay silent, so reading everything yields each press exactly once."""
    devs = []
    for path in evdev.list_devices():
        try:
            dev = evdev.InputDevice(path)
        except OSError:
            continue
        if _SKIP.search(dev.name):
            dev.close()
            continue
        keys = dev.capabilities().get(ecodes.EV_KEY, [])
        if ecodes.BTN_LEFT in keys or ecodes.KEY_A in keys:
            devs.append(dev)
        else:
            dev.close()
    return devs


class InputLogger(threading.Thread):
    def __init__(
        self,
        log: EventLog,
        clock: Clock,
        cursor_pos: Callable[[], tuple[int, int] | None],
        log_keys: bool = True,
    ):
        super().__init__(name="evdev", daemon=True)
        self.log = log
        self.clock = clock
        self.cursor_pos = cursor_pos
        self.log_keys = log_keys
        self.devices = input_devices()
        self.stop_event = threading.Event()
        self._held: dict[str, int] = {}
        self._monotonic: dict[int, bool] = {}
        for dev in self.devices:
            try:
                fcntl.ioctl(dev.fd, EVIOCSCLOCKID, struct.pack("i", time.CLOCK_MONOTONIC))
                self._monotonic[dev.fd] = True
            except OSError:
                self._monotonic[dev.fd] = False

    def device_names(self) -> list[str]:
        return [d.name for d in self.devices]

    def run(self) -> None:
        sel = selectors.DefaultSelector()
        for dev in self.devices:
            sel.register(dev.fd, selectors.EVENT_READ, dev)
        try:
            while not self.stop_event.is_set():
                for key, _ in sel.select(timeout=0.25):
                    dev: evdev.InputDevice = key.data
                    try:
                        for ev in dev.read():
                            self._handle(dev, ev)
                    except OSError:
                        sel.unregister(dev.fd)
        finally:
            for dev in self.devices:
                try:
                    dev.close()
                except OSError:
                    pass

    def _handle(self, dev: evdev.InputDevice, ev: evdev.InputEvent) -> None:
        if ev.type != ecodes.EV_KEY or ev.value == 2:
            return
        t = self.clock.from_monotonic(ev.timestamp()) if self._monotonic.get(dev.fd) else self.clock.now()
        state = "down" if ev.value else "up"
        if ev.code in BUTTONS:
            pos = self.cursor_pos()
            self.log.emit(
                "button", t, button=BUTTONS[ev.code], state=state,
                x=pos[0] if pos else None, y=pos[1] if pos else None, dev=dev.name,
            )
            return
        name = key_name(ev.code)
        mods = sorted({m for k, m in MODS.items() if self._held.get(k)})
        if name in MODS:
            self._held[name] = ev.value
        if self.log_keys:
            self.log.emit("key", t, key=name, state=state, mods=mods, dev=dev.name)

    def stop(self) -> None:
        self.stop_event.set()
        self.join(timeout=2)
