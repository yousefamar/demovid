"""Cursor rendering: smoothed track from cursor events, real captured shapes or a synthetic arrow, ripples."""

import math
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np

# Arrow outline in a 24-unit box, tip (hotspot) at the origin.
ARROW = np.array([(0, 0), (0, 17), (4.6, 13.2), (8, 20.2), (11.2, 18.7), (7.6, 11.6), (13.2, 11.6)], dtype=np.float32)
ARROW_UNITS = 24.0
SUPERSAMPLE = 4


class CursorTrack:
    """Cursor position per output frame, Gaussian-smoothed (zero lag: we know the future)."""

    def __init__(self, events: list[dict], t_start: float, fps: float, n_frames: int, smooth_s: float = 0.05):
        self.t_start, self.fps, self.n_frames = t_start, fps, n_frames
        pts = [(float(e["t"]), float(e["x"]), float(e["y"])) for e in events
               if e.get("kind") == "cursor" and e.get("x") is not None and e.get("y") is not None]
        pts.sort()
        self.present = bool(pts)
        if not pts:
            self.xs = self.ys = np.zeros(n_frames)
            self.first_t = self.last_t = 0.0
            return
        ts = np.array([p[0] for p in pts])
        frame_ts = t_start + np.arange(n_frames) / fps
        xs = np.interp(frame_ts, ts, [p[1] for p in pts])
        ys = np.interp(frame_ts, ts, [p[2] for p in pts])
        sigma = smooth_s * fps
        self.xs, self.ys = gaussian(xs, sigma), gaussian(ys, sigma)
        self.first_t, self.last_t = ts[0], ts[-1]

    def at(self, i: int) -> tuple[float, float]:
        return float(self.xs[i]), float(self.ys[i])

    def at_time(self, t: float) -> tuple[float, float] | None:
        i = int(round((t - self.t_start) * self.fps))
        if 0 <= i < self.n_frames:
            return self.at(i)
        return None


def gaussian(a: np.ndarray, sigma: float) -> np.ndarray:
    if sigma < 0.3 or len(a) < 3:
        return a
    r = int(math.ceil(3 * sigma))
    k = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma) ** 2)
    k /= k.sum()
    padded = np.pad(a, r, mode="edge")
    return np.convolve(padded, k, mode="valid")



class ShapeTrack:
    """The cursor images `rec` captured (cursors/<id>.png + `cursor_shape` events), keyed by time.

    A `cursor_shape` event marks the instant a new image became current, so the shape in force at t is
    the last event at or before t. Missing files degrade to the synthetic arrow.
    """

    def __init__(self, events: list[dict], shapes_dir: Path | None):
        self.dir = shapes_dir
        self.marks: list[tuple[float, str, tuple[int, int], float]] = []
        for e in events:
            if e.get("kind") != "cursor_shape" or not e.get("id"):
                continue
            hs = e.get("hotspot") or [0, 0]
            self.marks.append((float(e["t"]), str(e["id"]), (int(hs[0]), int(hs[1])), float(e.get("scale") or 1.0)))
        self.marks.sort()
        self.present = bool(self.marks) and shapes_dir is not None and shapes_dir.is_dir()

    def at(self, t: float):
        """(bgr, alpha, hotspot_x, hotspot_y) in source pixels, or None to fall back to the arrow."""
        if not self.present:
            return None
        i = -1
        for j, m in enumerate(self.marks):
            if m[0] <= t:
                i = j
            else:
                break
        if i < 0:
            i = 0  # before the first capture: the first shape is the best guess
        _, sid, hotspot, scale = self.marks[i]
        img = load_shape(self.dir / f"{sid}.png")
        if img is None:
            return None
        bgr, alpha = img
        return bgr, alpha, hotspot[0] / scale, hotspot[1] / scale


@lru_cache(maxsize=32)
def load_shape(path: Path):
    if not path.exists():
        return None
    img = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if img is None or img.ndim != 3:
        return None
    if img.shape[2] == 3:
        return img, np.ones(img.shape[:2], np.float32)
    return np.ascontiguousarray(img[..., :3]), img[..., 3].astype(np.float32) / 255.0


def draw_shape(frame: np.ndarray, x: float, y: float, shape, scale: float) -> None:
    """Draw a captured cursor image with its hotspot at (x, y), scaled by `scale`."""
    bgr, alpha, hx, hy = shape
    h, w = alpha.shape
    tw, th = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
    interp = cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC
    bgr_s = cv2.resize(bgr, (tw, th), interpolation=interp)
    alpha_s = np.clip(cv2.resize(alpha, (tw, th), interpolation=interp), 0.0, 1.0)
    pad = max(2, int(0.18 * max(tw, th)))
    shadow = np.zeros((th + 2 * pad, tw + 2 * pad), np.float32)
    dy = int(round(0.06 * th))
    shadow[pad + dy:pad + dy + th, pad:pad + tw] = alpha_s
    shadow = cv2.GaussianBlur(shadow, (0, 0), pad / 2.4) * 0.45
    blend(frame, np.zeros((*shadow.shape, 3), np.uint8), shadow,
          int(round(x - hx * scale)) - pad, int(round(y - hy * scale)) - pad)
    blend(frame, bgr_s, alpha_s, int(round(x - hx * scale)), int(round(y - hy * scale)))


