import json
from pathlib import Path

import numpy as np
import pytest

from demovid.rec.cursor import shape_id, unpremultiply
from demovid.render.chips import chips_from_events, chip_sprite, key_label, visible_chips
from demovid.render.cursor import ShapeTrack, draw_shape, load_shape
from demovid.rec import xcursor
from demovid.rec.xcursor import best_image

ADWAITA = Path("/usr/share/icons/Adwaita/cursors")


def write_shape(dir: Path, name: str, size: int = 24):
    """A real theme cursor saved the way rec saves compositor-captured ones."""
    import cv2

    _, w, h, xhot, yhot, bgra = best_image(ADWAITA / name, size)
    sid = shape_id(bgra)
    dir.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(dir / f"{sid}.png"), unpremultiply(bgra))
    return sid, (w, h), (xhot, yhot)


def test_shape_id_is_content_addressed():
    a = np.zeros((4, 4, 4), np.uint8)
    b = a.copy()
    b[0, 0, 0] = 1
    assert shape_id(a) == shape_id(a.copy())
    assert shape_id(a) != shape_id(b)


def test_unpremultiply_recovers_full_colour():
    bgra = np.array([[[128, 64, 32, 128]]], np.uint8)  # half-transparent, premultiplied
    assert list(unpremultiply(bgra)[0, 0]) == [255, 128, 64, 128]
    transparent = np.array([[[0, 0, 0, 0]]], np.uint8)
    assert list(unpremultiply(transparent)[0, 0]) == [0, 0, 0, 0]


@pytest.mark.skipif(not (ADWAITA / "left_ptr").exists(), reason="no Adwaita cursor theme")
def test_shape_track_picks_the_shape_in_force(tmp_path):
    shapes = tmp_path / "cursors"
    ptr, _, ptr_hot = write_shape(shapes, "left_ptr")
    hand, _, hand_hot = write_shape(shapes, "hand2")
    events = [
        {"t": 1.0, "kind": "cursor_shape", "id": ptr, "w": 24, "h": 24, "hotspot": list(ptr_hot), "scale": 1.0},
        {"t": 5.0, "kind": "cursor_shape", "id": hand, "w": 24, "h": 24, "hotspot": list(hand_hot), "scale": 1.0},
    ]
    track = ShapeTrack(events, shapes)
    assert track.present
    assert track.at(0.5)[2:] == ptr_hot          # before the first mark: first shape
    assert track.at(4.9)[2:] == ptr_hot
    assert track.at(5.1)[2:] == hand_hot
    assert track.at(900.0)[2:] == hand_hot       # last mark stands to the end


def test_shape_track_absent_without_dir_or_events(tmp_path):
    assert not ShapeTrack([], None).present
    assert ShapeTrack([], None).at(1.0) is None
    assert not ShapeTrack([{"t": 0, "kind": "cursor_shape", "id": "x"}], tmp_path / "nope").present


@pytest.mark.skipif(not (ADWAITA / "left_ptr").exists(), reason="no Adwaita cursor theme")
def test_missing_png_falls_back_to_the_arrow(tmp_path):
    shapes = tmp_path / "cursors"
    shapes.mkdir()
    (shapes / "keep.txt").write_text("x")
    track = ShapeTrack([{"t": 0.0, "kind": "cursor_shape", "id": "deadbeef", "hotspot": [0, 0]}], shapes)
    assert track.present          # the dir exists, so the track is live
    assert track.at(1.0) is None  # but the file is gone: render draws the synthetic arrow


@pytest.mark.skipif(not (ADWAITA / "left_ptr").exists(), reason="no Adwaita cursor theme")
def test_draw_shape_puts_the_hotspot_on_the_point(tmp_path):
    shapes = tmp_path / "cursors"
    sid, (w, h), hotspot = write_shape(shapes, "left_ptr")
    shape = (*load_shape(shapes / f"{sid}.png"), hotspot[0], hotspot[1])
    frame = np.zeros((200, 200, 3), np.uint8)
    draw_shape(frame, 100.0, 100.0, shape, 2.0)
    painted = np.argwhere(frame.any(axis=2))
    ys, xs = painted[:, 0], painted[:, 1]
    # the Adwaita pointer's tip is its hotspot: ink starts there and extends down-right
    assert 100 - 2 * hotspot[0] - 6 <= xs.min() <= 100
    assert 100 - 2 * hotspot[1] - 6 <= ys.min() <= 100 + 2
    assert xs.max() > 100 and ys.max() > 100 + h  # 24 px image at 2x reaches ~46 px below the tip


