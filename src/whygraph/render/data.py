"""Assemble the JSON payload that powers the static + serve viewer.

The payload shape lives in the v1.4 plan and is consumed by both the
static HTML and the server-driven page. Keep it dict-of-primitives
friendly — gets serialized with `json.dumps`.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from whygraph import backend as backend_module
from whygraph import mcp_queries
from whygraph.scan import authors as authors_module
from whygraph.scan import db as db_module
from whygraph.scan import git as git_module
from whygraph.scan.scoring import ValueGate


# Hierarchy levels for the viewer's level slider.
#
# Level 1 = Modules (always visible).
# Level 2 = + Classes / structural types.
# Level 3 = + Functions / methods.
# Level 4 = + Leaves (variables, constants, parameters).
#
# Unmapped kinds default to level 4 so future CodeGraph additions show
# up at "Everything" without disappearing from older artifacts.
_KIND_LEVEL: dict[str, int] = {
    # 1 — Modules
    "file": 1,
    "module": 1,
    "namespace": 1,
    "package": 1,
    # 2 — Classes / structural types
    "class": 2,
    "struct": 2,
    "interface": 2,
    "enum": 2,
    "trait": 2,
    "type": 2,
    "component": 2,
    # 3 — Functions / methods
    "function": 3,
    "method": 3,
    "route": 3,
    "property": 3,
    "constructor": 3,
}


def _level_for(kind: str | None) -> int:
    """Return the hierarchy level for a CodeGraph node kind."""
    return _KIND_LEVEL.get(kind or "", 4)


def _compute_parents(
    nodes: Iterable[backend_module.SymbolNode],
    edges: Iterable[tuple[str, str, str]],
) -> dict[str, str | None]:
    """Map node_id → parent_id for edge aggregation at filter time.

    Resolution order:

    1. CodeGraph ``contains`` edges (authoritative when present).
    2. Longest qualified-name prefix that matches another node's
       ``qualified_name``.
    3. The file-level node whose ``file_path`` matches.
    4. ``None`` (orphan; renders without aggregation).
    """
    nodes_list = list(nodes)
    by_id = {n.id: n for n in nodes_list}
    by_qname = {n.qualified_name: n.id for n in nodes_list if n.qualified_name}
    # File-level fallback only points at file/module-kind nodes — otherwise a
    # function would get aggregated to a sibling function.
    file_nodes_by_path = {
        n.file_path: n.id
        for n in nodes_list
        if n.kind in ("file", "module") and n.file_path
    }

    parents: dict[str, str | None] = {n.id: None for n in nodes_list}

    # Pass 1: CodeGraph `contains` edges win.
    for source, target, kind in edges:
        if kind != "contains":
            continue
        if target in parents and source in by_id:
            parents[target] = source

    # Pass 2: qname-prefix fallback for nodes still without a parent.
    for n in nodes_list:
        if parents.get(n.id) is not None:
            continue
        qname = n.qualified_name or ""
        if not qname:
            continue
        # Walk shortening prefixes until we hit a known qname.
        sep = "." if "." in qname else ("::" if "::" in qname else None)
        if sep is None:
            continue
        parts = qname.split(sep)
        for cut in range(len(parts) - 1, 0, -1):
            prefix = sep.join(parts[:cut])
            candidate = by_qname.get(prefix)
            if candidate and candidate != n.id:
                parents[n.id] = candidate
                break

    # Pass 3: fall back to the file-level node sharing this file_path.
    for n in nodes_list:
        if parents.get(n.id) is not None:
            continue
        if n.kind in ("file", "module"):
            continue  # file nodes are themselves the top-level anchor
        candidate = file_nodes_by_path.get(n.file_path or "")
        if candidate and candidate != n.id:
            parents[n.id] = candidate

    return parents


def assemble(
    *,
    repo_root: Path,
    codegraph_db: Path,
    whygraph_db: Path,
    runtime: str = "static",
    depth: int = 1,
    now: datetime | None = None,
) -> dict:
    """Return the full JSON payload for the viewer.

    ``runtime`` is reflected in ``meta.runtime`` and tells the page UI
    whether to render the "Generate rationale" button (serve mode) or
    the static placeholder.

    ``depth`` (1–4) caps which nodes get a populated ``node_details``
    entry. The graph itself always carries every node — only the
    per-node detail lookup table is gated. Higher levels still appear
    in the slider; clicking one shows a "re-render with --depth N"
    placeholder. Trims ~30% of the JSON when set to 1 on this repo.
    """
    if not 1 <= depth <= 4:
        raise ValueError(f"depth must be in 1..4 (got {depth})")
    if now is None:
        now = datetime.now(tz=timezone.utc)

    backend = backend_module.SqliteCodegraphBackend(codegraph_db)
    try:
        nodes = list(backend.iter_nodes())
        edges = list(backend.iter_edges())
    finally:
        backend.close()

    degrees: Counter[str] = Counter()
    for source, target, _kind in edges:
        degrees[source] += 1
        degrees[target] += 1

    parents = _compute_parents(nodes, edges)

    with db_module.Database(whygraph_db) as db:
        gate = ValueGate.percentile(db, fraction=0.5)
        node_rows: list[dict] = []
        node_details: dict[str, dict] = {}
        rationale_covered = 0
        for n in nodes:
            level = _level_for(n.kind)
            detail: dict | None = None
            if level <= depth:
                detail = _build_node_detail(
                    db=db,
                    repo_root=repo_root,
                    node=n,
                    gate=gate,
                )
                node_details[n.id] = detail
                if detail.get("rationale") is not None:
                    rationale_covered += 1
            primary = (detail.get("contributors") if detail else None) or []
            primary_author = primary[0]["name"] if primary else None
            node_rows.append(
                {
                    "id": n.id,
                    "qualified_name": n.qualified_name,
                    "kind": n.kind,
                    "name": n.name,
                    "file_path": n.file_path,
                    "language": n.language,
                    "start_line": n.start_line,
                    "end_line": n.end_line,
                    "signature": n.signature,
                    "docstring": n.docstring,
                    "degree": degrees.get(n.id, 0),
                    "level": level,
                    "parent_id": parents.get(n.id),
                    "primary_author": primary_author,
                    "has_rationale": detail is not None
                    and detail.get("rationale") is not None,
                }
            )

        edge_rows = [
            {"source": s, "target": t, "kind": k} for s, t, k in edges
        ]

        dashboard = _build_dashboard(db, repo_root, now)
        author_rows = _build_authors(db, now)

    return {
        "meta": {
            "generated_at": now.isoformat(),
            "repo_root": str(repo_root),
            "runtime": runtime,
            "depth": depth,
            "node_count": len(node_rows),
            "edge_count": len(edge_rows),
            "rationale_coverage": {
                "covered": rationale_covered,
                "total": len(node_rows),
            },
        },
        "nodes": node_rows,
        "edges": edge_rows,
        "node_details": node_details,
        "dashboard": dashboard,
        "authors": author_rows,
    }


def _build_node_detail(
    *,
    db: db_module.Database,
    repo_root: Path,
    node: backend_module.SymbolNode,
    gate: ValueGate,
) -> dict:
    """Build the per-node detail block.

    Best-effort: if the file is missing or blame fails, the contributors
    / activity / evidence lists come back empty. The node still appears
    on the graph so the UI can show "history unavailable".
    """
    rationale = db.get_rationale_cache_by_qname(node.qualified_name)
    rationale_view = (
        {
            "purpose": rationale["purpose"],
            "why": rationale["why"],
            "constraints": rationale["constraints"],
            "tradeoffs": rationale["tradeoffs"],
            "risks": rationale["risks"],
            "confidence": rationale["confidence"],
            "model": rationale.get("model"),
        }
        if rationale is not None
        else None
    )

    if not (
        node.file_path
        and node.start_line
        and node.end_line
        and (repo_root / node.file_path).exists()
    ):
        return {
            "contributors": [],
            "activity": {},
            "evidence": [],
            "rationale": rationale_view,
        }

    try:
        blame = mcp_queries.blame_line_range(
            repo_root, node.file_path, node.start_line, node.end_line
        )
    except git_module.GitError:
        blame = {}

    contributors = _aggregate_contributors(blame)
    activity = _aggregate_activity(blame)
    evidence = _build_evidence(db, blame, gate)

    return {
        "contributors": contributors,
        "activity": activity,
        "evidence": evidence,
        "rationale": rationale_view,
    }


def _aggregate_contributors(blame: dict[str, dict]) -> list[dict]:
    """Top contributors by lines-owned within the symbol's range."""
    if not blame:
        return []
    by_key: dict[tuple[str | None, str | None], dict] = {}
    for entry in blame.values():
        name = entry.get("author_name")
        email = entry.get("author_email")
        key = (name, email)
        slot = by_key.setdefault(
            key,
            {"name": name, "email": email, "login": None, "lines": 0},
        )
        slot["lines"] += int(entry.get("lines_owned") or 0)
    total = sum(s["lines"] for s in by_key.values()) or 1
    out = sorted(by_key.values(), key=lambda s: s["lines"], reverse=True)
    for s in out:
        s["percent"] = round(100.0 * s["lines"] / total, 1)
    return out[:5]


