"""Per-frame composition: zoom/pan warp, synthetic cursor + ripples, webcam PiP."""

import math
from dataclasses import dataclass
from functools import lru_cache

import cv2
import numpy as np

from demovid.render.chips import draw_chips, visible_chips
from demovid.render.cursor import CursorTrack, ShapeTrack, blend, draw_cursor, draw_ripple, draw_shape
from demovid.render.planner import Keyframe, state_at

SUPERSAMPLE = 4
WORK_PX = 96      # erase_box builds its fill at this resolution (see erase_box)


@dataclass
class Look:
    out_w: int
    out_h: int
    out_scale: float              # output px per source px at zoom 1
    cursor: bool = True
    cursor_scale: float = 2.5     # multiples of the 24 px system cursor at zoom 1
    cursor_style: str = "dark"    # dark: macOS-like black arrow, white edge; light: Adwaita-like
    cursor_real: bool = True      # use the shapes `rec` captured when the recording has them
    chips: bool = True            # keystroke chips
    chip_height: float = 0.045    # fraction of output height
    chip_margin: float = 0.055
    chip_gap: float = 0.012
    ripple: bool = True
    ripple_s: float = 0.45
    ripple_px: float = 34.0
    pip: bool = True
    pip_mode: str = "fixed"       # fixed: a corner of the output, never moving (the recorded preview is erased
                                  # from the source instead); scene: pasted over preview_rect, zooms with it
    pip_size: float = 0.15        # fraction of output width (layout.PIP_FRAC)
    pip_pos: str = "br"
    pip_margin: float = 0.02      # fraction of output width, imports only (recordings use layout.MARGIN_PX)
    pip_shape: str = "squircle"   # squircle | rounded | circle
    pip_radius: float = 0.22      # corner radius for `rounded`, fraction of pip size
    pip_shrink: float = 0.0       # shrink at full zoom; 0 so the camera is identical on every frame
    zoom_level: float = 1.8       # planner's zoom, for the shrink ramp
    interpolation: int = cv2.INTER_LINEAR
    pad: float = 0.0              # background frame: padding as a fraction of output height (0 = none)
    bg: str = "#141414"           # '#rrggbb', '#rrggbb,#rrggbb' (diagonal gradient) or an image path
    radius: float = 0.018         # rounded corners of the padded content, fraction of output height
    shadow: bool = True


