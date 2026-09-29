"""Captions: whisper-1 transcription of the audio tracks (cached in the recording dir) -> SRT in output time."""

import json
import os
import subprocess
import tempfile
import urllib.request
import uuid
from pathlib import Path

from demovid import CONFIG_DIR
from demovid.render.media import AudioTrack
from demovid.render.timing import TimeMap

API = "https://api.openai.com/v1/audio/transcriptions"
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
KEY_FILE = Path(CONFIG_DIR).expanduser() / "openai_key"


def openai_key() -> str | None:
    """`OPENAI_API_KEY`, else `~/.config/demovid/openai_key`.

    The env var only exists in interactive shells (it is exported from `~/.zshrc`), and the menu's
    render runs from sway via kitty — no rc file, no key. The file covers that case; `doctor` reports
    which source is in play.
    """
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if key:
        return key
    try:
        return KEY_FILE.read_text().strip() or None
    except OSError:
        return None


def transcribe(tracks: list[AudioTrack], cache: Path, language: str | None = None) -> list[dict]:
    """Segments [{start, end, text}] on tracks[0]'s own clock (the others are shifted onto it and summed,
    so a call's far side is captioned too). Cached at `cache` keyed by the files' size+mtime."""
    stats = [tr.path.stat() for tr in tracks]
    key = {"size": stats[0].st_size, "mtime": int(stats[0].st_mtime), "model": "whisper-1", "language": language}
    if len(tracks) > 1:
        key["tracks"] = [[tr.name, st.st_size, int(st.st_mtime)] for tr, st in zip(tracks, stats)]
    if cache.exists():
        try:
            data = json.loads(cache.read_text())
            if data.get("key") == key:
                return data["segments"]
        except (ValueError, KeyError):
            pass
    api_key = openai_key()
    if not api_key:
        raise SystemExit(f"--captions needs an OpenAI key: export OPENAI_API_KEY, or put it in {KEY_FILE} "
                         f"(the menu's render runs from sway, which never reads ~/.zshrc)")
    with tempfile.TemporaryDirectory() as td:
        # mono 16 kHz mp3 keeps an hour under whisper's 25 MB limit; flac would not
        mp3 = Path(td) / "audio.mp3"
        cmd = ["ffmpeg", "-v", "error", "-y"]
        for tr in tracks:
            cmd += ["-i", str(tr.path)]
        if len(tracks) > 1:
            cmd += ["-filter_complex", mix_graph(tracks), "-map", "[aout]"]
        cmd += ["-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "48k", str(mp3)]
        subprocess.run(cmd, check=True)
        if mp3.stat().st_size > MAX_UPLOAD_BYTES:
            raise SystemExit(f"audio is {mp3.stat().st_size / 1e6:.0f} MB compressed; whisper takes 25 MB (~70 min)")
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


def mix_graph(tracks: list[AudioTrack]) -> str:
    """Sum the tracks on tracks[0]'s clock: a later-starting track is delayed, an earlier one trimmed."""
    parts = ["[0:a]aformat=channel_layouts=stereo[a0]"]
    for i, tr in enumerate(tracks[1:], start=1):
        shift = tr.offset_s - tracks[0].offset_s
        if shift >= 0:
            align = f"adelay={shift * 1000:.0f}:all=1"
        else:
            align = f"atrim=start={-shift:.6f},asetpts=PTS-STARTPTS"
        parts.append(f"[{i}:a]{align},aformat=channel_layouts=stereo[a{i}]")
    labels = "".join(f"[a{i}]" for i in range(len(tracks)))
    return ";".join(parts) + f";{labels}amix=inputs={len(tracks)}:normalize=0:duration=longest[aout]"


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


def to_text(segments: list[dict], mic_offset_s: float, tm: TimeMap) -> str:
    """A readable transcript: one `[m:ss] text` line per segment inside the range, in output time."""
    lines = []
    for seg in segments:
        a, b = seg["start"] + mic_offset_s, seg["end"] + mic_offset_s
        if min(b, tm.t_to) - max(a, tm.t_from) < 0.15:
            continue
        m, sec = divmod(int(tm.out(max(a, tm.t_from))), 60)
        lines.append(f"[{m}:{sec:02d}] {seg['text']}")
    return "\n".join(lines) + "\n"


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
