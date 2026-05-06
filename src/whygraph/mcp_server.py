"""WhyGraph MCP server: resources/tools/prompts over the scan DB.

The scan DB at ``<repo_root>/.whygraph/whygraph.db`` is the single source of
truth. All resources/tools open it read-only per call. The CodeGraph DB
(used for symbol-name resolution) is opened lazily by tools that need it.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from concurrent.futures import ThreadPoolExecutor, as_completed

from whygraph import backend as backend_module
from whygraph import llm_subprocess, mcp_queries
from whygraph.llm_subprocess import LlmError
from whygraph.scan import db as db_module
from whygraph.scan import git as git_module
from whygraph.scan import llm_descriptions as scan_llm
from whygraph.scan.scoring import ValueGate

DEFAULT_RATIONALE_MODEL = "claude-opus-4-7"
DEFAULT_RATIONALE_TIMEOUT_SEC = 180
DEFAULT_LAZY_FILL_LIMIT = 3
DEFAULT_LAZY_FILL_MODEL = scan_llm.DEFAULT_MODEL
DEFAULT_LAZY_FILL_WORKERS = 4
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
        closing = [
            _hydrate_issue(i) for i in mcp_queries.closing_issues_for_pr(db, n)
        ]
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


def _lazy_describe_one(
    repo_root: Path, sha: str, next_sha: str, config: scan_llm.LlmConfig
) -> str | None:
    """Worker: compute the diff and describe it. None on any error."""
    try:
        diff = scan_llm.get_pair_diff(repo_root, sha, next_sha)
        return scan_llm.describe_pair(diff, config)
    except (git_module.GitError, LlmError):
        return None


def _lazy_fill_descriptions(
    db: db_module.Database,
    repo_root: Path,
    blame_entries: dict[str, dict],
    *,
    limit: int,
    model: str,
    anthropic_api_key: str | None,
) -> int:
    """Fill ``commits.llm_description`` for up to ``limit`` blame SHAs that lack one.

    Selection rules:
    - SHA must exist in the scan DB (blame-only / stale-DB rows are skipped;
      ``set_llm_description`` would no-op silently otherwise).
    - SHA must currently have ``llm_description IS NULL``.
    - SHA must have a successor on the first-parent walk (tip-of-branch
      commits have no diff partner; leave them NULL forever).
    - Sorted by ``blame_lines`` descending — fill the SHAs that own the
      most of the requested range first, since they're the highest-impact
      narratives for this query.

    Skips silently if the ``claude`` CLI isn't installed. Per-SHA errors
    are dropped (the next call will retry), matching scan's run_phase
    behavior. Returns the number of SHAs successfully filled.
    """
    if limit <= 0 or not blame_entries:
        return 0
    if not scan_llm.claude_cli_available():
        return 0

    try:
        branch = git_module._run_git(
            repo_root, ["rev-parse", "--abbrev-ref", "HEAD"]
        ).strip()
    except git_module.GitError:
        return 0
    walked = list(git_module.walk_first_parent(repo_root, branch))
    if len(walked) < 2:
        return 0
    sha_to_next = dict(zip(walked[:-1], walked[1:], strict=True))

    # Highest-blame-impact SHAs first.
    ordered_shas = [
        sha
        for sha, _ in sorted(
            blame_entries.items(),
            key=lambda kv: kv[1].get("lines_owned", 0),
            reverse=True,
        )
    ]
    candidates = [sha for sha in ordered_shas if sha in sha_to_next]
    if not candidates:
        return 0
    needs = db.commits_without_llm_description(candidates)
    fillable = [sha for sha in candidates if sha in needs][:limit]
    if not fillable:
        return 0

    config = scan_llm.LlmConfig(model=model, anthropic_api_key=anthropic_api_key)
    workers = min(len(fillable), DEFAULT_LAZY_FILL_WORKERS)
    filled = 0
    with ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="whygraph-lazy-llm"
    ) as ex:
        futures = {
            ex.submit(_lazy_describe_one, repo_root, sha, sha_to_next[sha], config): sha
            for sha in fillable
        }
        for fut in as_completed(futures):
            sha = futures[fut]
            description = fut.result()
            if description is None:
                continue
            db.set_llm_description(sha, description, model)
            filled += 1
    return filled


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
        return {
            "sha": sha,
            "narrative": blame_entry.get("summary"),
            "narrative_source": "git_blame_summary"
            if blame_entry.get("summary")
            else None,
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
    # If the commit has neither an llm_description nor a body/subject above
    # the gate, narrative is None — but we still surface the entry. The
    # blame `lines_owned` is itself signal (matches how PRs and issues are
    # surfaced even when their own narratives fail the gate).
    narrative, source = mcp_queries.commit_narrative(commit, gate)

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
        narrative_pr, source_pr = mcp_queries.pr_narrative(pr, gate)
        pr_entry = {
            "number": pr["number"],
            "narrative": narrative_pr,
            "narrative_source": source_pr,
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
            narrative_issue, source_issue = mcp_queries.issue_narrative(issue, gate)
            issues_collected[issue["number"]] = {
                "number": issue["number"],
                "narrative": narrative_issue,
                "narrative_source": source_issue,
                "author": issue.get("author"),
                "html_url": issue.get("html_url"),
            }
            if issue.get("author"):
                _add_author(issue["author"], None, None)

    return {
        "sha": sha,
        "narrative": narrative,
        "narrative_source": source,
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
        "Find historical evidence (commits + PRs + closing issues) and graph "
        "neighbours (callers/callees) for a chunk of code. Pass either "
        "(path, line_start, line_end) or a qualified_name (resolved via "
        "CodeGraph). Returns {target, evidence, callers, callees}. Evidence "
        "is filtered by TF-IDF harshness; commit narrative prefers "
        "llm_description, then body, then subject. Callers/callees are "
        "populated only when qualified_name is given. Up to "
        "`lazy_fill_limit` blame SHAs missing an llm_description will be "
        "filled on the fly via the local `claude` CLI (highest-blame-impact "
        "first); set 0 to disable."
    ),
)
def whygraph_evidence_for(
    path: str | None = None,
    line_start: int | None = None,
    line_end: int | None = None,
    qualified_name: str | None = None,
    min_score_pct: float = 0.5,
    limit: int = 10,
    lazy_fill_limit: int = DEFAULT_LAZY_FILL_LIMIT,
    lazy_fill_model: str = DEFAULT_LAZY_FILL_MODEL,
    anthropic_api_key: str | None = None,
) -> dict:
    if not 0.0 <= min_score_pct <= 1.0:
        raise WhyGraphError("min_score_pct must be in [0, 1]")
    if limit < 1:
        raise WhyGraphError("limit must be ≥ 1")
    if lazy_fill_limit < 0:
        raise WhyGraphError("lazy_fill_limit must be ≥ 0 (0 disables)")

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
    lazy_filled = 0
    if blame:
        with db_module.Database(_resolve_db_path()) as db:
            lazy_filled = _lazy_fill_descriptions(
                db,
                repo_root,
                blame,
                limit=lazy_fill_limit,
                model=lazy_fill_model,
                anthropic_api_key=anthropic_api_key,
            )
            gate = ValueGate.percentile(db, fraction=min_score_pct)
            for sha, blame_entry in blame.items():
                items.append(
                    _build_evidence_item(
                        db, sha=sha, blame_entry=blame_entry, gate=gate
                    )
                )
        items.sort(key=lambda it: (it.get("committed_at") or ""), reverse=True)
        items = items[:limit]

    return {
        "target": {
            "path": target["path"],
            "line_start": target["line_start"],
            "line_end": target["line_end"],
            "qualified_name": target["qualified_name"],
        },
        "evidence": items,
        "callers": target["callers"],
        "callees": target["callees"],
        "lazy_filled": lazy_filled,
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
You are a deterministic JSON producer. You receive a code-rationale evidence bundle and emit ONE JSON object — nothing else.

You CANNOT read files, run tools, search the codebase, or access anything beyond the evidence bundle the user provides. The bundle is your COMPLETE input. Do not request more information; do not narrate what you would check; do not propose tool calls.

Output schema (these top-level keys, no others):
{
  "purpose":     <string>  // one sentence stating what the code does today
  "why":         <string>  // one paragraph of historical/contextual rationale drawn from commits/PRs/issues
  "constraints": <array of strings>  // invariants/contracts the next editor must preserve
  "tradeoffs":   <array of strings>  // notable design decisions visible in evidence
  "risks":       <array of strings>  // risks of modifying this code
}

If the evidence bundle is sparse:
- "purpose" and "why" become short, factual sentences citing only what the bundle contains. If the bundle has zero entries, "why" may say "No evidence available in the scan."
- "constraints", "tradeoffs", "risks" become EMPTY ARRAYS [].
- NEVER substitute prose explaining what you would need. The output is JSON, period.

Evidence rules:
- Prefer commit narratives where narrative_source == "llm_description" — those are verbatim diff summaries and outrank human-written subjects/bodies.
- When narrative_source == "git_blame_summary", db_commit_present is false: the SHA exists in git but not in the scan DB. Still cite it; just don't claim PR/issue context for it.
- Use exact identifiers (file paths, function/class names) verbatim; do not paraphrase.
- No hedging ("seems", "may", "appears"). No invented rationale.

Graph neighbours (callers / callees):
- "callers" lists nodes that depend on the target. Use them to populate "risks" with concrete blast-radius items: "modifying X breaks <caller.qualified_name> at <caller.file_path>:<caller.start_line>".
- "callees" lists nodes the target depends on. Use them to populate "constraints" only when a real contract is visible (e.g. the target relies on <callee.qualified_name>'s signature). Do not fabricate contracts from a callee just because it exists.
- Only cite callers/callees that the bundle explicitly contains. Do not infer additional callers from imports or type names.

Output format: RAW JSON only. No prose, no code fences, no preamble, no trailing remarks. The first character of your output MUST be '{' and the last character MUST be '}'.
"""


