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
