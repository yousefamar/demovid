import base64
import hashlib
import http.server
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

from demovid import CONFIG_DIR

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/youtube"

CONFIG_PATH = Path(CONFIG_DIR).expanduser()
CLIENT_SECRET_PATH = CONFIG_PATH / "client_secret.json"
# gog's desktop OAuth client lives in the same GCP project (yousef-amar); reuse it so no Cloud Console step is needed.
GOG_CLIENT_PATH = Path("~/.config/gogcli/credentials.json").expanduser()
TOKEN_PATH = CONFIG_PATH / "youtube_token.json"


class AuthError(Exception):
    pass


def _client() -> dict:
    for path in (CLIENT_SECRET_PATH, GOG_CLIENT_PATH):
        if path.exists():
            data = json.loads(path.read_text())
            client = data.get("installed") or data.get("web") or data
            if "client_id" in client:
                return client
    raise AuthError(
        f"no OAuth client: put a Desktop-app client_secret.json at {CLIENT_SECRET_PATH} "
        f"(or install gog, whose client at {GOG_CLIENT_PATH} is reused)"
    )


def _write_private(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def _token_request(form: dict) -> dict:
    body = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise AuthError(f"token endpoint {e.code}: {detail}") from None


class _CodeCatcher(http.server.BaseHTTPRequestHandler):
    result: dict = {}

    def do_GET(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _CodeCatcher.result = {k: v[0] for k, v in qs.items()}
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        msg = "demovid: authorised, you can close this tab." if "code" in qs else f"demovid: auth failed: {qs}"
        self.wfile.write(msg.encode())

    def log_message(self, *_):
        pass


def authorise(open_browser: bool = True) -> dict:
    client = _client()
    server = http.server.HTTPServer(("127.0.0.1", 0), _CodeCatcher)
    redirect_uri = f"http://127.0.0.1:{server.server_port}/"
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    params = {
        "client_id": client["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"
    print(f"Open this URL to authorise demovid:\n{url}\n")
    if open_browser:
        webbrowser.open(url)
    server.timeout = 300
    _CodeCatcher.result = {}
    server.handle_request()
    server.server_close()
    result = _CodeCatcher.result
    if result.get("state") != state or "code" not in result:
        raise AuthError(f"no authorisation code received: {result or 'timed out'}")
    tokens = _token_request({
        "client_id": client["client_id"],
        "client_secret": client.get("client_secret", ""),
        "code": result["code"],
        "code_verifier": verifier,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    })
    if "refresh_token" not in tokens:
        raise AuthError("Google returned no refresh_token; revoke demovid at myaccount.google.com/permissions and retry")
    saved = {
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "expires_at": time.time() + tokens.get("expires_in", 3600),
        "scope": tokens.get("scope", SCOPE),
    }
    _write_private(TOKEN_PATH, saved)
    return saved


def access_token() -> str:
    if not TOKEN_PATH.exists():
        raise AuthError(f"not authorised — run `demovid upload --auth`")
    saved = json.loads(TOKEN_PATH.read_text())
    if time.time() < saved["expires_at"] - 60:
        return saved["access_token"]
    client = _client()
    try:
        fresh = _token_request({
            "client_id": client["client_id"],
            "client_secret": client.get("client_secret", ""),
            "refresh_token": saved["refresh_token"],
            "grant_type": "refresh_token",
        })
    except AuthError as e:
        if "invalid_grant" in str(e):
            raise AuthError(
                "refresh token rejected (expired or revoked) — run `demovid upload --auth`. "
                "If this recurs weekly, set the GCP OAuth app's publishing status to 'In production'."
            ) from None
        raise
    saved["access_token"] = fresh["access_token"]
    saved["expires_at"] = time.time() + fresh.get("expires_in", 3600)
    _write_private(TOKEN_PATH, saved)
    return saved["access_token"]
