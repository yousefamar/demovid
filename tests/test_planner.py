import pytest

from demovid.render.planner import Keyframe, PlanConfig, plan, state_at

CFG = PlanConfig(width=1920, height=1080, zoom=2.0, lead_s=0.5, zoom_in_s=0.8, zoom_out_s=1.0,
                 pan_s=0.6, hold_s=2.0)


def click(t, x, y, button="left"):
    return [{"t": t, "kind": "button", "button": button, "state": "down", "x": x, "y": y},
            {"t": t + 0.08, "kind": "button", "button": button, "state": "up", "x": x, "y": y}]


def keys(t0, chars, gap=0.12):
    out = []
    for i, ch in enumerate(chars):
        t = t0 + i * gap
        out.append({"t": t, "kind": "key", "key": ch, "state": "down", "mods": []})
        out.append({"t": t + 0.05, "kind": "key", "key": ch, "state": "up", "mods": []})
    return out


def st(frames, t):
    return state_at(frames, t, CFG.width, CFG.height)


def zoom_at(frames, t):
    return st(frames, t)[2]


def assert_monotonic(frames):
    assert all(b.t >= a.t for a, b in zip(frames, frames[1:]))
    assert all(f.zoom >= 1.0 for f in frames)


def test_no_events_is_a_single_rest_keyframe():
    assert plan([], CFG) == [Keyframe(0.0, 960.0, 540.0, 1.0)]


def test_cursor_only_never_zooms():
    events = [{"t": i * 0.01, "kind": "cursor", "x": 100 + i, "y": 200} for i in range(500)]
    frames = plan(events, CFG)
    assert all(f.zoom == 1.0 for f in frames)


def test_single_click_zooms_in_before_and_out_after():
    frames = plan(click(10.0, 600, 400), CFG)
    assert_monotonic(frames)
    assert zoom_at(frames, 9.0) == 1.0
    assert 1.0 < zoom_at(frames, 9.9) < 2.0          # mid zoom-in (starts at 9.5)
    assert zoom_at(frames, 10.3) == pytest.approx(2.0)  # settled at 9.5 + 0.8
    cx, cy, _ = st(frames, 11.0)
    assert (cx, cy) == (600, 400)
    assert zoom_at(frames, 12.0) == pytest.approx(2.0)  # hold until 10 + 2
    assert 1.0 < zoom_at(frames, 12.5) < 2.0            # zooming out
    assert zoom_at(frames, 13.1) == 1.0


def test_nearby_clicks_share_one_zoom_and_do_not_pan():
    frames = plan(click(10.0, 600, 400) + click(11.0, 650, 430) + click(12.0, 580, 380), CFG)
    assert_monotonic(frames)
    zoomed = [f for f in frames if f.zoom > 1.0]
    assert {(f.cx, f.cy) for f in zoomed} == {(600.0, 400.0)}
    assert zoom_at(frames, 13.5) == pytest.approx(2.0)   # hold extended to 12 + 2
    assert zoom_at(frames, 15.1) == 1.0


def test_far_click_while_zoomed_pans_minimally():
    frames = plan(click(10.0, 600, 400) + click(11.0, 1500, 400), CFG)
    assert_monotonic(frames)
    cx_before, _, z_before = st(frames, 10.4)
    cx_after, _, z_after = st(frames, 11.5)
    assert z_before == z_after == pytest.approx(2.0)
    assert cx_after > cx_before
    # crop at 2x is 960 wide; the point sits at the inner-60% edge, not dead centre
    half_keep = 1920 / 4 * CFG.keep_frac
    assert cx_after == pytest.approx(1500 - half_keep)


def test_activity_during_would_be_zoom_out_extends_hold_instead_of_bouncing():
    # second click lands 2.5 s after the first: inside hold(2)+zoom_out(1), so no bounce
    frames = plan(click(10.0, 600, 400) + click(12.5, 620, 410), CFG)
    assert_monotonic(frames)
    for t in (10.5, 11.5, 12.0, 12.5, 13.5, 14.4):
        assert zoom_at(frames, t) == pytest.approx(2.0), t
    assert zoom_at(frames, 15.6) == 1.0


def test_separate_clicks_make_two_segments():
    frames = plan(click(10.0, 600, 400) + click(20.0, 1200, 700), CFG)
    assert_monotonic(frames)
    assert zoom_at(frames, 13.1) == 1.0
    assert zoom_at(frames, 16.0) == 1.0
    assert zoom_at(frames, 20.3) == pytest.approx(2.0)
    assert st(frames, 21.0)[:2] == (1200, 700)


def test_click_near_the_edge_is_clamped_so_the_crop_stays_inside():
    frames = plan(click(10.0, 20, 1070), CFG)
    cx, cy, z = st(frames, 11.0)
    assert z == pytest.approx(2.0)
    assert (cx, cy) == (480, 810)  # crop is 960x540, so the centre can't be closer than half that


