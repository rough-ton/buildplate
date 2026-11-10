"""SQLite persistence for designs, versions, tags, chat history and token usage."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

from . import config, search

_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS designs (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'studio',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    current_version INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    design_id TEXT NOT NULL REFERENCES designs(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    scad TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    stats TEXT NOT NULL DEFAULT '{}',
    run_id TEXT,
    created_at REAL NOT NULL,
    origin TEXT,
    provider TEXT,
    model TEXT,
    client TEXT,
    UNIQUE(design_id, version)
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    design_id TEXT NOT NULL REFERENCES designs(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    run_id TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    design_id TEXT,
    source TEXT NOT NULL DEFAULT 'studio',
    prompt TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    iterations INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    cost_known INTEGER NOT NULL DEFAULT 1,
    connection_id TEXT,
    auth_method TEXT,
    billing TEXT
);
CREATE TABLE IF NOT EXISTS run_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    step INTEGER NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    tool_calls TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS design_tags (
    design_id TEXT NOT NULL REFERENCES designs(id) ON DELETE CASCADE,
    tag TEXT NOT NULL,
    PRIMARY KEY (design_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_design_tags_tag ON design_tags(tag);
CREATE VIRTUAL TABLE IF NOT EXISTS design_fts USING fts5(
    design_id UNINDEXED, title, tags, summary, prompt, provider, model, printer, source,
    tokenize = 'unicode61 remove_diacritics 2', prefix = '2 3'
);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_versions_design ON versions(design_id, version);
CREATE INDEX IF NOT EXISTS idx_messages_design ON messages(design_id, id);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_FILE, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


_conn: sqlite3.Connection | None = None


# Columns added after the first release: table -> {column: type}.
_ADDED_COLUMNS = {
    "runs": {"connection_id": "TEXT", "auth_method": "TEXT", "billing": "TEXT"},
}

# Bump when the search index layout or contents change; existing databases are
# reindexed on the next start.
INDEX_VERSION = 2

# Where a version came from: the built-in design agent, a manual save in Studio or
# the API, or an external MCP client calling save_design.
ORIGINS = ("agent", "manual", "mcp")

# Columns added to versions after the first release, with their types.
_VERSION_COLUMNS = {"origin": "TEXT", "provider": "TEXT", "model": "TEXT", "client": "TEXT"}


def _migrate(c: sqlite3.Connection) -> None:
    """Add columns introduced after the first release and backfill them. Idempotent."""
    have = {r["name"] for r in c.execute("PRAGMA table_info(versions)")}
    for col, typ in _VERSION_COLUMNS.items():
        if col not in have:
            c.execute(f"ALTER TABLE versions ADD COLUMN {col} {typ}")
    # Versions saved before the generating model was recorded on the version:
    # copy it from the run. Without a run, an MCP design's versions are assumed to
    # come from the MCP client and everything else from a manual save.
    c.execute(
        "UPDATE versions SET origin='agent', "
        "provider=(SELECT provider FROM runs WHERE runs.id=versions.run_id), "
        "model=(SELECT model FROM runs WHERE runs.id=versions.run_id) "
        "WHERE origin IS NULL AND run_id IN (SELECT id FROM runs)"
    )
    c.execute(
        "UPDATE versions SET origin=CASE "
        "WHEN run_id IS NOT NULL THEN 'agent' "
        "WHEN (SELECT source FROM designs WHERE designs.id=versions.design_id)='mcp' THEN 'mcp' "
        "ELSE 'manual' END WHERE origin IS NULL"
    )

# bm25 weights, in design_fts column order
_FTS_WEIGHTS = "0, 10.0, 6.0, 3.0, 2.0, 1.0, 1.0, 1.0, 1.0"


def init() -> None:
    global _conn
    config.ensure_dirs()
    _conn = _connect()
    _conn.executescript(SCHEMA)
    for table, cols in _ADDED_COLUMNS.items():
        have = {r["name"] for r in _conn.execute(f"PRAGMA table_info({table})")}
        for col, typ in cols.items():
            if col not in have:
                _conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    _conn.commit()
    with tx() as c:
        _migrate(c)
        if c.execute("PRAGMA user_version").fetchone()[0] < INDEX_VERSION:
            c.execute("DELETE FROM design_fts")
            ids = [r[0] for r in c.execute("SELECT id FROM designs")]
            c.execute(f"PRAGMA user_version = {INDEX_VERSION}")
        else:
            ids = [r[0] for r in c.execute("SELECT id FROM designs WHERE id NOT IN (SELECT design_id FROM design_fts)")]
        for did in ids:
            _reindex(c, did)


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    assert _conn is not None, "db.init() not called"
    with _lock:
        try:
            yield _conn
            _conn.commit()
        except Exception:
            _conn.rollback()
            raise


def _rows(cur: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [dict(r) for r in cur.fetchall()]


# ---- search index ----------------------------------------------------------
def _reindex(c: sqlite3.Connection, design_id: str) -> None:
    """Rebuild one design's full-text row from its title, tags, every version's summary,
    printer, provider, model and MCP client, and the prompts of the runs that produced versions."""
    c.execute("DELETE FROM design_fts WHERE design_id=?", (design_id,))
    d = c.execute("SELECT title, source FROM designs WHERE id=?", (design_id,)).fetchone()
    if d is None:
        return
    versions = c.execute(
        "SELECT summary, json_extract(stats, '$.printer') AS printer, provider, model, client FROM versions "
        "WHERE design_id=? ORDER BY version DESC",
        (design_id,),
    ).fetchall()
    runs = c.execute(
        "SELECT prompt FROM runs WHERE id IN "
        "(SELECT run_id FROM versions WHERE design_id=? AND run_id IS NOT NULL) ORDER BY started_at",
        (design_id,),
    ).fetchall()
    tags = [r[0] for r in c.execute("SELECT tag FROM design_tags WHERE design_id=? ORDER BY tag", (design_id,))]

    def join(values) -> str:
        return "\n".join(dict.fromkeys(v for v in values if v))

    c.execute(
        "INSERT INTO design_fts (design_id, title, tags, summary, prompt, provider, model, printer, source) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            design_id,
            d["title"],
            join(tags),
            join(v["summary"] for v in versions),
            join(r["prompt"] for r in runs),
            join(x for v in versions for x in (v["provider"], v["client"])),
            join(v["model"] for v in versions),
            join(v["printer"] for v in versions),
            d["source"],
        ),
    )


# ---- designs ---------------------------------------------------------------
def create_design(title: str, source: str = "studio") -> str:
    did = uuid.uuid4().hex[:12]
    now = time.time()
    with tx() as c:
        c.execute(
            "INSERT INTO designs (id, title, source, created_at, updated_at) VALUES (?,?,?,?,?)",
            (did, title[:120] or "Untitled", source, now, now),
        )
        _reindex(c, did)
    return did


def add_version(
    design_id: str,
    scad: str,
    summary: str,
    stats: dict,
    run_id: str | None,
    origin: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    client: str | None = None,
) -> int:
    """Store a new version. With a run_id the version is the agent's and its provider
    and model are copied from the run; otherwise origin is "manual" unless given
    ("mcp" with an optional self-reported client, provider and model)."""
    origin = origin or ("agent" if run_id else "manual")
    if origin not in ORIGINS:
        raise ValueError(f"Unknown origin {origin!r}")
    with tx() as c:
        row = c.execute("SELECT current_version FROM designs WHERE id=?", (design_id,)).fetchone()
        if row is None:
            raise KeyError(design_id)
        if run_id and not (provider or model):
            run = c.execute("SELECT provider, model FROM runs WHERE id=?", (run_id,)).fetchone()
            if run:
                provider, model = run["provider"], run["model"]
        v = row["current_version"] + 1
        c.execute(
            "INSERT INTO versions (design_id, version, scad, summary, stats, run_id, created_at, origin, provider, model, client) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                design_id, v, scad, summary, json.dumps(stats), run_id, time.time(), origin,
                (provider or "").strip()[:80] or None, (model or "").strip()[:120] or None, (client or "").strip()[:80] or None,
            ),
        )
        c.execute("UPDATE designs SET current_version=?, updated_at=? WHERE id=?", (v, time.time(), design_id))
        _reindex(c, design_id)
    return v


def rename_design(design_id: str, title: str) -> None:
    with tx() as c:
        c.execute("UPDATE designs SET title=?, updated_at=? WHERE id=?", (title[:120] or "Untitled", time.time(), design_id))
        _reindex(c, design_id)


def delete_design(design_id: str) -> None:
    with tx() as c:
        c.execute("DELETE FROM designs WHERE id=?", (design_id,))
        c.execute("DELETE FROM design_fts WHERE design_id=?", (design_id,))


_SIZE_AXIS = {
    0: "CAST(json_extract(v.stats, '$.size_mm[0]') AS REAL)",
    1: "CAST(json_extract(v.stats, '$.size_mm[1]') AS REAL)",
    2: "CAST(json_extract(v.stats, '$.size_mm[2]') AS REAL)",
}
_SIZE_AXIS[None] = f"max({_SIZE_AXIS[0]}, {_SIZE_AXIS[1]}, {_SIZE_AXIS[2]})"

SORTS = {
    "updated": "d.updated_at DESC",
    "created": "d.created_at DESC",
    "title": "d.title COLLATE NOCASE ASC, d.updated_at DESC",
    "largest": f"{_SIZE_AXIS[None]} DESC NULLS LAST, d.updated_at DESC",
    "smallest": f"{_SIZE_AXIS[None]} ASC NULLS LAST, d.updated_at DESC",
}


def _like(value: str) -> str:
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def list_designs(
    limit: int = 200, query: str = "", filters: search.Search | None = None, sort: str = ""
) -> list[dict[str, Any]]:
    """List designs matching a search string (see app.search) plus optional extra filters.
    sort is one of SORTS or "relevance"; the default is relevance when there is free text,
    otherwise most recently updated."""
    spec = search.parse(query)
    if filters:
        for f in ("words", "phrases", "tags", "providers", "models", "sources", "printers", "sizes"):
            getattr(spec, f).extend(x for x in getattr(filters, f) if x not in getattr(spec, f))
        spec.after = filters.after if filters.after is not None else spec.after
        spec.before = filters.before if filters.before is not None else spec.before
    sort = sort or ("relevance" if spec.has_text else "updated")
    if sort not in SORTS and sort != "relevance":
        raise ValueError(f"Unknown sort {sort!r}, use one of: relevance, {', '.join(SORTS)}")

    where: list[str] = []
    args: list[Any] = []
    sql = (
        "SELECT d.*, v.summary, v.stats, v.origin, v.provider, v.model, v.client, "
        "r.id AS run_id, r.input_tokens, r.output_tokens, r.cost_usd, r.cost_known, "
        "(SELECT json_group_array(tag) FROM (SELECT tag FROM design_tags WHERE design_id=d.id ORDER BY tag)) AS tags, "
        "(SELECT json_group_array(json_object('provider', provider, 'model', model)) FROM "
        " (SELECT DISTINCT provider, model FROM versions WHERE design_id=d.id AND model IS NOT NULL "
        "  ORDER BY provider, model)) AS models "
        "FROM designs d LEFT JOIN versions v ON v.design_id=d.id AND v.version=d.current_version "
        "LEFT JOIN runs r ON r.id=v.run_id "
    )
    if spec.has_text:
        sql += "JOIN design_fts f ON f.design_id=d.id "
        where.append("design_fts MATCH ?")
        args.append(spec.fts_query())
    for tag in spec.tags:
        where.append("EXISTS (SELECT 1 FROM design_tags t WHERE t.design_id=d.id AND t.tag=?)")
        args.append(tag)
    for col, values in (("provider", spec.providers), ("model", spec.models)):
        for val in values:
            where.append(
                "EXISTS (SELECT 1 FROM versions rv "
                f"WHERE rv.design_id=d.id AND lower(rv.{col}) LIKE ? ESCAPE '\\')"
            )
            args.append(_like(val))
    if spec.sources:
        where.append(f"d.source IN ({','.join('?' * len(spec.sources))})")
        args += spec.sources
    for val in spec.printers:
        where.append("lower(json_extract(v.stats, '$.printer')) LIKE ? ESCAPE '\\'")
        args.append(_like(val))
    for sf in spec.sizes:
        where.append(f"{_SIZE_AXIS[sf.axis]} {sf.op} ?")
        args.append(sf.value)
    if spec.after is not None:
        where.append("d.created_at >= ?")
        args.append(spec.after)
    if spec.before is not None:
        where.append("d.created_at < ?")
        args.append(spec.before)
    if where:
        sql += "WHERE " + " AND ".join(where) + " "
    if sort == "relevance" and spec.has_text:
        sql += f"ORDER BY bm25(design_fts, {_FTS_WEIGHTS}), d.updated_at DESC "
    else:
        sql += f"ORDER BY {SORTS.get(sort, SORTS['updated'])} "
    sql += "LIMIT ?"
    args.append(limit)
    with tx() as c:
        rows = _rows(c.execute(sql, args))
    for r in rows:
        r["stats"] = json.loads(r["stats"] or "{}")
        r["tags"] = json.loads(r["tags"] or "[]")
        r["models"] = json.loads(r["models"] or "[]")
        _attach_run(r)
    return rows


def _attach_run(row: dict[str, Any]) -> None:
    """Fold the generating run's usage columns into a nested "run" object (None when
    the version was not produced by a Buildplate run)."""
    keys = ("run_id", "input_tokens", "output_tokens", "cost_usd", "cost_known")
    vals = {k: row.pop(k, None) for k in keys}
    row["run"] = (
        None
        if vals["run_id"] is None
        else {
            "id": vals["run_id"],
            "input_tokens": vals["input_tokens"],
            "output_tokens": vals["output_tokens"],
            "cost_usd": vals["cost_usd"],
            "cost_known": bool(vals["cost_known"]),
        }
    )


def facets() -> dict[str, Any]:
    """Values present in the library, with design counts, for building filter controls."""
    with tx() as c:
        tags = _rows(c.execute("SELECT tag, COUNT(*) AS count FROM design_tags GROUP BY tag ORDER BY count DESC, tag"))
        models = _rows(
            c.execute(
                "SELECT COALESCE(provider, '') AS provider, model, COUNT(DISTINCT design_id) AS count FROM versions "
                "WHERE model IS NOT NULL GROUP BY 1, 2 ORDER BY 1, count DESC, model"
            )
        )
        sources = _rows(c.execute("SELECT source, COUNT(*) AS count FROM designs GROUP BY source ORDER BY count DESC"))
        printers = _rows(
            c.execute(
                "SELECT json_extract(v.stats, '$.printer') AS printer, COUNT(*) AS count FROM designs d "
                "JOIN versions v ON v.design_id=d.id AND v.version=d.current_version "
                "WHERE printer IS NOT NULL GROUP BY printer ORDER BY count DESC, printer"
            )
        )
    return {"tags": tags, "models": models, "sources": sources, "printers": printers, "sorts": ["relevance", *SORTS]}


# ---- tags --------------------------------------------------------------------
MAX_TAGS = 30


def get_tags(design_id: str) -> list[str]:
    with tx() as c:
        return [r[0] for r in c.execute("SELECT tag FROM design_tags WHERE design_id=? ORDER BY tag", (design_id,))]


def set_tags(design_id: str, tags: list[str]) -> list[str]:
    """Replace a design's tags. Tags are normalised (see search.normalize_tag) and deduplicated."""
    clean = list(dict.fromkeys(t for t in (search.normalize_tag(x) for x in tags) if t))
    if len(clean) > MAX_TAGS:
        raise ValueError(f"A design can have at most {MAX_TAGS} tags")
    with tx() as c:
        if c.execute("SELECT 1 FROM designs WHERE id=?", (design_id,)).fetchone() is None:
            raise KeyError(design_id)
        c.execute("DELETE FROM design_tags WHERE design_id=?", (design_id,))
        c.executemany("INSERT INTO design_tags (design_id, tag) VALUES (?,?)", [(design_id, t) for t in clean])
        _reindex(c, design_id)
    return sorted(clean)


