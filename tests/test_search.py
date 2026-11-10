import importlib
import sqlite3
import time

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


def _design(db, title, summary="", size=(10, 10, 10), printer="Bambu Lab P1S 0.4 nozzle", source="studio",
            provider=None, model=None, prompt="", tags=()):
    did = db.create_design(title, source)
    rid = None
    if provider:
        rid = db.start_run(did, prompt or title, provider, model, source)
    db.add_version(did, "cube(1);", summary, {"size_mm": list(size), "printer": printer}, rid)
    if rid:
        db.finish_run(rid, "ok", design_id=did)
    if tags:
        db.set_tags(did, list(tags))
    return did


@pytest.fixture()
def lib(db):
    ids = {
        "hook": _design(db, "Garage wall hook", "A sturdy hook for hanging tools.", (40, 20, 60),
                        provider="ollama", model="qwen2.5-coder:14b", prompt="hook for my garage wall", tags=["Garage", "wall mount"]),
        "clip": _design(db, "Cable clip", "Holds a 6 mm cable under a desk.", (18, 12, 8),
                        provider="anthropic", model="claude-sonnet-5-5", prompt="desk cable clip", tags=["desk", "cable management"]),
        "box": _design(db, "Parts box", "Box with a sliding lid.", (120, 80, 45), printer="Prusa MK4S 0.4 nozzle",
                       source="manual", tags=["storage"]),
        "bracket": _design(db, "Shelf bracket", "Right angle bracket for a 20 mm shelf.", (90, 30, 90), source="mcp",
                           tags=["garage"]),
    }
    return ids


def _titles(db, q="", **kw):
    from app import search

    sort = kw.pop("sort", "title")
    return [d["title"] for d in db.list_designs(200, q, search.filters(**kw) if kw else None, sort)]


def test_free_text_matches_every_field(db, lib):
    assert _titles(db, "bracket") == ["Shelf bracket"]  # title
    assert _titles(db, "sliding lid") == ["Parts box"]  # summary
    assert _titles(db, "storage") == ["Parts box"]  # tag
    assert _titles(db, "mount") == ["Garage wall hook"]  # multi-word tag
    assert _titles(db, "desk") == ["Cable clip"]  # prompt, summary and tag
    assert _titles(db, "ollama") == ["Garage wall hook"]  # provider
    assert _titles(db, "sonnet") == ["Cable clip"]  # model
    assert _titles(db, "prusa") == ["Parts box"]  # printer
    assert _titles(db, "mcp") == ["Shelf bracket"]  # source


def test_prefix_phrase_and_and(db, lib):
    assert _titles(db, "brack") == ["Shelf bracket"]
    assert _titles(db, "garage") == ["Garage wall hook", "Shelf bracket"]
    assert _titles(db, "garage hook") == ["Garage wall hook"]
    assert _titles(db, '"sliding lid"') == ["Parts box"]
    assert _titles(db, '"lid sliding"') == []
    assert _titles(db, "nothing-matches-this") == []
    # punctuation and FTS syntax never break the query
    assert _titles(db, 'AND OR NOT ( * "') == []
    assert len(_titles(db, "")) == 4


def test_relevance_ranks_title_first(db, lib):
    other = _design(db, "Hanger", "Works with any garage bracket rail.")
    rows = db.list_designs(200, "bracket")
    assert [r["title"] for r in rows] == ["Shelf bracket", "Hanger"]
    assert other in [r["id"] for r in rows]


def test_qualifiers(db, lib):
    assert _titles(db, "tag:garage") == ["Garage wall hook", "Shelf bracket"]
    assert _titles(db, "tag:GARAGE tag:\"wall mount\"") == ["Garage wall hook"]
    assert _titles(db, "tag:gar") == []  # tags match exactly
    assert _titles(db, "provider:ollama") == ["Garage wall hook"]
    assert _titles(db, "model:qwen") == ["Garage wall hook"]
    assert _titles(db, "model:sonnet-5") == ["Cable clip"]
    assert _titles(db, "source:manual") == ["Parts box"]
    assert _titles(db, "source:mcp source:manual") == ["Parts box", "Shelf bracket"]
    assert _titles(db, "printer:mk4") == ["Parts box"]


