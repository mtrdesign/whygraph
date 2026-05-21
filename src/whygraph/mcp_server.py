"""WhyGraph MCP server: resources/tools/prompts over the scan DB.

The scan DB at ``<repo_root>/.whygraph/whygraph.db`` is the single source of
truth. All resources/tools open it read-only per call. The CodeGraph DB
(used for symbol-name resolution) is opened lazily by tools that need it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from whygraph.core import configure_logging, get_config
from whygraph import mcp_queries
from whygraph.services.codegraph import CodeGraph
from whygraph.services.llm import ClaudeCliAdapter, CompletionRequest
from whygraph.scan import authors as authors_module
from whygraph.scan import db as db_module
from whygraph.scan import git as git_module
from whygraph.scan.scoring import ValueGate

DEFAULT_RATIONALE_MODEL = "claude-opus-4-7"
DEFAULT_RATIONALE_TIMEOUT_SEC = 180
CONFIDENCE_CEILING = 0.85

mcp = FastMCP("whygraph")


class WhyGraphError(RuntimeError):
    pass


def _resolve_codegraph_db_path() -> Path | None:
    if override := os.environ.get("CODEGRAPH_DB"):
        return Path(override)
    try:
        root = git_module.repo_root(Path.cwd())
    except git_module.GitError:
        root = Path.cwd()
    candidate = root / ".codegraph" / "codegraph.db"
    return candidate if candidate.exists() else None


def _resolve_repo_root() -> Path:
    try:
        return git_module.repo_root(Path.cwd())
    except git_module.GitError:
        return Path.cwd()


def _resolve_db_path() -> Path:
    """Locate the scan DB.

    Order: ``WHYGRAPH_DB`` env override, then ``git rev-parse --show-toplevel``
    from CWD, else CWD itself. Tests set ``WHYGRAPH_DB`` to a fixture path.
    """
    if override := os.environ.get("WHYGRAPH_DB"):
        return Path(override)
    try:
        root = git_module.repo_root(Path.cwd())
    except git_module.GitError:
        root = Path.cwd()
    return db_module.default_db_path(root)


def _parse_json_list(raw: Any) -> list:
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _hydrate_pr(row: dict) -> dict:
    """Decode JSON columns on a PR row in place."""
    out = dict(row)
    out["labels"] = _parse_json_list(out.get("labels"))
    out["commit_titles"] = _parse_json_list(out.get("commit_titles"))
    out["comments"] = _parse_json_list(out.get("comments"))
    return out


def _hydrate_issue(row: dict) -> dict:
    out = dict(row)
    out["labels"] = _parse_json_list(out.get("labels"))
    return out


def _hydrate_commit(row: dict) -> dict:
    out = dict(row)
    out["parent_shas"] = _parse_json_list(out.get("parent_shas"))
    return out


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------


@mcp.resource(
    "whygraph://repo/overview",
    name="repo_overview",
    description=(
        "Summary of the scanned repo: counts, scan freshness, scoring + LLM "
        "coverage, and top contributors."
    ),
    mime_type="application/json",
)
def repo_overview() -> dict:
    with db_module.Database(_resolve_db_path()) as db:
        return mcp_queries.repo_overview(db)


@mcp.resource(
    "whygraph://commit/{sha}",
    name="commit",
    description=(
        "Full commit row plus PRs that contain it and the closing issues "
        "for each linked PR."
    ),
    mime_type="application/json",
)
def commit_resource(sha: str) -> dict:
    with db_module.Database(_resolve_db_path()) as db:
        row = db.get_commit(sha)
        if row is None:
            return {"error": "not_found", "sha": sha}
        prs = [_hydrate_pr(p) for p in mcp_queries.prs_containing_commit(db, sha)]
        for pr in prs:
            pr["closing_issues"] = [
                _hydrate_issue(i)
                for i in mcp_queries.closing_issues_for_pr(db, pr["number"])
            ]
        return {"commit": _hydrate_commit(row), "linked_prs": prs}


@mcp.resource(
    "whygraph://pr/{number}",
    name="pull_request",
    description="Full PR row including commit_titles dicts, comments, and closing issues.",
    mime_type="application/json",
)
def pr_resource(number: str) -> dict:
    n = int(number)
    with db_module.Database(_resolve_db_path()) as db:
        row = db.get_pull_request(n)
        if row is None:
            return {"error": "not_found", "number": n}
        closing = [_hydrate_issue(i) for i in mcp_queries.closing_issues_for_pr(db, n)]
        return {"pull_request": _hydrate_pr(row), "closing_issues": closing}


@mcp.resource(
    "whygraph://issue/{number}",
    name="issue",
    description="Full issue row plus PRs that close it.",
    mime_type="application/json",
)
def issue_resource(number: str) -> dict:
    n = int(number)
    with db_module.Database(_resolve_db_path()) as db:
        row = db.get_issue(n)
        if row is None:
            return {"error": "not_found", "number": n}
        cur = db._conn.cursor()
        cur.execute(
            """
            SELECT p.* FROM pull_requests p
              JOIN pr_issue_links l ON l.pr_number = p.number
             WHERE l.issue_number = ? AND l.link_kind = 'closes'
             ORDER BY p.number
            """,
            (n,),
        )
        cols = [d[0] for d in cur.description]
        prs = [_hydrate_pr(dict(zip(cols, row, strict=True))) for row in cur.fetchall()]
        return {"issue": _hydrate_issue(row), "closing_prs": prs}


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def _resolve_target(
    *,
    path: str | None,
    line_start: int | None,
    line_end: int | None,
    qualified_name: str | None,
) -> dict:
    """Validate and normalize a tool target.

    Returns ``{path, line_start, line_end, qualified_name}``. ``qualified_name``
    is a *targeting convenience*: it resolves to the symbol's file/line range
    via CodeGraph but does not pull neighbours. For caller/callee context, use
    CodeGraph's MCP server or Claude Code's Explore agent.

    Raises ``WhyGraphError`` on bad input.
    """
    if qualified_name:
        if path or line_start or line_end:
            raise WhyGraphError(
                "Pass either qualified_name OR (path, line_start, line_end), not both."
            )
        cg_path = _resolve_codegraph_db_path()
        if cg_path is None:
            raise WhyGraphError(
                "qualified_name targeting requires CodeGraph. Set CODEGRAPH_DB or "
                "run `codegraph init` to create .codegraph/codegraph.db."
            )
        with CodeGraph(cg_path) as graph:
            node = graph.symbol(qualified_name)
        if node is None:
            raise WhyGraphError(
                f"qualified_name {qualified_name!r} not found in CodeGraph"
            )
        return {
            "path": node.file_path,
            "line_start": node.start_line,
            "line_end": node.end_line,
            "qualified_name": qualified_name,
        }

    if not (path and line_start and line_end):
        raise WhyGraphError(
            "Must pass either qualified_name OR all of (path, line_start, line_end)."
        )
    if line_start < 1 or line_end < line_start:
        raise WhyGraphError("line_start must be ≥ 1 and line_end ≥ line_start")
    return {
        "path": path,
        "line_start": line_start,
        "line_end": line_end,
        "qualified_name": None,
    }


def _build_evidence_item(
    db: db_module.Database,
    *,
    sha: str,
    blame_entry: dict,
    gate: ValueGate,
) -> dict:
    """Assemble an evidence item from blame + DB rows.

    If the SHA is missing from the scan DB (stale scan, partial walk), the
    blame's own author/summary metadata is surfaced instead and
    `db_commit_present` flips to ``False``. The agent can use that to
    decide whether to suggest a re-scan.
    """
    blame_lines = blame_entry["lines_owned"]
    commit = db.get_commit(sha)
    if commit is None:
        summary = blame_entry.get("summary")
        narratives = {"git_blame_summary": summary} if summary else {}
        return {
            "sha": sha,
            "narratives": narratives,
            "committed_at": blame_entry.get("committed_at"),
            "blame_lines": blame_lines,
            "commit_author": {
                "name": blame_entry.get("author_name"),
                "email": blame_entry.get("author_email"),
            },
            "prs": [],
            "issues": [],
            "all_authors": (
                [
                    {
                        "login": None,
                        "name": blame_entry.get("author_name"),
                        "email": blame_entry.get("author_email"),
                    }
                ]
                if blame_entry.get("author_name") or blame_entry.get("author_email")
                else []
            ),
            "db_commit_present": False,
        }
    # If no narrative qualifies (no llm_description and body/subject below
    # the gate), `narratives` is empty — but we still surface the entry.
    # The blame `lines_owned` is itself signal (matches how PRs and issues
    # are surfaced even when their own narratives fail the gate).
    narratives = mcp_queries.commit_narratives(commit, gate)

    prs_raw = mcp_queries.prs_containing_commit(db, sha)
    prs: list[dict] = []
    issues_collected: dict[int, dict] = {}
    all_authors: list[dict] = []
    seen_logins: set[str] = set()
    seen_email_name: set[tuple[str | None, str | None]] = set()

    def _add_author(login: str | None, name: str | None, email: str | None) -> None:
        if login:
            if login in seen_logins:
                return
            seen_logins.add(login)
        else:
            key = (name, email)
            if key == (None, None) or key in seen_email_name:
                return
            seen_email_name.add(key)
        all_authors.append({"login": login, "name": name, "email": email})

    _add_author(None, commit.get("author_name"), commit.get("author_email"))

    for pr in prs_raw:
        pr_entry = {
            "number": pr["number"],
            "narratives": mcp_queries.pr_narratives(pr, gate),
            "author": pr.get("author"),
            "html_url": pr.get("html_url"),
            "merged_at": pr.get("merged_at"),
        }
        commit_authors = mcp_queries.pr_authors(pr)
        pr_entry["commit_authors"] = commit_authors
        for a in commit_authors:
            _add_author(a.get("login"), a.get("name"), a.get("email"))
        prs.append(pr_entry)

        for issue in mcp_queries.closing_issues_for_pr(db, pr["number"]):
            if issue["number"] in issues_collected:
                continue
            issues_collected[issue["number"]] = {
                "number": issue["number"],
                "narratives": mcp_queries.issue_narratives(issue, gate),
                "author": issue.get("author"),
                "html_url": issue.get("html_url"),
                "labels": _parse_json_list(issue.get("labels")),
            }
            if issue.get("author"):
                _add_author(issue["author"], None, None)

    return {
        "sha": sha,
        "narratives": narratives,
        "committed_at": commit.get("committed_at"),
        "blame_lines": blame_lines,
        "commit_author": {
            "name": commit.get("author_name"),
            "email": commit.get("author_email"),
        },
        "prs": prs,
        "issues": list(issues_collected.values()),
        "all_authors": all_authors,
        "db_commit_present": True,
    }


@mcp.tool(
    name="whygraph_evidence_for",
    description=(
        "Find historical evidence (commits + PRs + closing issues) for a "
        "chunk of code. Pass either (path, line_start, line_end) or a "
        "qualified_name (CodeGraph resolves it to a file/line range — no "
        "graph traversal). Returns {target, evidence}. Evidence is filtered "
        "by TF-IDF harshness; commit narratives ship llm_description + "
        "subject + body when each clears the gate. For caller/callee "
        "context, query CodeGraph or the Explore agent separately. Run "
        "`whygraph scan` first to populate llm_descriptions — this tool "
        "does not call the LLM."
    ),
)
def whygraph_evidence_for(
    path: str | None = None,
    line_start: int | None = None,
    line_end: int | None = None,
    qualified_name: str | None = None,
    min_score_pct: float = 0.5,
    limit: int = 10,
) -> dict:
    if not 0.0 <= min_score_pct <= 1.0:
        raise WhyGraphError("min_score_pct must be in [0, 1]")
    if limit < 1:
        raise WhyGraphError("limit must be ≥ 1")

    target = _resolve_target(
        path=path,
        line_start=line_start,
        line_end=line_end,
        qualified_name=qualified_name,
    )
    repo_root = _resolve_repo_root()

    blame = mcp_queries.blame_line_range(
        repo_root, target["path"], target["line_start"], target["line_end"]
    )

    items: list[dict] = []
    if blame:
        with db_module.Database(_resolve_db_path()) as db:
            gate = ValueGate.percentile(db, fraction=min_score_pct)
            for sha, blame_entry in blame.items():
                items.append(
                    _build_evidence_item(
                        db, sha=sha, blame_entry=blame_entry, gate=gate
                    )
                )
        items.sort(key=lambda it: it.get("committed_at") or "", reverse=True)
        items = items[:limit]

    return {
        "target": {
            "path": target["path"],
            "line_start": target["line_start"],
            "line_end": target["line_end"],
            "qualified_name": target["qualified_name"],
        },
        "evidence": items,
    }


@mcp.tool(
    name="whygraph_search",
    description=(
        "LIKE-match query across commits/PRs/issues, ranked by TF-IDF. "
        "Commits with non-NULL llm_description always pass the harshness "
        "gate. Returns typed hits with id, narrative, score, authors."
    ),
)
def whygraph_search(
    query: str,
    kinds: list[str] | None = None,
    limit: int = 20,
    min_score_pct: float = 0.5,
) -> list[dict]:
    if not 0.0 <= min_score_pct <= 1.0:
        raise WhyGraphError("min_score_pct must be in [0, 1]")
    if limit < 1:
        raise WhyGraphError("limit must be ≥ 1")
    valid_kinds = {"commit", "pr", "issue"}
    selected = tuple(kinds) if kinds else ("commit", "pr", "issue")
    bad = set(selected) - valid_kinds
    if bad:
        raise WhyGraphError(
            f"unknown kinds {sorted(bad)} (allowed: {sorted(valid_kinds)})"
        )
    with db_module.Database(_resolve_db_path()) as db:
        gate = ValueGate.percentile(db, fraction=min_score_pct)
        return mcp_queries.search_text(
            db, query, kinds=selected, gate=gate, limit=limit
        )


@mcp.tool(
    name="whygraph_velocity_summary",
    description=(
        "Per-author commit velocity (window + all-time + recency, files "
        "touched, PRs authored), or per-path-prefix touch counts when "
        "group_by='path_prefix'. Path-prefix mode shells out to git log."
    ),
)
def whygraph_velocity_summary(
    window_days: int = 90,
    group_by: str = "author",
    top_n: int = 10,
) -> list[dict]:
    if window_days < 1:
        raise WhyGraphError("window_days must be ≥ 1")
    if top_n < 1:
        raise WhyGraphError("top_n must be ≥ 1")
    if group_by == "author":
        with db_module.Database(_resolve_db_path()) as db:
            return mcp_queries.velocity_by_author(
                db, window_days=window_days, top_n=top_n
            )
    if group_by == "path_prefix":
        repo_root = _resolve_repo_root()
        try:
            branch = git_module._run_git(
                repo_root, ["rev-parse", "--abbrev-ref", "HEAD"]
            ).strip()
        except git_module.GitError:
            branch = "HEAD"
        return mcp_queries.velocity_by_path_prefix(
            repo_root, branch, window_days=window_days, top_n=top_n
        )
    raise WhyGraphError(
        f"unknown group_by {group_by!r} (allowed: 'author', 'path_prefix')"
    )


_VALID_KINDS = ("commit", "pr", "issue")
_VALID_STATES = ("merged", "open", "closed")


@mcp.tool(
    name="whygraph_window",
    description=(
        "Window query over the scan DB. One generic primitive — compose "
        "with prompts (changelog / feature_timeline / user_profile) "
        "instead of asking for a per-report tool. "
        "Filters: since/until (ISO date or relative '30d'/'3m'/'1y'); "
        "kinds (subset of ['commit','pr','issue']); author (login | "
        "email | name → resolved via authors table); path_prefix "
        "(commits only); label (PR/issue exact match); state "
        "('merged'|'open'|'closed'). Returns time-ordered rows with "
        "kind/id/at/narratives/author."
    ),
)
def whygraph_window(
    since: str,
    until: str | None = None,
    kinds: list[str] | None = None,
    author: str | None = None,
    path_prefix: str | None = None,
    label: str | None = None,
    state: str | None = None,
    min_score_pct: float = 0.5,
    limit: int = 100,
) -> list[dict]:
    if not 0.0 <= min_score_pct <= 1.0:
        raise WhyGraphError("min_score_pct must be in [0, 1]")
    if limit < 1:
        raise WhyGraphError("limit must be ≥ 1")

    selected = tuple(kinds) if kinds else _VALID_KINDS
    bad = set(selected) - set(_VALID_KINDS)
    if bad:
        raise WhyGraphError(
            f"unknown kinds {sorted(bad)} (allowed: {list(_VALID_KINDS)})"
        )
    if state is not None and state not in _VALID_STATES:
        raise WhyGraphError(
            f"unknown state {state!r} (allowed: {list(_VALID_STATES)})"
        )

    try:
        since_dt = mcp_queries.parse_window_bound(since)
        until_dt = (
            mcp_queries.parse_window_bound(until) if until else mcp_queries.parse_window_bound("now")
        )
    except ValueError as exc:
        raise WhyGraphError(str(exc)) from exc
    if since_dt > until_dt:
        raise WhyGraphError(f"since ({since}) is after until ({until})")

    repo_root = _resolve_repo_root()
    with db_module.Database(_resolve_db_path()) as db:
        gate = ValueGate.percentile(db, fraction=min_score_pct)
        author_emails: list[str] | None = None
        author_logins: list[str] | None = None
        if author:
            resolved = authors_module.resolve_author(db, author)
            if resolved is None:
                raise WhyGraphError(
                    f"author {author!r} did not resolve to any identity in the "
                    "authors table (run `whygraph scan` to rebuild it)"
                )
            author_emails = list(resolved.get("emails") or [])
            author_logins = list(resolved.get("logins") or [])
        return mcp_queries.window_query(
            db,
            repo_root,
            since=since_dt,
            until=until_dt,
            kinds=selected,
            author_emails=author_emails,
            author_logins=author_logins,
            path_prefix=path_prefix,
            label=label,
            state=state,
            gate=gate,
            limit=limit,
        )


_RATIONALE_SYSTEM_PROMPT = """\
You are an analyst that explains why code exists, not just what it does. Given a code symbol's location and a bundle of evidence (commits, blame data, linked PRs and issues), generate a structured rationale grounded strictly in that bundle.