def add_tags(design_id: str, tags: list[str]) -> list[str]:
    return set_tags(design_id, get_tags(design_id) + list(tags))


def remove_tag(design_id: str, tag: str) -> list[str]:
    t = search.normalize_tag(tag)
    return set_tags(design_id, [x for x in get_tags(design_id) if x != t])


def get_design(design_id: str) -> dict[str, Any] | None:
    with tx() as c:
        d = c.execute("SELECT * FROM designs WHERE id=?", (design_id,)).fetchone()
        if d is None:
            return None
        design = dict(d)
        design["versions"] = _rows(
            c.execute(
                "SELECT v.version, v.summary, v.stats, v.created_at, v.origin, v.provider, v.model, v.client, "
                "r.id AS run_id, r.input_tokens, r.output_tokens, r.cost_usd, r.cost_known "
                "FROM versions v LEFT JOIN runs r ON r.id=v.run_id WHERE v.design_id=? ORDER BY v.version",
                (design_id,),
            )
        )
        design["messages"] = _rows(
            c.execute(
                "SELECT role, content, run_id, created_at FROM messages WHERE design_id=? ORDER BY id",
                (design_id,),
            )
        )
        design["tags"] = [r[0] for r in c.execute("SELECT tag FROM design_tags WHERE design_id=? ORDER BY tag", (design_id,))]
    for v in design["versions"]:
        v["stats"] = json.loads(v["stats"] or "{}")
        _attach_run(v)
    return design


