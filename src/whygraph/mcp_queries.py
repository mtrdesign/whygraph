"""Composite read queries for the MCP surface.

These joins are MCP-shaped (combine commits + PRs + issues with author lists,
text search ranking, velocity rollups) — kept out of `Database` so that class
stays scan-write-focused.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from whygraph.scan import db as db_module
from whygraph.scan import git as git_module
from whygraph.scan.scoring import ValueGate


def blame_line_range(
    repo_root: Path, path: str, line_start: int, line_end: int
) -> dict[str, dict]:
    """Return ``{sha: {lines_owned, author_name, author_email, summary, committed_at}}``.

    Parses ``git blame --porcelain`` output. Each block-leading header
    repeats the SHA on the first line and emits metadata (`author`,
    `author-mail`, `committer-time`, `summary`, …) on the following lines.
    Repeat-block headers (subsequent lines from the same commit) only have
    `<sha> <orig> <final>`. We accumulate per-SHA metadata once and bump
    ``lines_owned`` for every header occurrence.
    """
    out = git_module._run_git(
        repo_root,
        ["blame", f"-L{line_start},{line_end}", "--porcelain", "--", path],
    )
    entries: dict[str, dict] = {}
    current_sha: str | None = None
    for line in out.splitlines():
        if line.startswith("\t"):
            # Source-line content; not metadata.
            continue
        parts = line.split(" ", 3)
        if parts and len(parts[0]) == 40 and len(parts) >= 3:
            try:
                int(parts[1])
                int(parts[2])
            except ValueError:
                pass
            else:
                sha = parts[0]
                entry = entries.setdefault(
                    sha,
                    {
                        "lines_owned": 0,
                        "author_name": None,
                        "author_email": None,
                        "summary": None,
                        "committed_at": None,
                    },
                )
                entry["lines_owned"] += 1
                current_sha = sha
                continue
        if current_sha is None:
            continue
        entry = entries[current_sha]
        if line.startswith("author "):
            entry["author_name"] = line[len("author "):].strip() or None
        elif line.startswith("author-mail "):
            mail = line[len("author-mail "):].strip()
            if mail.startswith("<") and mail.endswith(">"):
                mail = mail[1:-1]
            entry["author_email"] = mail or None
        elif line.startswith("summary "):
            entry["summary"] = line[len("summary "):].strip() or None
        elif line.startswith("committer-time "):
            try:
                ts = int(line[len("committer-time "):].strip())
                entry["committed_at"] = datetime.fromtimestamp(
                    ts, tz=timezone.utc
                ).isoformat()
            except ValueError:
                pass
    return entries

_NARRATIVE_PRIORITY_COMMIT: tuple[str, ...] = ("llm_description", "body", "subject")
_NARRATIVE_PRIORITY_PR_ISSUE: tuple[str, ...] = ("body", "title")


def _row_to_dict(cur: sqlite3.Cursor, row: tuple) -> dict:
    return dict(zip([d[0] for d in cur.description], row, strict=True))


def commit_narrative(commit: dict, gate: ValueGate) -> tuple[str | None, str | None]:
    """Pick the narrative field for a commit, applying the harshness rule.

    Returns ``(text, source)`` where source is one of ``llm_description``,
    ``body``, ``subject``, or both ``None`` if the commit fails the gate.
    `llm_description` always wins when present — it describes the diff, not
    the human-written message.
    """
    llm = commit.get("llm_description")
    if llm:
        return llm, "llm_description"
    body = commit.get("body") or ""
    if body.strip() and gate.is_above(
        "commits", "body", float(commit.get("body_tfidf_score") or 0.0)
    ):
        return body, "body"
    subject = commit.get("subject") or ""
    if subject.strip() and gate.is_above(
        "commits", "subject", float(commit.get("subject_tfidf_score") or 0.0)
    ):
        return subject, "subject"
    return None, None


def pr_narrative(pr: dict, gate: ValueGate) -> tuple[str | None, str | None]:
    body = pr.get("body") or ""
    if body.strip() and gate.is_above(
        "pull_requests", "body", float(pr.get("body_tfidf_score") or 0.0)
    ):
        return body, "body"
    title = pr.get("title") or ""
    if title.strip() and gate.is_above(
        "pull_requests", "title", float(pr.get("title_tfidf_score") or 0.0)
    ):
        return title, "title"
    return None, None


def issue_narrative(issue: dict, gate: ValueGate) -> tuple[str | None, str | None]:
    body = issue.get("body") or ""
    if body.strip() and gate.is_above(
        "issues", "body", float(issue.get("body_tfidf_score") or 0.0)
    ):
        return body, "body"
    title = issue.get("title") or ""
    if title.strip() and gate.is_above(
        "issues", "title", float(issue.get("title_tfidf_score") or 0.0)
    ):
        return title, "title"
    return None, None


def commit_narratives(commit: dict, gate: ValueGate) -> dict[str, str]:
    """Return all qualifying commit narratives keyed by source.

    ``llm_description`` always passes when present — it's a mechanical
    diff summary with no human bias, so the gate doesn't apply. ``body``
    and ``subject`` clear the harshness gate independently and may
    appear alongside ``llm_description``.

    Empty dict means no qualifying narrative; the evidence item still
    surfaces (blame `lines_owned` is itself signal).
    """
    out: dict[str, str] = {}
    llm = commit.get("llm_description")
    if llm:
        out["llm_description"] = llm
    body = (commit.get("body") or "").strip()
    if body and gate.is_above(
        "commits", "body", float(commit.get("body_tfidf_score") or 0.0)
    ):
        out["body"] = body
    subject = (commit.get("subject") or "").strip()
    if subject and gate.is_above(
        "commits", "subject", float(commit.get("subject_tfidf_score") or 0.0)
    ):
        out["subject"] = subject
    return out


def pr_narratives(pr: dict, gate: ValueGate) -> dict[str, str]:
    """Return PR title + body if each clears the gate."""
    out: dict[str, str] = {}
    body = (pr.get("body") or "").strip()
    if body and gate.is_above(
        "pull_requests", "body", float(pr.get("body_tfidf_score") or 0.0)
    ):
        out["body"] = body
    title = (pr.get("title") or "").strip()
    if title and gate.is_above(
        "pull_requests", "title", float(pr.get("title_tfidf_score") or 0.0)
    ):
        out["title"] = title
    return out


def issue_narratives(issue: dict, gate: ValueGate) -> dict[str, str]:
    """Return issue title + body if each clears the gate."""
    out: dict[str, str] = {}
    body = (issue.get("body") or "").strip()
    if body and gate.is_above(
        "issues", "body", float(issue.get("body_tfidf_score") or 0.0)
    ):
        out["body"] = body
    title = (issue.get("title") or "").strip()
    if title and gate.is_above(
        "issues", "title", float(issue.get("title_tfidf_score") or 0.0)
    ):
        out["title"] = title
    return out


def prs_containing_commit(db: db_module.Database, sha: str) -> list[dict]:
    """Return PR rows structurally linked to ``sha``.

    A PR contains a commit if:
      - ``merge_commit_sha`` matches, OR
      - ``head_sha`` matches, OR
      - any ``commit_titles[*].oid`` (full SHA) matches.

    The commit_titles JSON oid match is an exact-string compare on the
    serialized JSON (cheap, avoids per-row JSON parse for every PR).
    """
    cur = db._conn.cursor()
    like = f'%"oid": "{sha}"%'
    cur.execute(
        """
        SELECT * FROM pull_requests
         WHERE merge_commit_sha = ?
            OR head_sha = ?
            OR commit_titles LIKE ?
         ORDER BY number ASC
        """,
        (sha, sha, like),
    )
    return [_row_to_dict(cur, row) for row in cur.fetchall()]


def closing_issues_for_pr(db: db_module.Database, pr_number: int) -> list[dict]:
    """Return issue rows linked to ``pr_number`` via pr_issue_links (closes)."""
    cur = db._conn.cursor()
    cur.execute(
        """
        SELECT i.* FROM issues i
          JOIN pr_issue_links l ON l.issue_number = i.number
         WHERE l.pr_number = ? AND l.link_kind = 'closes'
         ORDER BY i.number ASC
        """,
        (pr_number,),
    )
    return [_row_to_dict(cur, row) for row in cur.fetchall()]


def pr_authors(pr: dict) -> list[dict]:
    """Authors associated with a PR row: PR opener + all commit authors.

    Dedupe rule: if a login is shared between the PR opener and a commit
    entry, the commit entry wins because it carries name+email too.
    """
    out: list[dict] = []
    seen_logins: set[str] = set()
    seen_email_name: set[tuple[str | None, str | None]] = set()

    raw = pr.get("commit_titles") or "[]"
    try:
        entries = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        entries = []
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            login = entry.get("author_login")
            name = entry.get("author_name")
            email = entry.get("author_email")
            if login:
                if login in seen_logins:
                    continue
                seen_logins.add(login)
            else:
                key = (name, email)
                if key == (None, None) or key in seen_email_name:
                    continue
                seen_email_name.add(key)
            out.append({"login": login, "name": name, "email": email})

    pr_login = pr.get("author")
    if pr_login and pr_login not in seen_logins:
        seen_logins.add(pr_login)
        out.append({"login": pr_login, "name": None, "email": None})

    return out


def search_text(
    db: db_module.Database,
    query: str,
    *,
    kinds: tuple[str, ...] = ("commit", "pr", "issue"),
    gate: ValueGate,
    limit: int = 20,
) -> list[dict]:
    """LIKE-match query across selected kinds, ranked by TF-IDF.

    Commits with non-NULL ``llm_description`` always pass the gate (treated
    as the highest-confidence narrative). The matched field's TF-IDF score
    is the rank key.
    """
    if not query.strip():
        return []
    like = f"%{query}%"
    out: list[dict] = []
    cur = db._conn.cursor()

    if "commit" in kinds:
        cur.execute(
            """
            SELECT sha, subject, body, llm_description,
                   subject_tfidf_score, body_tfidf_score,
                   author_name, author_email, committed_at
              FROM commits
             WHERE subject LIKE ? OR body LIKE ? OR llm_description LIKE ?
            """,
            (like, like, like),
        )
        for row in cur.fetchall():
            d = _row_to_dict(cur, row)
            narrative, source = commit_narrative(d, gate)
            if narrative is None:
                continue
            score = float(
                d.get(f"{source}_tfidf_score") or 0.0
            ) if source != "llm_description" else 1.0
            out.append(
                {
                    "kind": "commit",
                    "id": d["sha"],
                    "narrative": narrative,
                    "narrative_source": source,
                    "score": score,
                    "authors": [
                        {
                            "login": None,
                            "name": d.get("author_name"),
                            "email": d.get("author_email"),
                        }
                    ],
                    "committed_at": d.get("committed_at"),
                }
            )

    if "pr" in kinds:
        cur.execute(
            """
            SELECT * FROM pull_requests
             WHERE title LIKE ? OR body LIKE ?
            """,
            (like, like),
        )
        for row in cur.fetchall():
            d = _row_to_dict(cur, row)
            narrative, source = pr_narrative(d, gate)
            if narrative is None:
                continue
            score = float(d.get(f"{source}_tfidf_score") or 0.0)
            out.append(
                {
                    "kind": "pr",
                    "id": d["number"],
                    "narrative": narrative,
                    "narrative_source": source,
                    "score": score,
                    "authors": pr_authors(d),
                    "html_url": d.get("html_url"),
                }
            )

    if "issue" in kinds:
        cur.execute(
            """
            SELECT * FROM issues
             WHERE title LIKE ? OR body LIKE ?
            """,
            (like, like),
        )
        for row in cur.fetchall():
            d = _row_to_dict(cur, row)
            narrative, source = issue_narrative(d, gate)
            if narrative is None:
                continue
            score = float(d.get(f"{source}_tfidf_score") or 0.0)
            out.append(
                {
                    "kind": "issue",
                    "id": d["number"],
                    "narrative": narrative,
                    "narrative_source": source,
                    "score": score,
                    "authors": [
                        {"login": d.get("author"), "name": None, "email": None}
                    ]
                    if d.get("author")
                    else [],
                    "html_url": d.get("html_url"),
                }
            )

    out.sort(key=lambda h: h["score"], reverse=True)
    return out[:limit]


def velocity_by_author(
    db: db_module.Database,
    *,
    window_days: int,
    top_n: int,
    now: datetime | None = None,
) -> list[dict]:
    """Per-author commit velocity (window + all-time + last-commit recency).

    PRs authored in window are joined heuristically: the PR's `author`
    column holds a GitHub login; commits don't carry login. We approximate
    by counting PRs authored by each `author_email`'s local-part match
    against PR `author` — imperfect but the only deterministic key. Callers
    needing exact join should consume `commit_titles[].author_login`.
    """
    cur = db._conn.cursor()
    if now is None:
        now = datetime.now(tz=timezone.utc)
    cutoff = (now - timedelta(days=window_days)).isoformat()

    cur.execute(
        """
        SELECT author_email, author_name,
               COUNT(*) AS all_time_commits,
               SUM(CASE WHEN committed_at >= ? THEN 1 ELSE 0 END) AS window_commits,
               SUM(CASE WHEN committed_at >= ? THEN files_changed ELSE 0 END) AS window_files,
               MAX(committed_at) AS last_commit_at
          FROM commits
         GROUP BY author_email, author_name
        """,
        (cutoff, cutoff),
    )
    rows = cur.fetchall()

    cur.execute(
        """
        SELECT author, COUNT(*) AS prs
          FROM pull_requests
         WHERE created_at >= ? AND author IS NOT NULL
         GROUP BY author
        """,
        (cutoff,),
    )
    prs_by_login: dict[str, int] = {row[0]: int(row[1]) for row in cur.fetchall()}

    out: list[dict] = []
    for email, name, all_commits, win_commits, win_files, last_at in rows:
        last_dt = (
            datetime.fromisoformat(last_at) if last_at else None
        )
        days_since = (
            (now - last_dt).days if last_dt is not None else None
        )
        local_part = (email or "").split("@", 1)[0] or None
        prs_in_window = prs_by_login.get(local_part or "", 0)
        out.append(
            {
                "author_email": email,
                "author_name": name,
                "all_time_commits": int(all_commits or 0),
                "window_commits": int(win_commits or 0),
                "window_files_changed": int(win_files or 0),
                "window_prs_authored": prs_in_window,
                "last_commit_at": last_at,
                "days_since_last_commit": days_since,
            }
        )
    out.sort(key=lambda r: r["window_commits"], reverse=True)
    return out[:top_n]


_PATH_PREFIX_RE = re.compile(r"^([^/]+/[^/]+)")


def _top_prefix(path: str) -> str:
    """Return the first two segments of a path (e.g. ``src/whygraph``)."""
    m = _PATH_PREFIX_RE.match(path)
    return m.group(1) if m else path


def velocity_by_path_prefix(
    repo_root: Path,
    branch: str,
    *,
    window_days: int,
    top_n: int,
    now: datetime | None = None,
) -> list[dict]:
    """Aggregate commit-touch counts by top-level path prefix.

    Reads `git log --since=<cutoff> --name-only --pretty=format:>>>SHA<<<`
    once per call and tallies in Python — the scan DB stores file counts,
    not file paths, so this is the cheapest way to get the breakdown.
    """
    if now is None:
        now = datetime.now(tz=timezone.utc)
    cutoff = (now - timedelta(days=window_days)).isoformat()
    out_text = git_module._run_git(
        repo_root,
        [
            "log",
            branch,
            f"--since={cutoff}",
            "--name-only",
            "--pretty=format:>>>%H<<<",
        ],
    )
    counts: Counter[str] = Counter()
    distinct_commits: dict[str, set[str]] = defaultdict(set)
    current_sha = ""
    for line in out_text.splitlines():
        if line.startswith(">>>") and line.endswith("<<<"):
            current_sha = line[3:-3]
            continue
        if not line.strip():
            continue
        prefix = _top_prefix(line.strip())
        counts[prefix] += 1
        if current_sha:
            distinct_commits[prefix].add(current_sha)
    out: list[dict] = []
    for prefix, file_touches in counts.most_common(top_n):
        out.append(
            {
                "path_prefix": prefix,
                "file_touches": file_touches,
                "distinct_commits": len(distinct_commits.get(prefix, set())),
            }
        )
    return out


def repo_overview(db: db_module.Database) -> dict:
    """Counts, date range, scoring + LLM coverage, top contributors."""
    cur = db._conn.cursor()
    cur.execute("SELECT COUNT(*), MIN(committed_at), MAX(committed_at) FROM commits")
    commit_count, first_commit, last_commit = cur.fetchone()
    cur.execute("SELECT COUNT(*) FROM pull_requests")
    pr_count = int(cur.fetchone()[0])
    cur.execute("SELECT COUNT(*) FROM issues")
    issue_count = int(cur.fetchone()[0])
    cur.execute(
        "SELECT COUNT(*) FROM commits WHERE llm_description IS NOT NULL"
    )
    llm_described = int(cur.fetchone()[0])
    cur.execute(
        "SELECT COUNT(*) FROM commits "
        "WHERE subject_tfidf_score > 0 OR body_tfidf_score > 0"
    )
    scored_commits = int(cur.fetchone()[0])
    cur.execute(
        """
        SELECT author_email, author_name, COUNT(*) AS n
          FROM commits
         GROUP BY author_email, author_name
         ORDER BY n DESC
         LIMIT 10
        """
    )
    top_contributors = [
        {"author_email": e, "author_name": n, "commits": int(c)}
        for e, n, c in cur.fetchall()
    ]
    return {
        "commits": int(commit_count or 0),
        "pull_requests": pr_count,
        "issues": issue_count,
        "first_commit_at": first_commit,
        "last_commit_at": last_commit,
        "llm_described_commits": llm_described,
        "scored_commits": scored_commits,
        "top_contributors": top_contributors,
    }