def test_size_filters(db, lib):
    assert _titles(db, "size:<50") == ["Cable clip"]
    assert _titles(db, "size:50") == ["Cable clip"]  # bare number = at most
    assert _titles(db, "size:>=90") == ["Parts box", "Shelf bracket"]
    assert _titles(db, "size:50..100") == ["Garage wall hook", "Shelf bracket"]
    assert _titles(db, "w:<50") == ["Cable clip", "Garage wall hook"]
    assert _titles(db, "h:>50 d:<=30") == ["Garage wall hook", "Shelf bracket"]
    assert _titles(db, min_size=50, max_size=100) == ["Garage wall hook", "Shelf bracket"]
    assert [d["title"] for d in db.list_designs(200, "", sort="largest")][:2] == ["Parts box", "Shelf bracket"]
    assert [d["title"] for d in db.list_designs(200, "", sort="smallest")][0] == "Cable clip"


def test_date_filters(db, lib):
    with db.tx() as c:
        c.execute("UPDATE designs SET created_at=? WHERE id=?", (time.mktime((2026, 9, 15, 12, 0, 0, 0, 0, -1)), lib["box"]))
    assert "Parts box" not in _titles(db, "after:2026-10-01")
    assert _titles(db, "before:2026-10-01") == ["Parts box"]
    assert _titles(db, "before:2026-09") == []
    assert _titles(db, before="2026-09") == []
    assert _titles(db, after="2026-09-15", before="2026-09-16") == ["Parts box"]


def test_combined_filters(db, lib):
    assert _titles(db, "garage size:<70") == ["Garage wall hook"]
    assert _titles(db, "tag:garage source:mcp") == ["Shelf bracket"]
    assert _titles(db, "hook", tags="garage", provider="ollama", max_size=100) == ["Garage wall hook"]
    assert _titles(db, "hook", provider="anthropic") == []
    # API params and typed qualifiers combine
    assert _titles(db, "tag:garage", source="studio") == ["Garage wall hook"]


def test_invalid_filters_raise(db, lib):
    from app import search

    for q in ("size:big", "source:web", "after:yesterday"):
        with pytest.raises(ValueError):
            db.list_designs(200, q)
    with pytest.raises(ValueError):
        db.list_designs(200, "", sort="random")
    # unknown keys are just text
    assert search.parse("m3:bolt").words == ["m3", "bolt"]


def test_tags_crud_reindexes(db, lib):
    did = lib["clip"]
    assert db.get_tags(did) == ["cable management", "desk"]
    assert db.add_tags(did, ["  Under   Desk ", "desk", "a,b"]) == ["a b", "cable management", "desk", "under desk"]
    assert _titles(db, "tag:\"under desk\"") == ["Cable clip"]
    assert db.remove_tag(did, "DESK") == ["a b", "cable management", "under desk"]
    assert _titles(db, "tag:desk") == []
    db.set_tags(did, ["office"])
    assert _titles(db, "office") == ["Cable clip"]
    assert _titles(db, "management") == []
    assert db.get_design(did)["tags"] == ["office"]
    with pytest.raises(KeyError):
        db.set_tags("missing", ["x"])
    with pytest.raises(ValueError):
        db.set_tags(did, [f"t{i}" for i in range(db.MAX_TAGS + 1)])


def test_rename_new_version_and_delete_reindex(db, lib):
    did = lib["bracket"]
    db.rename_design(did, "Floating shelf support")
    assert _titles(db, "floating") == ["Floating shelf support"]
    db.add_version(did, "cube(2);", "Now with a hidden screw slot.", {"size_mm": [90, 30, 90]}, None)
    assert _titles(db, "screw") == ["Floating shelf support"]
    db.delete_design(did)
    assert _titles(db, "floating") == []
    with db.tx() as c:
        assert c.execute("SELECT COUNT(*) FROM design_fts WHERE design_id=?", (did,)).fetchone()[0] == 0


