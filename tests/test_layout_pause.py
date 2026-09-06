import pytest

from demovid import layout
from demovid.render.timing import CUT, Segment, TimeMap, cuts, paused_intervals, subtract

WA = [0, 0, 1920, 1056]  # 24 px waybar along the bottom


def test_pip_sits_above_the_bar_with_the_layout_margin():
    x, y, side = layout.pip_square(1920, 1080, WA)
    assert side == 288
    assert x + side == 1920 - layout.MARGIN_PX
    assert y + side == 1056 - layout.MARGIN_PX          # above the bar, not on it


def test_preview_is_a_16px_aligned_square_the_pip_hides():
    px, py, ps = layout.pip_square(1920, 1080, WA)
    rx, ry, rw, rh = layout.preview_rect(1920, 1080, WA)
    assert rw == rh and rw % 16 == 0 and rw <= ps * layout.PREVIEW_FRAC
    assert px < rx and ry > py and rx + rw < px + ps and ry + rh < py + ps
    bx, by, bs = layout.pip_for_preview((rx, ry, rw, rh))
    # same centre (within rounding) and big enough that the squircle's inscribed square (0.84) covers it
    assert abs((bx + bs / 2) - (rx + rw / 2)) <= 1 and abs((by + bs / 2) - (ry + rh / 2)) <= 1
    assert bs * 0.84 >= rw


def test_no_workarea_means_screen_corner():
    x, y, side = layout.pip_square(1920, 1080, None)
    assert (x + side, y + side) == (1920 - layout.MARGIN_PX, 1080 - layout.MARGIN_PX)


def test_intersects():
    assert layout.intersects((0, 0, 10, 10), (5, 5, 10, 10))
    assert not layout.intersects((0, 0, 10, 10), (10, 0, 10, 10))


def test_paused_intervals_pair_up_and_clip():
    ev = [{"t": 1.0, "kind": "pause"}, {"t": 3.0, "kind": "resume"}, {"t": 8.0, "kind": "pause"}]
    assert paused_intervals(ev, 0.0, 10.0) == [(1.0, 3.0), (8.0, 10.0)]      # open pause runs to the end
    assert paused_intervals(ev, 2.0, 9.0) == [(2.0, 3.0), (8.0, 9.0)]
    assert paused_intervals([{"t": 1.0, "kind": "resume"}], 0.0, 5.0) == []   # stray resume ignored


def test_cut_segments_remove_time_and_audio():
    tm = TimeMap(0.0, 10.0, cuts([(2.0, 5.0)]))
    assert tm.duration == pytest.approx(7.0)
    assert tm.src(1.9) == pytest.approx(1.9)
    assert tm.src(2.0) == pytest.approx(5.0)          # the cut collapses to an instant
    assert tm.out(3.5) == pytest.approx(2.0)          # anything inside maps to the cut point
    assert tm.audio_keep() == [(0.0, 2.0), (5.0, 10.0)]
    assert Segment(2.0, 5.0, CUT).out_len == 0.0


def test_subtract_intervals():
    assert subtract([(0, 10)], [(2, 4), (6, 7)]) == [(0, 2), (4, 6), (7, 10)]
    assert subtract([(0, 10)], [(0, 10)]) == []
    assert subtract([(0, 5), (6, 8)], [(4, 7)]) == [(0, 4), (7, 8)]


def test_clamp_square_keeps_legacy_boxes_on_screen():
    assert layout.clamp_square((1476, 696, 480), 1920, 1080) == (1440, 600, 480)
    assert layout.clamp_square((10, 10, 100), 1920, 1080) == (10, 10, 100)
