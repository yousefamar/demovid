"""Record the binary-clock window with `demovid rec` and measure how well every artefact lines up.

    uv run python tests/syncprobe/run.py --seconds 60

Screen: each frame's decoded clock value vs (offset_s + frame pts) -> display latency, should be a
tight constant. Flash frames vs logged flash times -> same. Mic: click onset vs click spawn time
(includes speaker output latency + acoustics, expected a few tens of ms, positive). Cam: room
brightness bump vs flash time, if the flash lights the room at all.
"""
import argparse
import json
import os
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from demovid.rec import sway  # noqa: E402
from demovid.rec.session import RecOptions, Session  # noqa: E402

HERE = Path(__file__).resolve().parent
WIN_POS = (40, 60)


def start_clock_window(log_path: Path):
    """Overlay layer surface at WIN_POS: above every toplevel, so nothing Yousef opens can cover it."""
    proc = subprocess.Popen(["/usr/bin/python3", str(HERE / "clockwindow.py"), "--log", str(log_path),
                             "--layer-at", f"{WIN_POS[0]},{WIN_POS[1]}"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        time.sleep(0.1)
        if log_path.exists() and log_path.read_text().strip():
            meta = json.loads(log_path.read_text().splitlines()[0])
            return proc, (WIN_POS[0], WIN_POS[1], meta["width"], meta["height"])
    proc.kill()
    raise RuntimeError("clock window never started")


def decode_bits(rgb: np.ndarray, origin: tuple[int, int], meta: dict) -> tuple[int | None, bool]:
    x0, y0 = origin
    pad, cell, bits = meta["pad"], meta["cell"], meta["bits"]
    cy = y0 + pad + cell // 2
    samples = [rgb[cy, x0 + pad + i * cell + cell // 2].mean() for i in range(bits)]
    text_area = rgb[y0 + 100, x0 + meta["width"] // 2].mean()
    if text_area > 200 and min(samples) > 200:
        return None, True
    value = 0
    for s in samples:
        if 60 < s < 195:
            return None, False
        value = (value << 1) | (1 if s >= 195 else 0)
    return value, False


def analyze_screen(path: Path, offset: float, mono0_ns: int, origin, meta, flashes):
    import av

    deltas_ms, flash_frames, bad = [], [], 0
    with av.open(str(path)) as c:
        stream = c.streams.video[0]
        tb = float(stream.time_base)
        for frame in c.decode(stream):
            t_file = frame.pts * tb
            rgb = frame.to_ndarray(format="rgb24")
            ms, is_flash = decode_bits(rgb, origin, meta)
            t_cap = offset + t_file
            if is_flash:
                flash_frames.append(t_cap)
                continue
            if ms is None:
                bad += 1
                continue
            shown_t = (ms * 1_000_000 - mono0_ns) / 1e9
            d = (t_cap - shown_t) * 1000
            if -200 < d < 500:
                deltas_ms.append(d)
            else:
                bad += 1
    groups = []
    for t in flash_frames:
        if not groups or t - groups[-1][-1] > 0.1:
            groups.append([t])
        else:
            groups[-1].append(t)
    flash_deltas = []
    for f in flashes:
        t_flash = (f["flash_ns"] - mono0_ns) / 1e9
        near = [g[0] for g in groups if abs(g[0] - t_flash) < 0.5]
        if near:
            flash_deltas.append((near[0] - t_flash) * 1000)
    return {
        "frames_decoded": len(deltas_ms), "frames_unreadable": bad, "flash_groups": len(groups),
        "display_latency_ms": _stats(deltas_ms), "flash_first_frame_minus_flash_ms": [round(d, 1) for d in flash_deltas],
    }


def analyze_mic(path: Path, offset: float, mono0_ns: int, clicks):
    """Matched-filter the 2 kHz click against the mic track; reference = the time the click's first
    sample was scheduled in the paced pw-cat stream (write time + blocks written ahead)."""
    import av

    chunks = []
    with av.open(str(path)) as c:
        stream = c.streams.audio[0]
        rate = stream.rate
        for frame in c.decode(stream):
            chunks.append(frame.to_ndarray().astype(np.float32).reshape(-1))
    x = np.concatenate(chunks)
    n = int(rate * 0.015)
    tt = np.arange(n) / rate
    t_sin, t_cos = np.sin(2 * np.pi * 2000 * tt), np.cos(2 * np.pi * 2000 * tt)
    results = []
    for cl in clicks:
        t_sched = (cl["click_write_ns"] - mono0_ns) / 1e9 + cl["blocks_ahead"] * 0.01
        i0 = int((t_sched - 0.1 - offset) * rate)
        i1 = int((t_sched + 0.5 - offset) * rate)
        if i0 < 0 or i1 > len(x):
            results.append(None)
            continue
        seg = x[i0:i1]
        env = np.sqrt(np.correlate(seg, t_sin, mode="valid") ** 2 + np.correlate(seg, t_cos, mode="valid") ** 2)
        k = int(np.argmax(env))
        ratio = float(env[k] / (np.median(env) + 1e-9))
        if ratio < 8:
            results.append({"detected": False, "peak_ratio": round(ratio, 1)})
            continue
        t_onset = offset + (i0 + k) / rate
        results.append({"onset_minus_scheduled_ms": round((t_onset - t_sched) * 1000, 1), "peak_ratio": round(ratio, 1)})
    return {"rate": rate, "duration_s": round(len(x) / rate, 3), "clicks": results}


def analyze_cam(path: Path, offset: float, mono0_ns: int, flashes):
    import av

    ts, luma = [], []
    with av.open(str(path)) as c:
        stream = c.streams.video[0]
        tb = float(stream.time_base)
        for frame in c.decode(stream):
            ts.append(offset + frame.pts * tb)
            luma.append(float(frame.to_ndarray(format="gray").mean()))
    ts, luma = np.array(ts), np.array(luma)
    results = []
    for f in flashes:
        t_flash = (f["flash_ns"] - mono0_ns) / 1e9
        before = luma[(ts > t_flash - 1.0) & (ts < t_flash - 0.05)]
        during = (ts >= t_flash - 0.05) & (ts < t_flash + 0.4)
        if len(before) < 5 or not during.any():
            results.append(None)
            continue
        base, spread = before.mean(), before.std() + 1e-6
        bump = luma[during] - base
        k = int(np.argmax(bump))
        results.append({"peak_minus_flash_ms": round((ts[during][k] - t_flash) * 1000, 1),
                        "bump_sigma": round(float(bump[k] / spread), 1)})
    return {"frames": len(ts), "duration_s": round(float(ts[-1] - ts[0]), 3) if len(ts) else 0, "flash_bumps": results}


def _stats(v):
    if not v:
        return None
    v = sorted(v)
    return {"n": len(v), "median": round(statistics.median(v), 1), "p5": round(v[int(0.05 * len(v))], 1),
            "p95": round(v[int(0.95 * (len(v) - 1))], 1), "min": round(v[0], 1), "max": round(v[-1], 1)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=60)
    ap.add_argument("--out-root", type=Path, default=Path("/tmp/demovid-sync"))
    ap.add_argument("--analyze-only", type=Path, help="existing recording dir (with syncprobe.jsonl inside)")
    ap.add_argument("--mic-source", default="default",
                    help="e.g. the sink's .monitor to measure the capture path without speakers/HDMI latency")
    ns = ap.parse_args()

    if ns.analyze_only:
        rec_dir = ns.analyze_only
        probe_log = rec_dir / "syncprobe.jsonl"
        origin = tuple(json.loads((rec_dir / "syncprobe-origin.json").read_text()))
    else:
        ns.out_root.mkdir(parents=True, exist_ok=True)
        probe_log = ns.out_root / f"syncprobe-{int(time.time())}.jsonl"
        win, geom = start_clock_window(probe_log)
        origin = geom[:2]
        print(f"clock window content at {geom}", file=sys.stderr)
        time.sleep(1.0)
        session = Session(RecOptions(out_root=ns.out_root, notify=False, mic_source=ns.mic_source))
        threading.Timer(ns.seconds, session.stop_event.set).start()
        try:
            rec_dir = session.run()
        finally:
            win.terminate()
        time.sleep(0.5)
        os.replace(probe_log, rec_dir / "syncprobe.jsonl")
        probe_log = rec_dir / "syncprobe.jsonl"
        (rec_dir / "syncprobe-origin.json").write_text(json.dumps(origin))

    lines = [json.loads(l) for l in probe_log.read_text().splitlines()]
    meta = next(l for l in lines if l["event"] == "start")
    flashes = [l for l in lines if l["event"] == "flash"]
    clicks = [l for l in lines if l["event"] == "click"]
    manifest = json.loads((rec_dir / "manifest.json").read_text())
    mono0 = manifest["monotonic_ns"]
    streams = manifest["streams"]
    report = {"dir": str(rec_dir), "flashes": len(flashes), "clicks": len(clicks), "offsets": {k: v["offset_s"] for k, v in streams.items()}}
    report["screen"] = analyze_screen(rec_dir / "screen.mp4", streams["screen"]["offset_s"], mono0, origin, meta, flashes)
    if "mic" in streams:
        report["mic"] = analyze_mic(rec_dir / "mic.flac", streams["mic"]["offset_s"], mono0, clicks)
    if "cam" in streams:
        report["cam"] = analyze_cam(rec_dir / "cam.mp4", streams["cam"]["offset_s"], mono0, flashes)
    (rec_dir / "syncprobe-report.json").write_text(json.dumps(report, indent=1))
    print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
