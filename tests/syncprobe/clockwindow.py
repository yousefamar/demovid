#!/usr/bin/env python3
"""Binary monotonic clock window for A/V/event sync probes. Runs on the SYSTEM python3 (needs PyGObject).

Draws time.monotonic_ns() // 1e6 as 40 MSB-first squares (white = 1) every frame, and every
FLASH_PERIOD_S turns fully white for FLASH_MS while playing a click on the default sink, logging
the monotonic ns of each flash/click to --log as JSON lines.
"""
import argparse
import ctypes
import json
import math
import os
import struct
import subprocess
import sys
import threading
import time
import wave

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

BITS = 40
CELL = 16
PAD = 4
WIDTH = PAD * 2 + BITS * CELL
HEIGHT = 120
FLASH_MS = 120


def write_click(path: str, rate: int = 48000, ms: int = 15, freq: float = 2000.0) -> None:
    n = rate * ms // 1000
    frames = b"".join(struct.pack("<h", int(0.7 * 32767 * math.sin(2 * math.pi * freq * i / rate))) for i in range(n))
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames + bytes(rate // 10 * 2))


RATE = 48000
BLOCK = RATE // 100  # 10 ms of s16 mono
CLICK_MS = 15
CLICK_HZ = 2000.0


class AudioFeeder(threading.Thread):
    """Keeps a pw-cat playback stream fed with paced 10 ms blocks of silence (never more than
    ~20 ms ahead of real time), so a click written on request reaches the sink with only the
    stream's own latency. Logs the monotonic time the first click block was written."""

    def __init__(self, log):
        super().__init__(daemon=True)
        self.log = log
        self.click_pending = False
        n = RATE * CLICK_MS // 1000
        self.click = b"".join(struct.pack("<h", int(0.9 * 32767 * math.sin(2 * math.pi * CLICK_HZ * i / RATE)))
                              for i in range(n)) + bytes((BLOCK * 2 - (n * 2) % (BLOCK * 2)) % (BLOCK * 2))
        self.silence = bytes(BLOCK * 2)
        self.proc = subprocess.Popen(
            ["pw-cat", "-p", "--format", "s16", "--rate", str(RATE), "--channels", "1",
             "--latency", f"{BLOCK}", "-"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def run(self):
        next_t = time.monotonic_ns()
        ahead_ns = 20_000_000
        while self.proc.poll() is None:
            now = time.monotonic_ns()
            if next_t > now + ahead_ns:
                time.sleep((next_t - now - ahead_ns) / 1e9)
                continue
            if self.click_pending:
                self.click_pending = False
                self.log.write(json.dumps({"event": "click", "click_write_ns": time.monotonic_ns(),
                                           "blocks_ahead": round((next_t - now) / 1e7, 1)}) + "\n")
                data = self.click
            else:
                data = self.silence
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError):
                return
            next_t += (len(data) // 2) * 1_000_000_000 // RATE


def make_overlay_layer(window: Gtk.Window, x: int, y: int) -> None:
    """gtk-layer-shell via ctypes (no GI typelib installed): an OVERLAY layer surface anchored
    top-left with margins (x, y) sits above every toplevel, including floating popups."""
    lib = ctypes.CDLL("libgtk-layer-shell.so.0")
    ctypes.pythonapi.PyCapsule_GetPointer.restype = ctypes.c_void_p
    ctypes.pythonapi.PyCapsule_GetPointer.argtypes = [ctypes.py_object, ctypes.c_char_p]
    ptr = ctypes.c_void_p(ctypes.pythonapi.PyCapsule_GetPointer(window.__gpointer__, None))
    lib.gtk_layer_init_for_window(ptr)
    lib.gtk_layer_set_layer(ptr, 3)  # GTK_LAYER_SHELL_LAYER_OVERLAY
    lib.gtk_layer_set_namespace(ptr, b"demovid-syncprobe")
    for edge, on, margin in ((0, 1, x), (2, 1, y), (1, 0, 0), (3, 0, 0)):  # LEFT, TOP, RIGHT, BOTTOM
        lib.gtk_layer_set_anchor(ptr, edge, on)
        lib.gtk_layer_set_margin(ptr, edge, margin)
    lib.gtk_layer_set_exclusive_zone(ptr, 0)


class ClockWindow(Gtk.Window):
    def __init__(self, log_path: str, click_path: str, flash_period_s: float, first_flash_s: float,
                 layer_at: tuple[int, int] | None = None):
        super().__init__(title="demovid-syncprobe")
        self.set_default_size(WIDTH, HEIGHT)
        self.set_size_request(WIDTH, HEIGHT)
        self.set_resizable(False)
        if layer_at:
            make_overlay_layer(self, *layer_at)
        self.log = open(log_path, "a", buffering=1)
        self.click_path = click_path
        self.feeder = AudioFeeder(self.log)
        self.feeder.start()
        self.flash_until_ns = 0
        self.area = Gtk.DrawingArea()
        self.add(self.area)
        self.area.connect("draw", self.on_draw)
        self.connect("destroy", Gtk.main_quit)
        GLib.timeout_add(3, self._tick)
        GLib.timeout_add(int(first_flash_s * 1000), self._first_flash, int(flash_period_s * 1000))
        self.log.write(json.dumps({"event": "start", "monotonic_ns": time.monotonic_ns(), "pid": os.getpid(),
                                   "width": WIDTH, "height": HEIGHT, "bits": BITS, "cell": CELL, "pad": PAD}) + "\n")

    def _tick(self):
        self.area.queue_draw()
        return True

    def _first_flash(self, period_ms):
        self._flash()
        GLib.timeout_add(period_ms, self._flash)
        return False

    def _flash(self):
        now = time.monotonic_ns()
        self.flash_until_ns = now + FLASH_MS * 1_000_000
        self.area.queue_draw()
        self.feeder.click_pending = True
        self.log.write(json.dumps({"event": "flash", "flash_ns": now}) + "\n")
        return True

    def on_draw(self, widget, cr):
        now = time.monotonic_ns()
        if now < self.flash_until_ns:
            cr.set_source_rgb(1, 1, 1)
            cr.paint()
            return False
        cr.set_source_rgb(0.25, 0.25, 0.25)
        cr.paint()
        ms = now // 1_000_000
        for i in range(BITS):
            bit = (ms >> (BITS - 1 - i)) & 1
            cr.set_source_rgb(1, 1, 1) if bit else cr.set_source_rgb(0, 0, 0)
            cr.rectangle(PAD + i * CELL, PAD, CELL, CELL)
            cr.fill()
        cr.set_source_rgb(1, 1, 1)
        cr.set_font_size(36)
        cr.move_to(PAD, 80)
        cr.show_text(f"mono {ms} ms")
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", required=True)
    ap.add_argument("--flash-period", type=float, default=8.0)
    ap.add_argument("--first-flash", type=float, default=5.0)
    ap.add_argument("--layer-at", type=lambda v: tuple(int(p) for p in v.split(",")),
                    help="x,y: draw as an overlay layer surface at this output position instead of a toplevel")
    ns = ap.parse_args()
    click = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "demovid-syncprobe-click.wav")
    write_click(click)
    win = ClockWindow(ns.log, click, ns.flash_period, ns.first_flash, ns.layer_at)
    win.show_all()
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
