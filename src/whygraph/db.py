from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_SQL = """\
-- WhyGraph schema v1
--
-- Sits alongside CodeGraph's DB. Symbols are joined by node_id (CodeGraph's
-- nodes.id), with qualified_name kept as a fallback for cases where node IDs
-- shift across re-indexes.

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at INTEGER NOT NULL
);

-- Append-only raw signals. Idempotent on (node_id, source, ref).
CREATE TABLE IF NOT EXISTS evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    source TEXT NOT NULL,
    ref TEXT,
    payload TEXT NOT NULL,
    collected_at INTEGER NOT NULL,
    UNIQUE(node_id, source, ref)
);
CREATE INDEX IF NOT EXISTS idx_evidence_node ON evidence(node_id);
CREATE INDEX IF NOT EXISTS idx_evidence_qname ON evidence(qualified_name);

-- One row per node summarising the current evidence bundle.
-- bundle_hash is part of the rationale cache key.
-- head_at_collection is the file's HEAD sha when evidence was last collected;
-- the EvidenceService uses it (alongside built_at + a TTL) to decide freshness.
CREATE TABLE IF NOT EXISTS evidence_bundles (
    node_id TEXT PRIMARY KEY,
    bundle_hash TEXT NOT NULL,
    built_at INTEGER NOT NULL,
    head_at_collection TEXT
);

-- Cached rationale. Lookup matches only when (node_id, bundle_hash,
-- prompt_version, model) all agree, which makes cache hits content-addressable
-- and survives a CodeGraph re-index that shifts node_id values.
-- confidence is computed deterministically by score_confidence() in
-- rationale.py and capped at 0.85 until refactor-lineage detection lands.
-- Pre-existing rows can have NULL here; the read path backfills them.
CREATE TABLE IF NOT EXISTS rationale (
    node_id TEXT PRIMARY KEY,
    bundle_hash TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    model TEXT NOT NULL,
    purpose TEXT,
    why TEXT,
    constraints TEXT,
    tradeoffs TEXT,
    risks TEXT,
    confidence REAL,
    generated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at INTEGER NOT NULL,
    finished_at INTEGER,
    symbols_seen INTEGER,
    symbols_with_evidence INTEGER,
    errors TEXT
);

-- Per-commit file-list cache shared across symbols. Commits are immutable, so
-- a row never needs invalidation. Populated on demand by CoChangeService;
-- presence in commit_cache_meta is the "is this sha cached" marker (commits
-- with zero files would otherwise look uncached forever).
CREATE TABLE IF NOT EXISTS commit_files (
    commit_sha TEXT NOT NULL,
    file_path TEXT NOT NULL,
    PRIMARY KEY (commit_sha, file_path)
);
CREATE INDEX IF NOT EXISTS idx_commit_files_path ON commit_files(file_path);

CREATE TABLE IF NOT EXISTS commit_cache_meta (
    commit_sha TEXT PRIMARY KEY,
    cached_at INTEGER NOT NULL
);

INSERT OR IGNORE INTO schema_version (version, applied_at)
VALUES (1, strftime('%s', 'now') * 1000);
"""


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate(conn: sqlite3.Connection) -> None:
    if "head_at_collection" not in _column_names(conn, "evidence_bundles"):
        conn.execute("ALTER TABLE evidence_bundles ADD COLUMN head_at_collection TEXT")


def open_whygraph_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA_SQL)
    _migrate(conn)
    return conn
