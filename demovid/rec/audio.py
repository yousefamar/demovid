import re
import subprocess

TARGET_VOLUME_PCT = 85
MIN_VOLUME_PCT = 80


def _pactl(*args: str) -> str:
    try:
        return subprocess.run(["pactl", *args], capture_output=True, text=True, timeout=5).stdout
    except (subprocess.SubprocessError, OSError):
        return ""


def default_source() -> str | None:
    return _pactl("get-default-source").strip() or None


def source_volume_pct(source: str) -> int | None:
    m = re.search(r"(\d+)%", _pactl("get-source-volume", source))
    return int(m.group(1)) if m else None


def source_muted(source: str) -> bool:
    return "yes" in _pactl("get-source-mute", source)


def mic_warning(source: str) -> str | None:
    """Decision 4 (roadmap): verify the calibrated gain, never force it."""
    if source_muted(source):
        return f"mic is muted: pactl set-source-mute {source} 0"
    vol = source_volume_pct(source)
    if vol is not None and vol < MIN_VOLUME_PCT:
        return f"mic at {vol}% (calibrated {TARGET_VOLUME_PCT}%): pactl set-source-volume {source} {TARGET_VOLUME_PCT}%"
    return None