class Compositor:
    def __init__(self, keyframes: list[Keyframe], src_w: int, src_h: int, look: Look,
                 cursor: CursorTrack | None, clicks: list[tuple[float, float, float]],
                 preview_rect: list | None = None, shapes: ShapeTrack | None = None,
                 chips: list[tuple[float, float, str]] | None = None, refiner=None,
                 pip_box_src: tuple[int, int, int] | None = None):
        self.kf = keyframes
        self.refiner = refiner
        # the PiP square in SOURCE px: over the recorded preview when there is one, else a work-area corner
        self.pip_box_src = pip_box_src
        self.src_w, self.src_h = src_w, src_h
        self.look = look
        self.cursor = cursor
        # ripples anchor to where the drawn (smoothed) cursor is at click time, so they always coincide
        self.clicks = sorted((t, *(cursor.at_time(t) or (x, y))) if cursor is not None and cursor.present else (t, x, y)
                             for t, x, y in clicks)
        self.preview_rect = preview_rect
        self.shapes = shapes
        self.chips = chips or []
        self._click_i = 0
        # content area: the whole output, or a source-aspect box inset by `pad` on a background
        L = look
        if L.pad > 0:
            pad_px = int(round(L.pad * L.out_h))
            self.scale = min((L.out_w - 2 * pad_px) / src_w, (L.out_h - 2 * pad_px) / src_h)
            w, h = int(round(src_w * self.scale)), int(round(src_h * self.scale))
            self.content = ((L.out_w - w) // 2, (L.out_h - h) // 2, w, h)
            r = int(round(L.radius * L.out_h))
            self.bg = background(L.bg, L.out_w, L.out_h)
            if L.shadow:
                drop_shadow(self.bg, self.content, r)
            self.corners = corner_masks(w, h, r)
        else:
            self.scale = L.out_scale
            self.content = (0, 0, L.out_w, L.out_h)
            self.bg = None
            self.corners = []
        x0, y0, w, h = self.content
        self.cx0, self.cy0 = x0 + w / 2, y0 + h / 2

    def compose(self, frame: np.ndarray, t: float, frame_i: int, cam: np.ndarray | None) -> np.ndarray:
        L = self.look
        cx, cy, zoom = state_at(self.kf, t, self.src_w, self.src_h)
        s = zoom * self.scale
        tx, ty = self.cx0 - cx * s, self.cy0 - cy * s

        # The live preview window is baked into every frame. Erase it from the SOURCE (so it zooms
        # away with the content) and draw the real camera at a fixed output corner that never moves.
        # `scene` keeps the old behaviour of pasting the camera itself into the source.
        cover = False
        if L.pip and self.pip_box_src is not None:
            bx, by, bs = self.pip_box_src
            if cam is not None and L.pip_mode == "scene":
                self.paste_pip(frame, cam, [bx, by, bs, bs])
                cover = True
            elif self.in_view((bx, by, bs, bs), cx, cy, zoom):
                erase_box(frame, self.pip_box_src)

        if self.bg is not None:
            x0, y0, w, h = self.content
            M = np.array([[s, 0, tx - x0], [0, s, ty - y0]], dtype=np.float64)
            content = cv2.warpAffine(frame, M, (w, h), flags=L.interpolation, borderMode=cv2.BORDER_CONSTANT)
            out = self.bg.copy()
            out[y0:y0 + h, x0:x0 + w] = content
            for (mx, my, mask) in self.corners:
                roi = out[y0 + my:y0 + my + mask.shape[0], x0 + mx:x0 + mx + mask.shape[1]]
                bgroi = self.bg[y0 + my:y0 + my + mask.shape[0], x0 + mx:x0 + mx + mask.shape[1]]
                roi[:] = (roi * mask[..., None] + bgroi * (1 - mask[..., None])).astype(np.uint8)
        elif abs(s - 1.0) < 1e-9 and abs(tx) < 1e-6 and abs(ty) < 1e-6:
            out = frame
        else:
            M = np.array([[s, 0, tx], [0, s, ty]], dtype=np.float64)
            out = cv2.warpAffine(frame, M, (L.out_w, L.out_h), flags=L.interpolation, borderMode=cv2.BORDER_CONSTANT)

        if self.cursor is not None and self.cursor.present and L.cursor and self.cursor.visible_at(frame_i):
            px, py = self.cursor.at(frame_i)
            if self.refiner is not None:
                px, py = self.refiner.refine(frame, t, (px, py))
            ox, oy = px * s + tx, py * s + ty
            size = 24 * L.cursor_scale * self.scale * math.sqrt(zoom)
            if L.ripple:
                for ct, kx, ky in self.active_clicks(t):
                    draw_ripple(out, kx * s + tx, ky * s + ty, t - ct, L.ripple_s, L.ripple_px * self.scale * math.sqrt(zoom))
            pressed = any(0 <= t - ct < 0.12 for ct, _, _ in self.active_clicks(t))
            squash = 0.86 if pressed else 1.0
            shape = self.shapes.at(t) if (L.cursor_real and self.shapes is not None) else None
            if shape is not None:
                # the captured image is the real cursor at 1x, so scale it like the 24 px reference
                draw_shape(out, ox, oy, shape, size / max(shape[1].shape[0], 1) * squash)
            else:
                draw_cursor(out, ox, oy, size * squash, L.cursor_style)

        # camera, part 2: a fixed output corner, identical on every frame — it must never travel with a zoom
        if cam is not None and L.pip and not cover:
            ramp = min(max((zoom - 1) / max(L.zoom_level - 1, 1e-6), 0.0), 1.0)
            shrink = 1 - L.pip_shrink * ramp
            if self.pip_box_src is not None:
                bx, by, bs = self.pip_box_src
                # the same box the cover uses, mapped through the zoom-1 content transform, then shrunk about its centre
                fx, fy, fs = self.cx0 + (bx - self.src_w / 2) * self.scale, self.cy0 + (by - self.src_h / 2) * self.scale, bs * self.scale
                size = int(round(fs * shrink))
                x, y = int(round(fx + (fs - size) / 2)), int(round(fy + (fs - size) / 2))
            else:
                size = int(round(L.pip_size * L.out_w * shrink))
                margin = int(round(L.pip_margin * L.out_w))
                x = margin if "l" in L.pip_pos else L.out_w - margin - size
                y = margin if "t" in L.pip_pos else L.out_h - margin - size
            self.paste_pip(out, cam, [x, y, size, size])

        if L.chips and self.chips:
            draw_chips(out, visible_chips(self.chips, t), max(12, int(round(L.chip_height * L.out_h))),
                       int(round(L.chip_margin * L.out_h)), int(round(L.chip_gap * L.out_h)))
        return out

    def active_clicks(self, t: float):
        # clicks are sorted; skip everything older than the ripple window
        while self._click_i < len(self.clicks) and self.clicks[self._click_i][0] < t - self.look.ripple_s:
            self._click_i += 1
        out = []
        for c in self.clicks[self._click_i:]:
            if c[0] > t:
                break
            out.append(c)
        return out

    def in_view(self, box: tuple, cx: float, cy: float, zoom: float) -> bool:
        half_w, half_h = self.src_w / (2 * zoom), self.src_h / (2 * zoom)
        from demovid.layout import intersects
        return intersects(box, (cx - half_w, cy - half_h, 2 * half_w, 2 * half_h))

    def paste_pip(self, dst: np.ndarray, cam: np.ndarray, rect: list) -> None:
        x, y, w, h = (int(round(v)) for v in rect)
        if w <= 0 or h <= 0:
            return
        tile = aspect_crop(cam, w, h)
        tile = cv2.resize(tile, (w, h), interpolation=cv2.INTER_AREA if tile.shape[0] > h else cv2.INTER_LINEAR)
        mask, shadow, pad = shape_mask(w, h, self.look.pip_shape, self.look.pip_radius)
        blend(dst, np.zeros((*shadow.shape, 3), np.uint8), shadow, x - pad, y - pad + int(0.03 * h))
        blend(dst, tile, mask, x, y)


def erase_box(frame: np.ndarray, box: tuple[int, int, int], feather: int = 12) -> None:
    """Hide the recorded preview window in-place, in source pixels.

    The desktop behind it was never captured, so there is nothing to restore: inpaint it from the
    surrounding pixels (a flat fill reads as an obvious rectangle over textured content). An anonymous
    smear beats a stale copy of the camera — and unlike pasting the live camera here it never moves
    when the view zooms.
    """
    x, y, s = (int(round(v)) for v in box)
    H, W = frame.shape[:2]
    pad = max(8, feather)
    rx0, ry0 = max(0, x - pad), max(0, y - pad)
    rx1, ry1 = min(W, x + s + pad), min(H, y + s + pad)
    if rx1 - rx0 < 4 or ry1 - ry0 < 4:
        return
    roi = frame[ry0:ry1, rx0:rx1]
    rh, rw = roi.shape[:2]
    mask = np.zeros((rh, rw), np.uint8)
    mx0, my0 = max(0, x - rx0), max(0, y - ry0)
    mask[my0:my0 + s, mx0:mx0 + s] = 255
    # The result is a smooth colour field, so build it on a thumbnail: inpaint follows the local
    # structure (grass above, soil below still read correctly) and the blur kills its radial streaks.
    # Full resolution here costs 5x the render time for a field nobody can tell apart.
    f = min(1.0, WORK_PX / max(rw, rh))
    sw, sh = max(8, int(rw * f)), max(8, int(rh * f))
    small = cv2.resize(roi, (sw, sh), interpolation=cv2.INTER_AREA)
    small_mask = cv2.resize(mask, (sw, sh), interpolation=cv2.INTER_NEAREST)
    filled = cv2.inpaint(small, small_mask, 3, cv2.INPAINT_TELEA)
    soft = cv2.GaussianBlur(filled, (0, 0), max(1.0, sw / 12))
    soft = cv2.resize(soft, (rw, rh), interpolation=cv2.INTER_LINEAR)
    alpha = cv2.GaussianBlur(mask.astype(np.float32) / 255.0, (0, 0), max(1.5, feather / 2))
    roi[:] = (roi * (1 - alpha[..., None]) + soft * alpha[..., None]).astype(np.uint8)


def background(spec: str, w: int, h: int) -> np.ndarray:
    """Solid '#rrggbb', diagonal gradient '#rrggbb,#rrggbb', or an image file scaled to cover."""
    spec = spec.strip()
    if "," in spec:
        c1, c2 = (parse_color(c) for c in spec.split(",", 1))
        u = (np.arange(w, dtype=np.float32)[None, :] / max(w - 1, 1) + np.arange(h, dtype=np.float32)[:, None] / max(h - 1, 1)) / 2
        return (np.array(c1, np.float32) * (1 - u[..., None]) + np.array(c2, np.float32) * u[..., None]).astype(np.uint8)
    if spec.startswith("#"):
        return np.full((h, w, 3), parse_color(spec), np.uint8)
    img = cv2.imread(spec, cv2.IMREAD_COLOR)
    if img is None:
        raise SystemExit(f"--bg: cannot read {spec}")
    return cv2.resize(aspect_crop(img, w, h), (w, h), interpolation=cv2.INTER_AREA)


def parse_color(s: str) -> tuple[int, int, int]:
    s = s.strip().lstrip("#")
    if len(s) != 6:
        raise SystemExit(f"--bg: expected #rrggbb, got #{s}")
    r, g, b = (int(s[i:i + 2], 16) for i in (0, 2, 4))
    return (b, g, r)  # OpenCV is BGR


def drop_shadow(canvas: np.ndarray, rect: tuple, radius: int) -> None:
    x0, y0, w, h = rect
    H, W = canvas.shape[:2]
    blur = max(6.0, 0.035 * H)
    mask = np.zeros((H, W), np.float32)
    inner, _, _ = shape_mask(w, h, "rounded", radius / max(min(w, h), 1))
    dy = int(round(0.012 * H))
    ys, xs = slice(y0 + dy, y0 + dy + h), slice(x0, x0 + w)
    mask[ys, xs] = inner[:max(0, min(h, H - (y0 + dy))), :]
    mask = cv2.GaussianBlur(mask, (0, 0), blur) * 0.6
    canvas[:] = (canvas * (1 - mask[..., None])).astype(np.uint8)


def corner_masks(w: int, h: int, r: int) -> list[tuple[int, int, np.ndarray]]:
    """(x, y, mask) for the four r×r corner squares of a w×h rounded rect; mask=1 inside the content."""
    if r <= 0:
        return []
    mask, _, _ = shape_mask(w, h, "rounded", r / max(min(w, h), 1))
    return [(0, 0, mask[:r, :r]), (w - r, 0, mask[:r, w - r:]), (0, h - r, mask[h - r:, :r]), (w - r, h - r, mask[h - r:, w - r:])]


def aspect_crop(img: np.ndarray, w: int, h: int) -> np.ndarray:
    """Centre-crop img to the aspect ratio w:h."""
    ih, iw = img.shape[:2]
    if iw * h > ih * w:
        cw = int(round(ih * w / h))
        x0 = (iw - cw) // 2
        return img[:, x0:x0 + cw]
    ch = int(round(iw * h / w))
    y0 = (ih - ch) // 2
    return img[y0:y0 + ch]


@lru_cache(maxsize=32)
def shape_mask(w: int, h: int, shape: str, radius_frac: float) -> tuple[np.ndarray, np.ndarray, int]:
    """(mask HxW float32, shadow alpha (H+2p)x(W+2p) float32, pad) for a squircle, rounded rect or ellipse."""
    W, H = w * SUPERSAMPLE, h * SUPERSAMPLE
    if shape in ("squircle", "circle"):
        from demovid.layout import SQUIRCLE_N
        n = SQUIRCLE_N if shape == "squircle" else 2.0
        ys = (np.arange(H, dtype=np.float32) + 0.5) / H * 2 - 1
        xs = (np.arange(W, dtype=np.float32) + 0.5) / W * 2 - 1
        big = ((np.abs(xs[None, :]) ** n + np.abs(ys[:, None]) ** n) <= 1.0).astype(np.float32)
    else:
        r = int(round(min(w, h) * radius_frac * SUPERSAMPLE))
        big = np.zeros((H, W), np.float32)
        cv2.rectangle(big, (r, 0), (W - 1 - r, H - 1), 1.0, -1)
        cv2.rectangle(big, (0, r), (W - 1, H - 1 - r), 1.0, -1)
        for cx, cy in ((r, r), (W - 1 - r, r), (r, H - 1 - r), (W - 1 - r, H - 1 - r)):
            cv2.circle(big, (cx, cy), r, 1.0, -1, cv2.LINE_AA)
    mask = cv2.resize(big, (w, h), interpolation=cv2.INTER_AREA)
    pad = max(4, int(0.12 * min(w, h)))
    shadow = np.zeros((h + 2 * pad, w + 2 * pad), np.float32)
    shadow[pad:pad + h, pad:pad + w] = mask
    shadow = cv2.GaussianBlur(shadow, (0, 0), pad / 2.2) * 0.55
    return mask, shadow, pad