STYLES = {"dark": (20.0, 255.0), "light": (255.0, 0.0)}  # (fill, outline) grey levels


@lru_cache(maxsize=64)
def arrow_sprite(height_px: int, style: str = "dark") -> tuple[np.ndarray, np.ndarray, int]:
    """(bgr uint8 HxWx3, alpha float32 HxW, pad) with the hotspot at (pad, pad)."""
    fill_value, outline_value = STYLES[style]
    s = height_px / ARROW_UNITS * SUPERSAMPLE
    pad = max(2, int(0.12 * height_px)) * SUPERSAMPLE
    pts = ARROW * s + pad
    w = int(pts[:, 0].max()) + pad + 4 * SUPERSAMPLE
    h = int(pts[:, 1].max()) + pad + 4 * SUPERSAMPLE
    poly = np.round(pts).astype(np.int32)
    outline = max(1.0, height_px / 26) * SUPERSAMPLE

    shadow = np.zeros((h, w), np.float32)
    offset = np.array([int(0.04 * height_px * SUPERSAMPLE), int(0.08 * height_px * SUPERSAMPLE)])
    cv2.fillPoly(shadow, [poly + offset], 1.0, cv2.LINE_AA)
    shadow = cv2.GaussianBlur(shadow, (0, 0), 0.06 * height_px * SUPERSAMPLE) * 0.45
    fill = np.zeros((h, w), np.float32)
    cv2.fillPoly(fill, [poly], 1.0, cv2.LINE_AA)
    edge = np.zeros((h, w), np.float32)
    cv2.polylines(edge, [poly], True, 1.0, int(round(outline)), cv2.LINE_AA)

    # premultiplied compositing: shadow, then white fill, then black outline
    pm = np.zeros((h, w, 3), np.float32)
    alpha = np.zeros((h, w), np.float32)
    for layer_alpha, value in ((shadow, 0.0), (fill, fill_value), (edge, outline_value)):
        pm = pm * (1 - layer_alpha[..., None]) + value * layer_alpha[..., None]
        alpha = alpha * (1 - layer_alpha) + layer_alpha

    size = (w // SUPERSAMPLE, h // SUPERSAMPLE)
    small_pm = cv2.resize(pm, size, interpolation=cv2.INTER_AREA)
    small_alpha = cv2.resize(alpha, size, interpolation=cv2.INTER_AREA)
    rgb = np.clip(small_pm / np.maximum(small_alpha[..., None], 1e-6), 0, 255)
    return rgb.astype(np.uint8), small_alpha.astype(np.float32), pad // SUPERSAMPLE


def draw_cursor(frame: np.ndarray, x: float, y: float, height_px: int, style: str = "dark") -> None:
    rgb, alpha, pad = arrow_sprite(max(8, int(round(height_px))), style)
    blend(frame, rgb, alpha, int(round(x)) - pad, int(round(y)) - pad)


def draw_ripple(frame: np.ndarray, x: float, y: float, age: float, duration: float, radius: float) -> None:
    if age < 0 or age > duration:
        return
    u = age / duration
    r = radius * (1 - (1 - u) ** 2)  # ease-out
    fade = (1 - u) ** 1.5
    r_int = int(math.ceil(r + 4))
    x0, y0 = int(round(x)) - r_int, int(round(y)) - r_int
    roi = frame[max(0, y0):y0 + 2 * r_int, max(0, x0):x0 + 2 * r_int]
    if roi.size == 0:
        return
    overlay = roi.copy()
    c = (int(round(x)) - max(0, x0), int(round(y)) - max(0, y0))
    cv2.circle(overlay, c, int(round(r)), (255, 255, 255), -1, cv2.LINE_AA)
    cv2.addWeighted(overlay, 0.22 * fade, roi, 1 - 0.22 * fade, 0, roi)
    overlay = roi.copy()
    cv2.circle(overlay, c, int(round(r)), (255, 255, 255), max(2, int(radius / 14)), cv2.LINE_AA)
    cv2.addWeighted(overlay, 0.75 * fade, roi, 1 - 0.75 * fade, 0, roi)


def blend(frame: np.ndarray, rgb: np.ndarray, alpha: np.ndarray, x: int, y: int) -> None:
    h, w = alpha.shape
    fh, fw = frame.shape[:2]
    x0, y0, x1, y1 = max(x, 0), max(y, 0), min(x + w, fw), min(y + h, fh)
    if x1 <= x0 or y1 <= y0:
        return
    sx, sy = x0 - x, y0 - y
    a = alpha[sy:sy + (y1 - y0), sx:sx + (x1 - x0)][..., None]
    src = rgb[sy:sy + (y1 - y0), sx:sx + (x1 - x0)].astype(np.float32)
    dst = frame[y0:y1, x0:x1]
    dst[:] = (src * a + dst.astype(np.float32) * (1 - a)).astype(np.uint8)