def _build_rationale_user_prompt(
    *,
    target: dict,
    evidence: list[dict],
    callers: list[dict],
    callees: list[dict],
) -> str:
    payload = {
        "target": target,
        "evidence": evidence,
        "callers": callers,
        "callees": callees,
    }
    return (
        "Produce the rationale JSON for this target.\n\n"
        f"{json.dumps(payload, indent=2, default=str)}\n"
    )


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
            raise WhyGraphError(f"rationale: '{key}' must be a string, got {type(val).__name__}")
        out[key] = val
    for key in required_lists:
        val = payload.get(key)
        if val is None:
            out[key] = []
            continue
        if not isinstance(val, list):
            raise WhyGraphError(f"rationale: '{key}' must be a list, got {type(val).__name__}")
        if not all(isinstance(item, str) for item in val):
            raise WhyGraphError(f"rationale: '{key}' must contain strings only")
        out[key] = val
    return out


_NARRATIVE_TIER: dict[str | None, float] = {
    "llm_description": 1.0,    # verbatim diff summary — highest signal
    "body": 0.5,               # human-written body above the gate
    "subject": 0.5,            # human-written subject above the gate
    "git_blame_summary": 0.25, # SHA missing from scan DB; only blame metadata
    None: 0.0,                 # narrative failed the gate; only structural data
}


