"""ffmpeg subprocess plumbing: raw-frame readers, the encoder, and the audio filter chain."""

import json
import queue
import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np

LOUDNORM_TARGET = "I=-14:TP=-1.5:LRA=11"
# two voices levelled to -14 LUFS each can sum past 0 dBTP when they overlap; the limiter catches that
MIX_LIMITER = "alimiter=limit=0.891:level=false"


@dataclass(frozen=True)
class AudioTrack:
    """One recorded audio stream: `path` starts at `offset_s` on the recording clock."""
    name: str
    path: Path
    offset_s: float
    denoise: bool = False


class FrameReader:
    """Decodes a video to CFR bgr24 frames starting at `seek_s`, `duration_s` long."""

    def __init__(self, path: Path, fps: float, seek_s: float, duration_s: float | None, height: int | None = None):
        self.path = path
        src_w, src_h = probe_size(path)
        if height and height != src_h:
            self.width, self.height = int(round(src_w * height / src_h / 2)) * 2, height
        else:
            self.width, self.height = src_w, src_h
        filters = [f"fps={fps:g}"]
        if (self.width, self.height) != (src_w, src_h):
            filters.append(f"scale={self.width}:{self.height}")
        cmd = ["ffmpeg", "-v", "error", "-nostats", "-hide_banner"]
        if seek_s > 0:
            cmd += ["-ss", f"{seek_s:.6f}"]
        cmd += ["-i", str(path)]
        if duration_s is not None:
            cmd += ["-t", f"{max(duration_s, 0):.6f}"]
        cmd += ["-map", "0:v:0", "-vf", ",".join(filters), "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        self.frame_bytes = self.width * self.height * 3

    def read(self) -> np.ndarray | None:
        arr = np.empty(self.frame_bytes, np.uint8)
        view = memoryview(arr)
        n = 0
        while n < self.frame_bytes:
            k = self.proc.stdout.readinto(view[n:])
            if not k:
                return None
            n += k
        return arr.reshape(self.height, self.width, 3)

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.stdout.close()
        err = self.proc.stderr.read().decode(errors="replace").strip()
        self.proc.wait()
        if err:
            print(f"[decode {self.path.name}] {err}")


class Encoder:
    def __init__(self, out: Path, width: int, height: int, fps: float, encoder: str, quality: int,
                 audio: list[tuple[Path, float, float | None]] | None = None, audio_filter: str = "",
                 video_filter: str = ""):
        """`audio` = (path, seek_s, duration_s) per input; `audio_filter` is a filter_complex graph reading
        `[1:a]`..`[N:a]` and writing `[aout]` (see audio_chain)."""
        cmd = ["ffmpeg", "-v", "error", "-nostats", "-hide_banner", "-y",
               "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{width}x{height}", "-r", f"{fps:g}", "-i", "pipe:0"]
        for path, seek, duration in audio or []:
            if seek > 0:
                cmd += ["-ss", f"{seek:.6f}"]
            if duration is not None:
                cmd += ["-t", f"{duration:.6f}"]
            cmd += ["-i", str(path)]
        cmd += ["-map", "0:v:0"]
        if video_filter:
            cmd += ["-vf", video_filter]
        if audio:
            # apad (inside the graph) + -shortest: the video always decides the length; an audio track that
            # stopped early gets silence
            cmd += ["-filter_complex", audio_filter, "-map", "[aout]", "-c:a", "aac", "-b:a", "160k", "-ar", "48000"]
        if encoder == "h264_nvenc":
            cmd += ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", str(quality), "-b:v", "0"]
        else:
            cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(quality)]
        cmd += ["-pix_fmt", "yuv420p", "-movflags", "+faststart", "-shortest", str(out)]
        self.out = out
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)

    def write(self, frame: np.ndarray) -> None:
        self.proc.stdin.write(np.ascontiguousarray(frame).tobytes())

    def close(self) -> int:
        self.proc.stdin.close()
        err = self.proc.stderr.read().decode(errors="replace").strip()
        rc = self.proc.wait()
        if err:
            print(f"[encode] {err}")
        return rc


class Prefetcher:
    """Runs reader.read() on a thread so decoding overlaps with composition."""

    def __init__(self, reader: FrameReader, depth: int = 6):
        self.reader = reader
        self.q: queue.Queue = queue.Queue(maxsize=depth)
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        while True:
            try:
                frame = self.reader.read()
            except (ValueError, OSError):  # close() shut the pipe under us: the render already has every frame it wanted
                frame = None
            self.q.put(frame)
            if frame is None:
                return

    def read(self) -> np.ndarray | None:
        return self.q.get()

    def close(self) -> None:
        self.reader.close()
        while self.thread.is_alive():
            try:
                self.q.get_nowait()
            except queue.Empty:
                pass
            self.thread.join(timeout=0.05)


class AsyncEncoder:
    """Feeds the encoder from a thread so pipe writes overlap with composition."""

    def __init__(self, encoder: Encoder, depth: int = 6):
        self.encoder = encoder
        self.q: queue.Queue = queue.Queue(maxsize=depth)
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        while True:
            frame = self.q.get()
            if frame is None:
                return
            try:
                self.encoder.write(frame)
            except BaseException as e:  # surfaced by close()
                self.error = e
                return

    def write(self, frame: np.ndarray) -> None:
        if self.error is not None:
            raise RuntimeError("encoder failed") from self.error
        self.q.put(frame)

    def close(self) -> int:
        self.q.put(None)
        self.thread.join()
        return self.encoder.close()


def probe_size(path: Path) -> tuple[int, int]:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True, check=True)
    w, h = r.stdout.strip().split(",")[:2]
    return int(w), int(h)


