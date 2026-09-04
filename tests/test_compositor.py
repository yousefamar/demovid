import numpy as np
import pytest

from demovid.render.compositor import Compositor, Look, aspect_crop, rounded_mask
from demovid.render.cursor import CursorTrack, arrow_sprite, draw_cursor, draw_ripple, gaussian
from demovid.render.planner import Keyframe

W, H = 640, 360


def frame(value=40):
    return np.full((H, W, 3), value, np.uint8)


def test_gaussian_smoothing_preserves_constants_and_removes_jitter():
    a = np.full(100, 7.0)
    assert np.allclose(gaussian(a, 3.0), a)
    jitter = np.tile([0.0, 10.0], 50)
    sm = gaussian(jitter, 3.0)
    assert sm.std() < jitter.std() / 5
    assert abs(sm.mean() - 5.0) < 0.1


def test_cursor_track_interpolates_and_reports_presence():
    events = [{"t": 0.0, "kind": "cursor", "x": 0, "y": 0}, {"t": 1.0, "kind": "cursor", "x": 100, "y": 50}]
    track = CursorTrack(events, 0.0, 10, 11, smooth_s=0.0)
    assert track.present
    assert track.at(5) == pytest.approx((50.0, 25.0))
    assert not CursorTrack([{"t": 0, "kind": "button"}], 0.0, 10, 5).present


def test_arrow_sprites_have_a_body_with_a_contrasting_edge():
    rgb, alpha, pad = arrow_sprite(48, "dark")
    assert alpha.max() > 0.95 and alpha.min() == 0.0
    body = rgb[alpha > 0.95].mean(axis=1)
    assert (body < 60).mean() > 0.45      # black fill
    assert (body > 200).mean() > 0.15     # white outline
    assert pad >= 2 and rgb.shape[:2] == alpha.shape
    rgb, alpha, _ = arrow_sprite(48, "light")
    body = rgb[alpha > 0.95].mean(axis=1)
    assert (body > 200).mean() > 0.45 and (body < 60).mean() > 0.15


def test_draw_cursor_and_ripple_only_touch_their_neighbourhood():
    f = frame()
    draw_cursor(f, 300, 200, 48)
    changed = np.argwhere((f != 40).any(axis=2))
    assert len(changed) > 100
    assert changed[:, 0].min() >= 190 and changed[:, 1].min() >= 290
    assert changed[:, 0].max() <= 200 + 70 and changed[:, 1].max() <= 300 + 50
    f = frame()
    draw_ripple(f, 100, 100, 0.2, 0.45, 30)
    changed = np.argwhere((f != 40).any(axis=2))
    assert 0 < len(changed) and changed[:, 0].min() >= 60 and changed[:, 0].max() <= 140
    f = frame()
    draw_ripple(f, 100, 100, 0.5, 0.45, 30)  # past its lifetime
    assert (f == 40).all()
    draw_cursor(f, -5, -5, 48)  # partially off-frame is fine
    draw_cursor(f, W + 50, H + 50, 48)  # fully off-frame is fine


def test_rounded_mask_and_aspect_crop():
    mask, shadow, pad = rounded_mask(60, 60, 0.25)
    assert mask.shape == (60, 60) and mask[30, 30] == 1.0 and mask[0, 0] == 0.0
    assert shadow.shape == (60 + 2 * pad, 60 + 2 * pad)
    assert aspect_crop(np.zeros((720, 1280, 3), np.uint8), 100, 100).shape == (720, 720, 3)
    assert aspect_crop(np.zeros((720, 1280, 3), np.uint8), 160, 90).shape == (720, 1280, 3)
    assert aspect_crop(np.zeros((1000, 1000, 3), np.uint8), 200, 100).shape == (500, 1000, 3)


def test_compose_identity_leaves_frame_untouched_without_overlays():
    look = Look(W, H, 1.0, cursor=False, pip=False)
    comp = Compositor([Keyframe(0, W / 2, H / 2, 1.0)], W, H, look, None, [])
    f = frame()
    out = comp.compose(f, 0.0, 0, None)
    assert out is f and (out == 40).all()


def test_compose_zooms_and_paints_cursor_ripple_and_pip():
    kf = [Keyframe(0.0, W / 2, H / 2, 1.0), Keyframe(1.0, 200.0, 100.0, 2.0)]
    src = np.zeros((H, W, 3), np.uint8)
    src[95:105, 195:205] = 255                    # a white dot at the zoom target
    events = [{"t": t / 10, "kind": "cursor", "x": 200, "y": 100} for t in range(30)]
    track = CursorTrack(events, 0.0, 10, 30, smooth_s=0.0)
    cam = np.full((72, 128, 3), (0, 0, 200), np.uint8)
    look = Look(W, H, 1.0, cursor_scale=2.0, pip_size=0.2, pip_pos="br")
    comp = Compositor(kf, W, H, look, track, [(2.0, 200.0, 100.0)])

    plain = Compositor(kf, W, H, Look(W, H, 1.0, cursor=False, pip=False), None, []).compose(src.copy(), 2.0, 20, None)
    # the target dot ends up at the output centre, 2x larger
    assert (plain[H // 2 - 8:H // 2 + 8, W // 2 - 8:W // 2 + 8] == 255).all()
    assert (plain[H // 2 - 14:H // 2 - 11, W // 2 - 14:W // 2 - 11] == 0).all()

    out = comp.compose(src.copy(), 2.0, 20, cam)
    # cursor + ripple painted around the centre: not just the dot's pure white/black anymore
    region = out[H // 2 - 60:H // 2 + 80, W // 2 - 60:W // 2 + 80]
    assert len(np.unique(region.reshape(-1, 3), axis=0)) > 10
    assert (region != plain[H // 2 - 60:H // 2 + 80, W // 2 - 60:W // 2 + 80]).any()
    # PiP in the bottom-right corner is camera-red, with rounded transparent corners
    size = int(round(0.2 * W * (1 - look.pip_shrink)))
    margin = int(round(look.pip_margin * W))
    x0, y0 = W - margin - size, H - margin - size
    assert tuple(out[y0 + size // 2, x0 + size // 2]) == (0, 0, 200)
    assert tuple(out[y0, x0]) != (0, 0, 200)

    out = comp.compose(src.copy(), 0.0, 0, None)
    assert out.shape == (H, W, 3)
    plain = Compositor(kf, W, H, Look(W, H, 1.0, cursor=False, pip=False), None, []).compose(src.copy(), 0.0, 0, None)
    assert (plain == src).all()  # identity path keeps geometry
