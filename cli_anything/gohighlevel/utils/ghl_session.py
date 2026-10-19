"""Self-renewing GHL internal-API session (added 2026-10-19, TEG fork).

The web app keeps four cookies on app.gohighlevel.com:
  access-token-v1 / access-token-v2   1-hour LeadConnector JWT (same value)
  refresh-token-v1 / refresh-token-v2 30-day refresh JWTs

The app renews them with
  POST https://backend.leadconnectorhq.com/oauth/2/login/token
  header  refresh-token: <refresh-token-v1>
  body    {"refreshTokenV2": "<refresh-token-v2>"}
which returns {jwt, authToken, refreshToken, v1JWTs:{jwt, refreshJwt}} with
rotated refresh tokens. Old refresh tokens stay valid, so a browser session
and this file can share one login.

Session file: $GHL_SESSION_FILE or ~/.config/ghl-cli/session.json (0600).
"""
from __future__ import annotations

import base64
import json
import os
import ssl
import time
import urllib.error
import urllib.request
from typing import Optional

OAUTH_BASE = "https://backend.leadconnectorhq.com/oauth"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)
REFRESH_MARGIN = 300  # renew when < 5 min of access-token life left
CTX = ssl.create_default_context()


class SessionError(RuntimeError):
    pass


def session_path() -> str:
    return os.environ.get("GHL_SESSION_FILE") or os.path.expanduser(
        "~/.config/ghl-cli/session.json"
    )


def jwt_claims(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def load() -> Optional[dict]:
    path = session_path()
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save(sess: dict) -> None:
    path = session_path()
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(sess, f, indent=1)
    os.replace(tmp, path)


def from_cookies(cookies: dict) -> dict:
    """Build a session dict from app.gohighlevel.com cookie values."""
    need = ("access-token-v2", "refresh-token-v1", "refresh-token-v2")
    missing = [k for k in need if not cookies.get(k)]
    if missing:
        raise SessionError(f"missing cookies: {', '.join(missing)} (not logged in?)")
    claims = jwt_claims(cookies["refresh-token-v2"])
    return {
        "access_token": cookies["access-token-v2"],
        "refresh_token_v1": cookies["refresh-token-v1"],
        "refresh_token_v2": cookies["refresh-token-v2"],
        "user_id": claims.get("authClassId"),
        "source": "browser-cookies",
        "updated_at": int(time.time()),
    }


def refresh(sess: dict) -> dict:
    """Exchange the refresh tokens for a new access token; persists rotation."""
    req = urllib.request.Request(
        f"{OAUTH_BASE}/2/login/token",
        method="POST",
        data=json.dumps({"refreshTokenV2": sess["refresh_token_v2"]}).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "channel": "APP",
            "source": "WEB_USER",
            "version": "2021-07-28",
            "refresh-token": sess["refresh_token_v1"],
            "User-Agent": UA,
        },
    )
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=30) as r:
            body = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise SessionError(
            f"refresh rejected ({e.code}): {e.read()[:200]!r} — re-bootstrap with "
            "`ghl-session from-browser` after logging in"
        ) from None
    v1 = body.get("v1JWTs") or {}
    sess = dict(sess)
    sess["access_token"] = body.get("authToken") or body["jwt"]
    sess["refresh_token_v2"] = body.get("refreshToken") or sess["refresh_token_v2"]
    sess["refresh_token_v1"] = v1.get("refreshJwt") or sess["refresh_token_v1"]
    sess["updated_at"] = int(time.time())
    save(sess)
    return sess


def access_token(force: bool = False) -> Optional[str]:
    """Valid access token from the session file, renewing it if needed.
    Returns None when no session file exists."""
    sess = load()
    if not sess:
        return None
    exp = jwt_claims(sess.get("access_token", "")).get("exp", 0)
    if force or exp - time.time() < REFRESH_MARGIN:
        sess = refresh(sess)
    return sess["access_token"]


def status() -> dict:
    sess = load()
    if not sess:
        return {"session": None, "path": session_path()}
    now = time.time()
    a = jwt_claims(sess.get("access_token", ""))
    r = jwt_claims(sess.get("refresh_token_v2", ""))
    return {
        "path": session_path(),
        "user_id": sess.get("user_id"),
        "access_expires_in_min": round((a.get("exp", 0) - now) / 60, 1),
        "refresh_expires_in_days": round((r.get("exp", 0) - now) / 86400, 1),
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(sess.get("updated_at", 0))),
    }