def probe_duration(path: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True, check=True)
    return float(r.stdout.strip())


def level_track(mic: Path, win_s: float = 0.1, rate: int = 48000) -> list[float]:
    """RMS level in dBFS per `win_s` window over the whole mic track."""
    # astats' reset counts frames, so cut the stream into fixed win_s frames first
    r = subprocess.run(["ffmpeg", "-v", "info", "-nostats", "-hide_banner", "-i", str(mic),
                        "-af", f"aformat=channel_layouts=mono:sample_rates={rate},asetnsamples=n={int(win_s * rate)},"
                               "astats=metadata=1:reset=1,ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-",
                        "-f", "null", "-"], capture_output=True, text=True)
    levels = []
    for m in re.finditer(r"RMS_level=(-?[0-9.]+|-inf)", r.stdout):
        levels.append(-120.0 if m.group(1) == "-inf" else float(m.group(1)))
    return levels


def silences(tracks: list[AudioTrack], min_s: float = 0.6) -> list[tuple[float, float]]:
    """Stretches where nobody is talking on ANY track, on the recording clock. Each track's threshold
    adapts to its own floor; a track whose floor cannot be trusted only counts digital silence as quiet."""
    from demovid.render.timing import intersect, quiet_intervals, speech_threshold

    win_s = 0.1
    quiet: list[tuple[float, float]] | None = None
    for tr in tracks:
        levels = level_track(tr.path, win_s)
        thresh = speech_threshold(levels)
        if thresh is None:
            thresh = -100.0
        mine = [(a + tr.offset_s, b + tr.offset_s) for a, b in quiet_intervals(levels, win_s, thresh, min_s)]
        quiet = mine if quiet is None else intersect(quiet, mine)
    return quiet or []


def _track_pre(track: AudioTrack, t_from: float, keep: list[tuple[float, float]] | None) -> tuple[list[str], float]:
    """Filters that put `track` on the render's clock (filter time = t - t_from), plus the input seek."""
    from demovid.render.timing import aselect_expr

    seek = max(t_from - track.offset_s, 0.0)
    delay_ms = max(track.offset_s - t_from, 0.0) * 1000
    pre = []
    if delay_ms > 0.5:
        pre.append(f"adelay={delay_ms:.0f}:all=1")
    if keep is not None:
        pre.append(f"aselect='{aselect_expr(keep, t_from)}',asetpts=N/SR/TB")
    if track.denoise:
        pre.append("afftdn=nr=10:nf=-40:tn=1")
    return pre, seek


def _loudnorm(track: AudioTrack, pre: list[str], seek: float, duration: float | None) -> str:
    """Two-pass loudnorm: measure the track as it will be heard (after `pre`), then apply linearly."""
    measure = ",".join(pre + [f"loudnorm={LOUDNORM_TARGET}:print_format=json"])
    cmd = ["ffmpeg", "-v", "info", "-nostats", "-hide_banner"]
    if seek > 0:
        cmd += ["-ss", f"{seek:.6f}"]
    if duration is not None:
        cmd += ["-t", f"{duration:.6f}"]
    cmd += ["-i", str(track.path), "-af", measure, "-f", "null", "-"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", r.stderr, re.S)
    if not m:
        print(f"[audio] {track.name}: loudnorm measurement failed; falling back to single-pass")
        return f"loudnorm={LOUDNORM_TARGET}"
    stats = json.loads(m.group(0))
    print(f"[audio] {track.name}: measured {float(stats['input_i']):.1f} LUFS, peak {float(stats['input_tp']):.1f} dBTP"
          f" -> -14 LUFS")
    return (f"loudnorm={LOUDNORM_TARGET}:measured_I={stats['input_i']}:measured_TP={stats['input_tp']}"
            f":measured_LRA={stats['input_lra']}:measured_thresh={stats['input_thresh']}"
            f":offset={stats['target_offset']}:linear=true")


def audio_chain(tracks: list[AudioTrack], t_from: float, t_to: float | None,
                keep: list[tuple[float, float]] | None = None) -> tuple[str, list[tuple[Path, float, float | None]]]:
    """The encoder's audio: every track levelled to -14 LUFS (two-pass loudnorm, afftdn first where
    `denoise`), then, with more than one, summed as stereo and limited. Returns the filter_complex graph
    (inputs `[1:a]`.., output `[aout]`) and the (path, seek, duration) per input, in order.

    Each track starts at its own offset_s on the recording clock; the render starts at t_from. A track
    that starts after t_from is padded with silence via adelay, one that started before is seeked into.
    `keep` = source-time intervals to keep (idle speed-up drops the rest); filter time is t - t_from.
    """
    duration = (t_to - t_from) if t_to is not None else None
    inputs: list[tuple[Path, float, float | None]] = []
    chains: list[str] = []
    for i, tr in enumerate(tracks, start=1):
        pre, seek = _track_pre(tr, t_from, keep)
        inputs.append((tr.path, seek, duration))
        chain = pre + [_loudnorm(tr, pre, seek, duration)]
        if len(tracks) > 1:
            # loudnorm outputs 192 kHz; bring the tracks to one rate and layout before summing them
            chain += ["aresample=48000", "aformat=channel_layouts=stereo"]
        chains.append(f"[{i}:a]{','.join(chain)}[a{i}]")
    if len(tracks) == 1:
        graph = ";".join(chains).removesuffix("[a1]") + ",apad[aout]"
    else:
        labels = "".join(f"[a{i}]" for i in range(1, len(tracks) + 1))
        graph = ";".join(chains) + f";{labels}amix=inputs={len(tracks)}:normalize=0:duration=longest,{MIX_LIMITER},apad[aout]"
    return graph, inputs
