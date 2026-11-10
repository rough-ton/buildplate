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
    # input is all input tokens (cache reads and writes included), as for every other connection
    assert done["totals"]["input_tokens"] == 11250 and done["totals"]["output_tokens"] == 385
    assert done["totals"]["cost_usd"] == 0 and done["totals"]["est_cost_usd"] == 0.0321

    run = env.db.get_run(events[0]["run_id"])
    assert (run["status"], run["billing"], run["auth_method"], run["model"]) == ("ok", "subscription", "anthropic.claude_code", "claude-sonnet-5-5")
    assert (run["input_tokens"], run["output_tokens"], run["cache_read_tokens"], run["cache_write_tokens"]) == (11250, 385, 6500, 3500)
    assert (run["cost_usd"], run["est_cost_usd"], run["iterations"]) == (0, 0.0321, 3)
    assert [s["tool_calls"] for s in run["steps"]] == [["check_design"], ["submit_design"], []]
    summary = env.db.usage_summary(1)["totals"]
    assert summary["plan_runs"] == 1 and summary["cost_usd"] == 0 and abs(summary["plan_est_cost_usd"] - 0.0321) < 1e-9

    log = json.loads((env.claude_code.config_dir(cid) / "last-run.json").read_text())
    args = log["args"]
    assert args[args.index("--tools") + 1] == "" and "--restricted" in args and "--strict-mcp-config" in args
    assert args[args.index("--allowedTools") + 1] == "mcp__buildplate__check_design,mcp__buildplate__submit_design,mcp__buildplate__list_parts"
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


def test_plan_usage_metrics_are_accurate(server):
    """Every figure for a Claude plan request, checked against the stream by hand."""
    import litellm

    from app import providers

    env = server
    cid = env.connections.create("anthropic.claude_code", "Plan", {})["id"]
    events = []

    async def go():
        async for ev in env.agent.run("a 20 mm test block", connection=cid, model="sonnet"):
            events.append(ev)

    asyncio.run(go())
    assert events[-1]["type"] == "done", events[-1]
    run = env.db.get_run(events[0]["run_id"])
    steps = run["steps"]

    # per step: input = new + cache read + cache write; output is the final count from
    # message_delta, never the placeholder (1) on message_start and assistant messages
    got = [(s["input_tokens"], s["output_tokens"], s["cache_read_tokens"], s["cache_write_tokens"]) for s in steps]
    assert got == [(4000, 250, 0, 3000), (3700, 120, 3000, 500), (3550, 15, 3500, 0)]
    assert all(s["cost_usd"] == 0 for s in steps)
    # the subagent message (99,999 input) is not a step
    assert sum(s["input_tokens"] for s in steps) == run["input_tokens"] == 11250
    assert sum(s["output_tokens"] for s in steps) == run["output_tokens"] == 385

    # per-step API-price estimates, with the 1-hour cache write priced at the 1-hour rate
    p = litellm.model_cost["claude-sonnet-5-5"]
    step1 = 1000 * p["input_cost_per_token"] + 3000 * p["cache_creation_input_token_cost_above_1hr"] + 250 * p["output_cost_per_token"]
    assert abs(steps[0]["est_cost_usd"] - round(step1, 6)) < 1e-9
    step2 = 200 * p["input_cost_per_token"] + 3000 * p["cache_read_input_token_cost"] + 500 * p["cache_creation_input_token_cost"] + 120 * p["output_cost_per_token"]
    assert abs(steps[1]["est_cost_usd"] - round(step2, 6)) < 1e-9
    assert steps[2]["est_cost_usd"] == providers.estimate_cost("anthropic", "claude-sonnet-5-5", {
        "input_tokens": 3550, "cache_read_tokens": 3500, "output_tokens": 15})
    # the run's estimate is Claude Code's own, from modelUsage
    assert run["est_cost_usd"] == 0.0321

    # live Studio counters used the real output counts
    usage_events = [e for e in events if e["type"] == "usage"]
    assert [e["usage"]["output_tokens"] for e in usage_events] == [250, 120, 15]
    assert usage_events[-1]["totals"]["output_tokens"] == 385

    # the plan-limit warning seen during the run is kept on the connection
    lim = env.connections.get(cid)["status"]["plan_limit"]
    assert lim["status"] == "allowed_warning" and lim["utilization"] == 0.82

    # Usage: by-model and CSV carry the plan estimate and billing
    by_model = env.db.usage_summary(1)["by_model"]
    row = next(m for m in by_model if m["model"] == "claude-sonnet-5-5")
    assert (row["billing"], row["cost_usd"], row["est_cost_usd"], row["input_tokens"]) == ("subscription", 0, 0.0321, 11250)
    from fastapi.testclient import TestClient

    csv_text = TestClient(env.main.app).get("/api/usage/export.csv").text
    header, first = csv_text.splitlines()[0].split(","), csv_text.splitlines()[1].split(",")
    rec = dict(zip(header, first))
    assert {"billing", "connection_id", "auth_method", "est_cost_usd", "cache_write_tokens"} <= set(header)
    assert (rec["billing"], rec["est_cost_usd"], rec["input_tokens"]) == ("subscription", "0.0321", "11250")


def test_pricing_override_applies_to_plan_estimates(server):
    env = server
    env.store.update({"pricing": {"overrides": {"anthropic/claude-sonnet-5-5": {"input": 1.0, "output": 10.0}}}})
    cid = env.connections.create("anthropic.claude_code", "Plan", {})["id"]
    events = []

    async def go():
        async for ev in env.agent.run("block", connection=cid, model="sonnet"):
            events.append(ev)

    asyncio.run(go())
    run = env.db.get_run(events[0]["run_id"])
    assert run["est_cost_usd"] == round(11250 / 1e6 * 1.0 + 385 / 1e6 * 10.0, 6)
    assert run["steps"][0]["est_cost_usd"] == round(4000 / 1e6 + 250 / 1e6 * 10, 6)


