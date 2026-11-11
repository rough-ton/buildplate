import asyncio
import dataclasses
import importlib
import json
import os
import time
from types import SimpleNamespace

import pytest

ENV_VARS = ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY", "XAI_API_KEY",
            "MISTRAL_API_KEY", "GROQ_API_KEY", "DEEPSEEK_API_KEY", "OLLAMA_BASE_URL", "BUILDPLATE_ADMIN_PASSWORD"]


def _load(tmp_path, monkeypatch, settings_json=None, env=None):
    for v in ENV_VARS:
        monkeypatch.delenv(v, raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("BUILDPLATE_DATA", str(tmp_path))
    from app import config

    importlib.reload(config)
    config.ensure_dirs()
    from app import settings

    importlib.reload(settings)
    if settings_json is not None:
        # encrypt legacy keys with this install's secret, as the old code would have
        for p in settings_json.get("providers", {}).values():
            if p.get("api_key"):
                p["api_key"] = settings.store.encrypt(p["api_key"])
        config.SETTINGS_FILE.write_text(json.dumps(settings_json))
        os.utime(config.SETTINGS_FILE, (time.time() + 5, time.time() + 5))
    from app import auth, auth_methods, connections, db, providers

    for m in (auth_methods, connections, providers, auth, db):
        importlib.reload(m)
    db.init()
    return SimpleNamespace(config=config, settings=settings, store=settings.store, connections=connections, auth=auth, db=db)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    return _load(tmp_path, monkeypatch)


LEGACY = {
    "providers": {
        "anthropic": {"enabled": True, "api_key": "sk-ant-legacy-1234", "base_url": "https://api.anthropic.com", "models": ["claude-sonnet-5-5"]},
        "openai": {"enabled": False, "api_key": "sk-openai-disabled-9999", "models": []},
        "gemini": {"enabled": False, "api_key": "", "models": []},
        "ollama": {"enabled": True, "api_key": "", "base_url": "http://10.0.0.5:11434", "models": ["qwen2.5-coder:32b"]},
    },
    "agent": {"provider": "anthropic", "model": "claude-sonnet-5-5", "max_iterations": 9},
}


def test_legacy_settings_migrate_to_connections(tmp_path, monkeypatch):
    e = _load(tmp_path, monkeypatch, json.loads(json.dumps(LEGACY)))
    s = e.store.get()
    by_id = {c["id"]: c for c in s["connections"]}
    assert set(by_id) == {"c_anthropic", "c_openai", "c_ollama"}  # unconfigured gemini is dropped
    assert by_id["c_anthropic"]["method"] == "anthropic.api_key" and by_id["c_anthropic"]["secrets"]["api_key"] == "sk-ant-legacy-1234"
    assert by_id["c_anthropic"]["models"] == ["claude-sonnet-5-5"] and by_id["c_anthropic"]["status"]["state"] == "connected"
    assert by_id["c_openai"]["enabled"] is False and by_id["c_openai"]["secrets"]["api_key"] == "sk-openai-disabled-9999"
    assert by_id["c_ollama"]["fields"] == {"base_url": "http://10.0.0.5:11434"} and by_id["c_ollama"]["secrets"] == {}
    assert s["agent"]["connection"] == "c_anthropic" and s["agent"]["model"] == "claude-sonnet-5-5"
    assert s["agent"]["max_iterations"] == 9 and "provider" not in s["agent"]

    raw = json.loads(e.config.SETTINGS_FILE.read_text())
    assert raw["settings_version"] == 2 and "providers" not in raw
    assert "sk-ant-legacy-1234" not in e.config.SETTINGS_FILE.read_text()

    # the old request shape still resolves
    r = e.connections.resolve(provider="anthropic")
    assert r.id == "c_anthropic" and r.kwargs == {"model": "anthropic/claude-sonnet-5-5", "api_key": "sk-ant-legacy-1234"}


def test_public_masks_secrets_and_serves_legacy_view(tmp_path, monkeypatch):
    e = _load(tmp_path, monkeypatch, json.loads(json.dumps(LEGACY)))
    pub = e.store.public()
    blob = json.dumps(pub)
    assert "sk-ant-legacy-1234" not in blob and "sk-openai-disabled-9999" not in blob
    c = next(x for x in pub["connections"] if x["id"] == "c_anthropic")
    assert c["secrets"] == {"api_key": {"set": True, "hint": "****1234"}}
    assert c["family"] == "anthropic" and c["billing"] == "api"
    assert pub["providers"]["anthropic"]["api_key_hint"] == "****1234" and pub["providers"]["anthropic"]["enabled"]
    assert pub["providers"]["ollama"]["base_url"] == "http://10.0.0.5:11434"
    assert pub["providers"]["xai"]["meta"]["label"] == "xAI"
    assert pub["agent"]["provider"] == "anthropic"


def test_env_bootstrap_creates_connections(tmp_path, monkeypatch):
    e = _load(tmp_path, monkeypatch, env={"OPENAI_API_KEY": "sk-env-abcdefgh", "OLLAMA_BASE_URL": "http://box:11434", "GROQ_API_KEY": "gsk_123456789"})
    conns = {c["method"]: c for c in e.store.get()["connections"]}
    assert conns["openai.api_key"]["secrets"]["api_key"] == "sk-env-abcdefgh"
    assert conns["ollama.local"]["fields"]["base_url"] == "http://box:11434"
    assert conns["groq.api_key"]["enabled"]
    # an existing connection of that method is never duplicated or overwritten
    e.connections.update(conns["openai.api_key"]["id"], data={"api_key": "sk-user-changed99"})
    e.store._mtime = -1
    after = [c for c in e.store.get()["connections"] if c["method"] == "openai.api_key"]
    assert len(after) == 1 and after[0]["secrets"]["api_key"] == "sk-user-changed99"


def test_connection_crud(env):
    c = env.connections
    a = c.create("anthropic.api_key", "Work key", {"api_key": "sk-ant-work-0001", "bogus": "x"})
    b = c.create("anthropic.api_key", "", {"api_key": "sk-ant-home-0002"})
    assert a["label"] == "Work key" and b["label"] == "Anthropic" and a["id"] != b["id"]
    assert a["secrets"]["api_key"]["hint"] == "****0001" and "bogus" not in json.dumps(a)
    assert "sk-ant-work-0001" not in env.config.SETTINGS_FILE.read_text()
    with pytest.raises(c.MethodError):
        c.create("anthropic.api_key", "No key", {})
    with pytest.raises(c.MethodError):
        c.create("gemini.cli_login", "nope", {})

    # missing secret keeps the old one; renaming does not touch credentials
    c.update(a["id"], label="Renamed", data={})
    assert c.get(a["id"])["secrets"]["api_key"] == "sk-ant-work-0001" and c.get(a["id"])["label"] == "Renamed"
    # clearing a required secret is refused
    with pytest.raises(c.MethodError):
        c.update(a["id"], data={"api_key": ""})
    ok = c.create("openai_compatible.local", "LM Studio", {"base_url": "http://lm:1234/v1", "api_key": "lm-key-12345678"})
    c.update(ok["id"], data={"api_key": ""})  # optional secret can be cleared
    assert c.get(ok["id"])["secrets"] == {}

    env.store.update({"agent": {"connection": a["id"], "model": "claude-opus-5-5"}})
    c.delete(a["id"])
    assert env.store.get()["agent"]["connection"] == ""
    with pytest.raises(KeyError):
        c.delete(a["id"])
    with pytest.raises(ValueError):
        env.store.update({"agent": {"connection": "c_missing"}})


def test_resolve_builds_litellm_kwargs(env):
    c = env.connections
    oll = c.create("ollama.local", "", {"base_url": "http://10.0.0.5:11434/"})
    lm = c.create("openai_compatible.local", "", {"base_url": "http://lm:1234/v1"})
    xai = c.create("xai.api_key", "", {"api_key": "xai-12345678"})
    assert c.resolve(oll["id"], "qwen3").kwargs == {"model": "ollama_chat/qwen3", "api_base": "http://10.0.0.5:11434"}
    assert c.resolve(lm["id"], "local").kwargs == {"model": "openai/local", "api_key": "not-needed", "api_base": "http://lm:1234/v1"}
    assert c.resolve(xai["id"], "grok-4").kwargs == {"model": "xai/grok-4", "api_key": "xai-12345678"}

    with pytest.raises(c.MethodError, match="No AI model selected"):
        c.resolve()
    env.store.update({"agent": {"connection": oll["id"], "model": "qwen3"}})
    assert c.resolve().model == "qwen3"
    with pytest.raises(c.MethodError, match="No AI model selected"):
        c.resolve(lm["id"])  # another connection needs an explicit model
    c.update(oll["id"], enabled=False)
    with pytest.raises(c.MethodError, match="disabled"):
        c.resolve()
    with pytest.raises(c.MethodError, match="no longer exists"):
        c.resolve("c_gone", "m")
    with pytest.raises(c.MethodError, match="No enabled connection"):
        c.resolve(provider="gemini")


def test_test_saves_models_and_status(env, monkeypatch):
    from app import auth_methods

    c = env.connections
    a = c.create("anthropic.api_key", "", {"api_key": "sk-ant-good-0001"})

    async def ok(values):
        assert values["api_key"] == "sk-ant-good-0001"
        return ["claude-opus-5-5", "claude-sonnet-5-5"]

    m = auth_methods.METHODS["anthropic.api_key"]
    monkeypatch.setitem(auth_methods.METHODS, m.id, dataclasses.replace(m, list_models=ok))
    assert asyncio.run(c.test(a["id"])) == ["claude-opus-5-5", "claude-sonnet-5-5"]
    got = c.get(a["id"])
    assert got["status"]["state"] == "connected" and got["models"] == ["claude-opus-5-5", "claude-sonnet-5-5"]
    # the first working connection becomes the default
    assert env.store.get()["agent"] == {**env.store.get()["agent"], "connection": a["id"], "model": "claude-opus-5-5"}

    async def bad(values):
        raise auth_methods.MethodError("Provider returned HTTP 401 (check the API key).")

    monkeypatch.setitem(auth_methods.METHODS, m.id, dataclasses.replace(m, list_models=bad))
    with pytest.raises(auth_methods.MethodError):
        asyncio.run(c.test(a["id"]))
    assert c.get(a["id"])["status"]["state"] == "error" and "401" in c.get(a["id"])["status"]["detail"]


def test_admin_gate(env):
    from fastapi.testclient import TestClient

    from app import main

    lan = TestClient(main.app)  # client host "testclient": not loopback
    body = {"method": "anthropic.api_key", "label": "k", "values": {"api_key": "sk-ant-gate-0001"}}
    r = lan.post("/api/connections", json=body)
    assert r.status_code == 403 and "password" in r.json()["detail"]
    listing = lan.get("/api/connections").json()
    assert listing["editable"] is False and listing["connections"] == []
    assert lan.get("/api/auth-methods").json()["methods"][0]["id"] == "anthropic.api_key"
    assert lan.patch("/api/settings", json={"providers": {"openai": {"api_key": "x"}}}).status_code == 400

    local = TestClient(main.app, client=("127.0.0.1", 5000))
    created = local.post("/api/connections", json=body).json()
    assert created["secrets"]["api_key"]["hint"] == "****0001"
    assert local.get("/api/connections").json()["editable"] is True
    # a reverse proxy on the same host is not "local"
    assert local.patch(f"/api/connections/{created['id']}", json={"label": "x"}, headers={"X-Forwarded-For": "8.8.8.8"}).status_code == 403

    env.store.set_password("correct horse battery")
    lan.post("/api/auth/login", json={"password": "correct horse battery"})
    assert lan.patch(f"/api/connections/{created['id']}", json={"label": "Signed in"}).json()["label"] == "Signed in"
    # the MCP bearer token opens the API but not connection changes
    token = env.store.generate_mcp_token()
    bot = TestClient(main.app, headers={"Authorization": f"Bearer {token}"})
    assert bot.get("/api/connections").status_code == 200
    assert bot.delete(f"/api/connections/{created['id']}").status_code == 403
    assert lan.delete(f"/api/connections/{created['id']}").json() == {"ok": True}
    assert lan.delete(f"/api/connections/{created['id']}").status_code == 404


def test_agent_run_uses_connection_and_records_it(env, monkeypatch):
    import litellm

    from app import agent

    importlib.reload(agent)
    conn = env.connections.create("ollama.local", "Box", {"base_url": "http://box:11434"})
    calls = []

    async def fake_completion(**kw):
        calls.append(kw)
        raise RuntimeError("stop here")

    monkeypatch.setattr(litellm, "acompletion", fake_completion)
    events = []

    async def go():
        async for ev in agent.run("a hook", connection=conn["id"], model="qwen3"):
            events.append(ev)

    asyncio.run(go())
    run_ev = events[0]
    assert run_ev["type"] == "run" and run_ev["connection"] == conn["id"] and run_ev["billing"] == "local" and run_ev["provider"] == "ollama"
    assert calls[0]["model"] == "ollama_chat/qwen3" and calls[0]["api_base"] == "http://box:11434"
    assert events[-1]["type"] == "error"
    run = env.db.get_run(run_ev["run_id"])
    assert (run["connection_id"], run["auth_method"], run["billing"], run["provider"], run["model"]) == (conn["id"], "ollama.local", "local", "ollama", "qwen3")

    events.clear()

    async def missing():
        async for ev in agent.run("x", connection="c_gone", model="m"):
            events.append(ev)

    asyncio.run(missing())
    assert events == [{"type": "error", "message": "That AI connection no longer exists. Pick another model."}]


def test_note_resolved_alias(env):
    """Issue #27: the model an alias resolved to is kept per connection and sent to the
    browser, so the picker can show "sonnet (claude-sonnet-5-5)"."""
    c = env.connections
    a = c.create("anthropic.api_key", "Work key", {"api_key": "sk-ant-work-0001"})
    c.note_resolved(a["id"], "sonnet", "claude-sonnet-5-5")
    c.note_resolved(a["id"], "opus", "claude-opus-5-5")
    c.note_resolved(a["id"], "sonnet", "claude-sonnet-5-6")  # alias moved to a newer release
    c.note_resolved(a["id"], "claude-haiku-5-5", "claude-haiku-5-5")  # not an alias: ignored
    c.note_resolved(a["id"], "", "x")
    c.note_resolved("c_missing", "sonnet", "claude-sonnet-5-5")  # unknown connection: no error
    assert c.get(a["id"])["resolved"] == {"sonnet": "claude-sonnet-5-6", "opus": "claude-opus-5-5"}
    pub = next(x for x in c.public_list() if x["id"] == a["id"])
    assert pub["resolved"] == {"sonnet": "claude-sonnet-5-6", "opus": "claude-opus-5-5"}
    # kept when the connection is edited later
    c.update(a["id"], label="Renamed")
    assert c.get(a["id"])["resolved"]["opus"] == "claude-opus-5-5"