def test_chips_only_for_shortcuts_and_editing_keys():
    events = [
        {"t": 0.0, "kind": "key", "key": "a", "state": "down", "mods": []},
        {"t": 0.1, "kind": "key", "key": "leftctrl", "state": "down", "mods": []},
        {"t": 0.2, "kind": "key", "key": "c", "state": "down", "mods": ["ctrl"]},
        {"t": 0.3, "kind": "key", "key": "enter", "state": "down", "mods": []},
        {"t": 0.4, "kind": "key", "key": "c", "state": "up", "mods": ["ctrl"]},
    ]
    texts = [c[2] for c in chips_from_events(events)]
    assert texts == ["⌃ C", "⏎"]
    assert [c[2] for c in chips_from_events(events, shortcuts_only=False)][0] == "A"


def test_chips_extend_rather_than_stack_on_repeat():
    events = [{"t": t, "kind": "key", "key": "tab", "state": "down", "mods": []} for t in (0.0, 0.3, 0.6)]
    chips = chips_from_events(events, hold_s=1.0)
    assert len(chips) == 1
    assert chips[0][1] == pytest.approx(1.6)


def test_chips_ignore_bare_modifiers_from_both_naming_schemes():
    for name in ("alt", "leftalt", "super", "rightmeta"):
        assert chips_from_events([{"t": 0, "kind": "key", "key": name, "state": "down", "mods": []}]) == []


def test_visible_chips_fade_in_and_out_and_cap():
    chips = [(float(i), i + 1.0, f"K{i}") for i in range(6)]
    assert visible_chips(chips, -5.0) == []
    assert [t for t, _ in visible_chips(chips, 0.5)] == ["K0"]
    mid = visible_chips(chips, -0.09)  # half way through the 0.18 s fade-in
    assert 0.3 < mid[0][1] < 0.7
    assert len(visible_chips([(0.0, 10.0, f"K{i}") for i in range(9)], 5.0)) == 4


def test_key_label_glyphs_and_titles():
    assert key_label("enter") == "⏎"
    assert key_label("a") == "A"
    assert key_label("f5") == "F5"
    assert key_label("pageup") == "⇞"
    assert key_label("insert") == "Insert"


def test_chip_sprite_is_a_pill_with_ink(tmp_path):
    rgb, alpha = chip_sprite("⌃ C", 40)
    assert rgb.shape[:2] == alpha.shape == (40, rgb.shape[1])
    assert alpha[20, 2] > 0.5 and alpha[0, 0] < 0.5      # filled middle, rounded corner
    assert rgb[..., 0].max() > 200                        # light text on the dark pill


def test_events_jsonl_survives_a_shape_round_trip(tmp_path):
    """A cursor_shape line is plain jsonl the renderer can read back."""
    line = json.dumps({"t": 1.5, "kind": "cursor_shape", "id": "abc123", "w": 24, "h": 24,
                       "hotspot": [3, 1], "scale": 1.0})
    assert json.loads(line)["hotspot"] == [3, 1]
    track = ShapeTrack([json.loads(line)], tmp_path)
    assert track.marks[0][1] == "abc123"


class FakeRect:
    def __init__(self, x, y, w, h):
        self.x, self.y, self.width, self.height = x, y, w, h


class FakeCon:
    def __init__(self, id, app_id, rect, type="con", window_class=None):
        self.id, self.app_id, self.rect, self.type, self.window_class = id, app_id, rect, type, window_class


class FakeTree:
    def __init__(self, cons):
        self._cons = cons

    def descendants(self):
        return self._cons


def session_with(cons):
    """A Session stub carrying only what _window_at touches."""
    from demovid.rec.session import Session
    from demovid.rec.sway import Output

    s = Session.__new__(Session)
    s.output = Output("HDMI-A-1", 0, 0, 1920, 1080, 1.0, 144.0)
    s.conn = type("C", (), {"get_tree": staticmethod(lambda: FakeTree(cons))})()
    return s


