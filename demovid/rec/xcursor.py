"""Minimal Xcursor reader, plus theme lookup by hotspot.

The cursor-image capture in `ext-image-copy-capture-v1` only works for a HARDWARE cursor: with a software
cursor there is no cursor swapchain, so the compositor advertises a 0x0 buffer and no format. This machine
can never use a hardware cursor (the NVIDIA cursor plane only takes ARGB8888/LINEAR, which the renderer
cannot produce), so `rec` gets the picture from the theme on disk instead — and the protocol's hotspot tells
us WHICH cursor, because hotspots are near-unique per shape within a theme.

Reading the theme also beats capturing it: themes ship 24/32/48/64/96 px art, so an enlarged cursor is drawn
from real pixels rather than an upscaled 24 px grab.

Format: https://www.x.org/releases/current/doc/man/man3/Xcursor.3.xhtml (header, TOC, image chunks).
"""

import struct
from pathlib import Path

import numpy as np

IMAGE_TYPE = 0xFFFD0002


def read_images(path: Path) -> list[tuple[int, int, int, int, int, np.ndarray]]:
    """[(nominal_size, width, height, hotspot_x, hotspot_y, bgra premultiplied)] for every image chunk."""
    data = Path(path).read_bytes()
    magic, header, version, ntoc = struct.unpack_from("<4sIII", data, 0)
    if magic != b"Xcur":
        raise ValueError(f"{path}: not an Xcursor file")
    out = []
    for i in range(ntoc):
        chunk_type, subtype, position = struct.unpack_from("<III", data, header + 12 * i)
        if chunk_type != IMAGE_TYPE:
            continue
        _, _, _, _, w, h, xhot, yhot, _delay = struct.unpack_from("<IIIIIIIII", data, position)
        pixels = np.frombuffer(data, np.uint8, count=w * h * 4, offset=position + 36)
        out.append((subtype, w, h, xhot, yhot, pixels.reshape(h, w, 4).copy()))
    return out


def best_image(path: Path, size: int = 24):
    """The image chunk whose nominal size is closest to `size`."""
    images = read_images(path)
    if not images:
        raise ValueError(f"{path}: no image chunks")
    return min(images, key=lambda im: abs(im[0] - size))


THEME_DIRS = [
    Path("~/.local/share/icons").expanduser(),
    Path("~/.icons").expanduser(),
    Path("/usr/share/icons"),
    Path("/usr/local/share/icons"),
]


def theme_dir(theme: str) -> Path | None:
    for base in THEME_DIRS:
        d = base / theme / "cursors"
        if d.is_dir():
            return d
    return None


def inherits(theme: str) -> list[str]:
    """Themes named by `Inherits=` in the theme's index.theme, in order."""
    for base in THEME_DIRS:
        index = base / theme / "index.theme"
        if not index.is_file():
            continue
        for line in index.read_text(errors="replace").splitlines():
            if line.strip().lower().startswith("inherits"):
                _, _, value = line.partition("=")
                return [t.strip() for t in value.replace(";", ",").split(",") if t.strip()]
    return []


# Hotspots are shared by shapes that look different (Adwaita's `context-menu` and the plain arrow are both
# at 3,1), so a hotspot alone cannot name a cursor. Ties go to the common shapes, in this order.
PREFERRED = [
    "default", "left_ptr", "text", "xterm", "pointer", "hand2", "grab", "grabbing", "progress", "wait",
    "crosshair", "move", "not-allowed", "col-resize", "row-resize", "ew-resize", "ns-resize",
    "nesw-resize", "nwse-resize", "all-scroll", "zoom-in", "zoom-out", "help", "copy", "alias",
]


def _rank(name: str) -> tuple[int, str]:
    return (PREFERRED.index(name) if name in PREFERRED else len(PREFERRED), name)


def index_theme(theme: str, size: int) -> dict[tuple[int, int], str]:
    """{(hotspot_x, hotspot_y) at `size`: cursor name} for a theme, following Inherits.

    Symlinks are followed (themes ship `left_ptr` -> `default`), and where two shapes share a hotspot the
    commoner one wins — so an unrecognised badge-carrying variant is drawn as the plain arrow rather than
    the wrong picture.
    """
    index: dict[tuple[int, int], str] = {}
    seen_themes: set[str] = set()
    for theme_name in [theme, *inherits(theme), "Adwaita", "default"]:
        d = theme_dir(theme_name)
        if d is None or theme_name in seen_themes:
            continue
        seen_themes.add(theme_name)
        for path in sorted(d.iterdir()):
            if not path.is_file():
                continue
            try:
                _, _, _, xhot, yhot, _ = best_image(path, size)
            except (ValueError, OSError, struct.error):
                continue
            key = (xhot, yhot)
            if key not in index or _rank(path.name) < _rank(index[key]):
                index[key] = path.name
    return index


def load_shape(theme: str, name: str, size: int):
    """(width, height, hotspot_x, hotspot_y, bgra premultiplied) for the largest art available."""
    for candidate in [theme, *inherits(theme), "Adwaita", "default"]:
        d = theme_dir(candidate)
        if d is None or not (d / name).exists():
            continue
        images = read_images(d / name)
        if not images:
            continue
        _, w, h, xhot, yhot, bgra = max(images, key=lambda im: im[1] * im[2])
        return w, h, xhot, yhot, bgra
    return None
