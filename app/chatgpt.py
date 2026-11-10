"""ChatGPT plan connections, through OpenAI's Sign in with ChatGPT (SIWC) for
open-source, self-hosted apps.

Sign-in follows OpenAI's documented flow for open-source clients: OAuth 2.0
authorization code with PKCE and OpenID Connect, a first-time dynamic registration
(client_id=dynamic_agent_client, agent_name_hint=Buildplate) that issues this user a
client ID, a stable per-install host ID (ext_agent_host_id), and the plan-usage scopes.

OpenAI requires a loopback redirect (http://127.0.0.1:<port>/auth/callback), which
reaches the computer running the browser, not a remote server. So the user approves in
their browser, lands on a 127.0.0.1 page that does not load, and pastes that address
into Buildplate; the server then exchanges the code with the PKCE verifier it kept.
The tokens never leave the server.

Credentials (ID token, access token, rotating refresh token) are kept per connection in
an encrypted file, written atomically, and refreshed under a lock so two requests never
race a single-use refresh token.

Requests go to the Responses API with store=false and stream=true, as OpenAI requires
for plan usage. acompletion() adapts the design loop's chat-completions style calls to
that API, so the loop and its tools work unchanged.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from . import config
from .auth_methods import MethodError
from .settings import store

ISSUER = "https://auth.openai.com"
AUTHORIZE_URL = f"{ISSUER}/api/accounts/authorize"
TOKEN_URL = f"{ISSUER}/api/accounts/oauth/token"
JWKS_URL = f"{ISSUER}/.well-known/jwks.json"
RESOURCE = "https://api.openai.com/v1"
API = "https://api.openai.com/v1"
REDIRECT_URI = "http://127.0.0.1:1455/auth/callback"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
APP_NAME = "Buildplate"
MANAGE_USAGE_URL = "https://chatgpt.com/settings/usage"
PENDING_TTL = 900
TERMINAL_REFRESH = {"invalid_grant", "invalid_refresh_token", "token_expired", "refresh_token_expired",
                    "refresh_token_invalidated", "refresh_token_reused"}


class ChatGPTError(MethodError):
    """Shown to the user. connection_state marks the connection (needs_sign_in);
    manage_url points at ChatGPT's usage settings for limit errors."""

    def __init__(self, message: str, connection_state: str | None = None, manage_url: str | None = None):
        super().__init__(message)
        self.connection_state = connection_state
        self.manage_url = manage_url


# ---- credential storage ------------------------------------------------------------------
_file_lock = threading.Lock()
_refresh_locks: dict[str, asyncio.Lock] = {}


def _dir() -> Path:
    d = config.DATA_DIR / "chatgpt"
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def host_id() -> str:
    """This install's ext_agent_host_id: a urn:uuid made once and kept."""
    p = _dir() / "host-id"
    with _file_lock:
        if p.exists():
            return p.read_text().strip()
        hid = f"urn:uuid:{uuid.uuid4()}"
        p.write_text(hid)
        os.chmod(p, 0o600)
        return hid


def _path(cid: str) -> Path:
    if not cid.startswith("c_") or "/" in cid:
        raise ChatGPTError("Invalid connection id.")
    return _dir() / f"{cid}.json"


def load(cid: str) -> dict[str, Any]:
    p = _path(cid)
    if not p.exists():
        return {}
    try:
        return json.loads(store._dec(p.read_text()) or "{}")
    except json.JSONDecodeError:
        return {}


def save(cid: str, record: dict[str, Any]) -> None:
    """Encrypt and write atomically with owner-only permissions."""
    p = _path(cid)
    tmp = p.with_suffix(".tmp")
    with _file_lock:
        tmp.write_text(store.encrypt(json.dumps(record)))
        os.chmod(tmp, 0o600)
        tmp.replace(p)


def forget(cid: str, keep_registration: bool = True) -> None:
    """Drop the tokens. The issued client ID, account and ID token are kept so the next
    sign-in reuses the registration, as OpenAI asks."""
    rec = load(cid)
    if keep_registration and rec.get("client_id"):
        save(cid, {k: rec[k] for k in ("client_id", "subject", "email", "id_token") if k in rec})
    else:
        _path(cid).unlink(missing_ok=True)


