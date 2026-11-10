"""Sign in with ChatGPT and ChatGPT plan requests, against a mock OpenAI."""
import asyncio
import base64
import hashlib
import importlib
import json
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def jwk() -> dict:
    n = KEY.public_key().public_numbers()
    return {"kty": "RSA", "kid": "k1", "alg": "RS256", "n": b64(n.n.to_bytes(256, "big")), "e": b64(n.e.to_bytes(3, "big"))}


def id_token(claims: dict, key=KEY, kid="k1") -> str:
    head = b64(json.dumps({"alg": "RS256", "kid": kid}).encode())
    body = b64(json.dumps(claims).encode())
    sig = key.sign(f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{head}.{body}.{b64(sig)}"


class OpenAI:
    """Mock auth.openai.com and api.openai.com."""

    def __init__(self):
        self.calls = []
        self.nonce = None
        self.client_id = "oaiapp_123"
        self.scope = "chatgpt.tokens.use.direct email offline_access openid profile resource.invoke"
        self.refreshes = 0
        self.refresh_error = None
        self.streams = []  # scripted SSE event lists, one per /responses call
        self.response_status = 200
        self.sub = "user-1"

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        body = request.content.decode() if request.content else ""
        self.calls.append((request.method, url, body))
        if url.endswith("/.well-known/jwks.json"):
            return httpx.Response(200, json={"keys": [jwk()]})
        if url.endswith("/api/accounts/oauth/token"):
            form = {k: v[0] for k, v in parse_qs(body).items()}
            if form["grant_type"] == "authorization_code":
                tok = id_token({"iss": "https://auth.openai.com", "aud": self.client_id, "sub": self.sub, "email": "maker@example.com",
                                "exp": time.time() + 3600, "nonce": self.nonce})
                return httpx.Response(200, json={"access_token": "at-1", "refresh_token": "rt-1", "id_token": tok, "expires_in": 3600, "scope": self.scope})
            if self.refresh_error:
                return httpx.Response(400, json={"error": self.refresh_error})
            self.refreshes += 1
            assert form["refresh_token"] == f"rt-{self.refreshes}"
            return httpx.Response(200, json={"access_token": f"at-{self.refreshes + 1}", "refresh_token": f"rt-{self.refreshes + 1}", "expires_in": 3600})
        if url.endswith("/v1/models"):
            return httpx.Response(200, json={"models": [{"slug": "gpt-6.1-sol", "display_name": "GPT-6.1", "visibility": "list"},
                                                        {"slug": "internal", "visibility": "hide"}]})
        if url.endswith("/v1/responses"):
            if self.response_status != 200:
                return httpx.Response(self.response_status, json={"error": {"code": "subscription_sharing_usage_limit_exceeded"}})
            events = self.streams.pop(0)
            text = "".join(f"data: {json.dumps(e)}\n\n" for e in events)
            return httpx.Response(200, content=text.encode(), headers={"content-type": "text/event-stream"})
        return httpx.Response(404)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("BUILDPLATE_DATA", str(tmp_path))
    from app import config

    importlib.reload(config)
    from app import settings

    importlib.reload(settings)
    from app import auth, auth_methods, chatgpt, connections, db, providers

    for m in (auth_methods, connections, providers, auth, db, chatgpt):
        importlib.reload(m)
    db.init()
    fake = OpenAI()
    monkeypatch.setattr(chatgpt, "_client", lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(fake.handler), **kw))
    from app import agent

    importlib.reload(agent)
    return SimpleNamespace(store=settings.store, connections=connections, chatgpt=chatgpt, db=db, agent=agent, fake=fake, tmp=tmp_path)


def sign_in(env, cid):
    url = env.chatgpt.start(cid)
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    env.fake.nonce = q["nonce"]
    back = f"http://127.0.0.1:1455/auth/callback?code=c0de&state={q['state']}&client_id={env.fake.client_id}&scope=x"
    return q, asyncio.run(env.chatgpt.finish(cid, back))