You CANNOT read files, run tools, search the codebase, or access anything beyond the bundle the user provides. The bundle is your COMPLETE input. Do not request more information; do not narrate what you would check; do not propose tool calls.

Output schema — these top-level keys, no others:
{
  "purpose":     <string>           // one sentence stating what the code does today
  "why":         <string>           // one short paragraph of historical/contextual rationale drawn from the bundle
  "constraints": <array of strings> // invariants/contracts the next editor must preserve
  "tradeoffs":   <array of strings> // notable design decisions visible in the evidence
  "risks":       <array of strings> // risks of modifying this code
}

How to weigh narratives on commits:
- A commit may carry up to three narratives, each surfaced under a labelled section in the bundle: `LLM diff summary` (the `llm_description` field — a mechanical diff summary, no human framing), `Subject`, and `Body`. The latter two are the human author's own words.
- Treat `LLM diff summary` as authoritative for *what changed* — it describes the diff itself, with no rhetorical bias.
- Treat `Subject` and `Body` as authoritative for *intent and motivation* — the why behind the change, in the author's own words.
- When a commit has both, cite both: lift mechanism from the diff summary and intent from the subject/body. Do not substitute one for the other.
- Pull request `Title`/`Body` and issue `Title`/`Body` are the highest-signal source for narrative — read those first when present.
- A `Blame summary` block appears for SHAs that exist in git but not in the scan DB (`db_commit_present: false`). Still cite the SHA; do not claim PR/issue context for it.