def test_typing_burst_zooms_on_the_cursor_and_isolated_keys_do_not():
    cursor = [{"t": 5.0, "kind": "cursor", "x": 800, "y": 300}]
    frames = plan(cursor + keys(10.0, "a"), CFG)
    assert all(f.zoom == 1.0 for f in frames)
    frames = plan(cursor + keys(10.0, "hello"), CFG)
    assert_monotonic(frames)
    assert zoom_at(frames, 10.8) == pytest.approx(2.0)
    assert st(frames, 10.8)[:2] == (800, 300)


def test_modifier_shortcuts_do_not_count_as_typing():
    events = []
    for i in range(6):
        t = 10.0 + i * 0.2
        events += [{"t": t, "kind": "key", "key": "ctrl", "state": "down", "mods": []},
                   {"t": t + 0.05, "kind": "key", "key": "c", "state": "down", "mods": ["ctrl"]},
                   {"t": t + 0.1, "kind": "key", "key": "c", "state": "up", "mods": ["ctrl"]},
                   {"t": t + 0.15, "kind": "key", "key": "ctrl", "state": "up", "mods": []}]
    assert all(f.zoom == 1.0 for f in plan(events, CFG))


def focus(t, rect):
    return [{"t": t, "kind": "focus", "con_id": 1, "app_id": "x", "title": "x", "rect": rect}]


def test_focus_on_a_floating_window_fits_it():
    frames = plan(focus(10.0, [400, 200, 800, 450]), CFG)
    assert_monotonic(frames)
    cx, cy, z = st(frames, 11.0)
    assert z == pytest.approx(2.4)
    assert (cx, cy) == (800, 425)


def test_focus_on_a_half_or_full_screen_window_does_not_zoom():
    # fit respects aspect ratio: a full-height tile can't be zoomed without cropping it
    assert all(f.zoom == 1.0 for f in plan(focus(10.0, [0, 0, 960, 1080]), CFG))
    assert all(f.zoom == 1.0 for f in plan(focus(10.0, [0, 0, 1920, 1080]), CFG))


def test_typing_prefers_the_focused_window_over_the_cursor():
    events = [{"t": 5.0, "kind": "cursor", "x": 100, "y": 100}] + focus(9.0, [400, 200, 800, 450]) + keys(10.0, "hello")
    frames = plan(events, CFG)
    assert_monotonic(frames)
    cx, cy, z = st(frames, 11.0)
    assert (cx, cy, z) == (800, 425, pytest.approx(2.4))


def test_focus_change_while_zoomed_recentres_on_the_new_rect():
    events = click(10.0, 600, 400) + focus(11.0, [1000, 500, 800, 450])
    frames = plan(events, CFG)
    assert_monotonic(frames)
    cx, cy, z = st(frames, 12.0)
    assert (cx, cy, z) == (1400, 725, pytest.approx(2.4))


def test_unknown_kinds_and_missing_fields_are_ignored():
    events = [{"t": 1.0, "kind": "stream", "stream": "screen", "event": "first_frame"},
              {"t": 2.0, "kind": "mystery"},
              {"t": 3.0, "kind": "button", "state": "down"},  # no coords: falls back to cursor centre
              {"kind": "cursor", "x": 5, "y": 5}]
    frames = plan(events, CFG)
    assert_monotonic(frames)
    assert st(frames, 4.0) == (480.0, 270.0, pytest.approx(2.0))  # cursor (5,5) clamped at 2x


def test_no_zoom_config_is_expressible():
    frames = plan(click(10.0, 600, 400), PlanConfig(zoom=1.0, focus_zoom=False))
    assert all(f.zoom == 1.0 for f in frames)


def test_state_at_eases_between_keyframes():
    frames = [Keyframe(0.0, 0.0, 0.0, 1.0), Keyframe(1.0, 100.0, 0.0, 2.0)]
    assert state_at(frames, -1)[0] == 0.0
    assert state_at(frames, 0.5) == (50.0, 0.0, 1.5)
    assert state_at(frames, 0.25)[0] < 25.0   # smoothstep starts slow
    assert state_at(frames, 5)[0] == 100.0


def test_state_at_clamps_the_crop_inside_the_frame_during_zoom_out():
    frames = [Keyframe(0.0, 480.0, 270.0, 2.0), Keyframe(1.0, 480.0, 270.0, 1.0)]
    assert state_at(frames, 0.0, 1920, 1080) == (480.0, 270.0, 2.0)
    cx, cy, z = state_at(frames, 0.5, 1920, 1080)
    assert z == 1.5 and cx == 640.0 and cy == 360.0
    assert state_at(frames, 1.0, 1920, 1080) == (960.0, 540.0, 1.0)
