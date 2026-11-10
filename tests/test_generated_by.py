import asyncio
import importlib
import sqlite3

import pytest


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("BUILDPLATE_DATA", str(tmp_path))
    from app import config

    importlib.reload(config)
    from app import settings

    importlib.reload(settings)
    from app import db

    importlib.reload(db)
    db.init()
    return db


def _agent_version(db, did, provider, model, tokens=(1000, 200), cost=0.05):
    rid = db.start_run(did, "make it", provider, model, "studio")
    usage = {"input_tokens": tokens[0], "output_tokens": tokens[1], "cache_read_tokens": 0, "cache_write_tokens": 0, "cost_usd": cost, "cost_known": True}
    db.add_step(rid, 1, usage, 100, ["submit_design"])
    v = db.add_version(did, "cube(1);", "agent", {"size_mm": [1, 1, 1]}, rid)
    db.finish_run(rid, "ok", design_id=did)
    return v


def test_versions_record_who_made_them(db):
    did = db.create_design("Hook")
    _agent_version(db, did, "ollama", "qwen2.5-coder:32b")
    db.add_version(did, "cube(2);", "Edited manually.", {}, None)
    _agent_version(db, did, "anthropic", "claude-sonnet-5-5", (500, 100), 0.02)

    d = db.get_design(did)
    got = [(v["origin"], v["provider"], v["model"]) for v in d["versions"]]
    assert got == [("agent", "ollama", "qwen2.5-coder:32b"), ("manual", None, None), ("agent", "anthropic", "claude-sonnet-5-5")]
    assert d["versions"][0]["run"]["input_tokens"] == 1000 and d["versions"][0]["run"]["cost_known"] is True
    assert d["versions"][1]["run"] is None

    row = db.list_designs()[0]
    assert (row["origin"], row["provider"], row["model"]) == ("agent", "anthropic", "claude-sonnet-5-5")
    assert row["run"] == {"id": row["run"]["id"], "input_tokens": 500, "output_tokens": 100, "cost_usd": 0.02, "cost_known": True}
    assert row["models"] == [
        {"provider": "anthropic", "model": "claude-sonnet-5-5"},
        {"provider": "ollama", "model": "qwen2.5-coder:32b"},
    ]
    assert db.get_version(did)["model"] == "claude-sonnet-5-5"


def test_label_survives_pruned_runs(db):
    did = db.create_design("Clip")
    _agent_version(db, did, "openai", "gpt-5")
    with db.tx() as c:
        c.execute("DELETE FROM runs")
    row = db.list_designs()[0]
    assert (row["provider"], row["model"], row["run"]) == ("openai", "gpt-5", None)
    assert [d["title"] for d in db.list_designs(200, "model:gpt-5")] == ["Clip"]


def test_manual_and_mcp_designs(db):
    manual = db.create_design("Box", "manual")
    db.add_version(manual, "cube(3);", "Created manually.", {}, None)
    ext = db.create_design("Bracket", "mcp")
    db.add_version(ext, "cube(4);", "", {}, None, "mcp", "anthropic", "claude-opus-5-5", "Claude Desktop")
    anon = db.create_design("Knob", "mcp")
    db.add_version(anon, "cube(5);", "", {}, None, "mcp")

    rows = {r["title"]: r for r in db.list_designs()}
    assert (rows["Box"]["origin"], rows["Box"]["model"]) == ("manual", None)
    assert (rows["Bracket"]["origin"], rows["Bracket"]["model"], rows["Bracket"]["client"]) == ("mcp", "claude-opus-5-5", "Claude Desktop")
    assert (rows["Knob"]["origin"], rows["Knob"]["model"], rows["Knob"]["client"]) == ("mcp", None, None)
    # self-reported models are searchable and appear in the model filter
    assert [d["title"] for d in db.list_designs(200, "opus")] == ["Bracket"]
    assert [d["title"] for d in db.list_designs(200, "claude desktop")] == ["Bracket"]
    assert {"provider": "anthropic", "model": "claude-opus-5-5", "count": 1} in db.facets()["models"]
    with pytest.raises(ValueError):
        db.add_version(anon, "cube(1);", "", {}, None, "robot")


