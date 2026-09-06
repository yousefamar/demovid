"""Where the camera goes — one set of numbers for `rec` (the live preview window) and `render` (the PiP).

The preview window is recorded into screen.mp4, so the rendered PiP must sit exactly over it and be a
little bigger: a squircle (superellipse, n=4) whose inscribed square is 0.84 of its side hides a square
window of 0.80 of its side with a margin to spare. Everything is measured from the WORK AREA (the output
minus bars), so the camera floats above waybar rather than on it.
"""

PIP_FRAC = 0.15        # PiP side as a fraction of the output width
PREVIEW_FRAC = 0.80    # live preview window side as a fraction of the PiP side
MARGIN_PX = 12         # gap between the PiP and the work area's edges
SQUIRCLE_N = 4.0       # superellipse exponent; inscribed square = 2^(-1/n) = 0.84 of the side

Rect = tuple[int, int, int, int]


def pip_side(width: int) -> int:
    return int(round(PIP_FRAC * width))


def pip_square(width: int, height: int, workarea: list | tuple | None = None, pos: str = "br",
               margin: int = MARGIN_PX) -> tuple[int, int, int]:
    """(x, y, side) of the PiP in output pixels, tucked into a corner of the work area."""
    side = pip_side(width)
    wx, wy, ww, wh = tuple(workarea) if workarea else (0, 0, width, height)
    x = wx + margin if "l" in pos else wx + ww - margin - side
    y = wy + margin if "t" in pos else wy + wh - margin - side
    return int(x), int(y), side


def preview_rect(width: int, height: int, workarea: list | tuple | None = None, pos: str = "br") -> Rect:
    """The live preview window: a square centred in the PiP, at most PREVIEW_FRAC of its side.

    The side is a multiple of 16: the preview travels as raw I420 through a pipe, and odd chroma
    planes (side/2) make ffmpeg and GStreamer disagree on the stride — the picture shears.
    """
    px, py, side = pip_square(width, height, workarea, pos)
    inner = int(side * PREVIEW_FRAC) // 16 * 16
    off = (side - inner) // 2
    return px + off, py + off, inner, inner


def pip_for_preview(rect: list | tuple) -> tuple[int, int, int]:
    """The PiP square that hides a recorded preview window: same centre, side / PREVIEW_FRAC."""
    x, y, w, h = rect
    side = int(round(max(w, h) / PREVIEW_FRAC))
    cx, cy = x + w / 2, y + h / 2
    return int(round(cx - side / 2)), int(round(cy - side / 2)), side


def clamp_square(box: tuple[int, int, int], width: int, height: int) -> tuple[int, int, int]:
    """Keep an (x, y, side) square inside the frame (recordings made before this layout put the preview
    at the screen edge, so the square that hides it would overshoot)."""
    x, y, side = box
    side = min(side, width, height)
    return max(0, min(x, width - side)), max(0, min(y, height - side)), side


def intersects(a: tuple, b: tuple) -> bool:
    """Do two (x, y, w, h) boxes overlap?"""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah
