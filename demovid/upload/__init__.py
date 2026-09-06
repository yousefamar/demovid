import argparse
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from demovid import CONFIG_DIR

UPLOAD_LOG = Path(CONFIG_DIR).expanduser() / "uploads.jsonl"
PRIVACIES = ("unlisted", "private", "public")


def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("file", nargs="?", type=Path, help="mp4 to upload (a demovid render or any video)")
    parser.add_argument("--title")
    parser.add_argument("--description")
    parser.add_argument("--privacy", choices=PRIVACIES, default="unlisted")
    parser.add_argument("--no-copy", action="store_true", help="don't put the URL on the clipboard")
    parser.add_argument("--auth", action="store_true", help="(re)authorise with Google in the browser")
    parser.add_argument("--list", action="store_true", help="list the channel's uploads")
    parser.add_argument("--limit", type=int, default=20, help="rows for --list")
    parser.add_argument("--delete", metavar="ID_OR_URL", help="delete a video")


def metadata_for(path: Path, title: str | None, description: str | None, privacy: str) -> dict:
    manifest = _find_manifest(path)
    started = manifest.get("started_at")
    if not title:
        if started:
            title = "Demo " + datetime.fromisoformat(started).strftime("%Y-%m-%d %H:%M")
        else:
            title = path.stem
    if description is None:
        parts = []
        if started:
            parts.append(f"Recorded {started}")
        if manifest.get("duration_s"):
            parts.append(f"{manifest['duration_s']:.0f} s")
        parts.append("rendered with demovid")
        description = " · ".join(parts)
    return {
        "snippet": {"title": title[:100], "description": description[:5000], "categoryId": "28"},
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }


def _find_manifest(path: Path) -> dict:
    for candidate in (path.parent / "manifest.json", path.parent.parent / "manifest.json"):
        if candidate.is_file():
            try:
                return json.loads(candidate.read_text())
            except (OSError, ValueError):
                return {}
    return {}


def _copy(url: str) -> bool:
    if not shutil.which("wl-copy"):
        return False
    return subprocess.run(["wl-copy", url], check=False).returncode == 0


def _log_upload(entry: dict) -> None:
    UPLOAD_LOG.parent.mkdir(parents=True, exist_ok=True)
    with UPLOAD_LOG.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def main(ns: argparse.Namespace) -> int:
    from demovid.upload import oauth, youtube

    try:
        if ns.auth:
            oauth.authorise()
            print(f"authorised; token saved to {oauth.TOKEN_PATH}")
            if not (ns.file or ns.list or ns.delete):
                return 0
        if ns.list:
            rows = youtube.list_uploads(oauth.access_token(), ns.limit)
            for r in rows:
                print(f"{r['id']}  {r['privacy']:<8} {r['published'][:10]}  {r['title']}  {youtube.video_url(r['id'])}")
            return 0
        if ns.delete:
            vid = youtube.video_id_from(ns.delete)
            youtube.delete_video(oauth.access_token(), vid)
            print(f"deleted {vid}")
            return 0
        if not ns.file:
            print("nothing to do: give a file, or --auth / --list / --delete", file=sys.stderr)
            return 2
        return _upload(ns, oauth, youtube)
    except (oauth.AuthError, youtube.UploadError) as e:
        print(f"demovid upload: {e}", file=sys.stderr)
        return 1


def _upload(ns: argparse.Namespace, oauth, youtube) -> int:
    path: Path = ns.file.expanduser().resolve()
    if not path.is_file():
        print(f"demovid upload: no such file {path}", file=sys.stderr)
        return 2
    meta = metadata_for(path, ns.title, ns.description, ns.privacy)
    token = oauth.access_token()
    print(f"uploading {path.name} as {ns.privacy}: {meta['snippet']['title']}", file=sys.stderr)
    t0 = time.monotonic()
    video = youtube.upload_file(token, path, meta, progress=youtube.print_progress)
    vid = video["id"]
    url = youtube.video_url(vid)
    got_privacy = video.get("status", {}).get("privacyStatus", "?")
    copied = not ns.no_copy and _copy(url)
    _log_upload({
        "t": datetime.now().astimezone().isoformat(timespec="seconds"),
        "file": str(path), "id": vid, "url": url,
        "title": meta["snippet"]["title"], "privacy": got_privacy,
        "seconds": round(time.monotonic() - t0, 1),
    })
    print(url + ("  (copied)" if copied else ""))
    try:
        from demovid.island import refresh
        refresh()
    except Exception:  # the dashboard never fails an upload
        pass
    if got_privacy != ns.privacy:
        print(
            f"WARNING: YouTube set privacy to '{got_privacy}', not '{ns.privacy}'. Uploads from an unaudited "
            "API project are locked private until the project passes the YouTube API Services audit.",
            file=sys.stderr,
        )
        return 3
    return 0