def _evidence_tier(ev: dict) -> float:
    return _NARRATIVE_TIER.get(ev.get("narrative_source"), 0.0)


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


@mcp.tool(
    name="whygraph_rationale_brief",
    description=(
        "Generate the 5-section rationale card (purpose/why/constraints/"
        "tradeoffs/risks + confidence) for a code chunk. Calls the local "
        "`claude` CLI; uses subscription billing unless anthropic_api_key is "
        "passed. Lazy: no caching."
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
    lazy_fill_limit: int = DEFAULT_LAZY_FILL_LIMIT,
    lazy_fill_model: str = DEFAULT_LAZY_FILL_MODEL,
) -> dict:
    bundle = whygraph_evidence_for(
        path=path,
        line_start=line_start,
        line_end=line_end,
        qualified_name=qualified_name,
        min_score_pct=min_score_pct,
        limit=20,
        lazy_fill_limit=lazy_fill_limit,
        lazy_fill_model=lazy_fill_model,
        anthropic_api_key=anthropic_api_key,
    )
    target = bundle["target"]
    evidence = bundle["evidence"]
    callers = bundle["callers"]
    callees = bundle["callees"]
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
    return {
        "target": target,
        **rationale,
        "confidence": confidence,
        "evidence_count": {
            "commits": len(evidence),
            "prs": sum(len(ev.get("prs") or []) for ev in evidence),
            "issues": sum(len(ev.get("issues") or []) for ev in evidence),
            "callers": len(callers),
            "callees": len(callees),
        },
        "lazy_filled": bundle.get("lazy_filled", 0),
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
def prompt_explain_change(
    path: str, line_start: str, line_end: str
) -> str:
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
        f"Investigate this bug symptom: \"{symptom}\".",
        "1. Call whygraph_search with the symptom keywords across all kinds.",
    ]
    if hint_path and hint_line_start and hint_line_end:
        parts.append(
            f"2. Call whygraph_evidence_for with path='{hint_path}', "
            f"line_start={hint_line_start}, line_end={hint_line_end} to "
            "surface the commits/PRs that touched that range."
        )
        parts.append("3. Cross-reference: cluster results that mention the same SHAs, PRs, or issue numbers.")
    else:
        parts.append("2. Cross-reference: cluster results by author, file, or shared PR/issue.")
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
