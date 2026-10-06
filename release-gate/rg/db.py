"""SQLite persistence: releases, check runs, per-item evidence, waivers,
approvals, generations, idempotency keys and audit trail."""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS releases (
    id              TEXT PRIMARY KEY,
    manifest_json   TEXT NOT NULL,
    source_tree     TEXT,
    created_at      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    current_run_id  INTEGER
);

CREATE TABLE IF NOT EXISTS check_runs (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    release_id         TEXT NOT NULL REFERENCES releases(id),
    kind               TEXT NOT NULL,            -- 'evaluate' | 'publish'
    aggregate_fprint   TEXT NOT NULL,
    pre_tree_checksum  TEXT,
    post_tree_checksum TEXT,
    protocol_state     TEXT NOT NULL,            -- committed/aborted/stale...
    passed             INTEGER NOT NULL,
    created_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS check_items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER NOT NULL REFERENCES check_runs(id),
    release_id    TEXT NOT NULL,
    name          TEXT NOT NULL,
    title         TEXT NOT NULL,
    status        TEXT NOT NULL,                 -- pass|fail|blocked|waived
    blocking      INTEGER NOT NULL,
    evidence      TEXT NOT NULL,                 -- JSON: per-item proof
    detail        TEXT,
    depends_fprint TEXT,                         -- dependency-fingerprint verified
    waiver_id     INTEGER,
    updated_at    TEXT NOT NULL,
    UNIQUE(run_id, name)
);

CREATE TABLE IF NOT EXISTS waivers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    release_id  TEXT NOT NULL REFERENCES releases(id),
    item        TEXT NOT NULL,
    reason      TEXT NOT NULL,
    scope_fprint TEXT NOT NULL,                  -- valid only while inputs unchanged
    expires_at  TEXT,
    approver    TEXT NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    release_id  TEXT NOT NULL REFERENCES releases(id),
    run_id      INTEGER NOT NULL,
    approver    TEXT NOT NULL,
    signed_manifest_sha TEXT NOT NULL,
    aggregate_fprint TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE(release_id, approver)
);

CREATE TABLE IF NOT EXISTS generations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    release_id      TEXT NOT NULL,
    generation      INTEGER NOT NULL,
    manifest_signed TEXT NOT NULL,
    manifest_sha    TEXT NOT NULL,
    artifact_dir    TEXT NOT NULL,
    switched_at     TEXT,
    state           TEXT NOT NULL,               -- staged|active|superseded|rolled_back
    publish_run_id  INTEGER
);

CREATE TABLE IF NOT EXISTS active (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    generation   INTEGER NOT NULL,
    release_id   TEXT NOT NULL,
    switched_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS publish_intents (
    idempotency_key TEXT PRIMARY KEY,
    release_id      TEXT NOT NULL,
    generation      INTEGER,
    outcome         TEXT NOT NULL,               -- published|duplicate|conflict|failed
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    actor      TEXT,
    action     TEXT NOT NULL,
    release_id TEXT,
    payload    TEXT
);
"""

_lock = threading.Lock()


def connect(db_path: str | Path) -> sqlite3.Connection:
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    with _lock:
        conn.executescript(SCHEMA)
