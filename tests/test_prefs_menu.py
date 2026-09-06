import argparse
import json

import pytest

from demovid import menu, prefs
from demovid.rec import _options, add_args


@pytest.fixture(autouse=True)
def isolated_prefs(tmp_path, monkeypatch):
    monkeypatch.setattr(prefs, "PREFS_PATH", tmp_path / "prefs.json")


def rec_ns(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="rec")
    add_args(p)
    return p.parse_args(argv)


def test_defaults_when_no_file():
    assert prefs.load() == {"cam": True, "preview": True, "mic": True, "keys": True, "hide_cursor": False}


def test_toggle_round_trips_to_disk():
    assert prefs.toggle("cam") is False
    assert json.loads(prefs.PREFS_PATH.read_text())["cam"] is False
    assert prefs.load()["cam"] is False
    assert prefs.toggle("cam") is True


def test_unknown_pref_is_rejected():
    with pytest.raises(KeyError):
        prefs.toggle("nope")
    with pytest.raises(KeyError):
        prefs.set_value("nope", True)


def test_garbage_and_partial_files_fall_back_to_defaults():
    prefs.PREFS_PATH.write_text("not json")
    assert prefs.load()["cam"] is True
    prefs.PREFS_PATH.write_text(json.dumps({"cam": "yes", "mic": False}))
    values = prefs.load()
    assert values["cam"] is True      # wrong type ignored
    assert values["mic"] is False     # valid one honoured


def test_rec_uses_prefs_when_the_flag_is_absent():
    prefs.set_value("cam", False)
    prefs.set_value("hide_cursor", True)
    o = _options(rec_ns([]))
    assert o.cam_device is None and o.preview is False and o.hide_cursor is True


def test_explicit_rec_flags_beat_prefs_both_ways():
    prefs.set_value("cam", False)
    assert _options(rec_ns(["--cam"])).cam_device == "/dev/video0"
    prefs.set_value("cam", True)
    assert _options(rec_ns(["--no-cam"])).cam_device is None
    prefs.set_value("keys", False)
    assert _options(rec_ns(["--keys"])).log_keys is True
    prefs.set_value("hide_cursor", True)
    assert _options(rec_ns(["--show-cursor"])).hide_cursor is False


def test_preview_needs_the_camera():
    prefs.set_value("cam", False)
    prefs.set_value("preview", True)
    assert _options(rec_ns([])).preview is False


def test_menu_entries_are_ascii_safe_and_have_actions(monkeypatch):
    monkeypatch.setattr(menu, "latest", lambda: None)
    items = menu.entries()
    labels = [label for label, _ in items]
    actions = [action for _, action in items]
    assert actions[0] == "toggle-rec"
    assert "Start recording" in labels[0]
    assert [a for a in actions if a.startswith("toggle:")] == [
        "toggle:cam", "toggle:preview", "toggle:mic", "toggle:keys", "toggle:hide_cursor"]
    assert actions[-1] == "doctor"
    assert len(set(labels)) == len(labels)          # `--pick` matches on the label, so no duplicates
    for label in labels:
        assert all(ord(c) < 128 or 0xE000 <= ord(c) <= 0xF8FF for c in label)


def test_menu_while_recording_offers_pause_then_stop(monkeypatch):
    monkeypatch.setattr(menu, "latest", lambda: None)
    monkeypatch.setattr("demovid.rec.status",
                        lambda: {"recording": True, "paused": False, "dir": "/x", "pid": 1, "elapsed_s": 65.4})
    items = menu.entries()
    assert "Pause recording" in items[0][0] and "1:05" in items[0][0] and items[0][1] == "toggle-pause"
    assert "Stop recording" in items[1][0] and items[1][1] == "toggle-rec"
    assert all("(next recording)" in label for label, action in items if action.startswith("toggle:"))


def test_menu_while_paused_offers_resume_then_stop(monkeypatch):
    monkeypatch.setattr(menu, "latest", lambda: None)
    monkeypatch.setattr("demovid.rec.status",
                        lambda: {"recording": True, "paused": True, "dir": "/x", "pid": 1, "elapsed_s": 5.0})
    items = menu.entries()
    assert "Resume recording" in items[0][0] and items[0][1] == "toggle-pause"
    assert items[1][1] == "toggle-rec"


def test_click_pauses_a_live_recording_without_a_menu(monkeypatch):
    calls = []
    monkeypatch.setattr("demovid.rec.status",
                        lambda: {"recording": True, "paused": False, "dir": "/x", "pid": 1, "elapsed_s": 3.0})
    monkeypatch.setattr(menu, "run", lambda action: calls.append(action) or 0)
    monkeypatch.setattr(menu, "entries", lambda: (_ for _ in ()).throw(AssertionError("menu must not be built")))
    assert menu.main(argparse.Namespace(print=False, pick=None, lines=11, click=True)) == 0
    assert calls == ["toggle-pause"]


def test_click_while_paused_or_idle_opens_the_menu(monkeypatch):
    monkeypatch.setattr(menu, "latest", lambda: None)
    for st in ({"recording": True, "paused": True, "dir": "/x", "pid": 1, "elapsed_s": 3.0},
               {"recording": False, "paused": False, "stale": False}):
        monkeypatch.setattr("demovid.rec.status", lambda st=st: st)
        # --print short-circuits before fuzzel; reaching it proves the menu path was taken
        assert menu.main(argparse.Namespace(print=True, pick=None, lines=11, click=True)) == 0


def test_menu_pick_toggles_the_pref(monkeypatch):
    monkeypatch.setattr(menu, "latest", lambda: None)
    monkeypatch.setattr(menu, "notify", lambda body: None)
    monkeypatch.setattr("demovid.rec.state.poke_waybar", lambda: None)
    ns = argparse.Namespace(print=False, pick="toggle:mic", lines=11, click=False)
    assert menu.main(ns) == 0
    assert prefs.load()["mic"] is False


def test_menu_pick_rejects_an_unknown_entry(monkeypatch, capsys):
    monkeypatch.setattr(menu, "latest", lambda: None)
    ns = argparse.Namespace(print=False, pick="toggle:nothing", lines=11, click=False)
    assert menu.main(ns) == 1