def test_authorize_url_follows_openai_spec(env):
    cid = env.connections.create("openai.chatgpt", "ChatGPT", {})["id"]
    url = env.chatgpt.start(cid)
    assert url.startswith("https://auth.openai.com/api/accounts/authorize?")
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    assert q["client_id"] == "dynamic_agent_client" and q["agent_name_hint"] == "Buildplate"
    assert q["redirect_uri"] == "http://127.0.0.1:1455/auth/callback" and q["resource"] == "https://api.openai.com/v1"
    assert set(q["scope"].split()) == {"openid", "profile", "email", "offline_access", "resource.invoke", "chatgpt.tokens.use.direct"}
    assert q["code_challenge_method"] == "S256" and q["response_type"] == "code" and q["nonce"] and q["state"]
    assert q["ext_agent_host_id"].startswith("urn:uuid:") and env.chatgpt.host_id() == q["ext_agent_host_id"]  # stable per install


def test_sign_in_stores_encrypted_tokens(env):
    cid = env.connections.create("openai.chatgpt", "ChatGPT", {})["id"]
    assert env.connections.get(cid)["status"]["state"] == "needs_sign_in"
    q, result = sign_in(env, cid)
    assert result["state"] == "connected" and result["first_time"] is True
    exchange = next(b for m, u, b in env.fake.calls if u.endswith("/oauth/token"))
    form = {k: v[0] for k, v in parse_qs(exchange).items()}
    verifier_ok = b64(hashlib.sha256(form["code_verifier"].encode()).digest()) == q["code_challenge"]
    assert verifier_ok and form["client_id"] == "oaiapp_123" and form["redirect_uri"] == q["redirect_uri"] and form["resource"] == q["resource"]
    raw = (env.tmp / "chatgpt" / f"{cid}.json").read_text()
    assert "at-1" not in raw and "rt-1" not in raw  # encrypted at rest
    rec = env.chatgpt.load(cid)
    assert (rec["client_id"], rec["subject"], rec["email"], rec["refresh_token"]) == ("oaiapp_123", "user-1", "maker@example.com", "rt-1")
    assert oct((env.tmp / "chatgpt" / f"{cid}.json").stat().st_mode & 0o777) == "0o600"
    # a later sign-in reuses the issued client ID with hints, and is not "first time"
    url = env.chatgpt.start(cid)
    q2 = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    assert q2["client_id"] == "oaiapp_123" and "agent_name_hint" not in q2 and q2["login_hint"] == "maker@example.com" and q2["id_token_hint"]
    env.fake.nonce = q2["nonce"]
    again = asyncio.run(env.chatgpt.finish(cid, f"?code=c&state={q2['state']}"))
    assert again["first_time"] is False


def test_sign_in_rejections(env):
    C = env.chatgpt
    cid = env.connections.create("openai.chatgpt", "ChatGPT", {})["id"]
    with pytest.raises(C.ChatGPTError, match="old or different"):
        asyncio.run(C.finish(cid, "http://127.0.0.1:1455/auth/callback?code=x&state=forged"))
    q = {k: v[0] for k, v in parse_qs(urlparse(C.start(cid)).query).items()}
    with pytest.raises(C.ChatGPTError, match="declined"):
        asyncio.run(C.finish(cid, f"http://127.0.0.1:1455/auth/callback?error=access_denied&state={q['state']}"))
    q = {k: v[0] for k, v in parse_qs(urlparse(C.start(cid)).query).items()}
    with pytest.raises(C.ChatGPTError, match="did not finish registering"):
        asyncio.run(C.finish(cid, f"http://127.0.0.1:1455/auth/callback?code=x&state={q['state']}"))  # no issued client_id
    # a token for another nonce (replay) fails validation
    q = {k: v[0] for k, v in parse_qs(urlparse(C.start(cid)).query).items()}
    env.fake.nonce = "someone-elses"
    with pytest.raises(C.ChatGPTError, match="did not match"):
        asyncio.run(C.finish(cid, f"?code=x&state={q['state']}&client_id=oaiapp_123"))
    with pytest.raises(C.ChatGPTError, match="Paste the whole address"):
        asyncio.run(C.finish(cid, "   "))
    assert C.status(cid)["state"] == "needs_sign_in"