def _aggregate_activity(blame: dict[str, dict]) -> dict[str, int]:
    """Bucket commits by year-month for the inline timeline chart."""
    out: Counter[str] = Counter()
    for entry in blame.values():
        when = entry.get("committed_at")
        if not when:
            continue
        # ISO timestamp; first 7 chars are YYYY-MM.
        out[when[:7]] += 1
    # Return as sorted dict for stable JSON output.
    return dict(sorted(out.items()))


def _build_evidence(
    db: db_module.Database, blame: dict[str, dict], gate: ValueGate
) -> list[dict]:
    """Top-3 most recent commits affecting the symbol, with PRs + issues."""
    items: list[dict] = []
    for sha, blame_entry in blame.items():
        commit = db.get_commit(sha)
        if commit is None:
            summary = blame_entry.get("summary")
            narratives = {"git_blame_summary": summary} if summary else {}
            items.append(
                {
                    "sha": sha,
                    "narratives": narratives,
                    "committed_at": blame_entry.get("committed_at"),
                    "author": {
                        "name": blame_entry.get("author_name"),
                        "email": blame_entry.get("author_email"),
                    },
                    "prs": [],
                    "issues": [],
                    "db_commit_present": False,
                }
            )
            continue
        narratives = mcp_queries.commit_narratives(commit, gate)
        prs_raw = mcp_queries.prs_containing_commit(db, sha)
        prs: list[dict] = []
        issues: list[dict] = []
        seen_issue_numbers: set[int] = set()
        for pr in prs_raw:
            prs.append(
                {
                    "number": pr["number"],
                    "title": pr.get("title"),
                    "state": pr.get("state"),
                    "merged_at": pr.get("merged_at"),
                    "author": pr.get("author"),
                    "html_url": pr.get("html_url"),
                    "narratives": mcp_queries.pr_narratives(pr, gate),
                }
            )
            for issue in mcp_queries.closing_issues_for_pr(db, pr["number"]):
                if issue["number"] in seen_issue_numbers:
                    continue
                seen_issue_numbers.add(issue["number"])
                issues.append(
                    {
                        "number": issue["number"],
                        "title": issue.get("title"),
                        "state": issue.get("state"),
                        "html_url": issue.get("html_url"),
                        "narratives": mcp_queries.issue_narratives(issue, gate),
                    }
                )
        items.append(
            {
                "sha": sha,
                "narratives": narratives,
                "committed_at": commit.get("committed_at"),
                "author": {
                    "name": commit.get("author_name"),
                    "email": commit.get("author_email"),
                },
                "prs": prs,
                "issues": issues,
                "db_commit_present": True,
            }
        )
    items.sort(key=lambda it: it.get("committed_at") or "", reverse=True)
    return items[:3]