Be specific and honest:
- Prefer the language of the original commits/PRs/issues over your own paraphrasing. Use exact file paths and identifiers verbatim.
- Cite supporting evidence when making a claim: short SHA prefixes for commits, `#<n>` for PRs and issues.
- If the evidence bundle is sparse, write short factual sentences referencing only what's there. If the bundle has zero entries, `why` may read "Insufficient evidence in the scan." `constraints`, `tradeoffs`, and `risks` should then be empty arrays.
- No hedging ("seems", "may", "appears"). No invented rationale. An empty array is the correct answer when nothing supports an entry.
- Only cite SHAs / PR numbers / issue numbers that appear in the bundle. Do not infer additional context from identifiers that are not in the evidence.

Output format: RAW JSON only. No prose, no code fences, no preamble, no trailing remarks. The first character of your output MUST be '{' and the last character MUST be '}'.
"""


_NARRATIVE_LABEL: dict[str, str] = {
    "llm_description": "LLM diff summary",
    "subject": "Subject",
    "body": "Body",
    "title": "Title",
    "git_blame_summary": "Blame summary",
}

_NARRATIVE_ORDER_COMMIT: tuple[str, ...] = ("llm_description", "subject", "body", "git_blame_summary")
_NARRATIVE_ORDER_PR_ISSUE: tuple[str, ...] = ("title", "body")


def _format_narratives(
    narratives: dict[str, str],
    indent: str,
    *,
    order: tuple[str, ...],
) -> list[str]:
    """Emit labelled narrative blocks. Multi-line narratives wrap with the
    label on its own line followed by indented content."""
    lines: list[str] = []
    for source in order:
        text = narratives.get(source)
        if not text:
            continue
        label = _NARRATIVE_LABEL.get(source, source)
        cleaned = text.strip()
        if "\n" in cleaned:
            lines.append(f"{indent}{label}:")
            for body_line in cleaned.splitlines():
                lines.append(f"{indent}  {body_line.rstrip()}")
        else:
            lines.append(f"{indent}{label}: {cleaned}")
    return lines


def _format_target_header(target: dict) -> list[str]:
    lines: list[str] = []
    qn = target.get("qualified_name")
    path = target.get("path") or "?"
    start = target.get("line_start")
    end = target.get("line_end")
    location = f"{path}:{start}-{end}" if start and end else path
    if qn:
        lines.append(f"Symbol: {qn}")
        lines.append(f"Location: {location}")
    else:
        lines.append(f"Symbol: {location}")
    return lines


def _format_pr_block(pr: dict, indent: str) -> list[str]:
    number = pr.get("number")
    author = pr.get("author") or "unknown"
    merged_at = pr.get("merged_at") or "unmerged"
    lines = [f"{indent}Linked PR #{number}  merged {merged_at}  by {author}"]
    lines.extend(
        _format_narratives(
            pr.get("narratives") or {},
            indent + "  ",
            order=_NARRATIVE_ORDER_PR_ISSUE,
        )
    )
    return lines


def _format_issue_block(issue: dict, indent: str) -> list[str]:
    number = issue.get("number")
    labels = issue.get("labels") or []
    label_part = f"  [{', '.join(labels)}]" if labels else ""
    lines = [f"{indent}Closes issue #{number}{label_part}"]
    lines.extend(
        _format_narratives(
            issue.get("narratives") or {},
            indent + "  ",
            order=_NARRATIVE_ORDER_PR_ISSUE,
        )
    )
    return lines


def _format_evidence_block(ev: dict, indent: str) -> list[str]:
    sha = (ev.get("sha") or "")[:8] or "?"
    when = ev.get("committed_at") or "unknown"
    author = (ev.get("commit_author") or {}).get("name") or "unknown"
    blame_lines = ev.get("blame_lines")
    blame_part = f"  ({blame_lines} lines blamed)" if blame_lines else ""
    db_part = "" if ev.get("db_commit_present", True) else "  [SHA absent from scan DB]"
    lines = [f"{indent}COMMIT {sha}  {when}  by {author}{blame_part}{db_part}"]
    lines.extend(
        _format_narratives(
            ev.get("narratives") or {},
            indent + "  ",
            order=_NARRATIVE_ORDER_COMMIT,
        )
    )
    for pr in ev.get("prs") or []:
        lines.append("")
        lines.extend(_format_pr_block(pr, indent + "  "))
    # Issues are deduped to the commit level during build — render them
    # once after the PR list rather than under a particular PR.
    for issue in ev.get("issues") or []:
        lines.append("")
        lines.extend(_format_issue_block(issue, indent + "  "))
    return lines


def _build_rationale_user_prompt(
    *,
    target: dict,
    evidence: list[dict],
) -> str:
    """Render the evidence bundle as a structured text document.

    Sections in order: target header, evidence-count summary, then commits
    (newest first, with linked PRs and issues nested per commit). The model
    reads headers as editorial cues, so order matters.
    """
    pr_count = sum(len(ev.get("prs") or []) for ev in evidence)
    issue_count = sum(len(ev.get("issues") or []) for ev in evidence)

    lines: list[str] = []
    lines.append("Produce the rationale JSON for this target.")
    lines.append("")
    lines.extend(_format_target_header(target))
    lines.append("")
    lines.append(
        f"Evidence: {len(evidence)} commit(s), {pr_count} PR(s), "
        f"{issue_count} issue(s)."
    )

    if evidence:
        lines.append("")
        lines.append("Commits (newest first):")
        for ev in evidence:
            lines.append("")
            lines.extend(_format_evidence_block(ev, "  "))

    return "\n".join(lines) + "\n"


_FENCE_RE = re.compile(r"^```(?:json|JSON)?\s*\n|\n```\s*$")


def _extract_rationale_json(text: str) -> dict:
    trimmed = text.strip()
    try:
        return json.loads(trimmed)
    except ValueError:
        pass
    stripped = re.sub(r"^```(?:json|JSON)?\s*\r?\n", "", trimmed)
    stripped = re.sub(r"\r?\n```\s*$", "", stripped)
    if stripped != trimmed:
        try:
            return json.loads(stripped.strip())
        except ValueError:
            pass
    raise WhyGraphError(
        f"could not parse JSON from claude rationale output (first 200 chars: {trimmed[:200]})"
    )


def _validate_rationale(payload: dict) -> dict:
    """Validate shape; coerce list fields. Raises WhyGraphError on bad shape."""
    required_str = ("purpose", "why")
    required_lists = ("constraints", "tradeoffs", "risks")
    out: dict = {}
    for key in required_str:
        val = payload.get(key)
        if not isinstance(val, str):
            raise WhyGraphError(
                f"rationale: '{key}' must be a string, got {type(val).__name__}"
            )
        out[key] = val
    for key in required_lists:
        val = payload.get(key)
        if val is None:
            out[key] = []
            continue
        if not isinstance(val, list):
            raise WhyGraphError(
                f"rationale: '{key}' must be a list, got {type(val).__name__}"
            )
        if not all(isinstance(item, str) for item in val):
            raise WhyGraphError(f"rationale: '{key}' must contain strings only")
        out[key] = val
    return out


_NARRATIVE_TIER: dict[str, float] = {
    "llm_description": 1.0,  # verbatim diff summary — highest signal
    "body": 0.5,  # human-written body above the gate
    "subject": 0.5,  # human-written subject above the gate
    "git_blame_summary": 0.25,  # SHA missing from scan DB; only blame metadata
}


def _evidence_tier(ev: dict) -> float:
    """Highest-weighted narrative present on this evidence item.

    Matches the pre-multinarrative behaviour: a commit with both
    ``llm_description`` and ``body`` scores 1.0 (we don't double-count).
    Missing/empty ``narratives`` dict scores 0.0.
    """
    sources = (ev.get("narratives") or {}).keys()
    if not sources:
        return 0.0
    return max(_NARRATIVE_TIER.get(s, 0.0) for s in sources)


def _score_confidence(
    *, evidence: list[dict], constraints: list[str], risks: list[str]
) -> float:
    """Deterministic confidence in [0, 0.85].

    Tiered commit weighting (vs v1's flat 1.0-per-commit):

    - llm_description:    1.0   verbatim diff summary, highest signal
    - body / subject:     0.5   human-written, above the harshness gate
    - git_blame_summary:  0.25  blame-only (SHA missing from scan DB)
    - None:               0.0   narrative failed the gate

    `has_any_commits` and `num_commits_norm` use the tier sum, so a result
    composed of only weak evidence gets a proportionally lower score.
    Author / PR / issue / rationale-content signals are unchanged from v1
    — author info is useful regardless of narrative quality, and PR/issue
    structural links + non-empty constraints/risks already vouch for
    themselves.
    """
    tier_sum = sum(_evidence_tier(ev) for ev in evidence)
    has_any_commits = 1.0 if tier_sum > 0 else 0.0
    num_commits_norm = min(tier_sum / 5.0, 1.0)

    authors: set[str] = set()
    for ev in evidence:
        for a in ev.get("all_authors") or []:
            key = a.get("login") or a.get("email") or a.get("name")
            if key:
                authors.add(key)
    num_authors_norm = min(len(authors) / 3.0, 1.0)

    has_pr = 1.0 if any(ev.get("prs") for ev in evidence) else 0.0
    has_issue = 1.0 if any(ev.get("issues") for ev in evidence) else 0.0
    has_rationale_content = 1.0 if (constraints or risks) else 0.0

    raw = (
        0.20 * has_any_commits
        + 0.20 * num_commits_norm
        + 0.20 * num_authors_norm
        + 0.10 * has_pr
        + 0.10 * has_issue
        + 0.20 * has_rationale_content
    )
    return round(min(raw, 1.0) * CONFIDENCE_CEILING, 4)


# Bump on system-prompt or bundle-formatter changes that alter rationale
# wording. The prompt_version is part of the cache key, so bumping
# invalidates every cached row.
#   v1 → v2: structured text bundle, multi-narrative commits, analyst-tone
#            system prompt (was: dense JSON dump + winner-takes-all).
#   v2 → v3: dropped graph neighbours (callers/callees) from the bundle
#            and the system prompt — WhyGraph no longer does graph
#            traversal; CodeGraph / Explore agent owns that.
_PROMPT_VERSION = "v3"


def _target_id(target: dict) -> str:
    """Stable identity for a rationale target.

    qualified_name when present (graph-resolved); else
    ``<path>:<start>-<end>``. Used as the human-readable component of
    the cache key.
    """
    qn = target.get("qualified_name")
    if qn:
        return f"qn:{qn}"
    path = target.get("path") or "?"
    start = target.get("line_start")
    end = target.get("line_end")
    return f"loc:{path}:{start}-{end}"


def _compute_bundle_signature(evidence: list[dict]) -> str:
    """Hash the *content* of the evidence bundle.

    Captures every identifier the rationale could legitimately depend
    on: target commit SHAs and linked PR/issue numbers. When any of
    those change (a new commit lands on the lines, a PR gets linked,
    etc.), the signature flips and the cache misses.

    Sorted before hashing so the signature is stable regardless of how
    evidence was ordered in the bundle.
    """
    shas: set[str] = set()
    prs: set[int] = set()
    issues: set[int] = set()
    for ev in evidence:
        sha = ev.get("sha")
        if sha:
            shas.add(sha)
        for pr in ev.get("prs") or []:
            if pr.get("number") is not None:
                prs.add(int(pr["number"]))
        for issue in ev.get("issues") or []:
            if issue.get("number") is not None:
                issues.add(int(issue["number"]))

    payload = {
        "shas": sorted(shas),
        "prs": sorted(prs),
        "issues": sorted(issues),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _compute_cache_key(
    *,
    target: dict,
    bundle_signature: str,
    model: str,
    prompt_version: str,
) -> str:
    """Composite key: target identity + bundle content + model + prompt version."""
    raw = "|".join(
        (_target_id(target), bundle_signature, model, prompt_version)
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@mcp.tool(
    name="whygraph_rationale_brief",
    description=(
        "Generate the 5-section rationale card (purpose/why/constraints/"
        "tradeoffs/risks + confidence) for a code chunk. Calls the local "
        "`claude` CLI; uses subscription billing unless anthropic_api_key is "
        "passed. Cached in the scan DB by (target + bundle content + model "
        "+ prompt version) — re-invocation on unchanged code is a "
        "sub-millisecond DB read. Pass force_refresh=True to bypass."
    ),
)
def whygraph_rationale_brief(
    path: str | None = None,
    line_start: int | None = None,
    line_end: int | None = None,
    qualified_name: str | None = None,
    min_score_pct: float = 0.5,
    model: str = DEFAULT_RATIONALE_MODEL,
    timeout_sec: int = DEFAULT_RATIONALE_TIMEOUT_SEC,
    anthropic_api_key: str | None = None,
    force_refresh: bool = False,
) -> dict:
    bundle = whygraph_evidence_for(
        path=path,
        line_start=line_start,
        line_end=line_end,
        qualified_name=qualified_name,
        min_score_pct=min_score_pct,
        limit=20,
    )
    target = bundle["target"]
    evidence = bundle["evidence"]

    bundle_signature = _compute_bundle_signature(evidence)
    cache_key = _compute_cache_key(
        target=target,
        bundle_signature=bundle_signature,
        model=model,
        prompt_version=_PROMPT_VERSION,
    )

    if not force_refresh:
        with db_module.Database(_resolve_db_path()) as db:
            cached = db.get_rationale_cache(cache_key)
        if cached is not None:
            return {
                "target": target,
                "purpose": cached["purpose"],
                "why": cached["why"],
                "constraints": cached["constraints"],
                "tradeoffs": cached["tradeoffs"],
                "risks": cached["risks"],
                "confidence": cached["confidence"],
                "evidence_count": {
                    "commits": len(evidence),
                    "prs": sum(len(ev.get("prs") or []) for ev in evidence),
                    "issues": sum(len(ev.get("issues") or []) for ev in evidence),
                },
                "model": cached["model"],
                "cached": True,
            }

    user_prompt = _build_rationale_user_prompt(
        target=target,
        evidence=evidence,
    )
    _llm_client = ClaudeCliAdapter(
        model=model,
        api_key=anthropic_api_key,
        timeout_sec=timeout_sec,
    )
    raw = _llm_client.complete(
        CompletionRequest.of(user_prompt, system=_RATIONALE_SYSTEM_PROMPT)
    ).text
    parsed = _extract_rationale_json(raw)
    rationale = _validate_rationale(parsed)
    confidence = _score_confidence(
        evidence=evidence,
        constraints=rationale["constraints"],
        risks=rationale["risks"],
    )

    with db_module.Database(_resolve_db_path()) as db:
        db.set_rationale_cache(
            cache_key=cache_key,
            target_qualified_name=target.get("qualified_name"),
            target_path=target.get("path"),
            target_line_start=target.get("line_start"),
            target_line_end=target.get("line_end"),
            bundle_signature=bundle_signature,
            model=model,
            prompt_version=_PROMPT_VERSION,
            purpose=rationale["purpose"],
            why=rationale["why"],
            constraints=rationale["constraints"],
            tradeoffs=rationale["tradeoffs"],
            risks=rationale["risks"],
            confidence=confidence,
        )

    return {
        "target": target,
        **rationale,
        "confidence": confidence,
        "cached": False,
        "evidence_count": {
            "commits": len(evidence),
            "prs": sum(len(ev.get("prs") or []) for ev in evidence),
            "issues": sum(len(ev.get("issues") or []) for ev in evidence),
        },
        "model": model,
    }


# ---------------------------------------------------------------------------
# Prompts (composition templates — wire tools into agent-friendly recipes)
# ---------------------------------------------------------------------------


@mcp.prompt(
    name="explain_change",
    description=(
        "Generate a 5-section pre-edit rationale for a code chunk. Calls "
        "whygraph_rationale_brief and presents the card with citations."
    ),
)
def prompt_explain_change(path: str, line_start: str, line_end: str) -> str:
    return (
        f"Use the whygraph_rationale_brief tool with path='{path}', "
        f"line_start={line_start}, line_end={line_end} and present the five "
        "sections (purpose, why, constraints, tradeoffs, risks) along with "
        "the confidence score. For each non-trivial claim, cite the "
        "supporting commit SHA, PR number, or issue number from the "
        "evidence_count breakdown. Do not invent details the rationale "
        "doesn't support."
    )


@mcp.prompt(
    name="debug_history",
    description=(
        "Find historical candidate causes for a bug symptom. Combines "
        "whygraph_search and (optionally) whygraph_evidence_for to surface "
        "commits/PRs/issues most likely to explain a regression."
    ),
)
def prompt_debug_history(
    symptom: str,
    hint_path: str = "",
    hint_line_start: str = "",
    hint_line_end: str = "",
) -> str:
    parts = [
        f'Investigate this bug symptom: "{symptom}".',
        "1. Call whygraph_search with the symptom keywords across all kinds.",
    ]
    if hint_path and hint_line_start and hint_line_end:
        parts.append(
            f"2. Call whygraph_evidence_for with path='{hint_path}', "
            f"line_start={hint_line_start}, line_end={hint_line_end} to "
            "surface the commits/PRs that touched that range."
        )
        parts.append(
            "3. Cross-reference: cluster results that mention the same SHAs, PRs, or issue numbers."
        )
    else:
        parts.append(
            "2. Cross-reference: cluster results by author, file, or shared PR/issue."
        )
    parts.append(
        "Output: a ranked list of candidate causes with SHA / PR / issue references "
        "and a one-sentence rationale per candidate. Do not speculate — only cite "
        "items returned by the tools."
    )
    return "\n".join(parts)


@mcp.prompt(
    name="team_pulse",
    description=(
        "Produce a project-velocity narrative combining per-author stats and "
        "hot path-prefixes for the chosen window."
    ),
)
def prompt_team_pulse(window_days: str = "30") -> str:
    return (
        f"Call whygraph_velocity_summary with window_days={window_days} and "
        "group_by='author', then call it again with group_by='path_prefix'. "
        "Produce a one-page narrative covering: top contributors (by window "
        "commits + files touched), days-since-last-commit per top author, "
        "and the hottest path-prefixes. Cite numbers from the tool output; "
        "do not estimate."
    )


@mcp.prompt(
    name="changelog",
    description=(
        "Produce a themed markdown changelog of merged PRs in a date "
        "window, optionally scoped to a path prefix."
    ),
)
def prompt_changelog(since: str, until: str = "now", scope: str = "") -> str:
    scope_arg = f", path_prefix={scope!r}" if scope else ""
    scope_line = (
        f"Scope: only PRs touching files under '{scope}'."
        if scope
        else "Scope: entire repo."
    )
    return "\n".join(
        [
            f'Build a changelog for the period since={since!r} until={until!r}.',
            scope_line,
            "",
            "1. Call whygraph_window with:",
            f"   since={since!r}, until={until!r}, "
            f"kinds=['pr'], state='merged'{scope_arg}, limit=200.",
            "2. Group merged PRs into themed buckets (e.g. 'Features', "
            "'Bug fixes', 'Refactors', 'Docs') based on their titles, "
            "labels, and narratives. Use ONLY information returned by "
            "the tool — do not infer themes from PR numbers alone.",
            "3. For each bucket, list PRs as `- #<n> <title> — <one-line "
            "rationale from the narrative>`. Cite the PR number verbatim.",
            "4. Add a closing line with totals: `N PRs across M themes`.",
            "Do not speculate. If a PR has no narrative, list it under "
            "'Other' with just the title.",
        ]
    )


@mcp.prompt(
    name="feature_timeline",
    description=(
        "Render a Mermaid `timeline` block showing merged PRs (features) "
        "and issues opened in a date window."
    ),
)
def prompt_feature_timeline(since: str, until: str = "now") -> str:
    return "\n".join(
        [
            f'Render a Mermaid timeline for since={since!r} until={until!r}.',
            "",
            "1. Call whygraph_window twice:",
            f"   - kinds=['pr'], state='merged', since={since!r}, "
            f"until={until!r}, limit=200",
            f"   - kinds=['issue'], since={since!r}, until={until!r}, "
            "limit=200",
            "2. Output a single fenced ```mermaid block containing a "
            "`timeline` diagram. Group entries by month (YYYY-MM):",
            "   timeline",
            "       title Feature timeline",
            "       2026-01 : PR #42 short title",
            "                : PR #43 short title",
            "       2026-02 : Issue #50 short title",
            "3. Use the PR's merged_at (or issue's created_at) for the "
            "month bucket. Truncate titles to 60 characters. Cite IDs "
            "verbatim.",
            "4. After the diagram, add a one-paragraph commentary on "
            "what was shipped vs what was raised. Cite numbers only "
            "from the tool output.",
        ]
    )


@mcp.prompt(
    name="user_profile",
    description=(
        "Per-user contribution profile (commits, PRs, areas owned, "
        "issues closed) over a date window."
    ),
)
def prompt_user_profile(identity: str, since: str, until: str = "now") -> str:
    return "\n".join(
        [
            f'Build a contribution profile for identity={identity!r} '
            f'between since={since!r} and until={until!r}.',
            "",
            "1. Call whygraph_window with:",
            f"   author={identity!r}, since={since!r}, until={until!r}, "
            "limit=500",
            "   This returns the user's commits, PRs, and issues in window. "
            "If the tool errors with 'did not resolve', tell the user to "
            "run `whygraph scan` and try a different spelling (login, "
            "email, or local-part).",
            "2. Call whygraph_velocity_summary with group_by='path_prefix' "
            "for repo-wide context on the hottest areas (gives the user's "
            "areas a baseline to compare against).",
            "3. Produce a markdown profile with these sections:",
            "   - **Activity**: total commits / PRs authored / issues "
            "raised in window.",
            "   - **Areas touched**: top 5 path prefixes derived from "
            "the commits' file paths or PR titles. Cite counts.",
            "   - **Highlights**: top 3 commits or PRs by narrative "
            "richness (longest llm_description / body). Quote one line "
            "per item.",
            "   - **Closing issues**: issues whose closing PR they "
            "authored in window (if any).",
            "Cite SHAs (short), PR numbers, issue numbers verbatim. Do "
            "not infer information not present in the tool output.",
        ]
    )


@mcp.prompt(
    name="whygraph_plan",
    description=(
        "Plan an implementation task using rationale cards. Composes "
        "whygraph_search → CodeGraph (or Explore agent) for symbol "
        "resolution → whygraph_rationale_brief per candidate symbol."
    ),
)
def prompt_whygraph_plan(task: str) -> str:
    return "\n".join(
        [
            f'Plan an implementation approach for this task: "{task}".',
            "",
            "1. Call whygraph_search with task keywords (kinds=['commit',"
            "'pr','issue'], limit=20). Note the file paths and symbols "
            "that surface most often — these are likely affected.",
            "2. Symbol resolution: WhyGraph does NOT do graph traversal. "
            "For each likely-affected file, use CodeGraph (codegraph_* "
            "tools) or Claude Code's Explore agent to identify the "
            "specific functions/classes/methods that match the task's "
            "intent. Collect their qualified_names.",
            "3. For each candidate qualified_name, call "
            "whygraph_rationale_brief(qualified_name=<...>) — cached "
            "calls are sub-millisecond. Capture the purpose / why / "
            "constraints / tradeoffs / risks per symbol.",
            "4. Produce the plan with these sections:",
            "   - **Affected symbols**: bulleted list of "
            "<qualified_name> @ <file>:<lines> with one-line purpose.",
            "   - **Rationale per symbol**: nested under each, the "
            "key constraints and risks (cite SHAs/#PRs from the cards).",
            "   - **Steps**: ordered, surgical change list. Each step "
            "names the symbol(s) it touches and the constraint it "
            "must preserve.",
            "   - **Open questions**: anything the cards didn't "
            "answer, framed as a question for the user.",
            "Refuse to write code. The output is a plan; implementation "
            "happens in a follow-up turn.",
        ]
    )


def main() -> None:
    configure_logging(get_config().log_level)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
