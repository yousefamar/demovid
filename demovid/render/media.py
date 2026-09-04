"""ffmpeg subprocess plumbing: raw-frame readers, the encoder, and the audio filter chain."""

import json
import queue
import re
import subprocess
import threading
from pathlib import Path

import numpy as np

LOUDNORM_TARGET = "I=-14:TP=-1.5:LRA=11"


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
                 audio: Path | None = None, audio_filter: str = "", audio_offset_s: float = 0.0,
                 audio_duration_s: float | None = None):
        cmd = ["ffmpeg", "-v", "error", "-nostats", "-hide_banner", "-y",
               "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{width}x{height}", "-r", f"{fps:g}", "-i", "pipe:0"]
        if audio is not None:
            if audio_offset_s > 0:
                cmd += ["-ss", f"{audio_offset_s:.6f}"]
            cmd += ["-i", str(audio)]
            if audio_duration_s is not None:
                cmd += ["-t", f"{audio_duration_s:.6f}"]
        cmd += ["-map", "0:v:0"]
        if audio is not None:
            # apad + -shortest: the video always decides the length; a mic track that stopped early gets silence
            cmd += ["-map", "1:a:0", "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
                    "-af", ",".join(filter(None, [audio_filter, "apad"]))]
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
            frame = self.reader.read()
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


def audio_chain(mic: Path, stream_offset_s: float, t_from: float, t_to: float | None, denoise: bool) -> tuple[str, float, float | None]:
    """Two-pass loudnorm (+ optional afftdn). Returns (filter, input_seek_s, input_duration_s).

    The mic stream starts at stream_offset_s on the recording clock; the render starts at t_from.
    If the render starts before the mic does, the gap is padded with silence via adelay.
    """
    seek = max(t_from - stream_offset_s, 0.0)
    delay_ms = max(stream_offset_s - t_from, 0.0) * 1000
    duration = (t_to - t_from) if t_to is not None else None
    pre = []
    if delay_ms > 0.5:
        pre.append(f"adelay={delay_ms:.0f}:all=1")
    if denoise:
        pre.append("afftdn=nr=10:nf=-40:tn=1")
    measure = ",".join(pre + [f"loudnorm={LOUDNORM_TARGET}:print_format=json"])
    cmd = ["ffmpeg", "-v", "info", "-nostats", "-hide_banner"]
    if seek > 0:
        cmd += ["-ss", f"{seek:.6f}"]
    cmd += ["-i", str(mic)]
    if duration is not None:
        cmd += ["-t", f"{duration:.6f}"]
    cmd += ["-af", measure, "-f", "null", "-"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", r.stderr, re.S)
    if not m:
        print("[audio] loudnorm measurement failed; falling back to single-pass")
        return ",".join(pre + [f"loudnorm={LOUDNORM_TARGET}"]), seek, duration
    stats = json.loads(m.group(0))
    second = (f"loudnorm={LOUDNORM_TARGET}:measured_I={stats['input_i']}:measured_TP={stats['input_tp']}"
              f":measured_LRA={stats['input_lra']}:measured_thresh={stats['input_thresh']}"
              f":offset={stats['target_offset']}:linear=true")
    print(f"[audio] measured {float(stats['input_i']):.1f} LUFS, peak {float(stats['input_tp']):.1f} dBTP -> -14 LUFS")
    return ",".join(pre + [second]), seek, duration