def status(cid: str) -> dict[str, Any]:
    rec = load(cid)
    if not rec.get("refresh_token") and not rec.get("access_token"):
        return {"state": "needs_sign_in", "detail": "Not signed in. Select Continue with ChatGPT."}
    if PLAN_SCOPE not in rec.get("scopes", []):
        return {"state": "needs_sign_in", "detail": "ChatGPT plan use was not allowed. Continue with ChatGPT again and allow it."}
    return {"state": "connected", "detail": f"Using ChatGPT plan ({rec.get('email') or 'signed in'})"}


# ---- sign-in -----------------------------------------------------------------------------------
_pending: dict[str, dict[str, Any]] = {}


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def start(cid: str) -> str:
    """The authorize URL for a connection. state, nonce and the PKCE verifier stay on
    the server for 15 minutes."""
    now = time.time()
    for k in [k for k, p in _pending.items() if now - p["at"] > PENDING_TTL]:
        _pending.pop(k, None)
    rec = load(cid)
    verifier = secrets.token_urlsafe(64)
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    client_id = rec.get("client_id") or "dynamic_agent_client"
    _pending[state] = {"cid": cid, "verifier": verifier, "nonce": nonce, "client_id": client_id, "at": now}
    q: dict[str, str] = {
        "client_id": client_id,
        "ext_agent_host_id": host_id(),
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "resource": RESOURCE,
        "state": state,
        "nonce": nonce,
        "code_challenge_method": "S256",
        "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()),
    }
    if client_id == "dynamic_agent_client":
        q["agent_name_hint"] = APP_NAME
    else:
        if rec.get("id_token"):
            q["id_token_hint"] = rec["id_token"]
        if rec.get("email"):
            q["login_hint"] = rec["email"]
    return f"{AUTHORIZE_URL}?{urlencode(q)}"


def _parse_return(pasted: str) -> dict[str, str]:
    """Accept the full 127.0.0.1 address, or just its query string."""
    text = pasted.strip()
    query = urlparse(text).query if "://" in text else text.lstrip("?")
    params = {k: v[0] for k, v in parse_qs(query).items()}
    if not params:
        raise ChatGPTError("Paste the whole address from the page ChatGPT sent you to (it starts with http://127.0.0.1).")
    return params


async def finish(cid: str, pasted: str) -> dict[str, Any]:
    """Complete sign-in from the pasted return address. Returns the new status, with
    first_time=True the first time this connection gets ChatGPT plan use."""
    params = _parse_return(pasted)
    pending = _pending.get(params.get("state", ""))
    if not pending or pending["cid"] != cid or time.time() - pending["at"] > PENDING_TTL:
        raise ChatGPTError("That address is from an old or different sign-in. Select Continue with ChatGPT and try again.")
    _pending.pop(params["state"], None)
    if params.get("error"):
        if params["error"] == "access_denied":
            raise ChatGPTError("You declined in ChatGPT, so nothing was connected. Continue with ChatGPT again to allow it.")
        raise ChatGPTError(f"ChatGPT returned an error: {params.get('error_description') or params['error']}")
    code = params.get("code")
    if not code:
        raise ChatGPTError("The address has no sign-in code. Copy the whole address from the address bar.")
    client_id = pending["client_id"]
    if client_id == "dynamic_agent_client":
        client_id = params.get("client_id", "")
        if not client_id or client_id == "dynamic_agent_client":
            raise ChatGPTError("ChatGPT did not finish registering Buildplate. Try again.")
    elif params.get("client_id") and params["client_id"] != client_id:
        raise ChatGPTError("That sign-in belongs to a different registration. Try again.")

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(TOKEN_URL, data={"grant_type": "authorization_code", "client_id": client_id, "code": code,
                                               "code_verifier": pending["verifier"], "redirect_uri": REDIRECT_URI, "resource": RESOURCE})
    if r.status_code != 200:
        raise ChatGPTError(f"ChatGPT did not accept the sign-in ({_error_code(r) or r.status_code}). Continue with ChatGPT again.")
    tok = r.json()
    claims = await verify_id_token(tok.get("id_token", ""), client_id, pending["nonce"])
    old = load(cid)
    if old.get("subject") and old["subject"] != claims["sub"]:
        raise ChatGPTError("That is a different ChatGPT account. Add another connection for it instead.")
    scopes = str(tok.get("scope") or params.get("scope") or "").split()
    first_time = PLAN_SCOPE in scopes and PLAN_SCOPE not in old.get("scopes", [])
    save(cid, {
        "client_id": client_id, "subject": claims["sub"], "email": claims.get("email", ""), "host_id": host_id(),
        "id_token": tok.get("id_token", ""), "access_token": tok.get("access_token", ""), "refresh_token": tok.get("refresh_token", ""),
        "expires_at": time.time() + float(tok.get("expires_in") or 3600), "scopes": scopes, "saved_at": time.time(),
    })
    return {**status(cid), "first_time": first_time}


