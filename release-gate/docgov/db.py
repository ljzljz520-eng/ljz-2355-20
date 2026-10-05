"""SQLite persistence with long-transaction support.

Connections default to ``BEGIN`` (never autocommit DDL) and SERIALIZABLE-ish
semantics via WAL + a single writer.  A process-wide advisory lock (``gate``)
table serialises mutating release operations, while long read transactions use
``IMMEDIATE`` to take the writer lock and freeze a consistent snapshot across
evaluate/prepare — the heart of the "validate and precheck in one long
transaction" requirement.
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS releases (
  id TEXT PRIMARY KEY,
  release_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  manifest TEXT NOT NULL,
  mf TEXT NOT NULL,
  state TEXT NOT NULL,
  baseline_mf TEXT,
  seal TEXT,
  approve_sig TEXT,
  approve_by TEXT,
  staged_gen TEXT,
  published_gen TEXT,
  supersedes TEXT,
  idem_token TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(release_id, version)
);

CREATE TABLE IF NOT EXISTS check_rows (
  release_pk TEXT NOT NULL,
  code TEXT NOT NULL,
  status TEXT NOT NULL,           -- PASS | BLOCKED
  phase TEXT NOT NULL,            -- EVALUATE | PREPARE
  waivable INTEGER NOT NULL,
  waiver_id TEXT,
  evidence TEXT NOT NULL,
  evidence_hash TEXT NOT NULL,
  inputs TEXT NOT NULL,           -- JSON: input name -> fingerprint
  row_fp TEXT NOT NULL,
  evaluated_at TEXT NOT NULL,
  PRIMARY KEY (release_pk, code, phase)
);

CREATE TABLE IF NOT EXISTS waivers (
  id TEXT PRIMARY KEY,
  release_pk TEXT NOT NULL,
  code TEXT NOT NULL,
  reason TEXT NOT NULL,
  binding TEXT NOT NULL,          -- JSON: input name -> fingerprint at grant
  binding_hash TEXT NOT NULL,
  sig TEXT NOT NULL,
  granted_by TEXT NOT NULL,
  granted_at TEXT NOT NULL,
  revoked INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS generations (
  gen TEXT PRIMARY KEY,
  release_pk TEXT NOT NULL,
  mf TEXT NOT NULL,
  root TEXT,
  state TEXT NOT NULL,            -- STAGED | ACTIVE | RETIRED
  manifest_path TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS active (
  singleton INTEGER PRIMARY KEY DEFAULT 1,
  gen TEXT NOT NULL,
  mf TEXT NOT NULL,
  baseline_mf TEXT,
  switched_at TEXT NOT NULL,
  CHECK (singleton = 1)
);

CREATE TABLE IF NOT EXISTS feedback (
  id TEXT PRIMARY KEY,
  page_path TEXT NOT NULL,
  severity TEXT NOT NULL,         -- BLOCKER | MAJOR | MINOR
  status TEXT NOT NULL,           -- OPEN | RESOLVED
  summary TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS asset_registry (
  path TEXT PRIMARY KEY,
  licence TEXT NOT NULL,          -- LICENSED | REVOKED
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  actor TEXT,
  kind TEXT NOT NULL,
  release_pk TEXT,
  detail TEXT
);

CREATE TABLE IF NOT EXISTS idem_requests (
  token TEXT PRIMARY KEY,
  release_pk TEXT,
  result TEXT NOT NULL,
  at TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.init()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def init(self) -> None:
        with self._lock, self.connect() as conn:
            # executescript manages its own transaction
            conn.executescript(SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """Short transaction."""
        with self._lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield conn
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    @contextmanager
    def long_tx(self) -> Iterator[sqlite3.Connection]:
        """Long IMMEDIATE transaction: single consistent frozen snapshot.

        The writer lock is held for the duration, so concurrent evaluators /
        preparers queue instead of mixing green lights from different times.
        """
        with self._lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            ok = False
            try:
                yield conn
                ok = True
            finally:
                conn.execute("COMMIT" if ok else "ROLLBACK")
