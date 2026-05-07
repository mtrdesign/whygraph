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

from whygraph import backend as backend_module
from whygraph import llm_subprocess, mcp_queries
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


def _node_to_dict(node: backend_module.SymbolNode) -> dict:
    return {
        "qualified_name": node.qualified_name,
        "kind": node.kind,
        "file_path": node.file_path,
        "start_line": node.start_line,
        "end_line": node.end_line,
        "signature": node.signature,
        "docstring": node.docstring,
    }


def _resolve_target(
    *,
    path: str | None,
    line_start: int | None,
    line_end: int | None,
    qualified_name: str | None,
) -> dict:
    """Validate and normalize a tool target; fetch neighbours on the same pass.

    Returns ``{path, line_start, line_end, qualified_name, callers, callees}``.
    ``callers`` / ``callees`` are populated only when ``qualified_name`` was
    provided (path+lines targeting has no graph node to anchor to).

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
        backend = backend_module.SqliteCodegraphBackend(cg_path)
        try:
            node = backend.get_node(qualified_name)
            if node is None:
                raise WhyGraphError(
                    f"qualified_name {qualified_name!r} not found in CodeGraph"
                )
            callers = [_node_to_dict(n) for n in backend.get_callers(node.id)]
            callees = [_node_to_dict(n) for n in backend.get_callees(node.id)]
        finally:
            backend.close()
        return {
            "path": node.file_path,
            "line_start": node.start_line,
            "line_end": node.end_line,
            "qualified_name": qualified_name,
            "callers": callers,
            "callees": callees,
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
        "callers": [],
        "callees": [],
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


_NEIGHBOUR_EVIDENCE_LIMIT = 3


def _enrich_neighbour(
    *,
    db: db_module.Database,
    repo_root: Path,
    node_dict: dict,
    gate: ValueGate,
    limit: int = _NEIGHBOUR_EVIDENCE_LIMIT,
) -> dict:
    """Attach top-N evidence items to a neighbour node dict.

    Runs blame on the neighbour's full span (start_line..end_line as
    given by CodeGraph) and reuses ``_build_evidence_item`` per SHA so
    neighbours carry the same shape as the target's evidence list.
    Sorted by ``committed_at`` desc, capped at ``limit``.

    Best-effort: an empty list is returned when blame fails (file
    missing, renamed, binary), so the neighbour itself stays in the
    bundle even if its history can't be resolved.
    """
    out = dict(node_dict)
    out["evidence"] = []
    file_path = node_dict.get("file_path")
    start = node_dict.get("start_line")
    end = node_dict.get("end_line")
    if not file_path or not start or not end:
        return out
    try:
        blame = mcp_queries.blame_line_range(repo_root, file_path, start, end)
    except git_module.GitError:
        return out
    if not blame:
        return out
    items = [
        _build_evidence_item(db, sha=sha, blame_entry=entry, gate=gate)
        for sha, entry in blame.items()
    ]
    items.sort(key=lambda it: it.get("committed_at") or "", reverse=True)
    out["evidence"] = items[:limit]
    return out


@mcp.tool(
    name="whygraph_evidence_for",
    description=(
        "Find historical evidence (commits + PRs + closing issues) and graph "
        "neighbours (callers/callees) for a chunk of code. Pass either "
        "(path, line_start, line_end) or a qualified_name (resolved via "
        "CodeGraph). Returns {target, evidence, callers, callees}. Evidence "
        "is filtered by TF-IDF harshness; commit narrative prefers "
        "llm_description, then body, then subject. When qualified_name is "
        "given, each caller/callee is enriched with its own top-3 commits "
        "by recency (same evidence shape as the target). Run `whygraph "
        "scan` first to populate llm_descriptions — this tool does not "
        "call the LLM."
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
    callers = target["callers"]
    callees = target["callees"]
    if blame or callers or callees:
        with db_module.Database(_resolve_db_path()) as db:
            gate = ValueGate.percentile(db, fraction=min_score_pct)
            for sha, blame_entry in (blame or {}).items():
                items.append(
                    _build_evidence_item(
                        db, sha=sha, blame_entry=blame_entry, gate=gate
                    )
                )
            callers = [
                _enrich_neighbour(
                    db=db, repo_root=repo_root, node_dict=n, gate=gate
                )
                for n in callers
            ]
            callees = [
                _enrich_neighbour(
                    db=db, repo_root=repo_root, node_dict=n, gate=gate
                )
                for n in callees
            ]
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
        "callers": callers,
        "callees": callees,
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


_RATIONALE_SYSTEM_PROMPT = """\
You are an analyst that explains why code exists, not just what it does. Given a code symbol's location and a bundle of evidence (commits, blame data, linked PRs and issues, plus direct callers and callees), generate a structured rationale grounded strictly in that bundle.

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

Graph neighbours (callers / callees):
- The `Callers` section lists nodes that depend on the target. Use it to populate `risks` with concrete blast-radius items: "modifying X breaks <caller.qualified_name> at <caller.file_path>:<caller.start_line>".
- The `Callees` section lists nodes the target depends on. Use it to populate `constraints` only when a real contract is visible (e.g. the target relies on a specific callee signature). Do not fabricate contracts from a callee just because it exists.
- Each neighbour carries `Recent commits` (top 3 by recency) with the same narrative shape as the target's commits. Use that history to enrich risk/constraint statements: "modifying X breaks <caller> (last changed in PR #<n> to <reason>; preserve <invariant>)".
- Cite neighbour signatures and docstrings verbatim when they clarify the contract.
- Only cite SHAs / PR numbers / issue numbers that appear in the bundle — either in the target's evidence or in a neighbour's `Recent commits`. Do not infer additional callers from type names, and do not invent neighbour history.

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


