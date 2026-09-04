import ctypes
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable

from .clock import Clock

PR_SET_PDEATHSIG = 1

# wf-recorder constructs its FrameWriter on the first captured frame and prints "Using video filter"
# (stdout, flushed) 1-2 ms later; its pts base is that frame's compositor `presented` timestamp.
SCREEN_MARKER_LATENCY_S = 0.0015
# ffmpeg's pulse input reports early packets late (connection backlog), so the mic start is the
# median of per-packet implied starts read in this window after spawn, not the first packet's pts.
MIC_ESTIMATE_WINDOW_S = (0.5, 2.5)


def _die_with_parent() -> None:
    ctypes.CDLL("libc.so.6", use_errno=True).prctl(PR_SET_PDEATHSIG, signal.SIGINT)


class Stream:
    """A capture subprocess whose stderr reveals when its first sample was taken.

    `marker(line, arrival_t)` gets every stderr/stdout line plus the clock-relative time it arrived
    and returns the stream's first-sample time (clock-relative) whenever it has a (better) estimate.
    """

    def __init__(
        self,
        name: str,
        file: str,
        cmd: list[str],
        marker: Callable[[str, float], float | None],
        env: dict[str, str] | None = None,
        log_lines: int = 400,
        stdout_pipe: bool = False,
    ):
        self.name = name
        self.file = file
        self.cmd = cmd
        self.marker = marker
        self.env = env
        self.stdout_pipe = stdout_pipe
        self.proc: subprocess.Popen | None = None
        self.spawned_t = 0.0
        self.offset_s: float | None = None
        self.first_frame = threading.Event()
        self.log: deque[str] = deque(maxlen=log_lines)
        self._clock: Clock | None = None
        self._reader: threading.Thread | None = None

    def start(self, clock: Clock) -> None:
        self._clock = clock
        self.spawned_t = clock.now()
        env = dict(os.environ)
        if self.env:
            env.update(self.env)
        read_fd, write_fd = os.pipe()
        try:
            self.proc = subprocess.Popen(
                self.cmd, stdin=subprocess.DEVNULL, stderr=write_fd,
                stdout=subprocess.PIPE if self.stdout_pipe else write_fd,
                env=env, preexec_fn=_die_with_parent,
            )
        finally:
            os.close(write_fd)
        self._output = os.fdopen(read_fd, "r", errors="replace")
        self._reader = threading.Thread(target=self._read_output, name=f"{self.name}-output", daemon=True)
        self._reader.start()

    def _read_output(self) -> None:
        assert self.proc and self._clock
        for line in self._output:
            arrival = self._clock.now()
            line = line.rstrip("\n")
            self.log.append(line)
            got = self.marker(line, arrival)
            if got is not None:
                self.offset_s = got
                self.first_frame.set()

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc else None

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def interrupt(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)

    def wait(self, timeout: float = 10.0) -> int | None:
        if not self.proc:
            return None
        if self.proc.poll() is None:
            try:
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait()
        if self._reader:
            self._reader.join(timeout=2)
        return self.proc.returncode

    def stop(self, timeout: float = 10.0) -> int | None:
        self.interrupt()
        return self.wait(timeout)

    def write_log(self, path: Path) -> None:
        path.write_text("\n".join(self.log) + "\n")


_WF_PTS = re.compile(r"Read frame with in pts (\d+)")
_FF_START = re.compile(r"^\s*Duration:.*?\bstart: (-?[\d.]+)")
_ASHOWINFO = re.compile(r"ashowinfo.*\bn:(\d+) pts:(-?\d+) .*\bnb_samples:(\d+)")


