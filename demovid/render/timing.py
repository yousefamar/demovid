"""Idle speed-up: find stretches with no input and no speech, compress them, remap time. Pure."""

from bisect import bisect_right
from dataclasses import dataclass

ACTIVITY_KINDS = {"cursor", "button", "key", "focus", "window"}


@dataclass(frozen=True)
class IdleConfig:
    min_idle_s: float = 2.0       # shorter quiet stretches are left alone
    guard_after_s: float = 3.0    # keep this much after the last activity (zoom hold + zoom-out play at 1x)
    guard_before_s: float = 1.0   # keep this much before the next activity (zoom lead-in plays at 1x)
    speed: float = 8.0            # compression factor for idle stretches
    max_out_s: float = 1.5        # an idle stretch never lasts longer than this in the output


@dataclass(frozen=True)
class Segment:
    t0: float
    t1: float
    speed: float

    @property
    def out_len(self) -> float:
        return (self.t1 - self.t0) / self.speed


def activity_times(events: list[dict], min_move_px: float = 2.0) -> list[float]:
    """Times of input activity. A cursor sample only counts if the pointer actually moved — some
    sources (Screenix) log positions at a fixed rate even while it is still."""
    out: list[float] = []
    last: tuple[float, float] | None = None
    for e in sorted(events, key=lambda e: e.get("t", 0.0)):
        kind = e.get("kind")
        if kind not in ACTIVITY_KINDS or "t" not in e:
            continue
        if kind == "cursor":
            if e.get("x") is None or e.get("y") is None:
                continue
            pos = (float(e["x"]), float(e["y"]))
            moved = last is None or abs(pos[0] - last[0]) >= min_move_px or abs(pos[1] - last[1]) >= min_move_px
            last = pos
            if not moved:
                continue
        out.append(float(e["t"]))
    return out


def idle_intervals(events: list[dict], t_from: float, t_to: float, cfg: IdleConfig,
                   silences: list[tuple[float, float]] | None = None) -> list[tuple[float, float]]:
    """Source-time intervals where nothing happens: no input for a while and (if known) nobody talking."""
    times = [t for t in activity_times(events) if t_from <= t <= t_to]
    bounds = [t_from] + times + [t_to]
    quiet: list[tuple[float, float]] = []
    for prev, nxt in zip(bounds, bounds[1:]):
        a = prev + (cfg.guard_after_s if prev != t_from else 0.0)
        b = nxt - (cfg.guard_before_s if nxt != t_to else 0.0)
        if b - a >= cfg.min_idle_s:
            quiet.append((a, b))
    if silences is not None:
        quiet = [iv for iv in intersect(quiet, silences) if iv[1] - iv[0] >= cfg.min_idle_s]
    return quiet


def intersect(a: list[tuple[float, float]], b: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    i = j = 0
    a, b = sorted(a), sorted(b)
    while i < len(a) and j < len(b):
        lo, hi = max(a[i][0], b[j][0]), min(a[i][1], b[j][1])
        if hi > lo:
            out.append((lo, hi))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return out


def compress(intervals: list[tuple[float, float]], cfg: IdleConfig) -> list[Segment]:
    segs = []
    for a, b in intervals:
        length = b - a
        speed = max(cfg.speed, length / cfg.max_out_s if cfg.max_out_s > 0 else cfg.speed)
        segs.append(Segment(a, b, speed))
    return segs


class TimeMap:
    """Piecewise-linear map between source time [t_from, t_to] and output time [0, duration]."""

    def __init__(self, t_from: float, t_to: float, segments: list[Segment] = ()):
        self.t_from, self.t_to = t_from, t_to
        self.segments = sorted((s for s in segments if s.t1 > s.t0), key=lambda s: s.t0)
        # breakpoints: (src, out) at the start and end of every segment
        self._src: list[float] = [t_from]
        self._out: list[float] = [0.0]
        for s in self.segments:
            gap = s.t0 - self._src[-1]
            self._src.append(s.t0)
            self._out.append(self._out[-1] + gap)
            self._src.append(s.t1)
            self._out.append(self._out[-1] + s.out_len)
        self._src.append(t_to)
        self._out.append(self._out[-1] + (t_to - self._src[-2]))
        self.duration = self._out[-1]

    @property
    def saved_s(self) -> float:
        return (self.t_to - self.t_from) - self.duration

    def src(self, t_out: float) -> float:
        return _piecewise(self._out, self._src, t_out)

    def out(self, t_src: float) -> float:
        return _piecewise(self._src, self._out, t_src)

    def speed_at(self, t_src: float) -> float:
        for s in self.segments:
            if s.t0 <= t_src < s.t1:
                return s.speed
        return 1.0

    def audio_keep(self) -> list[tuple[float, float]]:
        """Source intervals whose audio is kept; each idle stretch keeps its first out_len seconds."""
        keep: list[tuple[float, float]] = []
        cur = self.t_from
        for s in self.segments:
            keep.append((cur, s.t0 + s.out_len))
            cur = s.t1
        keep.append((cur, self.t_to))
        return [(a, b) for a, b in keep if b > a]


def _piecewise(xs: list[float], ys: list[float], x: float) -> float:
    if x <= xs[0]:
        return ys[0] + (x - xs[0])
    if x >= xs[-1]:
        return ys[-1] + (x - xs[-1])
    i = bisect_right(xs, x) - 1
    x0, x1, y0, y1 = xs[i], xs[i + 1], ys[i], ys[i + 1]
    if x1 <= x0:
        return y1
    return y0 + (y1 - y0) * (x - x0) / (x1 - x0)


def speech_threshold(levels_db: list[float], margin_db: float = 6.0, bin_db: float = 1.0) -> float | None:
    """Room noise is stationary, so the most common level in the quiet half of the track is the noise
    floor; anything `margin_db` above it is somebody talking. None if the floor cannot be trusted
    (too little audio, or almost every window is above it — the 'floor' was just a rare dip)."""
    vals = sorted(v for v in levels_db if v > -100)
    if len(vals) < 20:
        return None
    lower = vals[: max(1, len(vals) // 2)]
    hist: dict[int, int] = {}
    for v in lower:
        b = int(v // bin_db)
        hist[b] = hist.get(b, 0) + 1
    mode_bin = max(hist, key=lambda b: (hist[b], b))
    floor = (mode_bin + 0.5) * bin_db
    thresh = floor + margin_db
    above = sum(1 for v in vals if v >= thresh) / len(vals)
    if above > 0.98:
        return None
    if above < 0.02 and floor > -45.0:
        return None  # loud throughout: that "floor" is speech or music, not the room
    return thresh


def quiet_intervals(levels_db: list[float], win_s: float, thresh_db: float, min_s: float) -> list[tuple[float, float]]:
    """Runs of windows below the threshold lasting at least min_s, as (start, end) seconds."""
    out: list[tuple[float, float]] = []
    start = None
    for i, v in enumerate(levels_db + [float("inf")]):
        if v < thresh_db:
            if start is None:
                start = i
        elif start is not None:
            if (i - start) * win_s >= min_s:
                out.append((start * win_s, i * win_s))
            start = None
    return out


def aselect_expr(keep: list[tuple[float, float]], origin: float) -> str:
    """ffmpeg aselect expression keeping the given source intervals; filter time = source time - origin."""
    parts = [f"between(t,{a - origin:.4f},{b - origin:.4f})" for a, b in keep]
    return "+".join(parts) if parts else "0"
