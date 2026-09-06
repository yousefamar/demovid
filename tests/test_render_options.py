import argparse

import pytest

from demovid.render import DEFAULTS, OPTIONS, add_args, clip_rect, crop_events, crop_rect, explicit_dests, parse_rect
from demovid.render import presets


def ns_for(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="render")
    add_args(p)
    return p.parse_args(argv)


def test_preset_sets_untouched_options():
    ns = ns_for(["rec"])
    changed = presets.apply(ns, DEFAULTS, "framed", explicit=set())
    assert ns.pad == 0.06 and ns.idle_speed == 8.0
    assert "pad=0.06" in changed


def test_studio_renders_full_bleed():
    ns = ns_for(["rec"])
    presets.apply(ns, DEFAULTS, "studio", explicit=set())
    assert ns.pad == 0.0            # no margins by default; the padded look is `framed`


def test_explicit_flag_beats_the_preset_even_when_it_equals_the_default(monkeypatch):
    ns = ns_for(["rec", "--idle-speed", "0"])
    monkeypatch.setattr("sys.argv", ["demovid", "render", "rec", "--idle-speed", "0"])
    presets.apply(ns, DEFAULTS, "framed", explicit_dests(OPTIONS))
    assert ns.idle_speed == 0.0     # typed, so the preset's 8.0 must not win
    assert ns.pad == 0.06           # not typed, so the preset applies


def test_explicit_dests_reads_equals_form(monkeypatch):
    monkeypatch.setattr("sys.argv", ["demovid", "render", "rec", "--zoom=2.4", "--no-pip"])
    assert {"zoom", "no_pip"} <= explicit_dests(OPTIONS)
    assert "pad" not in explicit_dests(OPTIONS)


def test_unknown_preset_and_unknown_key():
    with pytest.raises(SystemExit):
        presets.apply(ns_for(["rec"]), DEFAULTS, "nope")
    with pytest.raises(SystemExit):
        presets.apply(ns_for(["rec"]), {"pad": 0.0}, "framed")   # 'bg' is not in these defaults


def test_parse_rect():
    assert parse_rect("10,20,300,400") == (10, 20, 300, 400)
    assert parse_rect("0,0,960x540") == (0, 0, 960, 540)
    with pytest.raises(argparse.ArgumentTypeError):
        parse_rect("10,20,0,400")


def test_clip_rect_keeps_the_overlap_and_refuses_a_sliver():
    assert clip_rect((0, 0, 960, 1080), 1920, 1080) == (0, 0, 960, 1080)
    assert clip_rect((-40, -10, 200, 200), 1920, 1080) == (0, 0, 160, 190)
    with pytest.raises(SystemExit):
        clip_rect((3000, 0, 100, 100), 1920, 1080)


def test_crop_events_shifts_and_drops():
    crop = (100, 50, 800, 600)
    events = [{"t": 0, "kind": "button", "state": "down", "x": 150, "y": 100},
              {"t": 1, "kind": "cursor", "x": 50, "y": 100},                      # left of the crop
              {"t": 2, "kind": "focus", "rect": [100, 50, 400, 300], "con_id": 1},
              {"t": 3, "kind": "key", "key": "a", "state": "down", "mods": []}]
    out = crop_events(events, crop)
    assert [e["kind"] for e in out] == ["button", "focus", "key"]
    assert (out[0]["x"], out[0]["y"]) == (50.0, 50.0)
    assert out[1]["rect"] == [0.0, 0.0, 400.0, 300.0]


def test_crop_rect_of_the_preview_window():
    assert crop_rect([1512, 840, 384, 216], (960, 0, 960, 1080)) == [552, 840, 384, 216]
    assert crop_rect([0, 0, 100, 100], (960, 0, 960, 1080)) is None


def test_openai_key_falls_back_to_the_config_file(tmp_path, monkeypatch):
    from demovid.render import captions

    monkeypatch.setattr(captions, "KEY_FILE", tmp_path / "openai_key")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert captions.openai_key() is None                 # nothing anywhere
    captions.KEY_FILE.write_text("sk-from-file\n")
    assert captions.openai_key() == "sk-from-file"        # file, stripped
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    assert captions.openai_key() == "sk-from-env"         # env wins
    monkeypatch.setenv("OPENAI_API_KEY", "   ")
    assert captions.openai_key() == "sk-from-file"        # blank env is not a key


def test_recorded_spans_and_span_selection():
    from demovid.render import fmt_time
    from demovid.render.timing import parse_span_list, recorded_spans

    ev = [{"t": 285.8, "kind": "pause"}, {"t": 1061.3, "kind": "resume"},
          {"t": 1203.0, "kind": "pause"}, {"t": 6648.1, "kind": "resume"}]      # resume written at stop
    spans = recorded_spans(ev, 0.3, 6648.1)
    assert [(round(a, 1), round(b, 1)) for a, b in spans] == [(0.3, 285.8), (1061.3, 1203.0)]  # no 0-length tail
    assert parse_span_list("1", 2) == [0] and parse_span_list("1,2", 2) == [0, 1]
    assert parse_span_list("2-4", 5) == [1, 2, 3]
    with pytest.raises(ValueError):
        parse_span_list("3", 2)
    with pytest.raises(ValueError):
        parse_span_list("x", 2)
    assert fmt_time(285.8) == "4:45.8" and fmt_time(0.3) == "0.3s" and fmt_time(600.04) == "10:00.0"
