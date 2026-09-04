"""Minimal Xcursor reader — turns a real theme cursor into the ARGB image the cursor session would give.

Only used by tests and `scripts/cursor-fixture.py`: the recorder gets its images from the compositor.
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