def _format_neighbour_block(neighbour: dict, label: str) -> list[str]:
    qn = neighbour.get("qualified_name") or "?"
    file_path = neighbour.get("file_path") or "?"
    start = neighbour.get("start_line")
    end = neighbour.get("end_line")
    location = f"{file_path}:{start}-{end}" if start and end else file_path
    lines = [f"  {qn}  {location}"]
    sig = neighbour.get("signature")
    if sig:
        lines.append(f"    Signature: {sig}")
    doc = neighbour.get("docstring")
    if doc:
        lines.append("    Docstring:")
        for doc_line in doc.splitlines():
            lines.append(f"      {doc_line.rstrip()}")
    evidence = neighbour.get("evidence") or []
    if evidence:
        lines.append("    Recent commits:")
        for ev in evidence:
            lines.append("")
            lines.extend(_format_evidence_block(ev, "      "))
    return lines


def _build_rationale_user_prompt(
    *,
    target: dict,
    evidence: list[dict],
    callers: list[dict],
    callees: list[dict],
) -> str:
    """Render the evidence bundle as a structured text document.

    Sections in order: target header, evidence-count summary, commits
    (newest first, with linked PRs and issues nested per commit), then
    callers and callees (each with recent commits). The model reads
    headers as editorial cues, so order matters.
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
        f"{issue_count} issue(s), {len(callers)} caller(s), "
        f"{len(callees)} callee(s)."
    )

    if evidence:
        lines.append("")
        lines.append("Commits (newest first):")
        for ev in evidence:
            lines.append("")
            lines.extend(_format_evidence_block(ev, "  "))

    if callers:
        lines.append("")
        target_qn = target.get("qualified_name") or "the target"
        lines.append(f"Callers ({len(callers)} — symbols that call {target_qn}):")
        for c in callers:
            lines.append("")
            lines.extend(_format_neighbour_block(c, "caller"))

    if callees:
        lines.append("")
        target_qn = target.get("qualified_name") or "the target"
        lines.append(f"Callees ({len(callees)} — symbols called by {target_qn}):")
        for c in callees:
            lines.append("")
            lines.extend(_format_neighbour_block(c, "callee"))

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
_PROMPT_VERSION = "v2"


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


def _compute_bundle_signature(
    evidence: list[dict],
    callers: list[dict],
    callees: list[dict],
) -> str:
    """Hash the *content* of the evidence bundle.

    Captures every identifier the rationale could legitimately depend
    on: target commit SHAs, linked PR/issue numbers, neighbour commit
    SHAs, neighbour qualified_names. When any of those change (a new
    commit lands on the lines, a caller is added, etc.), the signature
    flips and the cache misses.

    Sorted before hashing so the signature is stable regardless of how
    evidence was ordered in the bundle.
    """

    def _evidence_ids(evs: list[dict]) -> tuple[set[str], set[int], set[int]]:
        shas: set[str] = set()
        prs: set[int] = set()
        issues: set[int] = set()
        for ev in evs:
            sha = ev.get("sha")
            if sha:
                shas.add(sha)
            for pr in ev.get("prs") or []:
                if pr.get("number") is not None:
                    prs.add(int(pr["number"]))
            for issue in ev.get("issues") or []:
                if issue.get("number") is not None:
                    issues.add(int(issue["number"]))
        return shas, prs, issues

    shas, prs, issues = _evidence_ids(evidence)
    caller_qns: set[str] = set()
    callee_qns: set[str] = set()
    for n in callers:
        if n.get("qualified_name"):
            caller_qns.add(n["qualified_name"])
        n_shas, n_prs, n_issues = _evidence_ids(n.get("evidence") or [])
        shas |= n_shas
        prs |= n_prs
        issues |= n_issues
    for n in callees:
        if n.get("qualified_name"):
            callee_qns.add(n["qualified_name"])
        n_shas, n_prs, n_issues = _evidence_ids(n.get("evidence") or [])
        shas |= n_shas
        prs |= n_prs
        issues |= n_issues

    payload = {
        "shas": sorted(shas),
        "prs": sorted(prs),
        "issues": sorted(issues),
        "callers": sorted(caller_qns),
        "callees": sorted(callee_qns),
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
    callers = bundle["callers"]
    callees = bundle["callees"]

    bundle_signature = _compute_bundle_signature(evidence, callers, callees)
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
                    "callers": len(callers),
                    "callees": len(callees),
                },
                "model": cached["model"],
                "cached": True,
            }

    user_prompt = _build_rationale_user_prompt(
        target=target,
        evidence=evidence,
        callers=callers,
        callees=callees,
    )
    raw = llm_subprocess.invoke_claude(
        user_prompt,
        model=model,
        timeout_sec=timeout_sec,
        anthropic_api_key=anthropic_api_key,
        system_prompt=_RATIONALE_SYSTEM_PROMPT,
    )
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
            "callers": len(callers),
            "callees": len(callees),
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


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
