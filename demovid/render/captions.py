"""Captions: whisper-1 transcription of mic.flac (cached in the recording dir) -> SRT in output time."""

import json
import os
import subprocess
import tempfile
import urllib.request
import uuid
from pathlib import Path

from demovid.render.timing import TimeMap

API = "https://api.openai.com/v1/audio/transcriptions"
MAX_UPLOAD_BYTES = 25 * 1024 * 1024


def transcribe(mic: Path, cache: Path, language: str | None = None) -> list[dict]:
    """Segments [{start, end, text}] on the mic's own clock. Cached at `cache` keyed by the flac's size+mtime."""
    st = mic.stat()
    key = {"size": st.st_size, "mtime": int(st.st_mtime), "model": "whisper-1", "language": language}
    if cache.exists():
        try:
            data = json.loads(cache.read_text())
            if data.get("key") == key:
                return data["segments"]
        except (ValueError, KeyError):
            pass
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("--captions needs OPENAI_API_KEY in the environment")
    with tempfile.TemporaryDirectory() as td:
        # mono 16 kHz mp3 keeps an hour under whisper's 25 MB limit; flac would not
        mp3 = Path(td) / "mic.mp3"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(mic), "-ac", "1", "-ar", "16000",
                        "-c:a", "libmp3lame", "-b:a", "48k", str(mp3)], check=True)
        if mp3.stat().st_size > MAX_UPLOAD_BYTES:
            raise SystemExit(f"mic track is {mp3.stat().st_size / 1e6:.0f} MB compressed; whisper takes 25 MB (~70 min)")
        fields = {"model": "whisper-1", "response_format": "verbose_json", "timestamp_granularities[]": "segment"}
        if language:
            fields["language"] = language
        body, ctype = multipart(fields, "file", mp3.name, mp3.read_bytes(), "audio/mpeg")
    req = urllib.request.Request(API, data=body, method="POST",
                                 headers={"Authorization": f"Bearer {api_key}", "Content-Type": ctype})
    with urllib.request.urlopen(req, timeout=600) as resp:
        result = json.loads(resp.read())
    segments = [{"start": float(s["start"]), "end": float(s["end"]), "text": s["text"].strip()}
                for s in result.get("segments", []) if s.get("text", "").strip()]
    cache.write_text(json.dumps({"key": key, "language": result.get("language"), "text": result.get("text", ""),
                                 "segments": segments}, indent=1, ensure_ascii=False))
    return segments


def multipart(fields: dict, file_field: str, filename: str, data: bytes, mime: str) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    out = bytearray()
    for k, v in fields.items():
        out += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
    out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; filename=\"{filename}\"\r\n"
            f"Content-Type: {mime}\r\n\r\n").encode()
    out += data + f"\r\n--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def to_srt(segments: list[dict], mic_offset_s: float, tm: TimeMap, max_chars: int = 84) -> str:
    """SRT text in output time for the segments that fall inside the rendered range."""
    cues = []
    for seg in segments:
        a, b = seg["start"] + mic_offset_s, seg["end"] + mic_offset_s
        a, b = max(a, tm.t_from), min(b, tm.t_to)
        if b - a < 0.15:
            continue
        for text, (sa, sb) in split_cue(seg["text"], a, b, max_chars):
            oa, ob = tm.out(sa), tm.out(sb)
            if ob - oa < 0.15:
                continue
            cues.append((oa, ob, text))
    lines = []
    for i, (a, b, text) in enumerate(cues, 1):
        lines += [str(i), f"{srt_time(a)} --> {srt_time(b)}", text, ""]
    return "\n".join(lines)


def split_cue(text: str, a: float, b: float, max_chars: int) -> list[tuple[str, tuple[float, float]]]:
    """Long whisper segments become several cues, time split proportionally to text length."""
    words = text.split()
    if len(text) <= max_chars or len(words) < 2:
        return [(text, (a, b))]
    chunks, cur = [], []
    for w in words:
        if cur and len(" ".join(cur + [w])) > max_chars:
            chunks.append(" ".join(cur))
            cur = []
        cur.append(w)
    if cur:
        chunks.append(" ".join(cur))
    total = sum(len(c) for c in chunks) or 1
    out, t = [], a
    for c in chunks:
        dt = (b - a) * len(c) / total
        out.append((c, (t, t + dt)))
        t += dt
    return out


def srt_time(t: float) -> str:
    t = max(t, 0.0)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int(round((s - int(s)) * 1000)) % 1000:03d}"


def subtitles_filter(srt: Path, out_h: int, above_chips: bool, side_margin_frac: float = 0.06) -> str:
    """ffmpeg `subtitles` (libass) filter with a Screen-Studio-ish caption style.

    libass lays SRT out on its default 384x288 PlayRes and scales to the video, so sizes are in those units.
    """
    size = 12
    margin = int(round(288 * (0.12 if above_chips else 0.05)))
    side = int(round(384 * side_margin_frac))
    style = (f"FontName=Inter,FontSize={size},Bold=1,PrimaryColour=&H00FFFFFF,OutlineColour=&H60000000,"
             f"BackColour=&H90000000,BorderStyle=4,Outline=0,Shadow=0,Alignment=2,MarginV={margin},MarginL={side},MarginR={side}")
    path = str(srt).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    return f"subtitles='{path}':force_style='{style}'"
