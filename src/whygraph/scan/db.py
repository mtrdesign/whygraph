"""SQLite storage for the WhyGraph evidence database."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from whygraph.scan.git import Commit
from whygraph.scan.github import Issue, PullRequest

SCHEMA_VERSION = 9

DB_DIR_NAME = ".whygraph"
DB_FILE_NAME = "whygraph.db"

_MIGRATIONS: dict[int, list[str]] = {
    1: [
        """
        CREATE TABLE schema_version (
          version INTEGER PRIMARY KEY,
          applied_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE commits (
          sha TEXT PRIMARY KEY,
          parent_shas TEXT NOT NULL,
          author_name TEXT NOT NULL,
          author_email TEXT NOT NULL,
          authored_at TEXT NOT NULL,
          committed_at TEXT NOT NULL,
          subject TEXT NOT NULL,
          body TEXT NOT NULL,
          files_changed INTEGER NOT NULL,
          insertions INTEGER NOT NULL,
          deletions INTEGER NOT NULL,
          scanned_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX idx_commits_authored_at ON commits(authored_at)",
        """
        CREATE TABLE scan_state (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )
        """,
    ],
    2: [
        """
        CREATE TABLE pull_requests (
          number INTEGER PRIMARY KEY,
          title TEXT NOT NULL,
          body TEXT,
          state TEXT NOT NULL,
          draft INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          closed_at TEXT,
          merged_at TEXT,
          merge_commit_sha TEXT,
          head_sha TEXT NOT NULL,
          head_ref TEXT,
          base_ref TEXT NOT NULL,
          author TEXT,
          html_url TEXT NOT NULL,
          labels TEXT NOT NULL,
          fetched_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX idx_pull_requests_merge_commit_sha ON pull_requests(merge_commit_sha)",
        "CREATE INDEX idx_pull_requests_state ON pull_requests(state)",
    ],
    3: [
        "ALTER TABLE pull_requests ADD COLUMN commit_titles TEXT NOT NULL DEFAULT '[]'",
    ],
    4: [
        """
        CREATE TABLE issues (
          number INTEGER PRIMARY KEY,
          title TEXT NOT NULL,
          body TEXT,
          state TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          closed_at TEXT,
          author TEXT,
          html_url TEXT NOT NULL,
          labels TEXT NOT NULL,
          fetched_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX idx_issues_state ON issues(state)",
        """
        CREATE TABLE pr_issue_links (
          pr_number INTEGER NOT NULL,
          issue_number INTEGER NOT NULL,
          link_kind TEXT NOT NULL,
          PRIMARY KEY (pr_number, issue_number, link_kind)
        )
        """,
        "CREATE INDEX idx_pr_issue_links_issue ON pr_issue_links(issue_number)",
    ],
    5: [
        "ALTER TABLE pull_requests ADD COLUMN comments TEXT NOT NULL DEFAULT '[]'",
    ],
    6: [
        "ALTER TABLE commits       ADD COLUMN subject_tfidf_score REAL NOT NULL DEFAULT 0",
        "ALTER TABLE commits       ADD COLUMN body_tfidf_score    REAL NOT NULL DEFAULT 0",
        "ALTER TABLE pull_requests ADD COLUMN title_tfidf_score   REAL NOT NULL DEFAULT 0",
        "ALTER TABLE pull_requests ADD COLUMN body_tfidf_score    REAL NOT NULL DEFAULT 0",
        "ALTER TABLE issues        ADD COLUMN title_tfidf_score   REAL NOT NULL DEFAULT 0",
        "ALTER TABLE issues        ADD COLUMN body_tfidf_score    REAL NOT NULL DEFAULT 0",
    ],
    7: [
        "ALTER TABLE commits ADD COLUMN llm_description       TEXT",
        "ALTER TABLE commits ADD COLUMN llm_description_model TEXT",
    ],
    8: [
        """
        CREATE TABLE rationale_cache (
          cache_key TEXT PRIMARY KEY,
          target_qualified_name TEXT,
          target_path TEXT,
          target_line_start INTEGER,
          target_line_end INTEGER,
          bundle_signature TEXT NOT NULL,
          model TEXT NOT NULL,
          prompt_version TEXT NOT NULL,
          purpose TEXT NOT NULL,
          why TEXT NOT NULL,
          constraints TEXT NOT NULL,
          tradeoffs TEXT NOT NULL,
          risks TEXT NOT NULL,
          confidence REAL NOT NULL,
          created_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX idx_rationale_cache_qname ON rationale_cache(target_qualified_name)",
    ],
    9: [
        """
        CREATE TABLE authors (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          primary_login TEXT,
          primary_name TEXT,
          primary_email TEXT,
          emails TEXT NOT NULL,
          logins TEXT NOT NULL,
          names TEXT NOT NULL,
          first_seen TEXT,
          last_seen TEXT,
          commit_count INTEGER NOT NULL DEFAULT 0,
          pr_count INTEGER NOT NULL DEFAULT 0,
          issue_count INTEGER NOT NULL DEFAULT 0
        )
        """,
        "CREATE INDEX idx_authors_primary_login ON authors(primary_login)",
        "CREATE INDEX idx_authors_primary_email ON authors(primary_email)",
    ],
}


def default_db_path(repo_root: Path) -> Path:
    return repo_root / DB_DIR_NAME / DB_FILE_NAME


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _row_to_dict(cur: sqlite3.Cursor, row: tuple) -> dict:
    return dict(zip([d[0] for d in cur.description], row, strict=True))


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _migrate(self) -> None:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
        )
        current = 0
        if cur.fetchone() is not None:
            cur.execute("SELECT MAX(version) FROM schema_version")
            row = cur.fetchone()
            current = row[0] if row and row[0] is not None else 0
        for v in sorted(_MIGRATIONS):
            if v > current:
                for stmt in _MIGRATIONS[v]:
                    cur.execute(stmt)
                cur.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (v, _now_iso()),
                )
        self._conn.commit()

    def upsert_commit(self, commit: Commit) -> bool:
        cur = self._conn.cursor()
        cur.execute("SELECT 1 FROM commits WHERE sha = ?", (commit.sha,))
        if cur.fetchone() is not None:
            return False
        cur.execute(
            """
            INSERT INTO commits (
              sha, parent_shas, author_name, author_email,
              authored_at, committed_at, subject, body,
              files_changed, insertions, deletions, scanned_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                commit.sha,
                json.dumps(commit.parent_shas),
                commit.author_name,
                commit.author_email,
                commit.authored_at,
                commit.committed_at,
                commit.subject,
                commit.body,
                commit.files_changed,
                commit.insertions,
                commit.deletions,
                _now_iso(),
            ),
        )
        self._conn.commit()
        return True

    def commit_count(self) -> int:
        cur = self._conn.cursor()
        cur.execute("SELECT COUNT(*) FROM commits")
        return int(cur.fetchone()[0])

    def commit_exists(self, sha: str) -> bool:
        cur = self._conn.cursor()
        cur.execute("SELECT 1 FROM commits WHERE sha = ?", (sha,))
        return cur.fetchone() is not None

    def set_scan_state(self, key: str, value: str) -> None:
        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO scan_state (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE
              SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, value, _now_iso()),
        )
        self._conn.commit()

    def get_scan_state(self, key: str) -> str | None:
        cur = self._conn.cursor()
        cur.execute("SELECT value FROM scan_state WHERE key = ?", (key,))
        row = cur.fetchone()
        return row[0] if row else None

    def upsert_pull_request(self, pr: PullRequest) -> bool:
        cur = self._conn.cursor()
        cur.execute("SELECT 1 FROM pull_requests WHERE number = ?", (pr.number,))
        existing = cur.fetchone() is not None
        now = _now_iso()
        if existing:
            cur.execute(
                """
                UPDATE pull_requests SET
                  title = ?, body = ?, state = ?, draft = ?,
                  updated_at = ?, closed_at = ?, merged_at = ?,
                  merge_commit_sha = ?, head_sha = ?, head_ref = ?,
                  base_ref = ?, author = ?, html_url = ?,
                  labels = ?, commit_titles = ?, comments = ?,
                  fetched_at = ?
                WHERE number = ?
                """,
                (
                    pr.title,
                    pr.body,
                    pr.state,
                    1 if pr.draft else 0,
                    pr.updated_at,
                    pr.closed_at,
                    pr.merged_at,
                    pr.merge_commit_sha,
                    pr.head_sha,
                    pr.head_ref,
                    pr.base_ref,
                    pr.author,
                    pr.html_url,
                    json.dumps(pr.labels),
                    json.dumps(pr.commit_titles),
                    json.dumps(pr.comments),
                    now,
                    pr.number,
                ),
            )
            self._conn.commit()
            return False
        cur.execute(
            """
            INSERT INTO pull_requests (
              number, title, body, state, draft,
              created_at, updated_at, closed_at, merged_at,
              merge_commit_sha, head_sha, head_ref, base_ref,
              author, html_url, labels, commit_titles, comments,
              fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                pr.number,
                pr.title,
                pr.body,
                pr.state,
                1 if pr.draft else 0,
                pr.created_at,
                pr.updated_at,
                pr.closed_at,
                pr.merged_at,
                pr.merge_commit_sha,
                pr.head_sha,
                pr.head_ref,
                pr.base_ref,
                pr.author,
                pr.html_url,
                json.dumps(pr.labels),
                json.dumps(pr.commit_titles),
                json.dumps(pr.comments),
                now,
            ),
        )
        self._conn.commit()
        return True

    def pull_request_count(self) -> int:
        cur = self._conn.cursor()
        cur.execute("SELECT COUNT(*) FROM pull_requests")
        return int(cur.fetchone()[0])

    def upsert_issue(self, issue: Issue) -> bool:
        cur = self._conn.cursor()
        cur.execute("SELECT 1 FROM issues WHERE number = ?", (issue.number,))
        existing = cur.fetchone() is not None
        now = _now_iso()
        if existing:
            cur.execute(
                """
                UPDATE issues SET
                  title = ?, body = ?, state = ?,
                  updated_at = ?, closed_at = ?,
                  author = ?, html_url = ?,
                  labels = ?, fetched_at = ?
                WHERE number = ?
                """,
                (
                    issue.title,
                    issue.body,
                    issue.state,
                    issue.updated_at,
                    issue.closed_at,
                    issue.author,
                    issue.html_url,
                    json.dumps(issue.labels),
                    now,
                    issue.number,
                ),
            )
            self._conn.commit()
            return False
        cur.execute(
            """
            INSERT INTO issues (
              number, title, body, state,
              created_at, updated_at, closed_at,
              author, html_url, labels, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                issue.number,
                issue.title,
                issue.body,
                issue.state,
                issue.created_at,
                issue.updated_at,
                issue.closed_at,
                issue.author,
                issue.html_url,
                json.dumps(issue.labels),
                now,
            ),
        )
        self._conn.commit()
        return True

    def issue_count(self) -> int:
        cur = self._conn.cursor()
        cur.execute("SELECT COUNT(*) FROM issues")
        return int(cur.fetchone()[0])

    def get_commit(self, sha: str) -> dict | None:
        """Return a single commit row as a dict (or None if not found)."""
        cur = self._conn.cursor()
        cur.execute("SELECT * FROM commits WHERE sha = ?", (sha,))
        row = cur.fetchone()
        if row is None:
            return None
        return _row_to_dict(cur, row)

    def get_pull_request(self, number: int) -> dict | None:
        cur = self._conn.cursor()
        cur.execute("SELECT * FROM pull_requests WHERE number = ?", (number,))
        row = cur.fetchone()
        if row is None:
            return None
        return _row_to_dict(cur, row)

    def get_issue(self, number: int) -> dict | None:
        cur = self._conn.cursor()
        cur.execute("SELECT * FROM issues WHERE number = ?", (number,))
        row = cur.fetchone()
        if row is None:
            return None
        return _row_to_dict(cur, row)

    def commits_without_llm_description(self, shas: list[str]) -> set[str]:
        """Return the subset of `shas` whose `llm_description IS NULL`."""
        if not shas:
            return set()
        cur = self._conn.cursor()
        placeholders = ",".join(["?"] * len(shas))
        cur.execute(
            f"SELECT sha FROM commits "
            f"WHERE sha IN ({placeholders}) AND llm_description IS NULL",
            shas,
        )
        return {row[0] for row in cur.fetchall()}

    def set_llm_description(
        self, sha: str, description: str, model: str
    ) -> None:
        cur = self._conn.cursor()
        cur.execute(
            """
            UPDATE commits
               SET llm_description       = ?,
                   llm_description_model = ?
             WHERE sha = ?
            """,
            (description, model, sha),
        )
        self._conn.commit()

    def get_rationale_cache(self, cache_key: str) -> dict | None:
        """Return a cached rationale row by cache_key, or None on miss.

        ``constraints``, ``tradeoffs``, ``risks`` are decoded from JSON
        back into lists so callers can use the row directly.
        """
        cur = self._conn.cursor()
        cur.execute(
            "SELECT * FROM rationale_cache WHERE cache_key = ?", (cache_key,)
        )
        row = cur.fetchone()
        if row is None:
            return None
        out = _row_to_dict(cur, row)
        for key in ("constraints", "tradeoffs", "risks"):
            try:
                out[key] = json.loads(out[key])
            except (TypeError, json.JSONDecodeError):
                out[key] = []
        return out

    def get_rationale_cache_by_qname(self, qualified_name: str) -> dict | None:
        """Return the most recent cached rationale for a qualified_name.

        When multiple cache rows exist for the same symbol (across model /
        prompt-version churn), this picks the latest by ``created_at``.
        Used by the renderer to surface "the rationale we have" without
        recomputing the cache_key.
        """
        cur = self._conn.cursor()
        cur.execute(
            "SELECT * FROM rationale_cache "
            "WHERE target_qualified_name = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (qualified_name,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        out = _row_to_dict(cur, row)
        for key in ("constraints", "tradeoffs", "risks"):
            try:
                out[key] = json.loads(out[key])
            except (TypeError, json.JSONDecodeError):
                out[key] = []
        return out

    def set_rationale_cache(
        self,
        *,
        cache_key: str,
        target_qualified_name: str | None,
        target_path: str | None,
        target_line_start: int | None,
        target_line_end: int | None,
        bundle_signature: str,
        model: str,
        prompt_version: str,
        purpose: str,
        why: str,
        constraints: list[str],
        tradeoffs: list[str],
        risks: list[str],
        confidence: float,
    ) -> None:
        """Upsert a cached rationale by ``cache_key``."""
        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO rationale_cache (
              cache_key, target_qualified_name, target_path,
              target_line_start, target_line_end,
              bundle_signature, model, prompt_version,
              purpose, why, constraints, tradeoffs, risks,
              confidence, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
              target_qualified_name = excluded.target_qualified_name,
              target_path           = excluded.target_path,
              target_line_start     = excluded.target_line_start,
              target_line_end       = excluded.target_line_end,
              bundle_signature      = excluded.bundle_signature,
              model                 = excluded.model,
              prompt_version        = excluded.prompt_version,
              purpose               = excluded.purpose,
              why                   = excluded.why,
              constraints           = excluded.constraints,
              tradeoffs             = excluded.tradeoffs,
              risks                 = excluded.risks,
              confidence            = excluded.confidence,
              created_at            = excluded.created_at
            """,
            (
                cache_key,
                target_qualified_name,
                target_path,
                target_line_start,
                target_line_end,
                bundle_signature,
                model,
                prompt_version,
                purpose,
                why,
                json.dumps(constraints),
                json.dumps(tradeoffs),
                json.dumps(risks),
                confidence,
                _now_iso(),
            ),
        )
        self._conn.commit()

    def set_pr_closing_issues(
        self, pr_number: int, issue_numbers: list[int]
    ) -> None:
        """Replace the 'closes' links for a PR with the given issue numbers."""
        cur = self._conn.cursor()
        cur.execute(
            "DELETE FROM pr_issue_links WHERE pr_number = ? AND link_kind = 'closes'",
            (pr_number,),
        )
        for n in issue_numbers:
            cur.execute(
                """
                INSERT OR IGNORE INTO pr_issue_links
                  (pr_number, issue_number, link_kind)
                VALUES (?, ?, 'closes')
                """,
                (pr_number, n),
            )
        self._conn.commit()

    def clear_authors(self) -> None:
        """Wipe the authors table. Used at the start of a rebuild."""
        cur = self._conn.cursor()
        cur.execute("DELETE FROM authors")
        # Reset AUTOINCREMENT so ids stay small across rebuilds.
        cur.execute("DELETE FROM sqlite_sequence WHERE name = 'authors'")
        self._conn.commit()

    def insert_author(
        self,
        *,
        primary_login: str | None,
        primary_name: str | None,
        primary_email: str | None,
        emails: list[str],
        logins: list[str],
        names: list[str],
        first_seen: str | None,
        last_seen: str | None,
        commit_count: int,
        pr_count: int,
        issue_count: int,
    ) -> int:
        """Insert one author row. Returns the new ``id``."""
        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO authors (
              primary_login, primary_name, primary_email,
              emails, logins, names,
              first_seen, last_seen,
              commit_count, pr_count, issue_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                primary_login,
                primary_name,
                primary_email,
                json.dumps(sorted(set(emails))),
                json.dumps(sorted(set(logins))),
                json.dumps(sorted(set(names))),
                first_seen,
                last_seen,
                commit_count,
                pr_count,
                issue_count,
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def iter_authors(self) -> list[dict]:
        """Return all authors with ``emails`` / ``logins`` / ``names``
        decoded back into Python lists."""
        cur = self._conn.cursor()
        cur.execute("SELECT * FROM authors ORDER BY commit_count DESC, id ASC")
        out: list[dict] = []
        for row in cur.fetchall():
            d = _row_to_dict(cur, row)
            for key in ("emails", "logins", "names"):
                try:
                    d[key] = json.loads(d[key])
                except (TypeError, json.JSONDecodeError):
                    d[key] = []
            out.append(d)
        return out

    def author_count(self) -> int:
        cur = self._conn.cursor()
        cur.execute("SELECT COUNT(*) FROM authors")
        return int(cur.fetchone()[0])