def test_window_at_picks_the_smallest_container_under_the_point():
    big = FakeCon(1, "brave", FakeRect(0, 0, 1920, 1080))
    small = FakeCon(2, "foot", FakeRect(100, 100, 400, 300))
    got = session_with([big, small])._window_at((200, 200))
    assert got["con_id"] == 2 and got["app_id"] == "foot"
    assert got["rect"] == [100, 100, 400, 300]
    assert session_with([big, small])._window_at((1000, 900))["con_id"] == 1


def test_window_at_reports_xwayland_class_and_skips_non_windows():
    xwl = FakeCon(3, None, FakeRect(0, 0, 800, 600), window_class="Steam")
    workspace = FakeCon(4, None, FakeRect(0, 0, 1920, 1080), type="workspace")
    got = session_with([workspace, xwl])._window_at((10, 10))
    assert got["con_id"] == 3 and got["class"] == "Steam" and got["app_id"] is None


def test_window_at_without_a_position_or_match_is_none():
    assert session_with([])._window_at(None) is None
    assert session_with([FakeCon(1, "brave", FakeRect(0, 0, 10, 10))])._window_at((500, 500)) is None


def test_window_at_translates_to_output_relative_coordinates():
    from demovid.rec.sway import Output

    s = session_with([FakeCon(1, "foot", FakeRect(1920, 40, 400, 300))])
    s.output = Output("DP-2", 1920, 0, 1920, 1080, 1.0, 60.0)  # second output, offset in the layout
    got = s._window_at((10, 50))  # output-relative → global 1930, 50
    assert got["con_id"] == 1 and got["rect"] == [0, 40, 400, 300]


# --- theme lookup by hotspot (the only route to a cursor picture on a software-cursor machine) ---

def test_index_theme_maps_hotspots_to_canonical_names():
    idx = xcursor.index_theme("Adwaita", 24)
    assert idx[(3, 1)] == "default"     # not `context-menu`, which shares the hotspot
    assert idx[(7, 5)] == "pointer"
    assert idx[(11, 12)] == "text"


def test_index_theme_falls_back_when_the_theme_is_missing():
    """An unknown theme still indexes, via the Adwaita/default fallback — better a plain arrow than none."""
    assert xcursor.theme_dir("no-such-theme-here") is None
    idx = xcursor.index_theme("no-such-theme-here", 24)
    assert idx.get((3, 1)) == "default"


def test_load_shape_returns_the_largest_art():
    w, h, xhot, yhot, bgra = xcursor.load_shape("Adwaita", "default", 24)
    assert (w, h) == (96, 96)           # themes ship up to 96 px: sharper than upscaling a 24 px grab
    assert (xhot, yhot) == (12, 4)      # hotspot scales with the art
    assert bgra.shape == (96, 96, 4)
    assert xcursor.load_shape("Adwaita", "no-such-cursor", 24) is None


def test_rank_prefers_common_shapes():
    assert xcursor._rank("default") < xcursor._rank("context-menu")
    assert xcursor._rank("text") < xcursor._rank("zz-unknown")


def test_read_images_rejects_non_xcursor_files(tmp_path):
    junk = tmp_path / "junk"
    junk.write_bytes(b"not a cursor at all")
    with pytest.raises(ValueError):
        xcursor.read_images(junk)


def test_hotspot_scaled_shape_lookup_round_trip(tmp_path):
    """What rec does: hotspot from the protocol -> name -> art -> PNG the renderer can load."""
    import cv2

    idx = xcursor.index_theme("Adwaita", 24)
    name = idx[(7, 5)]
    w, h, xhot, yhot, bgra = xcursor.load_shape("Adwaita", name, 24)
    sid = shape_id(bgra)
    (tmp_path / "cursors").mkdir()
    cv2.imwrite(str(tmp_path / "cursors" / f"{sid}.png"), unpremultiply(bgra))
    track = ShapeTrack([{"t": 0.0, "kind": "cursor_shape", "id": sid, "w": w, "h": h,
                         "hotspot": [xhot, yhot], "scale": 1.0, "name": name}], tmp_path / "cursors")
    shape = track.at(1.0)
    assert shape is not None
    assert shape[0].shape[:2] == (h, w)
    assert (shape[2], shape[3]) == (xhot, yhot)