class _WfMarker:
    """Primary: arrival of wf-recorder's "Using video filter" line (printed on the first frame).
    Fallback if that line never shows: min over --log frame lines of (arrival - pts)."""

    def __init__(self):
        self.primary: float | None = None
        self.fallback: float | None = None

    def __call__(self, line: str, arrival: float) -> float | None:
        if line.startswith("Using video filter"):
            self.primary = arrival - SCREEN_MARKER_LATENCY_S
            return self.primary
        if self.primary is not None:
            return None
        m = _WF_PTS.search(line)
        if not m:
            return None
        est = arrival - int(m.group(1)) / 1e6
        if self.fallback is None or est < self.fallback:
            self.fallback = est
        return self.fallback


def screen_stream(out_dir: Path, output: str, fps: int, codec: str = "h264_nvenc", cq: int = 20) -> Stream:
    cmd = [
        "wf-recorder", "--log", "--no-damage",
        "-o", output,
        "-r", str(fps),
        "-c", codec, "-p", "preset=p4", "-p", "rc=vbr", "-p", f"cq={cq}",
        "-f", str(out_dir / "screen.mp4"),
    ]
    return Stream("screen", "screen.mp4", cmd, _WfMarker(), log_lines=200)


class _FfmpegMarker:
    def __init__(self, clock: Clock, to_t: Callable[[Clock, float], float]):
        self.clock = clock
        self.to_t = to_t
        self.done = False

    def __call__(self, line: str, arrival: float) -> float | None:
        if self.done:
            return None
        m = _FF_START.match(line)
        if not m:
            return None
        self.done = True
        return self.to_t(self.clock, float(m.group(1)))


