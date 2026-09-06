"""Named bundles of render flags. Built-ins here, user ones in ~/.config/demovid/presets.toml."""

import argparse
import tomllib
from pathlib import Path

from demovid import CONFIG_DIR

USER_FILE = Path(CONFIG_DIR).expanduser() / "presets.toml"

# keys are argparse dests of `demovid render`; a preset only sets what the command line left at its default
BUILTIN: dict[str, dict] = {
    "studio": {
        "pad": 0.06, "bg": "#1c1b33,#4b2a7a", "radius": 0.018,
        "idle_speed": 8.0, "captions": False,
    },
    "clean": {
        "no_zoom": True, "no_cursor": True, "no_ripple": True, "no_chips": True, "no_pip": True,
    },
    "talk": {
        "no_zoom": True, "pip_size": 0.22, "captions": True, "idle_speed": 0.0,
    },
    "social": {
        "pad": 0.08, "bg": "#0f2027,#2c5364", "captions": True, "idle_speed": 8.0, "zoom": 2.0,
    },
}


def load() -> dict[str, dict]:
    presets = dict(BUILTIN)
    if USER_FILE.exists():
        try:
            user = tomllib.loads(USER_FILE.read_text())
        except tomllib.TOMLDecodeError as e:
            raise SystemExit(f"{USER_FILE}: {e}")
        for name, values in user.items():
            if isinstance(values, dict):
                presets[name] = {**presets.get(name, {}), **values}
    return presets


def apply(ns: argparse.Namespace, defaults: dict, name: str, explicit: set[str] | None = None) -> list[str]:
    """Set preset values on ns for every option the user did not type; returns what changed."""
    presets = load()
    if name not in presets:
        raise SystemExit(f"unknown preset {name!r}; have: {', '.join(sorted(presets))}")
    changed = []
    for key, value in presets[name].items():
        if key not in defaults:
            raise SystemExit(f"preset {name}: unknown option {key!r}")
        if key in (explicit or set()):
            continue
        if getattr(ns, key) == defaults[key]:
            setattr(ns, key, value)
            changed.append(f"{key}={value}")
    return changed


def describe() -> str:
    lines = []
    for name, values in sorted(load().items()):
        src = "user" if USER_FILE.exists() and name in tomllib.loads(USER_FILE.read_text()) else "built-in"
        lines.append(f"{name:<10} ({src}) " + " ".join(f"{k}={v}" for k, v in values.items()))
    lines.append(f"\nuser presets: {USER_FILE} — [name] tables of render option = value (dests, e.g. zoom_hold = 3.0)")
    return "\n".join(lines)
