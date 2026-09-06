"""Console Home canvas island: recent recordings, their renders, and upload links. Written straight to
~/.config/console/canvas/islands/demovid.{html,json}; the hub live-reloads on write."""

import html
import json
import os
from datetime import datetime
from pathlib import Path

from demovid import CONFIG_DIR, RECORDINGS_DIR

ISLAND_DIR = Path("~/.config/console/canvas/islands").expanduser()
SLUG = "demovid"
STREAM_FILES = {"screen.mp4", "cam.mp4", "mic.flac"}


def recordings(root: Path = Path(RECORDINGS_DIR).expanduser(), limit: int = 8) -> list[dict]:
    out = []
    for d in sorted((p for p in root.iterdir() if (p / "manifest.json").exists()), reverse=True)[:limit]:
        try:
            m = json.loads((d / "manifest.json").read_text())
        except (OSError, ValueError):
            continue
        renders = sorted((p for p in d.glob("*.mp4") if p.name not in STREAM_FILES), key=os.path.getmtime, reverse=True)
        out.append({
            "name": d.name, "path": str(d),
            "duration_s": m.get("duration_s"), "source": m.get("source", "demovid"),
            "cursor": m.get("cursor_source", "none"),
            "renders": [{"name": p.name, "mb": p.stat().st_size / 1e6,
                         "when": datetime.fromtimestamp(p.stat().st_mtime).strftime("%d %b %H:%M")} for p in renders],
        })
    return out


def uploads(limit: int = 6) -> list[dict]:
    log = Path(CONFIG_DIR).expanduser() / "uploads.jsonl"
    if not log.exists():
        return []
    rows = []
    for line in log.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows[-limit:][::-1]


def fmt_dur(s) -> str:
    if not s:
        return "–"
    m, sec = divmod(int(s), 60)
    return f"{m}:{sec:02d}"


def render_html(recs: list[dict], ups: list[dict]) -> str:
    e = html.escape
    rows = []
    for r in recs:
        rends = " ".join(f'<span class="r" title="{e(x["when"])}">{e(x["name"])} <i>{x["mb"]:.0f}MB</i></span>'
                         for x in r["renders"]) or '<span class="none">not rendered</span>'
        tag = "" if r["source"] == "demovid" else f' <b class="src">{e(r["source"])}</b>'
        rows.append(f'<tr><td class="n">{e(r["name"])}{tag}</td><td class="d">{fmt_dur(r["duration_s"])}</td>'
                    f'<td>{rends}</td></tr>')
    ups_html = "".join(f'<li><a href="{e(u["url"])}" target="_blank">{e(u.get("title") or u["url"])}</a>'
                       f' <i>{e(str(u.get("privacy", "")))} · {e(str(u.get("t", ""))[:10])}</i></li>' for u in ups)
    return f"""<style>
.dv{{font:12px/1.5 ui-monospace,monospace;color:#e5e5e5}}
.dv table{{border-collapse:collapse;width:100%}}
.dv td{{padding:2px 6px 2px 0;vertical-align:top;border-bottom:1px solid #222}}
.dv .n{{white-space:nowrap;color:#bbb}} .dv .d{{color:#888;text-align:right}}
.dv .r{{display:inline-block;margin-right:8px}} .dv .r i{{color:#666;font-style:normal}}
.dv .none{{color:#555}} .dv .src{{color:#7aa2f7;font-weight:normal;font-size:10px}}
.dv h4{{margin:10px 0 4px;color:#888;font-weight:normal;text-transform:uppercase;letter-spacing:.08em;font-size:10px}}
.dv ul{{margin:0;padding-left:14px}} .dv a{{color:#7dcfff;text-decoration:none}} .dv li i{{color:#666;font-style:normal}}
.dv .empty{{color:#555}}
</style>
<div class="dv">
<h4>recordings</h4>
{'<table>' + ''.join(rows) + '</table>' if rows else '<div class="empty">none yet — $mod+Shift+r</div>'}
<h4>uploads</h4>
{'<ul>' + ups_html + '</ul>' if ups_html else '<div class="empty">none yet</div>'}
</div>"""


def refresh() -> Path:
    ISLAND_DIR.mkdir(parents=True, exist_ok=True)
    (ISLAND_DIR / f"{SLUG}.html").write_text(render_html(recordings(), uploads()))
    (ISLAND_DIR / f"{SLUG}.json").write_text(json.dumps({"title": "Demovid", "agent": "demovid", "accent": "#7aa2f7",
                                                          "weight": 2}))
    return ISLAND_DIR / f"{SLUG}.html"


if __name__ == "__main__":
    print(refresh())
