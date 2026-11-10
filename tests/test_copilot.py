"""GitHub Copilot plan through the Copilot SDK (issue #12), against a fake SDK client."""
import asyncio
import importlib
from types import SimpleNamespace

import pytest

TOKEN = "github_pat_11ABCDEFG0_" + "x" * 60
STATS = {"size_mm": [20.0, 10.0, 5.0], "min_mm": [0, 0, 0], "max_mm": [20, 10, 5], "triangles": 12, "watertight": True, "bodies": 1, "issues": []}


def ev(kind, **data):
    return SimpleNamespace(type=SimpleNamespace(value=kind), data=SimpleNamespace(**data), agent_id=None)


class FakeSession:
    def __init__(self, fake, kw):
        self.fake, self.kw, self.handlers, self.results = fake, kw, [], []

    def on(self, handler):
        self.handlers.append(handler)
        return lambda: None

    def emit(self, e):
        for h in self.handlers:
            h(e)

    async def send_and_wait(self, prompt, timeout=60.0):
        self.fake.prompts.append(prompt)
        tools = {t.name: t for t in self.kw["tools"]}
        for step in self.fake.script.pop(0):
            if step[0] == "tool":
                self.results.append(await tools[step[1]].handler(SimpleNamespace(arguments=step[2])))
            elif step[0] == "usage":
                self.emit(ev("assistant.usage", model=step[1].pop("model", self.kw["model"]), duration=None, **step[1]))
            elif step[0] == "message":
                self.emit(ev("assistant.message", content=step[1]))
            elif step[0] == "error":
                self.emit(ev("session.error", **step[1]))
                raise RuntimeError(step[1]["message"])

    async def abort(self):
        self.fake.aborted += 1

    async def disconnect(self):
        pass


class FakeClient:
    def __init__(self, fake):
        self.fake = fake

    async def get_auth_status(self):
        return SimpleNamespace(isAuthenticated=self.fake.authenticated, login="octocat")

    async def list_models(self):
        return [SimpleNamespace(id=i, policy=SimpleNamespace(state=s)) for i, s in self.fake.models]

    async def create_session(self, **kw):
        self.fake.session = FakeSession(self.fake, kw)
        return self.fake.session

    async def stop(self):
        self.fake.stopped += 1


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("BUILDPLATE_DATA", str(tmp_path))
    from app import config

    importlib.reload(config)
    from app import settings

    importlib.reload(settings)
    from app import auth, auth_methods, connections, copilot, db, openscad, providers

    for m in (auth_methods, connections, providers, auth, db, copilot):
        importlib.reload(m)
    db.init()
    fake = SimpleNamespace(authenticated=True, models=[("gpt-5.2", "enabled"), ("claude-sonnet-4.5", "enabled"), ("o9", "disabled")],
                           script=[], prompts=[], session=None, stopped=0, aborted=0, tokens=[])

    async def client(token, connection_id, workdir):
        fake.tokens.append(token)
        return FakeClient(fake)

    monkeypatch.setattr(copilot, "_client", client)

    async def validate(code, params=None):
        return None, STATS

    async def preview(code, params=None, views=None):
        raise openscad.RenderError("none", "")

    monkeypatch.setattr(openscad, "validate", validate)
    monkeypatch.setattr(openscad, "preview_sheet", preview)
    from app import agent

    importlib.reload(agent)
    return SimpleNamespace(store=settings.store, connections=connections, copilot=copilot, db=db, agent=agent, fake=fake, tmp=tmp_path)


def connect(env):
    cid = env.connections.create("github.copilot", "Copilot", {"token": TOKEN})["id"]
    assert asyncio.run(env.connections.test(cid)) == ["gpt-5.2", "claude-sonnet-4.5"]
    return cid


def run(env, cid, model="claude-sonnet-4.5"):
    events = []

    async def go():
        async for e in env.agent.run("a 20 mm block", connection=cid, model=model):
            events.append(e)

    asyncio.run(go())
    return events


def test_token_checks(env):
    from app.auth_methods import MethodError

    m = env.connections.auth_methods.get("github.copilot")
    assert (m.billing, m.runtime, m.requires_password) == ("subscription", "sdk:copilot", True)
    assert "copilot_requests=write" in m.fields[0].help and m.fields[0].secret
    m.validate({"token": TOKEN})
    with pytest.raises(MethodError, match="Classic tokens"):
        m.validate({"token": "ghp_abcdefghijklmnopqrstuvwxyz0123456789"})
    with pytest.raises(MethodError, match="github_pat_"):
        m.validate({"token": "sk-not-github"})


def test_connect_lists_enabled_models(env):
    from app.auth_methods import MethodError

    cid = connect(env)
    c = env.connections.get(cid)
    assert c["status"]["state"] == "connected" and env.fake.tokens == [TOKEN]
    assert env.store.get()["agent"]["connection"] == cid
    env.fake.authenticated = False
    with pytest.raises(MethodError, match="didn't accept this token"):
        asyncio.run(env.connections.test(cid))
    assert env.connections.get(cid)["status"]["state"] == "error"
    env.fake.authenticated, env.fake.models = True, [("o9", "disabled")]
    with pytest.raises(MethodError, match="no models enabled"):
        asyncio.run(env.connections.test(cid))


