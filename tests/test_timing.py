import pytest

from demovid.render.timing import IdleConfig, Segment, TimeMap, aselect_expr, compress, idle_intervals, intersect

CFG = IdleConfig(min_idle_s=2.0, guard_after_s=3.0, guard_before_s=1.0, speed=8.0, max_out_s=1.5)


def click(t):
    return {"t": t, "kind": "button", "button": "left", "state": "down", "x": 10, "y": 10}


def test_no_activity_is_one_idle_stretch_with_no_guards():
    assert idle_intervals([], 0.0, 30.0, CFG) == [(0.0, 30.0)]


def test_guards_keep_hold_and_lead_in_at_normal_speed():
    ivs = idle_intervals([click(10.0), click(30.0)], 0.0, 40.0, CFG)
    # 0..10: no leading guard (recording start), 1 s before the click
    # 10..30: 3 s after, 1 s before
    # 30..40: 3 s after, no trailing guard (recording end)
    assert ivs == [(0.0, 9.0), (13.0, 29.0), (33.0, 40.0)]


def test_short_gaps_are_left_alone():
    ivs = idle_intervals([click(10.0), click(15.5)], 0.0, 16.0, CFG)
    assert ivs == [(0.0, 9.0)]  # 13.0..14.5 is only 1.5 s; after 15.5 the guard eats the rest


def test_speech_blocks_speed_up():
    ivs = idle_intervals([click(10.0)], 0.0, 40.0, CFG, silences=[(20.0, 40.0)])
    assert ivs == [(20.0, 40.0)]  # 0..9 is talking, 13..20 is talking


def test_intersect():
    assert intersect([(0, 10), (20, 30)], [(5, 25)]) == [(5, 10), (20, 25)]
    assert intersect([(0, 10)], [(10, 20)]) == []


def test_compress_caps_output_length():
    segs = compress([(0.0, 8.0), (10.0, 50.0)], CFG)
    assert segs[0] == Segment(0.0, 8.0, 8.0)          # 8 s / 8 = 1 s, under the cap
    assert segs[1].speed == pytest.approx(40 / 1.5)   # 40 s collapses to 1.5 s
    assert segs[1].out_len == pytest.approx(1.5)


def test_timemap_round_trip_and_duration():
    tm = TimeMap(0.0, 40.0, [Segment(10.0, 30.0, 10.0)])
    assert tm.duration == pytest.approx(22.0)
    assert tm.saved_s == pytest.approx(18.0)
    assert tm.src(5.0) == pytest.approx(5.0)
    assert tm.src(10.0) == pytest.approx(10.0)
    assert tm.src(11.0) == pytest.approx(20.0)      # inside the fast segment, 10x
    assert tm.src(12.0) == pytest.approx(30.0)
    assert tm.src(22.0) == pytest.approx(40.0)
    for t in (0.0, 7.3, 10.5, 11.9, 15.0, 22.0):
        assert tm.out(tm.src(t)) == pytest.approx(t)
    assert tm.speed_at(15.0) == 10.0 and tm.speed_at(5.0) == 1.0


def test_timemap_without_segments_is_identity():
    tm = TimeMap(3.0, 10.0)
    assert tm.duration == pytest.approx(7.0)
    assert tm.src(2.5) == pytest.approx(5.5)
    assert tm.out(5.5) == pytest.approx(2.5)


def test_audio_keep_matches_output_length():
    tm = TimeMap(0.0, 40.0, [Segment(10.0, 30.0, 10.0), Segment(35.0, 40.0, 5.0)])
    keep = tm.audio_keep()
    assert keep == [(0.0, 12.0), (30.0, 36.0)]
    assert sum(b - a for a, b in keep) == pytest.approx(tm.duration)


def test_aselect_expr_is_relative_to_origin():
    assert aselect_expr([(5.0, 7.5), (9.0, 10.0)], 5.0) == "between(t,0.0000,2.5000)+between(t,4.0000,5.0000)"
    assert aselect_expr([], 0.0) == "0"


def test_stationary_cursor_samples_are_not_activity():
    from demovid.render.timing import activity_times
    still = [{"t": t / 100, "kind": "cursor", "x": 500, "y": 300} for t in range(0, 1000)]  # 10 s @ 100 Hz, no motion
    assert activity_times(still) == [0.0]
    moving = still + [{"t": 10.5, "kind": "cursor", "x": 520, "y": 300}]
    assert activity_times(moving) == [0.0, 10.5]


def test_speech_threshold_is_noise_floor_plus_margin():
    from demovid.render.timing import quiet_intervals, speech_threshold
    noise = [-36.0 + (i % 5) * 0.3 for i in range(200)]
    speech = [-28.0 + (i % 13) * 0.9 for i in range(200)]   # broad, -28..-17
    thresh = speech_threshold(noise + speech)
    assert thresh == pytest.approx(-35.5 + 6.0)
    levels = noise[:30] + speech[:20] + noise[:10]      # 3 s quiet, 2 s talk, 1 s quiet at 100 ms windows
    assert quiet_intervals(levels, 0.1, thresh, 0.6) == [(0.0, 3.0), (5.0, 6.0)]


def test_speech_threshold_refuses_untrustworthy_floor():
    from demovid.render.timing import speech_threshold
    assert speech_threshold([-40.0] * 5) is None
    # one dip then everything loud: the "floor" is a fluke, refuse rather than call speech silent
    assert speech_threshold([-60.0] + [-20.0 + (i % 4) for i in range(300)]) is None
    # a genuinely quiet recording (nobody talks, low floor) is all silence — that is allowed
    assert speech_threshold([-58.0 + (i % 3) * 0.5 for i in range(300)]) == pytest.approx(-57.5 + 6.0)