def get_version(design_id: str, version: int | None = None) -> dict[str, Any] | None:
    with tx() as c:
        if version is None:
            row = c.execute(
                "SELECT v.* FROM versions v JOIN designs d ON d.id=v.design_id "
                "WHERE v.design_id=? AND v.version=d.current_version",
                (design_id,),
            ).fetchone()
        else:
            row = c.execute(
                "SELECT * FROM versions WHERE design_id=? AND version=?", (design_id, version)
            ).fetchone()
    if row is None:
        return None
    out = dict(row)
    out["stats"] = json.loads(out["stats"] or "{}")
    return out


def add_message(design_id: str, role: str, content: str, run_id: str | None = None) -> None:
    with tx() as c:
        c.execute(
            "INSERT INTO messages (design_id, role, content, run_id, created_at) VALUES (?,?,?,?,?)",
            (design_id, role, content, run_id, time.time()),
        )


# ---- usage -----------------------------------------------------------------
def start_run(
    design_id: str | None,
    prompt: str,
    provider: str,
    model: str,
    source: str,
    connection_id: str | None = None,
    auth_method: str | None = None,
    billing: str | None = None,
) -> str:
    """Record a design request. provider is the provider family (pricing and reports
    key on it); billing is api, subscription, cloud or local."""
    rid = uuid.uuid4().hex[:16]
    with tx() as c:
        c.execute(
            "INSERT INTO runs (id, design_id, source, prompt, provider, model, status, started_at, connection_id, auth_method, billing) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (rid, design_id, source, prompt[:4000], provider, model, "running", time.time(), connection_id, auth_method, billing),
        )
    return rid


