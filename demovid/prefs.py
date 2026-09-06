"""Sticky `rec` defaults, toggled from the waybar menu. ~/.config/demovid/prefs.json.

A pref is only a DEFAULT: an explicit command-line flag always wins (see rec.add_args).
"""

import json
from pathlib import Path

from demovid import CONFIG_DIR

PREFS_PATH = Path(CONFIG_DIR).expanduser() / "prefs.json"

# name -> (default, one-line description shown in the menu)
SPEC: dict[str, tuple[bool, str]] = {
    "cam": (True, "Webcam recorded to cam.mp4"),
    "preview": (True, "Webcam preview window while recording"),
    "mic": (True, "Microphone recorded to mic.flac"),
    "keys": (True, "Keystrokes logged to events.jsonl"),
    "hide_cursor": (False, "Blank the real cursor while recording"),
}


def load() -> dict[str, bool]:
    values = {name: default for name, (default, _) in SPEC.items()}
    try:
        stored = json.loads(PREFS_PATH.read_text())
    except (OSError, ValueError):
        return values
    for name in values:
        if isinstance(stored.get(name), bool):
            values[name] = stored[name]
    return values


def save(values: dict[str, bool]) -> None:
    PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PREFS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({k: values[k] for k in SPEC if k in values}, indent=1) + "\n")
    tmp.replace(PREFS_PATH)


def toggle(name: str) -> bool:
    if name not in SPEC:
        raise KeyError(name)
    values = load()
    values[name] = not values[name]
    save(values)
    return values[name]


def set_value(name: str, value: bool) -> bool:
    if name not in SPEC:
        raise KeyError(name)
    values = load()
    values[name] = value
    save(values)
    return value


# --- render settings, cycled from the same menu (~/.config/demovid/render.json) ---
# Each setting is a list of values the menu steps through; `render_flags()` turns them into command
# line flags, which beat any preset, so the menu is the source of truth for these knobs. Anything not
# listed here stays a flag / presets.toml job.
RENDER_PATH = Path(CONFIG_DIR).expanduser() / "render.json"
OFF = "off"

RENDER_SPEC: dict[str, dict] = {
    "zoom": {"label": "Zoom", "values": [OFF, 1.4, 1.6, 1.8, 2.0, 2.4], "default": 1.8, "unit": "x"},
    "zoom_hold": {"label": "Zoom hold", "values": [1.0, 1.5, 2.0, 3.0, 4.0], "default": 2.0, "unit": "s"},
    "cursor_scale": {"label": "Cursor size", "values": [1.0, 2.0, 2.5, 3.0], "default": 2.5, "unit": "x"},
    "camera": {"label": "Camera", "values": ["br", "bl", "tr", "tl", OFF], "default": "br"},
    "chips": {"label": "Keystroke chips", "values": [True, False], "default": True},
    "captions": {"label": "Captions", "values": [False, True], "default": False},
    "idle_speed": {"label": "Idle speed-up", "values": [OFF, 4.0, 8.0], "default": 8.0, "unit": "x"},
    "padding": {"label": "Padding frame", "values": [OFF, 0.04, 0.06, 0.08], "default": OFF},
}


def render_load() -> dict:
    values = {name: spec["default"] for name, spec in RENDER_SPEC.items()}
    try:
        stored = json.loads(RENDER_PATH.read_text())
    except (OSError, ValueError):
        return values
    for name, spec in RENDER_SPEC.items():
        if stored.get(name) in spec["values"]:
            values[name] = stored[name]
    return values


def render_save(values: dict) -> None:
    RENDER_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = RENDER_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({k: values[k] for k in RENDER_SPEC if k in values}, indent=1) + "\n")
    tmp.replace(RENDER_PATH)


def render_cycle(name: str) -> object:
    """Step a setting to its next value and persist; returns the new value."""
    if name not in RENDER_SPEC:
        raise KeyError(name)
    values = render_load()
    options = RENDER_SPEC[name]["values"]
    values[name] = options[(options.index(values[name]) + 1) % len(options)]
    render_save(values)
    return values[name]


def render_display(name: str, value: object) -> str:
    if value is True:
        return "on"
    if value is False:
        return "off"
    if value == OFF:
        return "off"
    if name == "camera":
        return {"br": "bottom right", "bl": "bottom left", "tr": "top right", "tl": "top left"}[str(value)]
    return f"{value:g}{RENDER_SPEC[name].get('unit', '')}"


def render_flags(values: dict | None = None) -> list[str]:
    v = values if values is not None else render_load()
    flags: list[str] = []
    flags += ["--no-zoom"] if v["zoom"] == OFF else ["--zoom", f"{v['zoom']:g}", "--zoom-hold", f"{v['zoom_hold']:g}"]
    flags += ["--cursor-scale", f"{v['cursor_scale']:g}"]
    flags += ["--no-pip"] if v["camera"] == OFF else ["--pip-pos", str(v["camera"])]
    flags += [] if v["chips"] else ["--no-chips"]
    flags += ["--captions"] if v["captions"] else []
    flags += ["--idle-speed", "0" if v["idle_speed"] == OFF else f"{v['idle_speed']:g}"]
    flags += [] if v["padding"] == OFF else ["--pad", f"{v['padding']:g}"]
    return flags