def _build_dashboard(
    db: db_module.Database, repo_root: Path, now: datetime
) -> dict:
    """Repo overview + 90-day per-author/per-path velocity + overall activity."""
    overview = mcp_queries.repo_overview(db)
    top_contributors = mcp_queries.velocity_by_author(
        db, window_days=90, top_n=10, now=now
    )
    try:
        branch = git_module._run_git(
            repo_root, ["rev-parse", "--abbrev-ref", "HEAD"]
        ).strip()
    except git_module.GitError:
        branch = "HEAD"
    try:
        hot_paths = mcp_queries.velocity_by_path_prefix(
            repo_root, branch, window_days=90, top_n=10, now=now
        )
    except git_module.GitError:
        hot_paths = []
    activity_overall = _activity_overall(db)
    return {
        "repo_overview": overview,
        "top_contributors_90d": top_contributors,
        "hot_paths_90d": hot_paths,
        "activity_overall": activity_overall,
    }


def _activity_overall(db: db_module.Database) -> dict[str, int]:
    """Commits per year-month over the full history."""
    cur = db._conn.cursor()
    cur.execute(
        "SELECT substr(committed_at, 1, 7) AS bucket, COUNT(*) "
        "FROM commits "
        "WHERE committed_at IS NOT NULL "
        "GROUP BY bucket ORDER BY bucket"
    )
    return {row[0]: int(row[1]) for row in cur.fetchall() if row[0]}


def _build_authors(db: db_module.Database, now: datetime) -> list[dict]:
    """Author rows with recent activity drill-down via window_query."""
    out: list[dict] = []
    rows = db.iter_authors()
    if not rows:
        return out
    gate = ValueGate.percentile(db, fraction=0.5)
    repo_root = Path(db.path).resolve().parent.parent  # `.whygraph/whygraph.db` → repo root
    since = now - timedelta(days=180)
    for row in rows:
        emails = list(row.get("emails") or [])
        logins = list(row.get("logins") or [])
        try:
            recent = mcp_queries.window_query(
                db,
                repo_root,
                since=since,
                until=now,
                kinds=("commit", "pr", "issue"),
                author_emails=emails or None,
                author_logins=logins or None,
                path_prefix=None,
                label=None,
                state=None,
                gate=gate,
                limit=20,
            )
        except Exception:  # noqa: BLE001 — best-effort; never fail the whole render
            recent = []
        out.append(
            {
                "id": row["id"],
                "primary_login": row.get("primary_login"),
                "primary_name": row.get("primary_name"),
                "primary_email": row.get("primary_email"),
                "emails": emails,
                "logins": logins,
                "names": list(row.get("names") or []),
                "commit_count": int(row.get("commit_count") or 0),
                "pr_count": int(row.get("pr_count") or 0),
                "issue_count": int(row.get("issue_count") or 0),
                "first_seen": row.get("first_seen"),
                "last_seen": row.get("last_seen"),
                "areas_touched": [],  # v1: blank — git-log-per-author scan deferred
                "recent_activity": recent,
            }
        )
    return out
