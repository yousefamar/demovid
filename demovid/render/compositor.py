"""Per-frame composition: zoom/pan warp, synthetic cursor + ripples, webcam PiP."""

import math
from dataclasses import dataclass
from functools import lru_cache

import cv2
import numpy as np

from demovid.render.cursor import CursorTrack, blend, draw_cursor, draw_ripple
from demovid.render.planner import Keyframe, state_at

SUPERSAMPLE = 4


@dataclass
class Look:
    out_w: int
    out_h: int
    out_scale: float              # output px per source px at zoom 1
    cursor: bool = True
    cursor_scale: float = 2.5     # multiples of the 24 px system cursor at zoom 1
    cursor_style: str = "dark"    # dark: macOS-like black arrow, white edge; light: Adwaita-like
    ripple: bool = True
    ripple_s: float = 0.45
    ripple_px: float = 34.0
    pip: bool = True
    pip_mode: str = "fixed"       # fixed: output corner; scene: pasted into the source at preview_rect
    pip_size: float = 0.15        # fraction of output width
    pip_pos: str = "br"
    pip_margin: float = 0.02      # fraction of output width
    pip_radius: float = 0.22      # fraction of pip size
    pip_shrink: float = 0.10      # shrink by this at full zoom
    zoom_level: float = 1.8       # planner's zoom, for the shrink ramp
    interpolation: int = cv2.INTER_LINEAR


class Compositor:
    def __init__(self, keyframes: list[Keyframe], src_w: int, src_h: int, look: Look,
                 cursor: CursorTrack | None, clicks: list[tuple[float, float, float]],
                 preview_rect: list | None = None):
        self.kf = keyframes
        self.src_w, self.src_h = src_w, src_h
        self.look = look
        self.cursor = cursor
        # ripples anchor to where the drawn (smoothed) cursor is at click time, so they always coincide
        self.clicks = sorted((t, *(cursor.at_time(t) or (x, y))) if cursor is not None and cursor.present else (t, x, y)
                             for t, x, y in clicks)
        self.preview_rect = preview_rect
        self._click_i = 0

    def compose(self, frame: np.ndarray, t: float, frame_i: int, cam: np.ndarray | None) -> np.ndarray:
        L = self.look
        cx, cy, zoom = state_at(self.kf, t, self.src_w, self.src_h)
        s = zoom * L.out_scale
        tx, ty = L.out_w / 2 - cx * s, L.out_h / 2 - cy * s

        if cam is not None and L.pip and L.pip_mode == "scene" and self.preview_rect:
            self.paste_pip(frame, cam, self.preview_rect, 0.0)

        if abs(s - 1.0) < 1e-9 and abs(tx) < 1e-6 and abs(ty) < 1e-6:
            out = frame
        else:
            M = np.array([[s, 0, tx], [0, s, ty]], dtype=np.float64)
            out = cv2.warpAffine(frame, M, (L.out_w, L.out_h), flags=L.interpolation, borderMode=cv2.BORDER_CONSTANT)

        if self.cursor is not None and self.cursor.present and L.cursor:
            px, py = self.cursor.at(frame_i)
            ox, oy = px * s + tx, py * s + ty
            size = 24 * L.cursor_scale * L.out_scale * math.sqrt(zoom)
            if L.ripple:
                for ct, kx, ky in self.active_clicks(t):
                    draw_ripple(out, kx * s + tx, ky * s + ty, t - ct, L.ripple_s, L.ripple_px * L.out_scale * math.sqrt(zoom))
            pressed = any(0 <= t - ct < 0.12 for ct, _, _ in self.active_clicks(t))
            draw_cursor(out, ox, oy, size * (0.86 if pressed else 1.0), L.cursor_style)

        if cam is not None and L.pip and L.pip_mode == "fixed":
            ramp = min(max((zoom - 1) / max(L.zoom_level - 1, 1e-6), 0.0), 1.0)
            size = int(round(L.pip_size * L.out_w * (1 - L.pip_shrink * ramp)))
            margin = int(round(L.pip_margin * L.out_w))
            x = margin if "l" in L.pip_pos else L.out_w - margin - size
            y = margin if "t" in L.pip_pos else L.out_h - margin - size
            self.paste_pip(out, cam, [x, y, size, size], L.pip_radius)
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

    def paste_pip(self, dst: np.ndarray, cam: np.ndarray, rect: list, radius_frac: float) -> None:
        x, y, w, h = (int(round(v)) for v in rect)
        if w <= 0 or h <= 0:
            return
        tile = aspect_crop(cam, w, h)
        tile = cv2.resize(tile, (w, h), interpolation=cv2.INTER_AREA if tile.shape[0] > h else cv2.INTER_LINEAR)
        mask, shadow, pad = rounded_mask(w, h, radius_frac)
        if radius_frac > 0:
            blend(dst, np.zeros((*shadow.shape, 3), np.uint8), shadow, x - pad, y - pad + int(0.03 * h))
        blend(dst, tile, mask, x, y)


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
def rounded_mask(w: int, h: int, radius_frac: float) -> tuple[np.ndarray, np.ndarray, int]:
    """(mask HxW float32, shadow alpha (H+2p)x(W+2p) float32, pad)."""
    r = int(round(min(w, h) * radius_frac * SUPERSAMPLE))
    W, H = w * SUPERSAMPLE, h * SUPERSAMPLE
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