def test_list_rows_include_tags_and_models(db, lib):
    row = next(d for d in db.list_designs() if d["id"] == lib["hook"])
    assert row["tags"] == ["garage", "wall mount"]
    assert row["models"] == [{"provider": "ollama", "model": "qwen2.5-coder:14b"}]
    assert row["stats"]["size_mm"] == [40, 20, 60]


def test_facets(db, lib):
    f = db.facets()
    assert {"tag": "garage", "count": 2} in f["tags"]
    assert {"provider": "ollama", "model": "qwen2.5-coder:14b", "count": 1} in f["models"]
    assert {s["source"]: s["count"] for s in f["sources"]} == {"studio": 2, "manual": 1, "mcp": 1}
    assert {"printer": "Prusa MK4S 0.4 nozzle", "count": 1} in f["printers"]


def test_existing_database_is_migrated_and_indexed(tmp_path, monkeypatch):
    """A database created before tags and search existed keeps its data and is indexed on start."""
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
        INSERT INTO designs VALUES ('old1', 'Legacy knob', 'studio', 1, 2, 1);
        INSERT INTO runs (id, design_id, prompt, provider, model, status, started_at) VALUES ('r1', 'old1', 'a knurled knob', 'gemini', 'gemini-pro', 'ok', 1);
        INSERT INTO versions (design_id, version, scad, summary, stats, run_id, created_at)
            VALUES ('old1', 1, 'cylinder(10);', 'Knurled grip knob.', '{"size_mm": [30, 30, 15]}', 'r1', 1);
        """
    )
    old.commit()
    old.close()

    from app import settings

    importlib.reload(settings)
    from app import db

    importlib.reload(db)
    db.init()
    assert [d["id"] for d in db.list_designs(200, "knurled")] == ["old1"]
    assert [d["id"] for d in db.list_designs(200, "gemini size:<40")] == ["old1"]
    assert db.get_version("old1")["scad"] == "cylinder(10);"
    db.set_tags("old1", ["kitchen"])
    # a second start does not rebuild or lose anything
    db.init()
    assert [d["id"] for d in db.list_designs(200, "tag:kitchen knob")] == ["old1"]


def test_api_and_mcp(db, lib):
    import asyncio

    from fastapi.testclient import TestClient

    from app import main, mcp_server

    # no lifespan: the fixture already initialised the db, and the MCP session manager can only start once
    client = TestClient(main.app)
    r = client.get("/api/designs", params={"q": "garage", "source": "studio"})
    assert [d["title"] for d in r.json()["designs"]] == ["Garage wall hook"]
    r = client.get("/api/designs", params={"min_size": 100})
    assert [d["title"] for d in r.json()["designs"]] == ["Parts box"]
    assert client.get("/api/designs", params={"q": "size:huge"}).status_code == 400
    assert client.get("/api/designs/facets").json()["tags"][0] == {"tag": "garage", "count": 2}
    did = lib["box"]
    assert client.post(f"/api/designs/{did}/tags", json={"tags": ["Workshop"]}).json()["tags"] == ["storage", "workshop"]
    assert client.put(f"/api/designs/{did}/tags", json={"tags": ["bins", "shop"]}).json()["tags"] == ["bins", "shop"]
    assert client.delete(f"/api/designs/{did}/tags/shop").json()["tags"] == ["bins"]
    assert client.get(f"/api/designs/{did}/tags").json()["tags"] == ["bins"]
    assert client.get(f"/api/designs/{did}").json()["tags"] == ["bins"]
    assert client.put("/api/designs/missing/tags", json={"tags": ["x"]}).status_code == 404

    rows = asyncio.run(mcp_server.list_designs(query="tag:garage", model="qwen"))
    assert [r["title"] for r in rows] == ["Garage wall hook"]
    assert rows[0]["tags"] == ["garage", "wall mount"] and rows[0]["size_mm"] == [40, 20, 60]
    assert asyncio.run(mcp_server.list_designs(query="size:nope")) == {"error": "Invalid size filter: 'nope'"}
    assert asyncio.run(mcp_server.set_design_tags(lib["clip"], ["Desk", "office"]))["tags"] == ["desk", "office"]
    assert "error" in asyncio.run(mcp_server.set_design_tags("missing", ["x"]))
