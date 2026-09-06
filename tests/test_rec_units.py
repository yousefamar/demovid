import json
import struct
import time

from demovid.rec import audio, blankcursor, inputs, streams
from demovid.rec.clock import Clock
from demovid.rec.events import EventLog
from demovid.rec.state import pid_alive
from demovid.rec.sway import Output


def test_clock_conversions_are_consistent():
    c = Clock.start()
    mono_now = time.monotonic()
    assert abs(c.from_monotonic(mono_now) - c.now()) < 0.01
    assert abs(c.from_epoch(time.time()) - c.now()) < 0.05
    assert c.from_monotonic(c.monotonic_ns / 1e9) == 0.0


def test_event_log_writes_jsonl_with_rounded_t(tmp_path):
    c = Clock.start()
    log = EventLog(tmp_path / "events.jsonl", c)
    log.emit("mark", 1.23456789, label="clap")
    log.emit("button", button="left", state="down", x=None, y=None)
    log.close()
    lines = [json.loads(l) for l in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert lines[0] == {"t": 1.234568, "kind": "mark", "label": "clap"}
    assert lines[1]["kind"] == "button" and lines[1]["x"] is None and lines[1]["t"] >= 0
    assert log.count == 2


def test_key_names_follow_format_md():
    from evdev import ecodes

    assert inputs.key_name(ecodes.KEY_A) == "a"
    assert inputs.key_name(ecodes.KEY_ENTER) == "enter"
    assert inputs.key_name(ecodes.KEY_LEFTSHIFT) == "leftshift"
    assert inputs.BUTTONS[ecodes.BTN_LEFT] == "left"
    assert set(inputs.MODS.values()) == {"ctrl", "alt", "shift", "super"}


def test_wf_marker_prefers_first_frame_line_over_pts_fallback():
    m = streams._WfMarker()
    assert m("Setting codec option: cq=20", 0.5) is None
    assert m("[Parsed_fps_0 @ 0x1] Read frame with in pts 0", 0.60) == 0.60
    assert m("[Parsed_fps_0 @ 0x1] Read frame with in pts 16666", 0.61) == 0.61 - 0.016666
    got = m("Using video filter: fps=60", 0.55)
    assert abs(got - (0.55 - streams.SCREEN_MARKER_LATENCY_S)) < 1e-9
    assert m("[Parsed_fps_0 @ 0x1] Read frame with in pts 33333", 0.70) is None


def test_ffmpeg_marker_parses_input_start_once():
    c = Clock(monotonic_ns=1_000_000_000_000, epoch_s=1_700_000_000.0)
    m = streams._FfmpegMarker(c, lambda clock, s: clock.from_monotonic(s))
    assert m("Input #0, video4linux2,v4l2, from '/dev/video0':", 0.1) is None
    assert abs(m("  Duration: N/A, start: 1000.250000, bitrate: N/A", 0.3) - 0.25) < 1e-9
    assert m("  Duration: N/A, start: 1000.900000, bitrate: N/A", 0.4) is None


def test_mic_marker_uses_median_of_implied_starts_in_window():
    c = Clock(monotonic_ns=0, epoch_s=1_700_000_000.0)
    spawn_t = 0.0
    m = streams._MicMarker(c, 48000, lambda: spawn_t)
    assert abs(m("  Duration: N/A, start: 1700000000.300000, bitrate: 768 kb/s", 0.3) - 0.3) < 1e-6
    # packets: true start 0.25 s; the first two report late (backlog), later ones are steady
    def line(n, pts_s):
        return f"[Parsed_ashowinfo_1 @ 0x1] n:{n} pts:{int(pts_s * 1e6)} pts_time:x fmt:s16 rate:48000 nb_samples:960 checksum:0"
    assert m(line(0, 0.30 + 1_700_000_000), 0.30) is None  # before window
    assert m(line(1, 0.27 + 0.02 + 1_700_000_000), 0.40) is None
    est = []
    for k in range(2, 60):
        got = m(line(k, 0.25 + k * 0.02 + 1_700_000_000), 0.6 + k * 0.02)
        if got is not None:
            est.append(got)
    assert est and abs(est[-1] - 0.25) < 1e-6
    assert m(line(200, 0.25 + 200 * 0.02 + 1_700_000_000), 5.0) is None  # after window


def test_blank_xcursor_file_layout():
    data = blankcursor.xcursor_bytes(24)
    magic, hdr, ver, ntoc = struct.unpack_from("<4sIII", data, 0)
    assert (magic, hdr, ver, ntoc) == (b"Xcur", 16, 0x10000, 1)
    typ, sub, pos = struct.unpack_from("<III", data, 16)
    assert (typ, sub, pos) == (0xFFFD0002, 24, 28)
    chunk = struct.unpack_from("<IIIIIIIII", data, pos)
    assert chunk[:6] == (36, 0xFFFD0002, 24, 1, 24, 24)
    assert len(data) == pos + 36 + 24 * 24 * 4
    assert set(data[pos + 36:]) == {0}


def test_output_relative_rects():
    class R:
        def __init__(self, x, y, w, h):
            self.x, self.y, self.width, self.height = x, y, w, h

    out = Output("HDMI-A-1", 1920, 0, 1920, 1080, 1.0, 144.0)
    assert out.relative(R(1930, 23, 800, 600)) == [10, 23, 800, 600]


def test_pid_alive_and_mic_warning_text():
    import os

    assert pid_alive(os.getpid())
    assert not pid_alive(2**22 - 1)
    assert audio.MIN_VOLUME_PCT <= audio.TARGET_VOLUME_PCT


def test_cli_status_when_idle(monkeypatch, tmp_path):
    from demovid import rec

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert rec.status() == {"recording": False, "paused": False, "stale": False}