def test_client_is_isolated(env, monkeypatch):
    """The real client is built in empty mode, with the token and a clean environment."""
    import copilot as sdk

    seen = {}

    class Recorder:
        def __init__(self, **kw):
            seen.update(kw)

    monkeypatch.setattr(sdk, "CopilotClient", Recorder)
    monkeypatch.setenv("GH_TOKEN", "ghp_leak")
    monkeypatch.delenv("COPILOT_CLI_EXTRACT_DIR", raising=False)
    env.copilot._new_client(TOKEN, "c_1", str(env.tmp))
    assert seen["github_token"] == TOKEN and seen["use_logged_in_user"] is False and seen["mode"] == "empty"
    assert "GH_TOKEN" not in seen["env"] and seen["env"]["HOME"].startswith(str(env.tmp))
    assert seen["base_directory"].startswith(str(env.tmp / "copilot" / "c_1"))
    import os

    assert os.environ["COPILOT_CLI_EXTRACT_DIR"].startswith(str(env.tmp / "tools" / "copilot"))


def test_design_request_through_copilot(env):
    cid = connect(env)
    env.fake.script.append([
        ("tool", "check_design", {"code": "cube([20,10,5]);"}),
        ("usage", {"input_tokens": 1200, "output_tokens": 300, "cache_read_tokens": 400}),
        ("message", "Here is a 20 mm block."),
        ("tool", "submit_design", {"code": "cube([20,10,5]);", "title": "Block", "summary": "A block."}),
        ("usage", {"input_tokens": 1500, "output_tokens": 80}),
    ])
    events = run(env, cid)
    done = events[-1]
    assert done["type"] == "done" and done["title"] == "Block", done
    assert any(e["type"] == "assistant" and "20 mm block" in e["text"] for e in events)
    kw = env.fake.session.kw
    assert kw["model"] == "claude-sonnet-4.5"
    assert kw["available_tools"] == ["custom:check_design", "custom:list_parts", "custom:submit_design"]
    assert [t.name for t in kw["tools"]] == ["check_design", "list_parts", "submit_design"]
    assert [t.is_terminal for t in kw["tools"]] == [False, False, True] and all(t.skip_permission for t in kw["tools"])
    assert kw["system_message"]["mode"] == "append" and "Your only tools are" in kw["system_message"]["content"]
    assert "a 20 mm block" in env.fake.prompts[0]
    r = env.db.get_run(events[0]["run_id"])
    assert (r["billing"], r["cost_usd"], r["input_tokens"], r["output_tokens"], r["cache_read_tokens"]) == ("subscription", 0, 2700, 380, 400)
    assert len(r["steps"]) == 2 and "check_design" in str(r["steps"][0]["tool_calls"])
    assert env.fake.stopped >= 2  # the test's client and the run's client were both stopped


def test_failed_submit_keeps_going(env):
    from app import openscad

    cid = connect(env)

    async def broken(code, params=None):
        if "broken" in code:
            raise openscad.RenderError("syntax error", "")
        return None, STATS

    openscad.validate = broken
    env.fake.script.append([
        ("tool", "submit_design", {"code": "broken(", "title": "X", "summary": "x"}),
        ("tool", "submit_design", {"code": "cube(5);", "title": "Cube", "summary": "A cube."}),
    ])
    events = run(env, cid)
    assert env.fake.session.results[0].result_type == "failure" and "Not saved" in env.fake.session.results[0].text_result_for_llm
    assert events[-1]["type"] == "done" and events[-1]["title"] == "Cube"


@pytest.mark.parametrize("status,text,state,manage", [
    (429, "You have exceeded your premium request quota", None, True),
    (401, "Bad credentials", "error", False),
    (403, "Copilot is not enabled for this account", None, True),
])
def test_errors_are_explained(env, status, text, state, manage):
    cid = connect(env)
    env.fake.script.append([("error", {"error_type": "api", "message": text, "status_code": status})])
    events = run(env, cid)
    err = events[-1]
    assert err["type"] == "error"
    assert (err["manage_url"] == "https://github.com/settings/copilot/features") is manage
    if state:
        assert env.connections.get(cid)["status"]["state"] == state and "paste a new token" in err["message"]


def test_password_gate_and_usage_card(env, monkeypatch):
    from fastapi.testclient import TestClient

    from app import main

    monkeypatch.setattr(main, "store", env.store)
    local = TestClient(main.app, client=("127.0.0.1", 5000))
    r = local.post("/api/connections", json={"method": "github.copilot", "label": "Copilot", "values": {"token": TOKEN}})
    assert r.status_code == 403  # plan connections need a password session
    env.store.set_password("correct horse battery")
    client = TestClient(main.app)
    client.post("/api/auth/login", json={"password": "correct horse battery"})
    cid = client.post("/api/connections", json={"method": "github.copilot", "label": "Copilot", "values": {"token": TOKEN}}).json()["id"]
    assert client.post(f"/api/connections/{cid}/test").json()["count"] == 2
    u = client.get(f"/api/connections/{cid}/plan-usage").json()
    assert u["manage_url"].startswith("https://github.com/") and "Copilot" in u["manage_text"]
    (env.tmp / "copilot" / cid).mkdir(parents=True)
    assert client.delete(f"/api/connections/{cid}").json()["ok"]
    assert not (env.tmp / "copilot" / cid).exists()