def add_step(run_id: str, step: int, usage: dict[str, Any], duration_ms: int, tool_calls: list[str]) -> None:
    with tx() as c:
        c.execute(
            "INSERT INTO run_steps (run_id, step, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, cost_usd, duration_ms, tool_calls) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                step,
                usage["input_tokens"],
                usage["output_tokens"],
                usage["cache_read_tokens"],
                usage["cache_write_tokens"],
                usage["cost_usd"],
                duration_ms,
                json.dumps(tool_calls),
            ),
        )
        c.execute(
            "UPDATE runs SET iterations=?, input_tokens=input_tokens+?, output_tokens=output_tokens+?, "
            "cache_read_tokens=cache_read_tokens+?, cache_write_tokens=cache_write_tokens+?, cost_usd=cost_usd+?, "
            "cost_known=MIN(cost_known, ?) WHERE id=?",
            (
                step,
                usage["input_tokens"],
                usage["output_tokens"],
                usage["cache_read_tokens"],
                usage["cache_write_tokens"],
                usage["cost_usd"],
                1 if usage["cost_known"] else 0,
                run_id,
            ),
        )


def finish_run(run_id: str, status: str, error: str | None = None, design_id: str | None = None) -> None:
    with tx() as c:
        c.execute(
            "UPDATE runs SET status=?, error=?, ended_at=?, design_id=COALESCE(?, design_id) WHERE id=?",
            (status, error, time.time(), design_id, run_id),
        )