class _MicMarker(_FfmpegMarker):
    """First the input dump's `start:`, then refined: every ashowinfo packet k implies
    start = pts_k - samples_before_k / rate; the median over the steady-state window wins."""

    def __init__(self, clock: Clock, sample_rate: int, spawned_at: Callable[[], float]):
        super().__init__(clock, lambda c, s: c.from_epoch(s))
        self.sample_rate = sample_rate
        self.spawned_at = spawned_at
        self.samples_before = 0
        self.estimates: list[float] = []

    def __call__(self, line: str, arrival: float) -> float | None:
        m = _ASHOWINFO.search(line)
        if not m:
            return super().__call__(line, arrival)
        pts_us, nb = int(m.group(2)), int(m.group(3))
        implied = self.clock.from_epoch(pts_us / 1e6 - self.samples_before / self.sample_rate)
        self.samples_before += nb
        since_spawn = arrival - self.spawned_at()
        lo, hi = MIC_ESTIMATE_WINDOW_S
        if since_spawn < lo or since_spawn > hi:
            return None
        self.estimates.append(implied)
        return sorted(self.estimates)[len(self.estimates) // 2]


def cam_stream(
    out_dir: Path, clock: Clock, device: str, width: int, height: int, fps: int,
    preview_size: tuple[int, int] | None, cq: int = 23,
) -> Stream:
    """Encodes the webcam to cam.mp4; with a preview size, also emits scaled I420 frames on stdout."""
    graph = "[0:v]format=yuv420p,split[rec][pv]" if preview_size else "[0:v]format=yuv420p[rec]"
    if preview_size:
        graph += f";[pv]scale={preview_size[0]}:{preview_size[1]}[pvo]"
    cmd = [
        "ffmpeg", "-hide_banner", "-nostdin", "-nostats", "-loglevel", "info",
        "-f", "v4l2", "-input_format", "mjpeg", "-video_size", f"{width}x{height}", "-framerate", str(fps),
        "-i", device,
        "-filter_complex", graph,
        "-map", "[rec]", "-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", str(cq),
        "-fps_mode", "vfr", str(out_dir / "cam.mp4"),
    ]
    if preview_size:
        cmd += ["-map", "[pvo]", "-f", "rawvideo", "pipe:1"]
    # v4l2 timestamps are CLOCK_MONOTONIC seconds
    marker = _FfmpegMarker(clock, lambda c, s: c.from_monotonic(s))
    return Stream("cam", "cam.mp4", cmd, marker, stdout_pipe=bool(preview_size))


class Preview:
    """A waylandsink window showing the cam stream's stdout frames.

    waylandsink only maps its window once the first frame arrives, so `spawn()` (get a pid for a
    Sway `[pid=N]` rule) and `run()` (start feeding frames) are separate steps. Frames are relayed
    by a thread so a dead preview never breaks ffmpeg's cam recording with EPIPE.
    """

    def __init__(self, cam: Stream, size: tuple[int, int], fps: int):
        self.cam = cam
        self.size = size
        self.fps = fps
        self.proc: subprocess.Popen | None = None

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc else None

    def spawn(self) -> int:
        if not shutil.which("gst-launch-1.0"):
            raise RuntimeError("gst-launch-1.0 not installed")
        w, h = self.size
        self.proc = subprocess.Popen(
            ["gst-launch-1.0", "-q", "fdsrc", "fd=0", "!",
             "rawvideoparse", f"width={w}", f"height={h}", "format=i420", f"framerate={self.fps}/1", "!",
             "videoconvert", "!", "waylandsink"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            preexec_fn=_die_with_parent,
        )
        return self.proc.pid

    def run(self) -> None:
        threading.Thread(target=self._relay, name="preview-relay", daemon=True).start()

    def _relay(self) -> None:
        assert self.cam.proc and self.cam.proc.stdout and self.proc and self.proc.stdin
        frame = self.size[0] * self.size[1] * 3 // 2
        sink: object = self.proc.stdin
        while True:
            data = self.cam.proc.stdout.read(frame)
            if not data:
                break
            if sink is not None:
                try:
                    sink.write(data)  # type: ignore[attr-defined]
                except (BrokenPipeError, OSError):
                    sink = None
        if sink is not None:
            try:
                sink.close()  # type: ignore[attr-defined]
            except OSError:
                pass

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def mic_stream(out_dir: Path, clock: Clock, source: str, sample_rate: int = 48000) -> Stream:
    # pulse timestamps are latency-corrected wall clock (epoch seconds); -copyts keeps them for ashowinfo
    cmd = [
        "ffmpeg", "-hide_banner", "-nostdin", "-nostats", "-loglevel", "info", "-copyts",
        "-f", "pulse", "-channels", "1", "-sample_rate", str(sample_rate), "-fragment_size", "1920",
        "-i", source,
        "-af", "asettb=AVTB,ashowinfo",
        "-c:a", "flac", str(out_dir / "mic.flac"),
    ]
    stream = Stream("mic", "mic.flac", cmd, lambda line, t: None)
    stream.marker = _MicMarker(clock, sample_rate, lambda: stream.spawned_t)
    return stream


def unsuspend_source(source: str) -> None:
    """The C615 emits ~150 ms of garbage when it wakes from SUSPENDED; wake it well before capture."""
    subprocess.run(["pactl", "suspend-source", source, "0"], capture_output=True, timeout=5)


def tool_versions() -> dict[str, str]:
    out: dict[str, str] = {}
    for tool, args, rx in [
        ("wf-recorder", ["--version"], r"([\d.]+)"),
        ("ffmpeg", ["-version"], r"ffmpeg version (\S+)"),
    ]:
        if not shutil.which(tool):
            continue
        try:
            txt = subprocess.run([tool, *args], capture_output=True, text=True, timeout=5).stdout
            m = re.search(rx, txt)
            out[tool] = m.group(1) if m else txt.strip().splitlines()[0]
        except (subprocess.SubprocessError, OSError):
            pass
    return out


def wait_first_frames(streams: list[Stream], timeout: float) -> list[str]:
    """Block until every stream saw its first frame (or died). Returns the names that didn't."""
    deadline = time.monotonic() + timeout
    missing = []
    for s in streams:
        remaining = max(0.0, deadline - time.monotonic())
        while not s.first_frame.wait(min(0.2, remaining) or 0.01):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not s.alive():
                break
        if s.offset_s is None:
            missing.append(s.name)
    return missing
