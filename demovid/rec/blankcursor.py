import os
import struct
from pathlib import Path

THEME = "demovid-blank"
NAMES = [
    "default", "left_ptr", "pointer", "arrow", "text", "xterm", "hand1", "hand2", "pointing_hand", "grab",
    "grabbing", "crosshair", "move", "all-scroll", "watch", "wait", "progress", "left_ptr_watch", "help",
    "question_arrow", "not-allowed", "crossed_circle", "context-menu", "vertical-text", "alias", "copy",
    "cell", "zoom-in", "zoom-out", "dnd-move", "dnd-none", "dnd-copy", "dnd-link", "col-resize",
    "row-resize", "sb_h_double_arrow", "sb_v_double_arrow", "n-resize", "s-resize", "e-resize", "w-resize",
    "ne-resize", "nw-resize", "se-resize", "sw-resize", "ns-resize", "ew-resize", "nesw-resize",
    "nwse-resize", "top_side", "bottom_side", "left_side", "right_side", "top_left_corner",
    "top_right_corner", "bottom_left_corner", "bottom_right_corner",
]


def xcursor_bytes(size: int) -> bytes:
    """A fully transparent Xcursor file with one image of `size`×`size`."""
    image_type = 0xFFFD0002
    header = struct.pack("<4sIII", b"Xcur", 16, 0x10000, 1)
    toc_pos = 16 + 12
    toc = struct.pack("<III", image_type, size, toc_pos)
    chunk = struct.pack("<IIIIIIIII", 36, image_type, size, 1, size, size, 0, 0, 0)
    return header + toc + chunk + bytes(size * size * 4)


def ensure_theme(size: int = 24) -> str:
    root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "icons" / THEME
    cursors = root / "cursors"
    cursors.mkdir(parents=True, exist_ok=True)
    (root / "index.theme").write_text(f"[Icon Theme]\nName={THEME}\n")
    (cursors / "default").write_bytes(xcursor_bytes(size))
    for name in NAMES[1:]:
        link = cursors / name
        if not link.is_symlink():
            link.symlink_to("default")
    return THEME
