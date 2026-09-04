"""Zoom planner: events -> keyframes. Pure; never opens a video.

Keyframes are {t, cx, cy, zoom}. The renderer eases (smoothstep) between consecutive
keyframes, so a hold is two equal keyframes and a transition is two different ones.
"""

from dataclasses import dataclass, field, replace
from typing import Iterable

MODIFIER_KEYS = {"ctrl", "alt", "shift", "super", "leftctrl", "rightctrl", "leftalt", "rightalt",
                 "leftshift", "rightshift", "leftmeta", "rightmeta", "capslock"}


@dataclass(frozen=True)
class PlanConfig:
    width: int = 1920
    height: int = 1080
    zoom: float = 1.8            # zoom level for click/typing targets
    max_zoom: float = 2.5        # cap for focus-fit targets
    lead_s: float = 0.5          # start zooming this long before the click
    zoom_in_s: float = 0.8
    zoom_out_s: float = 1.0
    pan_s: float = 0.6           # re-target while zoomed
    hold_s: float = 2.0          # keep zoom this long after the last activity
    keep_frac: float = 0.6       # pan only when activity leaves the inner fraction of the crop
    min_pan_px: float = 8.0      # ignore pans smaller than this (edge clamping leftovers)
    typing_burst_keys: int = 3   # key-downs within typing_window_s that count as typing
    typing_window_s: float = 1.0
    focus_zoom: bool = True      # zoom to fit the focused window on focus change
    focus_min_fit: float = 1.15  # ignore focus rects that would zoom less than this
    ignore_buttons: frozenset = field(default_factory=lambda: frozenset({"side", "extra"}))


@dataclass(frozen=True)
class Keyframe:
    t: float
    cx: float
    cy: float
    zoom: float

    def as_dict(self) -> dict:
        return {"t": round(self.t, 4), "cx": round(self.cx, 2), "cy": round(self.cy, 2), "zoom": round(self.zoom, 4)}


@dataclass(frozen=True)
class Target:
    t: float
    cx: float
    cy: float
    zoom: float


def plan(events: Iterable[dict], cfg: PlanConfig = PlanConfig()) -> list[Keyframe]:
    targets = attention_targets(list(events), cfg)
    rest = Keyframe(0.0, cfg.width / 2, cfg.height / 2, 1.0)
    if not targets:
        return [rest]

    frames: list[Keyframe] = [rest]
    cur: Target | None = None      # what we're currently zoomed on
    zoomed_until = 0.0             # when the current hold expires
    for tg in targets:
        # Activity that lands during a would-be zoom-out just extends the hold: we know the
        # future, so never bounce out and straight back in.
        if cur is None or tg.t > zoomed_until + cfg.zoom_out_s:
            if cur is not None:
                frames += zoom_out(cur, zoomed_until, cfg)
            tg = clamp_target(tg, cfg)
            start = max(tg.t - cfg.lead_s, frames[-1].t)
            frames.append(Keyframe(start, rest.cx, rest.cy, 1.0))
            frames.append(Keyframe(start + cfg.zoom_in_s, tg.cx, tg.cy, tg.zoom))
            cur = tg
        else:
            nxt = retarget(cur, tg, cfg)
            if nxt is not None:
                start = max(tg.t - cfg.lead_s, frames[-1].t)
                frames.append(Keyframe(start, cur.cx, cur.cy, cur.zoom))
                frames.append(Keyframe(start + cfg.pan_s, nxt.cx, nxt.cy, nxt.zoom))
                cur = nxt
        zoomed_until = max(zoomed_until, tg.t + cfg.hold_s, frames[-1].t)
    frames += zoom_out(cur, zoomed_until, cfg)
    return dedupe(frames)


def zoom_out(cur: Target, at: float, cfg: PlanConfig) -> list[Keyframe]:
    return [Keyframe(at, cur.cx, cur.cy, cur.zoom), Keyframe(at + cfg.zoom_out_s, cur.cx, cur.cy, 1.0)]


def retarget(cur: Target, tg: Target, cfg: PlanConfig) -> Target | None:
    """Where to pan so tg is comfortably in view; None if it already is.

    A zoom change (focus fit) re-centres outright. Otherwise the centre moves by the smallest
    vector that brings the point inside the inner `keep_frac` of the current crop.
    """
    if abs(tg.zoom - cur.zoom) > 1e-3:
        return clamp_target(tg, cfg)
    half_w = cfg.width / (2 * cur.zoom) * cfg.keep_frac
    half_h = cfg.height / (2 * cur.zoom) * cfg.keep_frac
    dx = (tg.cx - (cur.cx + half_w)) if tg.cx > cur.cx + half_w else (tg.cx - (cur.cx - half_w)) if tg.cx < cur.cx - half_w else 0.0
    dy = (tg.cy - (cur.cy + half_h)) if tg.cy > cur.cy + half_h else (tg.cy - (cur.cy - half_h)) if tg.cy < cur.cy - half_h else 0.0
    if dx == 0.0 and dy == 0.0:
        return None
    nxt = clamp_target(Target(tg.t, cur.cx + dx, cur.cy + dy, cur.zoom), cfg)
    if abs(nxt.cx - cur.cx) < cfg.min_pan_px and abs(nxt.cy - cur.cy) < cfg.min_pan_px:
        return None
    return nxt