def test_stopped_or_crashed_run_keeps_its_usage(server):
    env = server
    cid = env.connections.create("anthropic.claude_code", "Plan", {})["id"]
    events = []

    async def go():
        async for ev in env.agent.run("CRASH block", connection=cid, model="sonnet"):
            events.append(ev)

    asyncio.run(go())
    assert events[-1]["type"] == "error"
    run = env.db.get_run(events[0]["run_id"])
    # no result message: totals come from the step Claude Code completed
    assert (run["status"], run["input_tokens"], run["output_tokens"], run["iterations"]) == ("error", 4000, 250, 1)
    assert run["est_cost_usd"] == run["steps"][0]["est_cost_usd"] and run["est_cost_usd"] > 0


def test_plan_limit_rejection(server):
    env = server
    cid = env.connections.create("anthropic.claude_code", "Plan", {})["id"]
    events = []

    async def go():
        async for ev in env.agent.run("RATE_LIMIT block", connection=cid, model="sonnet"):
            events.append(ev)

    asyncio.run(go())
    msg = events[-1]["message"]
    assert events[-1]["type"] == "error" and "usage limit has been reached" in msg and "resets at" in msg
    assert env.connections.get(cid)["status"]["plan_limit"]["status"] == "rejected"
    assert env.connections.get(cid)["status"]["state"] != "needs_sign_in"  # a limit is not a sign-in problem
    run = env.db.get_run(events[0]["run_id"])
    assert (run["input_tokens"], run["output_tokens"]) == (4000, 250)


def test_plan_usage_report(env):
    from fastapi.testclient import TestClient

    client = TestClient(env.main.app)
    cid = env.connections.create("anthropic.claude_code", "Plan", {})["id"]
    r = client.get(f"/api/connections/{cid}/plan-usage")
    assert r.status_code == 400 and "not signed in" in r.json()["detail"]
    env.claude_code.child_env(cid)
    (env.claude_code.config_dir(cid) / "fake-signed-in").write_text("1")
    data = client.get(f"/api/connections/{cid}/plan-usage?refresh=1").json()
    rows = data["report"]["rate_limits"]["limits"]
    assert [(r["kind"], r["percent"]) for r in rows] == [("session", 23.5), ("weekly_all", 41), ("weekly_scoped", 88)]
    assert data["report"]["rate_limits"]["extra_usage"]["used_credits"] == 1250 and "23%" in data["text"]
    # cached for a minute, so the Usage page does not start Claude Code on every load
    (env.claude_code.config_dir(cid) / "fake-signed-in").unlink()
    assert client.get(f"/api/connections/{cid}/plan-usage").json()["report"] is not None
    ollama = env.connections.create("ollama.local", "", {"base_url": "http://x:1"})["id"]
    assert client.get(f"/api/connections/{ollama}/plan-usage").status_code == 400


def test_v040_plan_runs_are_corrected_once(tmp_path, monkeypatch):
    """v0.4.0 stored plan runs' input without cached tokens; the first start of this
    version adds them back, once."""
    import sqlite3

    monkeypatch.setenv("BUILDPLATE_DATA", str(tmp_path))
    from app import config

    importlib.reload(config)
    from app import db

    importlib.reload(db)
    db.init()
    with db.tx() as c:
        c.execute("DELETE FROM meta")  # as if the database came from v0.4.0
        c.execute("INSERT INTO runs (id, prompt, provider, model, status, started_at, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, billing, est_cost_usd) "
                  "VALUES ('plan', 'p', 'anthropic', 'claude-sonnet-5-5', 'ok', 1, 1250, 385, 6500, 3500, 'subscription', 0.0321)")
        c.execute("INSERT INTO runs (id, prompt, provider, model, status, started_at, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, billing) "
                  "VALUES ('api', 'p', 'anthropic', 'claude-sonnet-5-5', 'ok', 1, 9000, 100, 6000, 0, 'api')")
        c.execute("INSERT INTO run_steps (run_id, step, input_tokens, cache_read_tokens, cache_write_tokens) VALUES ('plan', 1, 1000, 0, 3000)")
    db.init()
    db.init()  # idempotent
    assert db.get_run("plan")["input_tokens"] == 11250 and db.get_run("plan")["steps"][0]["input_tokens"] == 4000
    assert db.get_run("api")["input_tokens"] == 9000  # pay-per-use runs were already right



def test_reference_picture_goes_in_a_stream_json_message(server):
    import io

    from PIL import Image

    from app import reference

    env = server
    cid = env.connections.create("anthropic.claude_code", "Plan", {})["id"]
    env.claude_code.child_env(cid)
    (env.claude_code.config_dir(cid) / "fake-signed-in").write_text("1")
    buf = io.BytesIO()
    Image.new("RGB", (20, 20), "red").save(buf, "PNG")
    ref = asyncio.run(reference.save("photo.png", buf.getvalue()))
    events = []

    async def go():
        async for ev in env.agent.run("a 20 mm test block like this", connection=cid, model="sonnet", reference=ref["id"]):
            events.append(ev)

    asyncio.run(go())
    assert events[-1]["type"] == "done", events[-1]
    last = json.loads((env.claude_code.config_dir(cid) / "last-run.json").read_text())
    args = last["args"]
    assert args[args.index("--input-format") + 1] == "stream-json"
    msg = json.loads(last["prompt"])
    assert msg["type"] == "user"
    text, image = msg["message"]["content"]
    assert "Reference: photo.png" in text["text"]
    assert image["type"] == "image" and image["source"]["media_type"] == "image/png"
