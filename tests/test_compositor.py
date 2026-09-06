import numpy as np
import pytest

from demovid.render.compositor import Compositor, Look, aspect_crop, shape_mask
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


def test_shape_masks_and_aspect_crop():
    mask, shadow, pad = shape_mask(60, 60, "rounded", 0.25)
    assert mask.shape == (60, 60) and mask[30, 30] == 1.0 and mask[0, 0] == 0.0
    assert shadow.shape == (60 + 2 * pad, 60 + 2 * pad)
    sq, _, _ = shape_mask(100, 100, "squircle", 0.0)
    assert sq[50, 50] == 1.0 and sq[0, 0] == 0.0 and sq[50, 0] > 0.9      # flat-ish sides, cut corners
    # inscribed square of an n=4 squircle is 2^(-1/4) = 0.84 of the side: a 0.80 preview fits inside
    inner = int(100 * 0.80); off = (100 - inner) // 2
    assert sq[off:off + inner, off:off + inner].min() > 0.99
    circ, _, _ = shape_mask(100, 100, "circle", 0.0)
    assert circ[50, 50] == 1.0 and circ[2, 2] == 0.0 and circ[50, 1] > 0.5
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


def test_background_solid_and_gradient_and_bad_spec():
    import numpy as np
    import pytest
    from demovid.render.compositor import background, parse_color
    solid = background("#102030", 8, 4)
    assert solid.shape == (4, 8, 3) and tuple(solid[0, 0]) == (0x30, 0x20, 0x10)   # BGR
    grad = background("#000000,#ffffff", 16, 16)
    assert grad[0, 0].max() == 0 and grad[-1, -1].min() == 255
    assert grad[0, 15].mean() == pytest.approx(grad[15, 0].mean(), abs=1)          # diagonal
    assert parse_color("#ff0000") == (0, 0, 255)
    with pytest.raises(SystemExit):
        background("#fff", 4, 4)
    with pytest.raises(SystemExit):
        background("/nonexistent/bg.png", 4, 4)


def test_padded_compositor_insets_the_content_and_keeps_aspect():
    import numpy as np
    from demovid.render.compositor import Compositor, Look
    from demovid.render.planner import Keyframe
    look = Look(out_w=192, out_h=108, out_scale=0.1, pad=0.1, bg="#000000", cursor=False, pip=False, chips=False)
    comp = Compositor([Keyframe(0.0, 960, 540, 1.0)], 1920, 1080, look, None, [])
    x, y, w, h = comp.content
    pad = round(0.1 * 108)
    assert w / h == pytest.approx(1920 / 1080, abs=0.02)   # source aspect preserved
    assert x >= pad and y >= pad and x + w <= 192 - pad and y + h <= 108 - pad
    assert (x, y) == ((192 - w) // 2, (108 - h) // 2)      # centred
    frame = np.full((1080, 1920, 3), 200, np.uint8)
    out = comp.compose(frame, 0.0, 0, None)
    assert out.shape == (108, 192, 3)
    assert out[0, 0].max() < 60          # background (with shadow) at the corner
    assert out[54, 96].min() > 150       # content in the middle
