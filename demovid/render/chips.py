"""Keystroke chips: the keys being pressed, drawn as rounded pills near the bottom of the frame."""

from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np

from demovid.render.cursor import blend

SUPERSAMPLE = 3
# DejaVu first: it is the only one here with ⌘ ⇧ ⌫ ⏎ coverage (verified 2026-09-05)
FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]
GLYPHS = {
    "enter": "⏎", "return": "⏎", "backspace": "⌫", "delete": "⌦", "tab": "⇥",
    "esc": "esc", "space": "space", "up": "↑", "down": "↓", "left": "←", "right": "→",
    "ctrl": "⌃", "alt": "⌥", "shift": "⇧", "super": "⌘",
    "pageup": "⇞", "pagedown": "⇟", "home": "⇱", "end": "⇲",
}
MOD_ORDER = ["ctrl", "alt", "shift", "super"]
# a modifier alone never earns a chip; Screenix imports name them bare ("alt"), rec uses evdev ("leftalt")
MOD_KEYS = {"leftctrl", "rightctrl", "leftalt", "rightalt", "leftshift", "rightshift", "leftmeta", "rightmeta",
            "ctrl", "alt", "shift", "super", "meta", "capslock"}
# a burst of ordinary typing reads as noise; only shortcuts and standalone editing keys earn a chip
TYPING_KEYS = set("abcdefghijklmnopqrstuvwxyz0123456789") | {
    "space", "comma", "dot", "slash", "semicolon", "apostrophe", "minus", "equal",
    "leftbrace", "rightbrace", "backslash", "grave",
}


def key_label(key: str) -> str:
    if key in GLYPHS:
        return GLYPHS[key]
    if len(key) == 1:
        return key.upper()
    if key.startswith("f") and key[1:].isdigit():
        return key.upper()
    return {"capslock": "Caps", "printscreen": "PrtSc"}.get(key, key.title())


def chips_from_events(events: list[dict], hold_s: float = 1.1,
                      shortcuts_only: bool = True) -> list[tuple[float, float, str]]:
    """(t_from, t_to, text) per chip. By default only shortcuts (with a modifier) and editing keys."""
    out: list[tuple[float, float, str]] = []
    for e in events:
        if e.get("kind") != "key" or e.get("state") != "down":
            continue
        key = str(e.get("key") or "")
        mods = [m for m in MOD_ORDER if m in (e.get("mods") or [])]
        if key in MOD_KEYS:
            continue
        if shortcuts_only and not mods and key in TYPING_KEYS:
            continue
        text = " ".join([GLYPHS[m] for m in mods] + [key_label(key)])
        t = float(e["t"])
        if out and out[-1][2] == text and t - out[-1][0] < hold_s:
            out[-1] = (out[-1][0], t + hold_s, text)  # repeat: extend rather than stack
        else:
            out.append((t, t + hold_s, text))
    return out


@lru_cache(maxsize=1)
def font_path() -> str | None:
    for p in FONT_CANDIDATES:
        if Path(p).exists():
            return p
    return None


@lru_cache(maxsize=64)
def chip_sprite(text: str, height: int) -> tuple[np.ndarray, np.ndarray]:
    """(bgr, alpha) for a dark rounded pill with the text centred."""
    from PIL import Image, ImageDraw, ImageFont

    H = height * SUPERSAMPLE
    pad_x, radius = int(0.42 * H), int(0.30 * H)
    fp = font_path()
    font = ImageFont.truetype(fp, int(0.52 * H)) if fp else ImageFont.load_default(int(0.52 * H))
    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    tw = int(probe.textlength(text, font=font))
    W = tw + 2 * pad_x
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, W - 1, H - 1), radius, fill=235)
    label = Image.new("L", (W, H), 0)
    ImageDraw.Draw(label).text((W / 2, H / 2), text, font=font, fill=255, anchor="mm")

    alpha = np.asarray(mask, np.float32) / 255.0
    text_a = np.asarray(label, np.float32) / 255.0
    bg = np.full((H, W, 3), 18.0, np.float32)
    rgb = bg * (1 - text_a[..., None]) + 245.0 * text_a[..., None]
    size = (max(1, W // SUPERSAMPLE), max(1, H // SUPERSAMPLE))
    return (cv2.resize(rgb, size, interpolation=cv2.INTER_AREA).astype(np.uint8),
            cv2.resize(alpha, size, interpolation=cv2.INTER_AREA))


def draw_chips(frame: np.ndarray, texts: list[tuple[str, float]], height: int, margin: int, gap: int) -> None:
    """Bottom-centred row, newest last. `texts` is (text, opacity)."""
    if not texts:
        return
    sprites = [(chip_sprite(t, height), o) for t, o in texts]
    total = sum(s[0][0].shape[1] for s in sprites) + gap * (len(sprites) - 1)
    x = (frame.shape[1] - total) // 2
    y = frame.shape[0] - margin - height
    for (rgb, alpha), opacity in sprites:
        pad = max(3, int(0.22 * height))
        shadow = np.zeros((rgb.shape[0] + 2 * pad, rgb.shape[1] + 2 * pad), np.float32)
        shadow[pad:pad + rgb.shape[0], pad:pad + rgb.shape[1]] = alpha
        shadow = cv2.GaussianBlur(shadow, (0, 0), pad / 2.2) * 0.5 * opacity
        blend(frame, np.zeros((*shadow.shape, 3), np.uint8), shadow, x - pad, y - pad + int(0.08 * height))
        blend(frame, rgb, alpha * opacity, x, y)
        x += rgb.shape[1] + gap


def visible_chips(chips: list[tuple[float, float, str]], t: float, fade_s: float = 0.18,
                  max_shown: int = 4) -> list[tuple[str, float]]:
    out = []
    for t0, t1, text in chips:
        if not (t0 - fade_s <= t <= t1):
            continue
        opacity = min(1.0, (t - t0 + fade_s) / max(fade_s, 1e-6), max(0.0, (t1 - t) / fade_s))
        if opacity > 0.02:
            out.append((text, min(1.0, opacity)))
    return out[-max_shown:]
