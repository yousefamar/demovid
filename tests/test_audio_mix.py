"""The render's audio with one or two tracks (mic + PC audio): graph shape, alignment, speech detection."""

from pathlib import Path

from demovid.render import captions, media
from demovid.render.media import AudioTrack, audio_chain, silences

MIC = AudioTrack("mic", Path("/r/mic.flac"), 0.30, denoise=True)
SYS = AudioTrack("system", Path("/r/system.flac"), 0.45)


def stub_loudnorm(monkeypatch):
    monkeypatch.setattr(media, "_loudnorm", lambda tr, pre, seek, dur: f"LN({tr.name})")


def test_single_track_graph_is_the_old_chain(monkeypatch):
    stub_loudnorm(monkeypatch)
    graph, inputs = audio_chain([MIC], t_from=0.0, t_to=10.0)
    assert graph == "[1:a]adelay=300:all=1,afftdn=nr=10:nf=-40:tn=1,LN(mic),apad[aout]"
    assert inputs == [(MIC.path, 0.0, 10.0)]


def test_two_tracks_are_levelled_summed_and_limited(monkeypatch):
    stub_loudnorm(monkeypatch)
    graph, inputs = audio_chain([MIC, SYS], t_from=1.0, t_to=11.0, keep=[(1.0, 5.0), (7.0, 11.0)])
    mic_chain, sys_chain, mix = graph.split(";")
    # mic started before t_from: seeked into, no delay; system too; both keep the same aselect
    assert inputs == [(MIC.path, 0.7, 10.0), (SYS.path, 0.55, 10.0)]
    assert mic_chain.startswith("[1:a]aselect='between(t,0.0000,4.0000)+between(t,6.0000,10.0000)',asetpts=N/SR/TB,afftdn=")
    assert "afftdn" not in sys_chain           # PC audio is clean digital audio: never denoised
    assert mic_chain.endswith("LN(mic),aresample=48000,aformat=channel_layouts=stereo[a1]")
    assert sys_chain.endswith("LN(system),aresample=48000,aformat=channel_layouts=stereo[a2]")
    assert mix == f"[a1][a2]amix=inputs=2:normalize=0:duration=longest,{media.MIX_LIMITER},apad[aout]"


def test_encoder_command_takes_n_audio_inputs(monkeypatch):
    seen = {}

    class FakeProc:
        def __init__(self, cmd, **kw):
            seen["cmd"] = cmd
    monkeypatch.setattr(media.subprocess, "Popen", FakeProc)
    media.Encoder(Path("/r/out.mp4"), 640, 360, 30, "libx264", 23,
                  audio=[(MIC.path, 0.0, 9.5), (SYS.path, 0.15, 9.5)], audio_filter="G[aout]", video_filter="vf")
    cmd = seen["cmd"]
    assert cmd.count("-i") == 3
    i_sys = cmd.index(str(SYS.path))
    assert cmd[i_sys - 5:i_sys] == ["-ss", "0.150000", "-t", "9.500000", "-i"]  # per-input options precede its -i
    assert "-ss" not in cmd[:cmd.index(str(MIC.path))]                    # seek 0 -> no -ss
    assert cmd[cmd.index("-filter_complex") + 1] == "G[aout]"
    assert cmd[cmd.index("-map", cmd.index("-filter_complex")) + 1] == "[aout]"
    assert cmd[cmd.index("-vf") + 1] == "vf"
    media.Encoder(Path("/r/out.mp4"), 640, 360, 30, "libx264", 23)
    assert "-filter_complex" not in seen["cmd"] and seen["cmd"].count("-i") == 1


def test_silences_need_every_track_quiet(monkeypatch):
    # 0.1 s windows: mic talks 0-1 s, PC audio talks 2-3 s, both floors at -60 dBFS
    def levels(path, win_s):
        talk = range(0, 10) if path.name == "mic.flac" else range(20, 30)
        return [-20.0 if i in talk else -60.0 for i in range(40)]
    monkeypatch.setattr(media, "level_track", levels)
    quiet = silences([MIC, SYS], min_s=0.5)
    # mic quiet 1-4 s (+0.30), system quiet 0-2 s and 3-4 s (+0.45); intersection on the recording clock
    assert [(round(a, 2), round(b, 2)) for a, b in quiet] == [(1.3, 2.45), (3.45, 4.3)]


def test_digital_silence_counts_as_quiet_when_the_floor_is_untrusted(monkeypatch):
    # a call app sends true silence when the far side is muted: too few loud windows for a floor estimate
    monkeypatch.setattr(media, "level_track", lambda p, w: [-120.0] * 50 + [-20.0] * 5 + [-120.0] * 45)
    assert silences([SYS], min_s=0.5) == [(0.45, 5.45), (5.95, 10.45)]


def test_captions_mix_aligns_the_other_tracks_to_the_first():
    g = captions.mix_graph([MIC, SYS])
    assert g == ("[0:a]aformat=channel_layouts=stereo[a0];[1:a]adelay=150:all=1,aformat=channel_layouts=stereo[a1];"
                 "[a0][a1]amix=inputs=2:normalize=0:duration=longest[aout]")
    early = AudioTrack("system", SYS.path, 0.10)
    assert "atrim=start=0.200000,asetpts=PTS-STARTPTS" in captions.mix_graph([MIC, early])


def test_audio_chain_without_padding_for_audio_only_output(monkeypatch):
    stub_loudnorm(monkeypatch)
    graph, _ = audio_chain([MIC, SYS], 0.0, 5.0, pad=False)
    assert graph.endswith(f"{media.MIX_LIMITER}[aout]") and "apad" not in graph


def test_render_of_an_audio_only_recording_mixes_and_cuts_pauses(tmp_path):
    """Real ffmpeg: two 6 s tones, a 2 s pause in the middle -> a 4 s m4a with both frequencies."""
    import json
    import subprocess

    import numpy as np

    from demovid.render import add_args, main
    import argparse

    rec = tmp_path / "2026-01-01-00-00-00"
    rec.mkdir()
    for name, hz in (("mic", 440), ("system", 1000)):
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency={hz}:duration=6",
                        "-ar", "48000", "-ac", "1" if name == "mic" else "2", str(rec / f"{name}.flac")], check=True)
    (rec / "manifest.json").write_text(json.dumps({
        "version": 1, "duration_s": 6.5, "output": {"name": "HDMI-A-1", "width": 1920, "height": 1080},
        "streams": {"mic": {"file": "mic.flac", "offset_s": 0.5, "sample_rate": 48000, "channels": 1},
                    "system": {"file": "system.flac", "offset_s": 0.5, "sample_rate": 48000, "channels": 2}},
        "source": "demovid"}))
    (rec / "events.jsonl").write_text('{"t": 2.5, "kind": "pause"}\n{"t": 4.5, "kind": "resume"}\n')
    p = argparse.ArgumentParser()
    add_args(p)
    out = tmp_path / "out.m4a"
    assert main(p.parse_args([str(rec), "-o", str(out)])) == 0
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(out)],
                               capture_output=True, text=True).stdout)
    assert abs(dur - 4.0) < 0.15, dur
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(out), "-ac", "1", "-f", "f32le", "-"], capture_output=True).stdout
    x = np.frombuffer(raw, np.float32)
    spec = np.abs(np.fft.rfft(x[:48000 * 3]))
    hz = np.fft.rfftfreq(48000 * 3, 1 / 48000)
    peaks = {int(round(hz[i])) for i in np.argsort(spec)[-2:]}
    assert peaks == {440, 1000}, peaks
    assert 0.5 < np.abs(x).max() <= 0.9