def get_run(run_id: str) -> dict[str, Any] | None:
    with tx() as c:
        r = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if r is None:
            return None
        run = dict(r)
        run["steps"] = _rows(c.execute("SELECT * FROM run_steps WHERE run_id=? ORDER BY step", (run_id,)))
    for s in run["steps"]:
        s["tool_calls"] = json.loads(s["tool_calls"])
    return run


def list_runs(limit: int = 50, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
    with tx() as c:
        rows = _rows(
            c.execute(
                "SELECT r.*, d.title AS design_title FROM runs r LEFT JOIN designs d ON d.id=r.design_id "
                "ORDER BY r.started_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )
        )
        total = c.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    return rows, total


def usage_summary(days: int = 30) -> dict[str, Any]:
    since = time.time() - days * 86400
    with tx() as c:
        tot = dict(
            c.execute(
                "SELECT COUNT(*) AS runs, COALESCE(SUM(input_tokens),0) AS input_tokens, "
                "COALESCE(SUM(output_tokens),0) AS output_tokens, COALESCE(SUM(cache_read_tokens),0) AS cache_read_tokens, "
                "COALESCE(SUM(cost_usd),0) AS cost_usd, COALESCE(AVG(input_tokens+output_tokens),0) AS avg_tokens "
                "FROM runs WHERE started_at>=?",
                (since,),
            ).fetchone()
        )
        daily = _rows(
            c.execute(
                "SELECT date(started_at,'unixepoch','localtime') AS day, SUM(input_tokens) AS input_tokens, "
                "SUM(output_tokens) AS output_tokens, SUM(cost_usd) AS cost_usd, COUNT(*) AS runs "
                "FROM runs WHERE started_at>=? GROUP BY day ORDER BY day",
                (since,),
            )
        )
        by_model = _rows(
            c.execute(
                "SELECT provider, model, COUNT(*) AS runs, SUM(input_tokens) AS input_tokens, "
                "SUM(output_tokens) AS output_tokens, SUM(cost_usd) AS cost_usd "
                "FROM runs WHERE started_at>=? GROUP BY provider, model ORDER BY cost_usd DESC, runs DESC",
                (since,),
            )
        )
    return {"days": days, "totals": tot, "daily": daily, "by_model": by_model}


def all_runs_for_export() -> list[dict[str, Any]]:
    with tx() as c:
        return _rows(c.execute("SELECT * FROM runs ORDER BY started_at DESC"))
