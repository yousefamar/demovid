from pathlib import Path

import pytest

from demovid.render.captions import split_cue, srt_time, subtitles_filter, to_srt
from demovid.render.timing import Segment, TimeMap

SEGS = [{"start": 1.0, "end": 3.0, "text": "hello there"},
        {"start": 4.0, "end": 6.0, "text": "second line"}]


def test_srt_time():
    assert srt_time(0) == "00:00:00,000"
    assert srt_time(3661.5) == "01:01:01,500"
    assert srt_time(-1) == "00:00:00,000"


def test_to_srt_is_in_output_time():
    tm = TimeMap(0.0, 10.0)
    srt = to_srt(SEGS, 0.0, tm)
    assert "00:00:01,000 --> 00:00:03,000" in srt
    assert srt.strip().endswith("second line")
    assert srt.count("-->") == 2


def test_to_srt_shifts_by_the_mic_offset_and_the_time_map():
    tm = TimeMap(0.0, 10.0, [Segment(3.0, 4.0, 10.0)])   # 1 s idle stretch becomes 0.1 s
    srt = to_srt(SEGS, 0.5, tm)
    # the first cue runs into the sped-up stretch, so its tail is compressed with the video
    assert "00:00:01,500 --> 00:00:03,050" in srt
    assert "00:00:03,600" in srt                        # 4.5 s source -> 3.6 s out
    assert srt.count("-->") == 2


def test_cues_outside_the_render_range_are_dropped():
    assert to_srt(SEGS, 0.0, TimeMap(3.5, 10.0)).count("-->") == 1
    assert to_srt(SEGS, 0.0, TimeMap(20.0, 30.0)) == ""


def test_split_cue_splits_long_text_proportionally():
    parts = split_cue("a" * 40 + " " + "b" * 40 + " " + "c" * 40, 0.0, 6.0, max_chars=45)
    assert len(parts) == 3
    assert parts[0][1][0] == 0.0
    assert parts[-1][1][1] == pytest.approx(6.0)
    assert all(len(text) <= 45 for text, _ in parts)


def test_short_text_is_one_cue():
    assert split_cue("short", 0.0, 1.0, 45) == [("short", (0.0, 1.0))]


def test_subtitles_filter_escapes_the_path_and_uses_libass_units():
    f = subtitles_filter(Path("/tmp/a b/c.srt"), out_h=1080, above_chips=True)
    assert "subtitles='/tmp/a b/c.srt'" in f
    assert "FontSize=12" in f and "MarginV=35" in f
    assert "MarginL=23" in subtitles_filter(Path("/x.srt"), 1080, False, side_margin_frac=0.06)
