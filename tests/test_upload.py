import http.server
import json
import os
import re
import threading
from pathlib import Path

import pytest

from demovid.upload import metadata_for
from demovid.upload import youtube


class FakeYouTube(http.server.BaseHTTPRequestHandler):
    received = bytearray()
    total = 0
    fail_at_offsets: set[int] = set()
    metadata: dict = {}
    puts = 0

    def log_message(self, *_):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        FakeYouTube.metadata = json.loads(body)
        FakeYouTube.total = int(self.headers["X-Upload-Content-Length"])
        FakeYouTube.received = bytearray()
        self.send_response(200)
        self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/upload/sess1")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_PUT(self):
        FakeYouTube.puts += 1
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        m = re.fullmatch(r"bytes (\*|(\d+)-(\d+))/(\d+)", self.headers["Content-Range"])
        assert m and int(m.group(4)) == FakeYouTube.total
        if m.group(1) != "*":
            start = int(m.group(2))
            if start in FakeYouTube.fail_at_offsets:
                FakeYouTube.fail_at_offsets.discard(start)
                self.send_response(503)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            assert start == len(FakeYouTube.received), "client sent a chunk from the wrong offset"
            FakeYouTube.received += body
        if len(FakeYouTube.received) < FakeYouTube.total:
            self.send_response(308)
            if FakeYouTube.received:
                self.send_header("Range", f"bytes=0-{len(FakeYouTube.received) - 1}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        payload = json.dumps({"id": "abcdefghijk", "status": {"privacyStatus": "unlisted"}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def server(monkeypatch):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeYouTube)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(youtube, "UPLOAD_BASE", f"http://127.0.0.1:{srv.server_port}")
    monkeypatch.setattr(youtube, "CHUNK", 256 * 1024)
    monkeypatch.setattr(youtube.time, "sleep", lambda *_: None)
    FakeYouTube.fail_at_offsets = set()
    FakeYouTube.puts = 0
    yield srv
    srv.shutdown()


@pytest.fixture
def video(tmp_path):
    p = tmp_path / "out.mp4"
    p.write_bytes(os.urandom(700 * 1024))
    return p


def test_upload_streams_whole_file(server, video):
    res = youtube.upload_file("tok", video, {"snippet": {"title": "t"}})
    assert res["id"] == "abcdefghijk"
    assert bytes(FakeYouTube.received) == video.read_bytes()
    assert FakeYouTube.metadata == {"snippet": {"title": "t"}}
    assert FakeYouTube.puts == 3


def test_upload_resumes_after_server_error(server, video):
    FakeYouTube.fail_at_offsets = {256 * 1024}
    seen = []
    res = youtube.upload_file("tok", video, {}, progress=lambda d, t: seen.append(d))
    assert res["id"] == "abcdefghijk"
    assert bytes(FakeYouTube.received) == video.read_bytes()
    assert seen[-1] == video.stat().st_size


def test_video_id_parsing():
    assert youtube.video_id_from("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert youtube.video_id_from("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=1") == "dQw4w9WgXcQ"
    assert youtube.video_id_from("dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    with pytest.raises(youtube.UploadError):
        youtube.video_id_from("nope")


def test_metadata_from_manifest(tmp_path):
    rec = tmp_path / "2026-09-04-18-50-12"
    rec.mkdir()
    (rec / "manifest.json").write_text(json.dumps({"started_at": "2026-09-04T18:50:12+01:00", "duration_s": 61.2}))
    out = rec / "demo.mp4"
    out.write_bytes(b"x")
    meta = metadata_for(out, None, None, "unlisted")
    assert meta["snippet"]["title"] == "Demo 2026-09-04 18:50"
    assert meta["snippet"]["description"] == "Recorded 2026-09-04T18:50:12+01:00 · 61 s · rendered with demovid"
    assert meta["status"] == {"privacyStatus": "unlisted", "selfDeclaredMadeForKids": False}


def test_metadata_without_manifest(tmp_path):
    out = tmp_path / "clip.mp4"
    out.write_bytes(b"x")
    meta = metadata_for(out, None, None, "private")
    assert meta["snippet"]["title"] == "clip"
    assert meta["snippet"]["description"] == "rendered with demovid"
    assert meta["status"]["privacyStatus"] == "private"