def test_id_token_signature_is_checked(env):
    C = env.chatgpt
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = id_token({"iss": "https://auth.openai.com", "aud": "oaiapp_1", "sub": "u", "exp": time.time() + 60, "nonce": "n"}, key=other)
    with pytest.raises(C.ChatGPTError, match="failed verification"):
        asyncio.run(C.verify_id_token(forged, "oaiapp_1", "n"))
    good = id_token({"iss": "https://auth.openai.com", "aud": "oaiapp_1", "sub": "u", "exp": time.time() + 60, "nonce": "n"})
    assert asyncio.run(C.verify_id_token(good, "oaiapp_1", "n"))["sub"] == "u"
    with pytest.raises(C.ChatGPTError, match="did not match"):
        asyncio.run(C.verify_id_token(good, "oaiapp_OTHER", "n"))


def test_plan_scope_declined(env):
    env.fake.scope = "openid profile email offline_access"
    cid = env.connections.create("openai.chatgpt", "ChatGPT", {})["id"]
    _, result = sign_in(env, cid)
    assert result["state"] == "needs_sign_in" and "not allowed" in result["detail"] and result["first_time"] is False
    with pytest.raises(env.chatgpt.ChatGPTError, match="isn't allowed"):
        asyncio.run(env.chatgpt.access_token(cid))


def test_refresh_rotates_and_is_serialised(env):
    C = env.chatgpt
    cid = env.connections.create("openai.chatgpt", "ChatGPT", {})["id"]
    sign_in(env, cid)
    rec = C.load(cid)
    rec["expires_at"] = time.time() + 30  # about to expire
    C.save(cid, rec)

    async def many():
        return await asyncio.gather(*[C.access_token(cid) for _ in range(5)])

    assert asyncio.run(many()) == ["at-2"] * 5 and env.fake.refreshes == 1  # one refresh for five requests
    assert C.load(cid)["refresh_token"] == "rt-2"
    form = {k: v[0] for k, v in parse_qs([b for m, u, b in env.fake.calls if "refresh_token" in b][-1]).items()}
    assert form == {"grant_type": "refresh_token", "client_id": "oaiapp_123", "refresh_token": "rt-1", "resource": "https://api.openai.com/v1"}
    # a terminal refresh error clears the tokens but keeps the registration
    rec = C.load(cid)
    rec["expires_at"] = 0
    C.save(cid, rec)
    env.fake.refresh_error = "refresh_token_reused"
    with pytest.raises(C.ChatGPTError) as e:
        asyncio.run(C.access_token(cid))
    assert e.value.connection_state == "needs_sign_in"
    assert C.load(cid) == {k: C.load(cid)[k] for k in ("client_id", "subject", "email", "id_token")} and C.status(cid)["state"] == "needs_sign_in"


def test_responses_request_shape(env):
    msgs = [
        {"role": "system", "content": "You design parts."},
        {"role": "user", "content": "a hook"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "check_design", "arguments": '{"code":"cube(1);"}'}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "Render: OK"},
        {"role": "user", "content": [{"type": "text", "text": "Preview:"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}}]},
    ]
    tools = [{"type": "function", "function": {"name": "check_design", "description": "d", "parameters": {"type": "object"}}}]
    body = env.chatgpt.to_responses(msgs, tools)
    assert body["instructions"] == "You design parts." and body["store"] is False and body["stream"] is True
    assert all(i.get("role") != "system" for i in body["input"])
    assert body["input"][1] == {"type": "function_call", "call_id": "call_1", "name": "check_design", "arguments": '{"code":"cube(1);"}'}
    assert body["input"][2] == {"type": "function_call_output", "call_id": "call_1", "output": "Render: OK"}
    assert body["input"][3]["content"][1] == {"type": "input_image", "image_url": "data:image/png;base64,AAA"}
    assert body["tools"][0]["type"] == "namespace" and body["tools"][0]["tools"][0]["name"] == "check_design"