def _error_code(r: httpx.Response) -> str:
    try:
        body = r.json()
    except ValueError:
        return ""
    err = body.get("error")
    if isinstance(err, dict):
        return str(err.get("code") or err.get("type") or "")
    return str(err or body.get("detail") or "")


# ---- ID token validation ----------------------------------------------------------------------
_jwks: dict[str, Any] = {}


async def _keys() -> list[dict[str, Any]]:
    if not _jwks or time.time() - _jwks.get("at", 0) > 3600:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(JWKS_URL)
            r.raise_for_status()
        _jwks.update(keys=r.json().get("keys", []), at=time.time())
    return _jwks["keys"]


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


async def verify_id_token(token: str, client_id: str, nonce: str) -> dict[str, Any]:
    """Check the ID token's RS256 signature against OpenAI's published keys, and its
    issuer, audience, expiry and nonce."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    try:
        head_b64, body_b64, sig_b64 = token.split(".")
        head, claims = json.loads(_b64d(head_b64)), json.loads(_b64d(body_b64))
    except (ValueError, json.JSONDecodeError):
        raise ChatGPTError("ChatGPT returned an unreadable sign-in token. Try again.") from None
    if head.get("alg") != "RS256":
        raise ChatGPTError("ChatGPT's sign-in token uses an unexpected algorithm.")
    key = next((k for k in await _keys() if k.get("kid") == head.get("kid")), None)
    if key is None:
        _jwks.clear()
        key = next((k for k in await _keys() if k.get("kid") == head.get("kid")), None)
    if key is None:
        raise ChatGPTError("Could not verify ChatGPT's sign-in token (unknown key).")
    pub = rsa.RSAPublicNumbers(int.from_bytes(_b64d(key["e"]), "big"), int.from_bytes(_b64d(key["n"]), "big")).public_key()
    try:
        pub.verify(_b64d(sig_b64), f"{head_b64}.{body_b64}".encode(), padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature:
        raise ChatGPTError("ChatGPT's sign-in token failed verification.") from None
    aud = claims.get("aud")
    auds = aud if isinstance(aud, list) else [aud]
    if claims.get("iss") != ISSUER or client_id not in auds or float(claims.get("exp", 0)) < time.time() - 60 or claims.get("nonce") != nonce:
        raise ChatGPTError("ChatGPT's sign-in token did not match this sign-in. Try again.")
    if not claims.get("sub"):
        raise ChatGPTError("ChatGPT's sign-in token has no account.")
    return claims


# ---- access tokens ---------------------------------------------------------------------------------
async def access_token(cid: str) -> str:
    """A valid access token, refreshing it (once, under a lock) when it is about to expire."""
    rec = load(cid)
    if not rec.get("access_token"):
        raise ChatGPTError("Sign in to ChatGPT first: Settings > AI connections > Continue with ChatGPT.", "needs_sign_in")
    if PLAN_SCOPE not in rec.get("scopes", []):
        raise ChatGPTError("ChatGPT plan use isn't allowed for this sign-in. Continue with ChatGPT again and allow it, or use an API key.", "needs_sign_in")
    if float(rec.get("expires_at", 0)) - time.time() > 120:
        return rec["access_token"]
    lock = _refresh_locks.setdefault(cid, asyncio.Lock())
    async with lock:
        rec = load(cid)  # another request may have refreshed while we waited
        if float(rec.get("expires_at", 0)) - time.time() > 120:
            return rec["access_token"]
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(TOKEN_URL, data={"grant_type": "refresh_token", "client_id": rec["client_id"],
                                                   "refresh_token": rec.get("refresh_token", ""), "resource": RESOURCE})
        if r.status_code != 200:
            code = _error_code(r)
            if code in TERMINAL_REFRESH or r.status_code in (400, 401):
                forget(cid)
                raise ChatGPTError("Your ChatGPT sign-in has expired or was disconnected. Continue with ChatGPT to sign in again.", "needs_sign_in")
            raise ChatGPTError(f"Could not renew the ChatGPT sign-in right now ({code or r.status_code}). Try again shortly.")
        tok = r.json()
        rec.update(access_token=tok["access_token"], refresh_token=tok.get("refresh_token") or rec.get("refresh_token"),
                   expires_at=time.time() + float(tok.get("expires_in") or 3600), saved_at=time.time())
        if tok.get("scope"):
            rec["scopes"] = str(tok["scope"]).split()
        save(cid, rec)  # access token, expiry, scopes and the rotated refresh token together
        return rec["access_token"]


async def list_models(cid: str) -> list[str]:
    token = await access_token(cid)
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(f"{API}/models", headers={"Authorization": f"Bearer {token}"})
    if r.status_code != 200:
        raise _api_error(r.status_code, _error_code(r))
    models = r.json().get("models") or r.json().get("data") or []
    return [m.get("slug") or m.get("id") for m in models if m.get("visibility", "list") == "list" and (m.get("slug") or m.get("id"))]


# ---- errors ------------------------------------------------------------------------------------------
def _api_error(status: int, code: str) -> ChatGPTError:
    if code == "subscription_sharing_usage_limit_exceeded" or status == 429:
        return ChatGPTError("Usage limit reached for your ChatGPT plan or for Buildplate. Review it in ChatGPT's usage settings, "
                            "or pick a model on another connection for this request.", manage_url=MANAGE_USAGE_URL)
    if code == "subscription_sharing_user_not_eligible":
        return ChatGPTError("ChatGPT plan use isn't available for this account or workspace (it needs a Plus or Pro plan). "
                            "Use an OpenAI API key connection instead.")
    if code in ("subscription_sharing_usage_unavailable", "subscription_sharing_user_unavailable") or status == 503:
        return ChatGPTError("ChatGPT couldn't check your plan's usage just now. Try again in a minute.")
    if code == "subscription_sharing_unsupported_capability":
        return ChatGPTError("ChatGPT plan use doesn't support part of this request (for example this model). Pick another model.")
    if code == "subscription_sharing_invalid_user" or status == 401:
        return ChatGPTError("ChatGPT didn't accept the sign-in. Continue with ChatGPT to sign in again.", "needs_sign_in")
    if status == 403:
        return ChatGPTError(f"ChatGPT refused the request ({code or 'policy or region restriction'}).")
    return ChatGPTError(f"ChatGPT request failed ({code or status}).")


# ---- Responses API adapter ------------------------------------------------------------------------------
NAMESPACE = "buildplate"


def _content(content: Any, role: str) -> Any:
    if isinstance(content, str):
        return content
    parts = []
    for c in content or []:
        if c.get("type") == "text":
            parts.append({"type": "input_text" if role == "user" else "output_text", "text": c["text"]})
        elif c.get("type") == "image_url":
            parts.append({"type": "input_image", "image_url": c["image_url"]["url"]})
    return parts


def to_responses(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Chat-completions messages and tools as a Responses request: the system message
    becomes instructions (system items are not accepted for plan usage), tool calls and
    results become function_call / function_call_output items, and tools are grouped
    in a namespace."""
    instructions, items = "", []
    for m in messages:
        role = m.get("role")
        if role == "system":
            instructions += (("\n\n" if instructions else "") + (m["content"] if isinstance(m["content"], str) else ""))
        elif role == "tool":
            items.append({"type": "function_call_output", "call_id": m["tool_call_id"], "output": str(m.get("content") or "")})
        elif role == "assistant":
            if m.get("content"):
                items.append({"role": "assistant", "content": m["content"] if isinstance(m["content"], str) else _content(m["content"], "assistant")})
            for tc in m.get("tool_calls") or []:
                items.append({"type": "function_call", "call_id": tc["id"], "name": tc["function"]["name"],
                              "arguments": tc["function"].get("arguments") or "{}"})
        else:
            items.append({"role": "user", "content": _content(m.get("content"), "user")})
    body: dict[str, Any] = {"instructions": instructions, "input": items, "store": False, "stream": True}
    if tools:
        body["tools"] = [{
            "type": "namespace", "name": NAMESPACE, "description": "Buildplate's design tools.",
            "tools": [{"type": "function", "name": t["function"]["name"], "description": t["function"].get("description", ""),
                       "parameters": t["function"].get("parameters") or {"type": "object", "properties": {}}} for t in tools],
        }]
    return body