def attention_targets(events: list[dict], cfg: PlanConfig) -> list[Target]:
    """Moments worth zooming on, in time order: clicks, typing bursts, focus changes."""
    out: list[Target] = []
    cursor = (cfg.width / 2, cfg.height / 2)
    focus_rect: list | None = None
    recent_keys: list[float] = []
    typing_until = -1.0
    for e in sorted(events, key=lambda e: e.get("t", 0.0)):
        kind = e.get("kind")
        t = float(e.get("t", 0.0))
        if kind == "cursor" and e.get("x") is not None:
            cursor = (float(e["x"]), float(e["y"]))
        elif kind == "button":
            if e.get("state") != "down" or e.get("button") in cfg.ignore_buttons:
                continue
            x, y = e.get("x"), e.get("y")
            if x is None or y is None:
                x, y = cursor
            out.append(Target(t, float(x), float(y), cfg.zoom))
        elif kind == "key":
            if e.get("state") != "down" or str(e.get("key", "")).lower() in MODIFIER_KEYS or e.get("mods"):
                continue
            recent_keys = [k for k in recent_keys if t - k <= cfg.typing_window_s] + [t]
            if t <= typing_until:
                typing_until = t + cfg.hold_s
                out.append(typing_target(t, cursor, focus_rect, cfg))
            elif len(recent_keys) >= cfg.typing_burst_keys:
                typing_until = t + cfg.hold_s
                out.append(typing_target(recent_keys[0], cursor, focus_rect, cfg))
        elif kind == "focus":
            rect = e.get("rect")
            if rect and len(rect) == 4:
                focus_rect = [float(v) for v in rect]
                if cfg.focus_zoom:
                    tg = fit_target(t, focus_rect, cfg)
                    if tg is not None:
                        out.append(tg)
    out.sort(key=lambda tg: tg.t)
    return out


def typing_target(t: float, cursor: tuple, focus_rect: list | None, cfg: PlanConfig) -> Target:
    if focus_rect is not None:
        fit = fit_target(t, focus_rect, cfg)
        if fit is not None:
            return fit
    return Target(t, cursor[0], cursor[1], cfg.zoom)


def fit_target(t: float, rect: list, cfg: PlanConfig) -> Target | None:
    x, y, w, h = rect
    if w <= 0 or h <= 0:
        return None
    zoom = min(cfg.width / w, cfg.height / h, cfg.max_zoom)
    if zoom < cfg.focus_min_fit:
        return None
    return Target(t, x + w / 2, y + h / 2, zoom)


def clamp_target(tg: Target, cfg: PlanConfig) -> Target:
    cx, cy = clamp_center(tg.cx, tg.cy, tg.zoom, cfg.width, cfg.height)
    return replace(tg, cx=cx, cy=cy)


def clamp_center(cx: float, cy: float, zoom: float, width: float, height: float) -> tuple[float, float]:
    half_w, half_h = width / (2 * zoom), height / (2 * zoom)
    return min(max(cx, half_w), width - half_w), min(max(cy, half_h), height - half_h)


def dedupe(frames: list[Keyframe]) -> list[Keyframe]:
    out: list[Keyframe] = []
    for f in frames:
        if out and abs(out[-1].t - f.t) < 1e-6:
            out[-1] = f
        elif out and out[-1].t > f.t:
            continue
        else:
            out.append(f)
    return out


def smoothstep(u: float) -> float:
    u = min(max(u, 0.0), 1.0)
    return u * u * (3 - 2 * u)


def state_at(frames: list[Keyframe], t: float, width: float | None = None, height: float | None = None) -> tuple[float, float, float]:
    """(cx, cy, zoom) at time t, easing between neighbouring keyframes.

    Keyframe centres are desired centres; with width/height the result is clamped so the crop
    stays inside the frame (a zoom-out keeps its target centre and slides into place).
    """
    if not frames:
        raise ValueError("no keyframes")
    cx, cy, zoom = _interp(frames, t)
    if width is not None and height is not None:
        cx, cy = clamp_center(cx, cy, zoom, width, height)
    return cx, cy, zoom


def _interp(frames: list[Keyframe], t: float) -> tuple[float, float, float]:
    if t <= frames[0].t:
        f = frames[0]
        return f.cx, f.cy, f.zoom
    for a, b in zip(frames, frames[1:]):
        if t <= b.t:
            u = smoothstep((t - a.t) / (b.t - a.t)) if b.t > a.t else 1.0
            return a.cx + (b.cx - a.cx) * u, a.cy + (b.cy - a.cy) * u, a.zoom + (b.zoom - a.zoom) * u
    f = frames[-1]
    return f.cx, f.cy, f.zoom