def test_design_request_through_chatgpt(env, monkeypatch):
    from app import openscad

    async def validate(code, params=None):
        return None, {"size_mm": [20.0, 10.0, 5.0], "min_mm": [0, 0, 0], "max_mm": [20, 10, 5], "triangles": 12, "watertight": True, "bodies": 1, "issues": []}

    async def preview(code, params=None, views=None):
        raise openscad.RenderError("none", "")

    monkeypatch.setattr(openscad, "validate", validate)
    monkeypatch.setattr(openscad, "preview_sheet", preview)
    cid = env.connections.create("openai.chatgpt", "ChatGPT", {})["id"]
    sign_in(env, cid)
    assert asyncio.run(env.connections.test(cid)) == ["gpt-6.1-sol"]
    call = {"type": "function_call", "call_id": "call_9", "name": "submit_design",
            "arguments": json.dumps({"code": "cube([20,10,5]);", "title": "Block", "summary": "A block."})}
    env.fake.streams.append([
        {"type": "response.output_text.delta", "delta": "Here it is."},
        {"type": "response.output_item.done", "item": call},
        {"type": "response.completed", "response": {"usage": {"input_tokens": 900, "output_tokens": 120, "input_tokens_details": {"cached_tokens": 300}}}},
    ])
    events = []

    async def go():
        async for ev in env.agent.run("a 20 mm block", connection=cid, model="gpt-6.1-sol"):
            events.append(ev)

    asyncio.run(go())
    done = events[-1]
    assert done["type"] == "done" and done["title"] == "Block", events[-1]
    run = env.db.get_run(events[0]["run_id"])
    assert (run["billing"], run["cost_usd"], run["input_tokens"], run["output_tokens"], run["cache_read_tokens"]) == ("subscription", 0, 900, 120, 300)
    sent = json.loads([b for m, u, b in env.fake.calls if u.endswith("/v1/responses")][-1])
    assert sent["model"] == "gpt-6.1-sol" and "temperature" not in sent and "max_tokens" not in sent and "max_output_tokens" not in sent
    assert sent["tools"][0]["type"] == "namespace"

    # a plan limit stops with a Manage usage link
    env.fake.response_status = 429
    events.clear()
    asyncio.run(go())
    assert events[-1]["type"] == "error" and "Usage limit reached" in events[-1]["message"]
    assert events[-1]["manage_url"] == "https://chatgpt.com/settings/usage"


def test_endpoints(env, monkeypatch):
    from fastapi.testclient import TestClient

    from app import main

    monkeypatch.setattr(main, "store", env.store)
    local = TestClient(main.app, client=("127.0.0.1", 5000))
    r = local.post("/api/connections", json={"method": "openai.chatgpt", "label": "ChatGPT", "values": {}})
    assert r.status_code == 403  # plan connections need a password session
    env.store.set_password("correct horse battery")
    client = TestClient(main.app)
    client.post("/api/auth/login", json={"password": "correct horse battery"})
    cid = client.post("/api/connections", json={"method": "openai.chatgpt", "label": "ChatGPT", "values": {}}).json()["id"]
    url = client.post(f"/api/connections/{cid}/chatgpt/start").json()["url"]
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    env.fake.nonce = q["nonce"]
    done = client.post(f"/api/connections/{cid}/chatgpt/finish", json={"url": f"http://127.0.0.1:1455/auth/callback?code=c&state={q['state']}&client_id=oaiapp_123"}).json()
    assert done["first_time"] is True and done["connection"]["status"]["state"] == "connected" and done["connection"]["models"] == ["gpt-6.1-sol"]
    assert client.post(f"/api/connections/{cid}/chatgpt/finish", json={"url": "?code=c&state=nope"}).status_code == 400
    assert client.get(f"/api/connections/{cid}/plan-usage").json()["manage_url"] == "https://chatgpt.com/settings/usage"
    assert client.post(f"/api/connections/{cid}/sign-out").json()["connection"]["status"]["state"] == "needs_sign_in"
    assert client.delete(f"/api/connections/{cid}").json() == {"ok": True}
    assert not (env.tmp / "chatgpt" / f"{cid}.json").exists()
