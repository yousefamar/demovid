"""Dump the Wayland globals the current compositor advertises: uv run scripts/wl-globals.py [grep]"""
import sys
from pywayland.client import Display

d = Display()
d.connect()
reg = d.get_registry()
globs = []
reg.dispatcher["global"] = lambda r, n, iface, ver: globs.append((iface, ver))
d.roundtrip()
d.disconnect()
needle = sys.argv[1] if len(sys.argv) > 1 else ""
for iface, ver in sorted(globs):
    if needle in iface:
        print(f"{iface} v{ver}")