def _tool_name(name: str) -> str:
    for sep in (".", "__", "/"):
        if name.startswith(NAMESPACE + sep):
            return name[len(NAMESPACE) + len(sep):]
    return name


async def acompletion(cid: str, *, model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, **_ignored: Any) -> Any:
    """Run one design-loop step through the Responses API and return it shaped like a
    LiteLLM chat completion. temperature and max_tokens are dropped: plan usage
    rejects them."""
    token = await access_token(cid)
    body = {"model": model, **to_responses(messages, tools)}
    text, calls, usage, failure = [], [], {}, None
    async with httpx.AsyncClient(timeout=httpx.Timeout(30, read=300)) as client:
        async with client.stream("POST", f"{API}/responses", json=body,
                                 headers={"Authorization": f"Bearer {token}", "Accept": "text/event-stream"}) as r:
            if r.status_code != 200:
                await r.aread()
                raise _api_error(r.status_code, _error_code(r))
            completed = False
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                ev = json.loads(data)
                kind = ev.get("type", "")
                if kind == "response.output_text.delta":
                    text.append(ev.get("delta", ""))
                elif kind == "response.output_item.done":
                    item = ev.get("item") or {}
                    if item.get("type") == "function_call":
                        calls.append(SimpleNamespace(id=item.get("call_id") or item.get("id"), type="function",
                                                     function=SimpleNamespace(name=_tool_name(item.get("name", "")), arguments=item.get("arguments") or "{}")))
                elif kind == "response.completed":
                    completed = True
                    usage = (ev.get("response") or {}).get("usage") or {}
                elif kind in ("response.failed", "error"):
                    err = (ev.get("response") or {}).get("error") or ev.get("error") or ev
                    failure = str(err.get("code") or err.get("type") or "")
                elif kind == "response.incomplete":
                    usage = (ev.get("response") or {}).get("usage") or {}
                    completed = True
    if failure:
        raise _api_error(429 if "limit" in failure else 0, failure)
    if not completed:
        raise ChatGPTError("The ChatGPT response stopped before it finished. Try again.")
    cached = int(((usage.get("input_tokens_details") or {}).get("cached_tokens")) or 0)
    return SimpleNamespace(
        model=model,
        choices=[SimpleNamespace(message=SimpleNamespace(content="".join(text), tool_calls=calls or None))],
        usage=SimpleNamespace(prompt_tokens=int(usage.get("input_tokens") or 0), completion_tokens=int(usage.get("output_tokens") or 0),
                              cache_read_input_tokens=cached, cache_creation_input_tokens=0,
                              prompt_tokens_details=SimpleNamespace(cached_tokens=cached)),
    )