def test_existing_versions_are_backfilled(tmp_path, monkeypatch):
    monkeypatch.setenv("BUILDPLATE_DATA", str(tmp_path))
    from app import config

    importlib.reload(config)
    config.ensure_dirs()
    old = sqlite3.connect(config.DB_FILE)
    old.executescript(
        """
        CREATE TABLE designs (id TEXT PRIMARY KEY, title TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'studio',
            created_at REAL NOT NULL, updated_at REAL NOT NULL, current_version INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE versions (id INTEGER PRIMARY KEY AUTOINCREMENT, design_id TEXT NOT NULL REFERENCES designs(id) ON DELETE CASCADE,
            version INTEGER NOT NULL, scad TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '', stats TEXT NOT NULL DEFAULT '{}',
            run_id TEXT, created_at REAL NOT NULL, UNIQUE(design_id, version));
        CREATE TABLE runs (id TEXT PRIMARY KEY, design_id TEXT, source TEXT NOT NULL DEFAULT 'studio', prompt TEXT NOT NULL,
            provider TEXT NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL, error TEXT, started_at REAL NOT NULL, ended_at REAL,
            iterations INTEGER NOT NULL DEFAULT 0, input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
            cache_read_tokens INTEGER NOT NULL DEFAULT 0, cache_write_tokens INTEGER NOT NULL DEFAULT 0,
            cost_usd REAL NOT NULL DEFAULT 0, cost_known INTEGER NOT NULL DEFAULT 1);
        INSERT INTO designs VALUES ('s1', 'Studio part', 'studio', 1, 2, 2);
        INSERT INTO designs VALUES ('m1', 'MCP part', 'mcp', 1, 2, 1);
        INSERT INTO runs (id, design_id, prompt, provider, model, status, started_at, input_tokens) VALUES ('r1', 's1', 'p', 'gemini', 'gemini-pro', 'ok', 1, 42);
        INSERT INTO versions (design_id, version, scad, run_id, created_at) VALUES ('s1', 1, 'cube(1);', 'r1', 1);
        INSERT INTO versions (design_id, version, scad, run_id, created_at) VALUES ('s1', 2, 'cube(2);', NULL, 2);
        INSERT INTO versions (design_id, version, scad, run_id, created_at) VALUES ('m1', 1, 'cube(3);', NULL, 1);
        """
    )
    old.commit()
    old.close()

    from app import settings

    importlib.reload(settings)
    from app import db

    importlib.reload(db)
    db.init()
    s1 = db.get_design("s1")["versions"]
    assert [(v["origin"], v["provider"], v["model"]) for v in s1] == [("agent", "gemini", "gemini-pro"), ("manual", None, None)]
    assert s1[0]["run"]["input_tokens"] == 42
    assert db.get_design("m1")["versions"][0]["origin"] == "mcp"
    assert [d["id"] for d in db.list_designs(200, "model:gemini")] == ["s1"]
    db.init()  # idempotent
    assert db.get_design("s1")["versions"][0]["model"] == "gemini-pro"


def test_api_and_mcp_report_generated_by(db, monkeypatch):
    from fastapi.testclient import TestClient

    from app import main, mcp_server, openscad

    async def fake_validate(code, params=None):
        return None, {"size_mm": [10, 10, 10]}

    monkeypatch.setattr(openscad, "validate", fake_validate)
    did = db.create_design("Hook")
    _agent_version(db, did, "ollama", "qwen2.5-coder:32b")

    saved = asyncio.run(mcp_server.save_design("cube(1);", "Ext part", client="Claude Code", model="claude-opus-5-5", provider="anthropic"))
    assert asyncio.run(mcp_server.get_design(saved["design_id"]))["generated_by"] == {
        "origin": "mcp", "provider": "anthropic", "model": "claude-opus-5-5", "client": "Claude Code"}
    rows = {r["title"]: r for r in asyncio.run(mcp_server.list_designs())}
    assert rows["Hook"]["generated_by"]["model"] == "qwen2.5-coder:32b"

    # no lifespan: the fixture already initialised the db, and the MCP session manager can only start once
    client = TestClient(main.app)
    r = client.post("/api/designs", json={"code": "cube(1);", "title": "Hand made"})
    hand = r.json()["design_id"]
    listed = {d["title"]: d for d in client.get("/api/designs").json()["designs"]}
    assert listed["Hook"]["provider"] == "ollama" and listed["Hook"]["run"]["input_tokens"] == 1000
    assert listed["Hand made"]["origin"] == "manual" and listed["Hand made"]["run"] is None
    assert listed["Ext part"]["client"] == "Claude Code"
    client.post(f"/api/designs/{did}/versions", json={"code": "cube(2);"})
    versions = client.get(f"/api/designs/{did}").json()["versions"]
    assert [(v["origin"], v["model"]) for v in versions] == [("agent", "qwen2.5-coder:32b"), ("manual", None)]
    assert client.get(f"/api/designs/{hand}/versions/1").json()["origin"] == "manual"
