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
    source TEXT NOT NULL,        -- git_commit | git_blame | pr | issue | docstring | test_ref
    ref TEXT,                    -- commit sha, PR number, etc; NULL for unkeyed entries
    payload TEXT NOT NULL,       -- JSON
    collected_at INTEGER NOT NULL,
    UNIQUE(node_id, source, ref)
);
CREATE INDEX IF NOT EXISTS idx_evidence_node ON evidence(node_id);
CREATE INDEX IF NOT EXISTS idx_evidence_qname ON evidence(qualified_name);

-- One row per node summarising the current evidence bundle.
-- bundle_hash is the cache key for rationale.
CREATE TABLE IF NOT EXISTS evidence_bundles (
    node_id TEXT PRIMARY KEY,
    bundle_hash TEXT NOT NULL,
    built_at INTEGER NOT NULL
);

-- Cached rationale. Regenerated when bundle_hash, prompt_version, or model changes.
CREATE TABLE IF NOT EXISTS rationale (
    node_id TEXT PRIMARY KEY,
    bundle_hash TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    model TEXT NOT NULL,
    purpose TEXT,
    why TEXT,
    constraints TEXT,            -- JSON array
    tradeoffs TEXT,              -- JSON array
    risks TEXT,                  -- JSON array
    confidence REAL,             -- 0..1, deterministic
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

INSERT OR IGNORE INTO schema_version (version, applied_at)
VALUES (1, strftime('%s', 'now') * 1000);
