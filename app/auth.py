"""Optional web UI password (signed session cookie) and MCP bearer token."""

from __future__ import annotations

import hashlib
import hmac
import time

from fastapi import Request

from .settings import store

COOKIE = "buildplate_session"
OPEN_API = ("/api/auth/", "/api/health")


def make_session() -> tuple[str, int]:
    hours = int(store.get()["security"]["session_hours"])
    exp = int(time.time()) + hours * 3600
    sig = hmac.new(store.signing_key, str(exp).encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}", hours * 3600


def valid_session(value: str | None) -> bool:
    if not value or "." not in value:
        return False
    exp, sig = value.split(".", 1)
    good = hmac.new(store.signing_key, exp.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, good) and exp.isdigit() and int(exp) > time.time()


def bearer(request: Request) -> str:
    h = request.headers.get("authorization", "")
    return h[7:].strip() if h.lower().startswith("bearer ") else ""


def token_ok(request: Request) -> bool:
    token = store.get()["security"]["mcp_token"]
    return bool(token) and hmac.compare_digest(bearer(request), token)


def api_allowed(request: Request) -> bool:
    if not store.password_enabled():
        return True
    if any(request.url.path.startswith(p) for p in OPEN_API):
        return True
    return valid_session(request.cookies.get(COOKIE)) or token_ok(request)


_LOOPBACK = {"127.0.0.1", "::1"}
_FORWARDED = ("x-forwarded-for", "forwarded", "x-real-ip")


def is_loopback(request: Request) -> bool:
    """A request made on the server itself. A reverse proxy on the same host also
    connects from loopback, so any forwarding header disqualifies the request."""
    host = request.client.host if request.client else ""
    return host in _LOOPBACK and not any(h in request.headers for h in _FORWARDED)


ADMIN_REQUIRED = (
    "Set a UI password first (Settings > Access and MCP), then sign in. AI connections hold "
    "credentials, so changing them needs a password unless you are on the server itself."
)


def admin_allowed(request: Request) -> bool:
    """Changing AI connections needs a signed-in UI password session, or a request from
    the server itself. An MCP bearer token is not enough."""
    if store.password_enabled() and valid_session(request.cookies.get(COOKIE)):
        return True
    return is_loopback(request)


def mcp_allowed(request: Request) -> bool:
    token = store.get()["security"]["mcp_token"]
    if not token:
        return True
    return token_ok(request)
