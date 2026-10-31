"""SQLite persistence for designs, versions, chat history and token usage."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

from . import config

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
    cost_known INTEGER NOT NULL DEFAULT 1
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


def init() -> None:
    global _conn
    config.ensure_dirs()
    _conn = _connect()
    _conn.executescript(SCHEMA)
    _conn.commit()


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


# ---- designs ---------------------------------------------------------------
def create_design(title: str, source: str = "studio") -> str:
    did = uuid.uuid4().hex[:12]
    now = time.time()
    with tx() as c:
        c.execute(
            "INSERT INTO designs (id, title, source, created_at, updated_at) VALUES (?,?,?,?,?)",
            (did, title[:120] or "Untitled", source, now, now),
        )
    return did


def add_version(design_id: str, scad: str, summary: str, stats: dict, run_id: str | None) -> int:
    with tx() as c:
        row = c.execute("SELECT current_version FROM designs WHERE id=?", (design_id,)).fetchone()
        if row is None:
            raise KeyError(design_id)
        v = row["current_version"] + 1
        c.execute(
            "INSERT INTO versions (design_id, version, scad, summary, stats, run_id, created_at) VALUES (?,?,?,?,?,?,?)",
            (design_id, v, scad, summary, json.dumps(stats), run_id, time.time()),
        )
        c.execute("UPDATE designs SET current_version=?, updated_at=? WHERE id=?", (v, time.time(), design_id))
    return v


def rename_design(design_id: str, title: str) -> None:
    with tx() as c:
        c.execute("UPDATE designs SET title=?, updated_at=? WHERE id=?", (title[:120], time.time(), design_id))


def delete_design(design_id: str) -> None:
    with tx() as c:
        c.execute("DELETE FROM designs WHERE id=?", (design_id,))


def list_designs(limit: int = 200, query: str = "") -> list[dict[str, Any]]:
    with tx() as c:
        sql = (
            "SELECT d.*, v.summary, v.stats FROM designs d "
            "LEFT JOIN versions v ON v.design_id=d.id AND v.version=d.current_version "
        )
        args: list[Any] = []
        if query:
            sql += "WHERE d.title LIKE ? "
            args.append(f"%{query}%")
        sql += "ORDER BY d.updated_at DESC LIMIT ?"
        args.append(limit)
        rows = _rows(c.execute(sql, args))
    for r in rows:
        r["stats"] = json.loads(r["stats"] or "{}")
    return rows


def get_design(design_id: str) -> dict[str, Any] | None:
    with tx() as c:
        d = c.execute("SELECT * FROM designs WHERE id=?", (design_id,)).fetchone()
        if d is None:
            return None
        design = dict(d)
        design["versions"] = _rows(
            c.execute(
                "SELECT version, summary, stats, run_id, created_at FROM versions WHERE design_id=? ORDER BY version",
                (design_id,),
            )
        )
        design["messages"] = _rows(
            c.execute(
                "SELECT role, content, run_id, created_at FROM messages WHERE design_id=? ORDER BY id",
                (design_id,),
            )
        )
    for v in design["versions"]:
        v["stats"] = json.loads(v["stats"] or "{}")
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
def start_run(design_id: str | None, prompt: str, provider: str, model: str, source: str) -> str:
    rid = uuid.uuid4().hex[:16]
    with tx() as c:
        c.execute(
            "INSERT INTO runs (id, design_id, source, prompt, provider, model, status, started_at) VALUES (?,?,?,?,?,?,?,?)",
            (rid, design_id, source, prompt[:4000], provider, model, "running", time.time()),
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
