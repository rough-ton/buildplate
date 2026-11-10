import asyncio
import hashlib
import importlib
import json
import os
import socket
import stat
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

FAKE = Path(__file__).parent / "fixtures" / "fake_claude.py"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# One real server per test session: the MCP session managers can only start once per
# process. Every test points Buildplate at this port.
PORT = _free_port()
_SERVER: dict = {}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    # an executable stand-in for Claude Code, run by this interpreter
    fake = tmp_path / "bin" / "claude"
    fake.parent.mkdir()
    fake.write_text(f"#!{sys.executable}\n" + FAKE.read_text())
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    port = PORT
    for var in ("ANTHROPIC_API_KEY", "BUILDPLATE_CLAUDE_CODE_VERSION"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("BUILDPLATE_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("BUILDPLATE_PORT", str(port))
    monkeypatch.setenv("BUILDPLATE_CLAUDE_BIN", str(fake))
    # credentials in Buildplate's own environment must never reach Claude Code
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    from app import config

    importlib.reload(config)
    config.ensure_dirs()
    from app import settings

    importlib.reload(settings)
    from app import agent, auth, auth_methods, claude_code, connections, db, providers

    for m in (auth_methods, claude_code, connections, providers, auth, db, agent):
        importlib.reload(m)
    db.init()
    from app import main

    monkeypatch.setattr(main, "store", settings.store)
    return SimpleNamespace(config=config, store=settings.store, connections=connections, claude_code=claude_code,
                           db=db, agent=agent, main=main, port=port, fake=fake, tmp=tmp_path)


def test_method_and_status(env):
    from app import auth_methods

    m = auth_methods.METHODS["anthropic.claude_code"]
    assert (m.kind, m.runtime, m.billing, m.requires_password, m.fields) == ("cli_session", "cli:claude", "subscription", True, ())
    c = env.connections.create("anthropic.claude_code", "My Max plan", {})
    with pytest.raises(auth_methods.MethodError, match="Not signed in"):
        asyncio.run(env.connections.test(c["id"]))
    got = env.connections.get(c["id"])
    assert got["status"]["state"] == "needs_sign_in" and got["models"] == ["sonnet", "opus", "haiku", "fable"]
    # each connection has its own Claude Code folder
    cdir = env.claude_code.config_dir(c["id"])
    (cdir / "fake-signed-in").write_text("1")
    assert asyncio.run(env.connections.test(c["id"])) == ["sonnet", "opus", "haiku", "fable"]
    assert env.connections.get(c["id"])["status"] == {**env.connections.get(c["id"])["status"], "state": "connected", "detail": "Signed in (Max plan)"}
    assert env.store.get()["agent"]["connection"] == c["id"]
    asyncio.run(env.claude_code.logout(c["id"]))
    assert not (cdir / "fake-signed-in").exists()
    with pytest.raises(env.claude_code.ClaudeCodeError):
        env.claude_code.config_dir("../etc")


def test_child_env_is_scrubbed(env):
    e = env.claude_code.child_env("c_abc")
    assert "ANTHROPIC_API_KEY" not in e and "CLAUDE_CODE_USE_BEDROCK" not in e
    assert e["CLAUDE_CONFIG_DIR"].endswith("claude-code/c_abc") and e["DISABLE_UPDATES"] == "1"
    assert oct(os.stat(e["CLAUDE_CONFIG_DIR"]).st_mode & 0o777) == "0o700"


def test_password_gate(env):
    from fastapi.testclient import TestClient

    main = env.main
    local = TestClient(main.app, client=("127.0.0.1", 5000))
    # the server-itself exception does not apply to Claude plan connections
    r = local.post("/api/connections", json={"method": "anthropic.claude_code", "label": "x", "values": {}})
    assert r.status_code == 403 and "password" in r.json()["detail"]
    assert local.post("/api/connections", json={"method": "ollama.local", "values": {"base_url": "http://x:1"}}).status_code == 200
    listing = local.get("/api/connections").json()
    assert listing["editable"] is True and listing["password_session"] is False
    assert local.post("/api/claude-code/install").status_code == 403

    env.store.set_password("correct horse battery")
    lan = TestClient(main.app)
    lan.post("/api/auth/login", json={"password": "correct horse battery"})
    created = lan.post("/api/connections", json={"method": "anthropic.claude_code", "label": "Plan", "values": {}}).json()
    assert created["billing"] == "subscription" and created["secrets"] == {}
    assert lan.get("/api/connections").json()["password_session"] is True
    # the websocket refuses a missing session or a foreign origin
    from starlette.websockets import WebSocketDisconnect

    anon = TestClient(main.app)
    with pytest.raises(WebSocketDisconnect):
        with anon.websocket_connect(f"/api/connections/{created['id']}/terminal?action=login", headers={"origin": "http://testserver"}) as ws:
            ws.receive_json()
    with pytest.raises(WebSocketDisconnect):
        with lan.websocket_connect(f"/api/connections/{created['id']}/terminal?action=login", headers={"origin": "http://evil.example"}) as ws:
            ws.receive_json()
    with pytest.raises(WebSocketDisconnect):
        with lan.websocket_connect(f"/api/connections/{created['id']}/terminal?action=bash", headers={"origin": "http://testserver"}) as ws:
            ws.receive_json()


def test_terminal_sign_in_and_out(env):
    from fastapi.testclient import TestClient

    env.store.set_password("correct horse battery")
    client = TestClient(env.main.app)
    client.post("/api/auth/login", json={"password": "correct horse battery"})
    cid = client.post("/api/connections", json={"method": "anthropic.claude_code", "values": {}}).json()["id"]

    def session(action, answer=None):
        seen, code = "", None
        with client.websocket_connect(f"/api/connections/{cid}/terminal?action={action}", headers={"origin": "http://testserver"}) as ws:
            ws.send_json({"type": "resize", "cols": 120, "rows": 40})
            while True:
                msg = ws.receive_json()
                if msg["type"] == "output":
                    seen += msg["data"]
                    if answer and "Paste code here" in seen:
                        ws.send_json({"type": "input", "data": answer + "\r"})
                        answer = None
                elif msg["type"] == "exit":
                    code = msg["code"]
                    break
                elif msg["type"] == "error":
                    raise AssertionError(msg)
        return seen, code

    seen, code = session("login", "wrong-code")
    assert "https://claude.ai/oauth/authorize" in seen and "Invalid code" in seen and code == 1
    assert env.connections.get(cid)["status"]["state"] == "needs_sign_in"
    seen, code = session("login", "good-code")
    assert "Login successful" in seen and code == 0
    assert env.connections.get(cid)["status"]["state"] == "connected"
    r = client.post(f"/api/connections/{cid}/sign-out").json()
    assert r["connection"]["status"]["state"] == "needs_sign_in"
    # deleting the connection signs out and removes its Claude Code folder
    cdir = env.claude_code.config_dir(cid)
    assert cdir.exists()
    assert client.delete(f"/api/connections/{cid}").json() == {"ok": True}
    assert not cdir.exists()


@pytest.fixture()
def server(env, monkeypatch):
    """A real Buildplate server, so Claude Code (the fake) can reach /mcp-run."""
    import uvicorn

    from app import openscad

    async def fake_validate(code, params=None):
        return None, {"size_mm": [20.0, 10.0, 5.0], "min_mm": [0, 0, 0], "max_mm": [20, 10, 5], "triangles": 12,
                      "watertight": True, "bodies": 1, "issues": [], "printer": "Test"}

    async def fake_preview(code, params=None, views=None):
        return b"\x89PNG\r\n\x1a\nfake", None

    monkeypatch.setattr(openscad, "validate", fake_validate)
    monkeypatch.setattr(openscad, "preview_sheet", fake_preview)
    if "srv" not in _SERVER:
        srv = uvicorn.Server(uvicorn.Config(env.main.app, host="127.0.0.1", port=PORT, log_level="warning", lifespan="on"))
        threading.Thread(target=srv.run, daemon=True).start()
        for _ in range(200):
            if srv.started:
                break
            time.sleep(0.05)
        assert srv.started, "test server did not start"
        _SERVER["srv"] = srv
    yield env


def test_design_run_through_claude_code(server):
    env = server
    cid = env.connections.create("anthropic.claude_code", "Plan", {})["id"]
    env.claude_code.child_env(cid)  # creates the connection's Claude Code folder
    (env.claude_code.config_dir(cid) / "fake-signed-in").write_text("1")
    events = []

    async def go(prompt):
        events.clear()
        async for ev in env.agent.run(prompt, connection=cid, model="sonnet"):
            events.append(ev)

    asyncio.run(go("a 20 mm test block"))
    kinds = [e["type"] for e in events]
    assert kinds[0] == "run" and events[0]["billing"] == "subscription" and kinds[-1] == "done", events[-1]
    assert {"status", "assistant", "tool", "code", "check", "preview", "usage"} <= set(kinds)
    done = events[-1]
    assert done["title"] == "Fake block" and done["tags"] == ["block", "test"] and done["summary"] == "A 20 mm block."
    assert done["totals"]["input_tokens"] == 4100 and done["totals"]["cost_usd"] == 0 and done["totals"]["est_cost_usd"] == 0.0421

    run = env.db.get_run(events[0]["run_id"])
    assert (run["status"], run["billing"], run["auth_method"], run["model"]) == ("ok", "subscription", "anthropic.claude_code", "claude-sonnet-5-5")
    assert (run["input_tokens"], run["output_tokens"], run["cost_usd"], run["est_cost_usd"]) == (4100, 320, 0, 0.0421)
    assert [s["tool_calls"] for s in run["steps"]] == [["check_design"], ["submit_design"], []]
    summary = env.db.usage_summary(1)["totals"]
    assert summary["plan_runs"] == 1 and summary["cost_usd"] == 0 and abs(summary["plan_est_cost_usd"] - 0.0421) < 1e-9

    log = json.loads((env.claude_code.config_dir(cid) / "last-run.json").read_text())
    args = log["args"]
    assert args[args.index("--tools") + 1] == "" and "--restricted" in args and "--strict-mcp-config" in args
    assert args[args.index("--allowedTools") + 1] == "mcp__buildplate__check_design,mcp__buildplate__submit_design"
    assert args[args.index("--permission-mode") + 1] == "dontAsk" and "--no-session-persistence" in args
    assert "ANTHROPIC_API_KEY" not in log["env"] and "CLAUDE_CODE_USE_BEDROCK" not in log["env"]
    assert log["cwd_files"] == [".mcp-run.json"]  # an empty working folder apart from the MCP config
    assert "a 20 mm test block" in log["prompt"]
    check = json.loads((env.claude_code.config_dir(cid) / "last-check.json").read_text())
    assert check["kinds"] == ["text", "image"] and "Render: OK" in check["text"]
    # the run's token stopped working when the run ended
    assert not any(k for k in __import__("app.claude_runtime", fromlist=["_RUNS"])._RUNS)

    # a follow-up request sends the earlier conversation and current code
    design_id = done["design_id"]

    async def follow():
        events.clear()
        async for ev in env.agent.run("make it taller", design_id=design_id, connection=cid, model="sonnet"):
            events.append(ev)

    asyncio.run(follow())
    log = json.loads((env.claude_code.config_dir(cid) / "last-run.json").read_text())
    assert "Earlier conversation" in log["prompt"] and "cube([20, 10, 5]);" in log["prompt"] and "make it taller" in log["prompt"]
    assert events[-1]["type"] == "done" and events[-1]["version"] == 2


def test_run_without_submit_saves_last_good(server):
    env = server
    cid = env.connections.create("anthropic.claude_code", "Plan", {})["id"]
    events = []

    async def go():
        async for ev in env.agent.run("NO_SUBMIT block", connection=cid, model="opus"):
            events.append(ev)

    asyncio.run(go())
    assert events[-1]["type"] == "done" and "last design that rendered" in events[-1]["summary"]


def test_auth_failure_marks_connection(server):
    env = server
    cid = env.connections.create("anthropic.claude_code", "Plan", {})["id"]
    events = []

    async def go():
        async for ev in env.agent.run("FAIL_AUTH", connection=cid, model="sonnet"):
            events.append(ev)

    asyncio.run(go())
    assert events[-1]["type"] == "error" and "not signed in" in events[-1]["message"]
    assert env.connections.get(cid)["status"]["state"] == "needs_sign_in"
    assert env.db.get_run(events[0]["run_id"])["status"] == "error"


def test_mcp_run_endpoint_rejects_strangers(server):
    import httpx

    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    r = httpx.post(f"http://127.0.0.1:{server.port}/mcp-run", json=body, headers={"Authorization": "Bearer bprun_guess"})
    assert r.status_code == 401
    r = httpx.post(f"http://127.0.0.1:{server.port}/mcp-run", json=body)
    assert r.status_code == 401


def test_install_verifies_checksum(env, monkeypatch, tmp_path):
    import httpx

    cc = env.claude_code
    monkeypatch.delenv("BUILDPLATE_CLAUDE_BIN")
    payload = b"#!/bin/sh\necho 2.1.311\n"
    good = hashlib.sha256(payload).hexdigest()
    plat = cc.platform_id()
    manifest = {"platforms": {plat: {"checksum": good, "size": len(payload)}}}
    served = {"payload": payload}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/manifest.json"):
            assert request.url.path == f"/claude-code-releases/{cc.version()}/manifest.json"
            return httpx.Response(200, json=manifest)
        assert request.url.path == f"/claude-code-releases/{cc.version()}/{plat}/claude"
        return httpx.Response(200, content=served["payload"])

    real = httpx.AsyncClient

    def client(**kw):
        kw.pop("follow_redirects", None)
        return real(transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(cc.httpx, "AsyncClient", client)
    served["payload"] = b"tampered"
    with pytest.raises(cc.ClaudeCodeError, match="checksum"):
        asyncio.run(cc.install())
    assert cc.binary() is None and not cc.INSTALL["running"] and "checksum" in cc.INSTALL["error"]
    served["payload"] = payload
    path = asyncio.run(cc.install())
    assert path == cc.binary() and path.read_bytes() == payload and os.access(path, os.X_OK)
    assert cc.info()["installed"] is True
