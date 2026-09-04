import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

API_BASE = "https://www.googleapis.com/youtube/v3"
UPLOAD_BASE = "https://www.googleapis.com/upload/youtube/v3"
CHUNK = 16 * 1024 * 1024  # must be a multiple of 256 KiB
MAX_RETRIES = 8


class UploadError(Exception):
    pass


def video_id_from(ref: str) -> str:
    ref = ref.strip()
    m = re.search(r"(?:youtu\.be/|[?&]v=|/shorts/|/embed/)([A-Za-z0-9_-]{11})", ref)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", ref):
        return ref
    raise UploadError(f"not a YouTube video id or URL: {ref}")


def video_url(video_id: str) -> str:
    return f"https://youtu.be/{video_id}"


def _request(method: str, url: str, token: str, *, body: bytes | None = None, headers: dict | None = None,
             timeout: int = 60) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def _api(method: str, path: str, token: str, params: dict | None = None, payload: dict | None = None) -> dict:
    url = f"{API_BASE}/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if body else {}
    status, _, data = _request(method, url, token, body=body, headers=headers)
    if status == 204:
        return {}
    if status >= 400:
        raise UploadError(f"{method} {path} → {status}: {data.decode(errors='replace')[:500]}")
    return json.loads(data) if data else {}


def start_session(token: str, path: Path, metadata: dict) -> str:
    size = path.stat().st_size
    body = json.dumps(metadata).encode()
    url = f"{UPLOAD_BASE}/videos?" + urllib.parse.urlencode({"uploadType": "resumable", "part": "snippet,status"})
    status, headers, data = _request("POST", url, token, body=body, headers={
        "Content-Type": "application/json; charset=UTF-8",
        "X-Upload-Content-Type": "video/mp4",
        "X-Upload-Content-Length": str(size),
    })
    if status != 200 or "Location" not in headers:
        raise UploadError(f"could not start upload session: {status} {data.decode(errors='replace')[:500]}")
    return headers["Location"]


def _committed_offset(token: str, session: str, size: int) -> int:
    status, headers, data = _request("PUT", session, token, body=b"", headers={
        "Content-Length": "0", "Content-Range": f"bytes */{size}"})
    if status == 308:
        rng = headers.get("Range")
        return int(rng.split("-")[1]) + 1 if rng else 0
    if status in (200, 201):
        return size
    raise UploadError(f"upload session lost ({status}): {data.decode(errors='replace')[:300]}")


def upload_file(token: str, path: Path, metadata: dict, progress: Callable[[int, int], None] | None = None) -> dict:
    size = path.stat().st_size
    session = start_session(token, path, metadata)
    offset = 0
    retries = 0
    with path.open("rb") as f:
        while True:
            f.seek(offset)
            chunk = f.read(CHUNK)
            end = offset + len(chunk) - 1
            try:
                status, headers, data = _request("PUT", session, token, body=chunk, headers={
                    "Content-Type": "video/mp4",
                    "Content-Range": f"bytes {offset}-{end}/{size}",
                }, timeout=300)
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                status, headers, data = 0, {}, str(e).encode()
            if status in (200, 201):
                if progress:
                    progress(size, size)
                return json.loads(data)
            if status == 308:
                rng = headers.get("Range")
                new_offset = int(rng.split("-")[1]) + 1 if rng else offset
                if new_offset > offset:
                    retries = 0
                else:
                    retries += 1
                    if retries > MAX_RETRIES:
                        raise UploadError("server keeps acknowledging 308 without accepting bytes")
                offset = new_offset
                if progress:
                    progress(offset, size)
                continue
            if status == 0 or status >= 500 or status == 429:
                retries += 1
                if retries > MAX_RETRIES:
                    raise UploadError(f"gave up after {MAX_RETRIES} retries: {status} {data.decode(errors='replace')[:300]}")
                time.sleep(min(2 ** retries, 60))
                offset = _committed_offset(token, session, size)
                continue
            raise UploadError(f"upload failed ({status}): {data.decode(errors='replace')[:500]}")


def list_uploads(token: str, limit: int = 20) -> list[dict]:
    chans = _api("GET", "channels", token, {"part": "contentDetails", "mine": "true"})
    items = chans.get("items") or []
    if not items:
        raise UploadError("this Google account has no YouTube channel — create one at youtube.com first")
    playlist = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    out: list[dict] = []
    page = None
    while len(out) < limit:
        params = {"part": "snippet,status", "playlistId": playlist, "maxResults": str(min(50, limit - len(out)))}
        if page:
            params["pageToken"] = page
        res = _api("GET", "playlistItems", token, params)
        for it in res.get("items", []):
            out.append({
                "id": it["snippet"]["resourceId"]["videoId"],
                "title": it["snippet"]["title"],
                "published": it["snippet"]["publishedAt"],
                "privacy": it.get("status", {}).get("privacyStatus", "?"),
            })
        page = res.get("nextPageToken")
        if not page:
            break
    return out


def delete_video(token: str, video_id: str) -> None:
    _api("DELETE", "videos", token, {"id": video_id})


def set_privacy(token: str, video_id: str, privacy: str) -> dict:
    return _api("PUT", "videos", token, {"part": "status"},
                {"id": video_id, "status": {"privacyStatus": privacy}})


def print_progress(done: int, total: int) -> None:
    pct = 100 * done / total if total else 100
    sys.stderr.write(f"\r  {done / 2**20:7.1f} / {total / 2**20:.1f} MiB  {pct:5.1f}%")
    sys.stderr.flush()
    if done >= total:
        sys.stderr.write("\n")
