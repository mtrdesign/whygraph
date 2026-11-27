"""Plan 62 spike D0: measure the call-site ring (section 15, Part D).

Read-only and offline (no LLM call, no scan, no portal, no GitHub). For a
seeded mechanical sample of target symbols (functions / methods with at least
two distinct callers in other files) it follows the section 15 design sketch:
up to ``IMPACT_CALL_SITES`` distinct caller symbols per target (dedupe by
caller node; other files first; then the newest ``commit_file_change`` date of
the caller's file), ``git blame`` of the call line (``edges.line``) at HEAD,
the blamed commit, and what a card would render for it (header, the stored raw
description, the PR title). A call line whose text no longer contains the
target's bare name is skipped and counted.

It then writes a judging sheet for the owner (or an independent judge) with a
verdict line per call site, and a "Measures" section on top.

Usage (always pass COPIES of the databases, never one a running portal holds;
the checkout is only read through ``git``)::

    uv run --no-sync python scripts/call_site_spike.py \\
        --repo axios=ROOT:WDB.db:CDB.db --repo whygraph=ROOT:WDB.db:CDB.db \\
        --out plans/62-eval/d0-call-sites.md --json measures.json

Sampling rules (all mechanical, seeded):

* targets: ``function`` / ``method`` nodes whose bare name has at least
  ``MIN_NAME`` characters, outside test paths, with at least two distinct
  caller nodes (``calls`` edges) living in files other than the target's;
* the candidates are shuffled with ``--seed``; targets are taken in that order
  until ``--per-repo`` call sites are judged; each target contributes its first
  ``--sites-per-target`` rendered call sites in ring order.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sqlite3
import statistics
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

IMPACT_CALL_SITES = 5
MIN_NAME = 4
MAX_DESCRIBED_POOL = 50
# Names that say nothing about what is called (or that only tests use): a target or a
# caller with one of these bare names is skipped and counted.
GENERIC_NAMES = frozenset(
    "test tests it describe main new get set run init default clone from into fmt eq len "
    "next drop hash parse build create update add push pop map call apply handle then "
    "done close open read write send start stop reset setup teardown before after each "
    "all any value values keys key size name type id data item items list find has is "
    "to_string as_str unwrap expect iter".split()
)
TEST_NAME = re.compile(
    r"^(test|tests|it|describe|spec|mock|fixture|bench)([_A-Z0-9]|$)|([_a-z])(test|tests|spec)$"
)
TEST_PATH = re.compile(
    r"(^|/)(tests?|__tests__|__mocks__|spec|specs|e2e)(/|$)|(^|/)test_|[._]test\.|[._]spec\."
    r"|(^|/)testing\.|test_?utils|_tests?\.|/testdata/"
)
BODY_CHARS = 700
DESC_CHARS = 1200


@dataclass
class Site:
    """One rendered call site (a card item) and what was learned for it."""

    caller_id: str
    caller_name: str
    caller_path: str
    line: int
    call_line: str
    context: list[str]
    sha: str
    subject: str
    body: str
    date: str
    description: str | None
    pr_number: int | None
    pr_title: str | None
    rendered: str
    blame_ms: float
    same_as_target: bool = False


@dataclass
class Target:
    """A sampled target symbol and the call sites examined for it."""

    node_id: str
    name: str
    qualified: str
    path: str
    start: int
    end: int
    signature: str
    callers_total: int
    target_shas: set[str] = field(default_factory=set)
    sites: list[Site] = field(default_factory=list)
    judged: list[Site] = field(default_factory=list)
    examined: int = 0
    skipped_text: int = 0
    skipped_null_line: int = 0
    skipped_other: int = 0
    null_edges: int = 0
    edges_total: int = 0
    unresolved_rows: int = 0
    unresolved_callers: int = 0
    skipped_generic_callers: int = 0
    skipped_undescribed: int = 0


@dataclass
class Repo:
    """One repo under measurement: a checkout plus copies of both databases."""

    label: str
    root: Path
    wdb: sqlite3.Connection
    cdb: sqlite3.Connection
    head: str = ""
    targets: list[Target] = field(default_factory=list)
    calls_edges: int = 0
    calls_null: int = 0
    commits: int = 0
    described: int = 0
    candidates: int = 0
    skipped_generic_targets: int = 0
    mode: str = "plain"
    plain_undescribed: tuple[int, int] = (0, 0)
    _files: dict[str, list[str] | None] = field(default_factory=dict)
    _dates: dict[str, str | None] = field(default_factory=dict)


def git(root: Path, *args: str) -> str:
    """Run a read-only git command in ``root`` and return stdout."""
    out = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
        encoding="utf-8",
        errors="replace",
    )
    return out.stdout


def file_lines(repo: Repo, path: str) -> list[str] | None:
    """Return the lines of ``path`` at HEAD, or None when it is not there."""
    if path not in repo._files:
        proc = subprocess.run(
            ["git", "-C", str(repo.root), "show", f"HEAD:{path}"],
            capture_output=True,
            check=False,
        )
        repo._files[path] = (
            proc.stdout.decode("utf-8", "replace").split("\n")
            if proc.returncode == 0
            else None
        )
    return repo._files[path]


def blame_lines(
    repo: Repo, path: str, start: int, end: int
) -> dict[int, tuple[str, str]]:
    """Blame ``start..end`` at HEAD: final line number -> (sha, line text)."""
    text = git(
        repo.root, "blame", "--porcelain", "-L", f"{start},{end}", "HEAD", "--", path
    )
    result: dict[int, tuple[str, str]] = {}
    sha, final = "", 0
    for raw in text.split("\n"):
        if raw.startswith("\t"):
            result[final] = (sha, raw[1:])
        else:
            parts = raw.split(" ")
            if len(parts) >= 3 and re.fullmatch(r"[0-9a-f]{40}", parts[0]):
                sha, final = parts[0], int(parts[2])
    return result


def newest_date(repo: Repo, path: str) -> str | None:
    """Newest ``commit_file_change`` date of ``path`` (DB only, no git log)."""
    if path not in repo._dates:
        row = repo.wdb.execute(
            'SELECT MAX(c.authored_at) FROM commit_file_change f JOIN "commit" c '
            "ON c.sha = f.commit_sha WHERE f.path = ?",
            (path,),
        ).fetchone()
        repo._dates[path] = row[0]
    return repo._dates[path]


def pr_for(repo: Repo, sha: str) -> tuple[int | None, str | None]:
    """Return the PR (number, title) whose merge or head SHA is ``sha``."""
    row = repo.wdb.execute(
        "SELECT number, title FROM pull_request WHERE merge_commit_sha = ? OR head_sha = ? "
        "ORDER BY (merge_commit_sha = ?) DESC, number LIMIT 1",
        (sha, sha, sha),
    ).fetchone()
    return (row[0], row[1]) if row else (None, None)


def render(
    caller: str,
    path: str,
    line: int,
    sha: str,
    date: str,
    subject: str,
    desc: str | None,
    pr_title: str | None,
    pr_number: int | None,
) -> str:
    """Render a call site the way a card item would carry it (compact)."""
    parts = [
        f"CALL SITE in {caller} ({path}:{line})",
        f"{sha[:7]} {date[:10]} {subject}",
    ]
    if desc:
        parts.append(desc)
    if pr_title:
        parts.append(f"PR #{pr_number}: {pr_title}")
    return "\n".join(parts)


def generic_name(name: str) -> bool:
    """Return True for a generic or test-only bare name."""
    bare = name.split("::")[-1].split(".")[-1]
    return bare.lower() in GENERIC_NAMES or bool(TEST_NAME.search(bare))


def candidates(repo: Repo) -> list[sqlite3.Row]:
    """Return every target that qualifies (rows ordered by node id)."""
    rows = repo.cdb.execute(
        """
        SELECT t.id, t.name, t.qualified_name, t.file_path, t.start_line, t.end_line,
               COALESCE(t.signature, '') AS signature, COUNT(DISTINCT e.source) AS callers
        FROM edges e
        JOIN nodes s ON s.id = e.source
        JOIN nodes t ON t.id = e.target
        WHERE e.kind = 'calls' AND t.kind IN ('function', 'method')
          AND s.file_path != t.file_path AND length(t.name) >= ?
        GROUP BY t.id HAVING COUNT(DISTINCT e.source) >= 2
        ORDER BY t.id
        """,
        (MIN_NAME,),
    ).fetchall()
    rows = [r for r in rows if not TEST_PATH.search(r["file_path"])]
    kept = [r for r in rows if not generic_name(r["name"])]
    repo.skipped_generic_targets = len(rows) - len(kept)
    return kept


def unresolved_for(repo: Repo, name: str) -> tuple[int, int]:
    """Count unresolved ``calls`` references whose name is ``name``."""
    row = repo.cdb.execute(
        "SELECT COUNT(*), COUNT(DISTINCT from_node_id) FROM unresolved_refs "
        "WHERE reference_kind = 'calls' AND (reference_name = ? OR name_tail = ? "
        "OR reference_name LIKE ?)",
        (name, name, f"%.{name}"),
    ).fetchone()
    return row[0], row[1]


def examine(repo: Repo, row: sqlite3.Row, described_only: bool = False) -> Target:
    """Build the ring for one target: rank callers, blame call lines, render."""
    tgt = Target(
        node_id=row["id"],
        name=row["name"],
        qualified=row["qualified_name"],
        path=row["file_path"],
        start=row["start_line"],
        end=row["end_line"],
        signature=row["signature"],
        callers_total=row["callers"],
    )
    tgt.unresolved_rows, tgt.unresolved_callers = unresolved_for(repo, tgt.name)
    edges = repo.cdb.execute(
        "SELECT e.source, e.line, s.qualified_name, s.file_path, s.kind FROM edges e "
        "JOIN nodes s ON s.id = e.source WHERE e.kind = 'calls' AND e.target = ? "
        "ORDER BY e.source, (e.line IS NULL), e.line",
        (tgt.node_id,),
    ).fetchall()
    tgt.edges_total = len(edges)
    tgt.null_edges = sum(1 for e in edges if e["line"] is None)
    by_caller: dict[str, sqlite3.Row] = {}
    for e in edges:
        by_caller.setdefault(e["source"], e)  # dedupe by caller node, first line
    usable = {
        k: e
        for k, e in by_caller.items()
        if not generic_name(e["qualified_name"])
        and not TEST_PATH.search(e["file_path"])
    }
    tgt.skipped_generic_callers = len(by_caller) - len(usable)
    by_caller = usable
    if sum(1 for e in by_caller.values() if e["file_path"] != tgt.path) < 2:
        return tgt  # fewer than two usable callers in other files: no ring
    # newest file activity first (no date last), then other files before the target's own
    ranked = sorted(by_caller.values(), key=lambda e: e["source"])
    ranked.sort(key=lambda e: newest_date(repo, e["file_path"]) or "", reverse=True)
    ranked.sort(key=lambda e: e["file_path"] == tgt.path)
    target_blame = blame_lines(repo, tgt.path, tgt.start, tgt.end)
    tgt.target_shas = {sha for sha, _ in target_blame.values()}
    for e in ranked[:IMPACT_CALL_SITES]:
        tgt.examined += 1
        if e["line"] is None:
            tgt.skipped_null_line += 1
            continue
        lines = file_lines(repo, e["file_path"])
        if lines is None or e["line"] > len(lines):
            tgt.skipped_other += 1
            continue
        began = time.perf_counter()
        blamed = blame_lines(repo, e["file_path"], e["line"], e["line"])
        ms = (time.perf_counter() - began) * 1000
        if e["line"] not in blamed:
            tgt.skipped_other += 1
            continue
        sha, text = blamed[e["line"]]
        if tgt.name not in text:
            tgt.skipped_text += 1
            continue
        commit = repo.wdb.execute(
            'SELECT subject, body, authored_at, llm_description FROM "commit" WHERE sha = ?',
            (sha,),
        ).fetchone()
        subject, body, date, desc = (
            (
                commit["subject"],
                commit["body"],
                commit["authored_at"],
                commit["llm_description"],
            )
            if commit
            else ("(commit not in the WhyGraph DB)", "", "", None)
        )
        if described_only and not desc:
            tgt.skipped_undescribed += 1
            continue
        number, title = pr_for(repo, sha)
        ln = e["line"]
        tgt.sites.append(
            Site(
                caller_id=e["source"],
                caller_name=e["qualified_name"],
                caller_path=e["file_path"],
                line=ln,
                call_line=text,
                context=[
                    f"{'>' if i == ln else ' '} {i:>5}  {lines[i - 1]}"
                    for i in range(max(1, ln - 1), min(len(lines), ln + 1) + 1)
                ],
                sha=sha,
                subject=subject,
                body=body,
                date=date,
                description=desc or None,
                pr_number=number,
                pr_title=title,
                rendered=render(
                    e["qualified_name"],
                    e["file_path"],
                    ln,
                    sha,
                    date,
                    subject,
                    desc,
                    title,
                    number,
                ),
                blame_ms=ms,
                same_as_target=sha in tgt.target_shas,
            )
        )
    return tgt


def sample(
    repo: Repo,
    seed: int,
    per_repo: int,
    per_target: int,
    described_only: bool = False,
    pool: int = MAX_DESCRIBED_POOL,
) -> None:
    """Fill ``repo.targets`` with a seeded sample worth ``per_repo`` sites.

    With ``described_only`` only call sites whose commit has a stored description are
    rendered, and only the first ``MAX_DESCRIBED_POOL`` seeded candidates are tried.
    """
    repo.targets = []
    cands = candidates(repo)
    repo.candidates = len(cands)
    random.Random(seed).shuffle(cands)
    if described_only:
        cands = cands[:pool]
        repo.mode = f"described-only (first {pool} seeded candidates)"
    judged = 0
    for row in cands:
        if judged >= per_repo:
            break
        if file_lines(repo, row["file_path"]) is None:
            continue
        tgt = examine(repo, row, described_only)
        if not tgt.sites:
            continue
        repo.targets.append(tgt)
        judged += min(len(tgt.sites), per_target, per_repo - judged)
    # trim to the judged sites: first per_target in ring order, capped at per_repo
    budget = per_repo
    for tgt in repo.targets:
        keep = min(len(tgt.sites), per_target, budget)
        tgt.judged = tgt.sites[:keep]
        budget -= keep


def open_repo(spec: str) -> Repo:
    """Open ``LABEL=ROOT:WDB:CDB`` with both databases read-only."""
    label, rest = spec.split("=", 1)
    root, wdb, cdb = rest.split(":")
    conns = []
    for path in (wdb, cdb):
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conns.append(conn)
    repo = Repo(label=label, root=Path(root), wdb=conns[0], cdb=conns[1])
    repo.head = git(repo.root, "rev-parse", "--short", "HEAD").strip()
    repo.calls_edges, repo.calls_null = repo.cdb.execute(
        "SELECT COUNT(*), COALESCE(SUM(line IS NULL), 0) FROM edges WHERE kind = 'calls'"
    ).fetchone()
    repo.commits, repo.described = repo.wdb.execute(
        "SELECT COUNT(*), COALESCE(SUM(llm_description IS NOT NULL AND llm_description != ''), 0) "
        'FROM "commit"'
    ).fetchone()
    return repo


def pct(a: float, b: float) -> str:
    """Format ``a / b`` as a percentage."""
    return f"{100 * a / b:.1f} %" if b else "n/a"


def p90(values: list[float]) -> float:
    """Return the 90th percentile (nearest rank)."""
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(0.9 * len(ordered)))] if ordered else 0.0


def measures(repos: list[Repo]) -> dict:
    """Compute the section 15 measures per repo and in total."""
    out: dict = {}
    for repo in [*repos, None]:
        group = repos if repo is None else [repo]
        targets = [t for r in group for t in r.targets]
        judged = [s for t in targets for s in t.judged]
        rendered = [s for t in targets for s in t.sites]
        toks = [len(s.rendered) / 4 for s in judged]
        ms = [s.blame_ms for s in rendered]
        out["total" if repo is None else repo.label] = {
            "head": ", ".join(r.head for r in group),
            "targets": len(targets),
            "candidate_targets": sum(r.candidates for r in group),
            "judged_sites": len(judged),
            "examined_sites": sum(t.examined for t in targets),
            "rendered_sites": len(rendered),
            "tokens_mean": statistics.fmean(toks) if toks else 0,
            "tokens_p90": p90(toks),
            "tokens_max": max(toks) if toks else 0,
            "blame_ms_mean": statistics.fmean(ms) if ms else 0,
            "blame_ms_p90": p90(ms),
            "null_line_edges_global": sum(r.calls_null for r in group),
            "calls_edges_global": sum(r.calls_edges for r in group),
            "null_line_edges_sample": sum(t.null_edges for t in targets),
            "calls_edges_sample": sum(t.edges_total for t in targets),
            "skipped_null_line": sum(t.skipped_null_line for t in targets),
            "skipped_text": sum(t.skipped_text for t in targets),
            "skipped_other": sum(t.skipped_other for t in targets),
            "skipped_generic_targets": sum(r.skipped_generic_targets for r in group),
            "skipped_generic_callers": sum(t.skipped_generic_callers for t in targets),
            "skipped_undescribed": sum(t.skipped_undescribed for t in targets),
            "mode": "; ".join(r.mode for r in group),
            "plain_undescribed": " / ".join(
                f"{r.plain_undescribed[0]} of {r.plain_undescribed[1]}" for r in group
            ),
            "unresolved_rows": sum(t.unresolved_rows for t in targets),
            "unresolved_callers": sum(t.unresolved_callers for t in targets),
            "callers_resolved_total": sum(t.callers_total for t in targets),
            "same_as_target": sum(s.same_as_target for s in judged),
            "no_description": sum(s.description is None for s in judged),
            "no_commit_row": sum(s.subject.startswith("(commit not") for s in judged),
            "with_pr": sum(s.pr_number is not None for s in judged),
            "commits": sum(r.commits for r in group),
            "described_commits": sum(r.described for r in group),
        }
    return out


def measures_md(
    m: dict, repos: list[Repo], extra: tuple[str, dict] | None = None
) -> str:
    """Render the measures table."""
    cols = [r.label for r in repos]
    if extra:
        m = {**m, extra[0]: extra[1]}
        cols.append(extra[0])
    rows = [
        ("HEAD", lambda x: x["head"]),
        (
            "Candidate targets (function / method, >= 2 callers in other files)",
            lambda x: x["candidate_targets"],
        ),
        (
            "Candidates dropped first as generic or test-only names (targets)",
            lambda x: x["skipped_generic_targets"],
        ),
        ("Sampling mode", lambda x: x["mode"]),
        (
            "Call-site commits without a stored description in the plain seeded sample",
            lambda x: x["plain_undescribed"],
        ),
        ("Targets sampled", lambda x: x["targets"]),
        ("Call sites judged", lambda x: x["judged_sites"]),
        ("Caller slots examined (up to 5 per target)", lambda x: x["examined_sites"]),
        ("Call sites rendered (examined minus skipped)", lambda x: x["rendered_sites"]),
        (
            "Tokens per judged call site, mean / p90 / max (chars / 4)",
            lambda x: (
                f"{x['tokens_mean']:.0f} / {x['tokens_p90']:.0f} / {x['tokens_max']:.0f}"
            ),
        ),
        (
            "Blame time per call site, mean / p90 (ms)",
            lambda x: f"{x['blame_ms_mean']:.0f} / {x['blame_ms_p90']:.0f}",
        ),
        (
            "Null-`line` rate of `calls` edges, whole index",
            lambda x: (
                f"{x['null_line_edges_global']} of {x['calls_edges_global']} ({pct(x['null_line_edges_global'], x['calls_edges_global'])})"
            ),
        ),
        (
            "Null-`line` rate, edges into the sampled targets",
            lambda x: (
                f"{x['null_line_edges_sample']} of {x['calls_edges_sample']} ({pct(x['null_line_edges_sample'], x['calls_edges_sample'])})"
            ),
        ),
        (
            "Unresolved `calls` references matching a sampled target's name (rows / distinct callers)",
            lambda x: (
                f"{x['unresolved_rows']} / {x['unresolved_callers']} (against {x['callers_resolved_total']} resolved callers)"
            ),
        ),
        (
            "`edges.line` no longer holds the call (skipped: line text lacks the bare name)",
            lambda x: (
                f"{x['skipped_text']} of {x['examined_sites'] - x['skipped_null_line']} ({pct(x['skipped_text'], x['examined_sites'] - x['skipped_null_line'])})"
            ),
        ),
        (
            "Callers dropped as generic / test-only names or test paths (sampled targets)",
            lambda x: x["skipped_generic_callers"],
        ),
        (
            "Call sites skipped for an undescribed commit (described-only mode)",
            lambda x: x["skipped_undescribed"],
        ),
        (
            "Skipped for a null `line` / a line missing from the file",
            lambda x: f"{x['skipped_null_line']} / {x['skipped_other']}",
        ),
        (
            "Call-site commit is also a line-level commit of the target (no new information)",
            lambda x: f"{x['same_as_target']} of {x['judged_sites']}",
        ),
        (
            "Call-site commit has no stored description",
            lambda x: (
                f"{x['no_description']} of {x['judged_sites']} (commits with a description in the DB: {x['described_commits']} of {x['commits']})"
            ),
        ),
        (
            "Call-site commit has a PR in the DB",
            lambda x: f"{x['with_pr']} of {x['judged_sites']}",
        ),
    ]
    lines = ["| Measure | " + " | ".join(cols) + " |", "|---|" + "---|" * len(cols)]
    for label, fn in rows:
        cells = []
        for c in cols:
            try:
                cells.append(str(fn(m[c])))
            except KeyError:
                cells.append("n/a")
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def clip(text: str, n: int) -> str:
    """Clip ``text`` to ``n`` characters."""
    text = text.strip()
    return text if len(text) <= n else text[:n].rstrip() + " [...]"


def fence(text: str) -> str:
    """Return ``text`` in a code fence that cannot be closed by its content."""
    return "```text\n" + text.replace("```", "'''") + "\n```"


def sheet(
    repos: list[Repo], m: dict, seed: int, extra: tuple[str, dict] | None = None
) -> str:
    """Render the judging sheet."""
    out = [
        "# Plan 62 spike D0: call-site judging sheet",
        "",
        "Generated by `scripts/call_site_spike.py` (read-only, no LLM call). "
        f"Seed {seed}. Verdicts are for the owner or an independent judge; none are filled in.",
        "",
        "## Rubric",
        "",
        "A call site is **useful** (y) when the call line's commit (subject, body, stored description "
        "or PR title) names the target or the contract the caller relies on: an argument, the return "
        "value, ordering, or an error case. Otherwise **n** (the commit only touched the line "
        "incidentally, or says nothing about the target). Judge the commit text as shown, not the code.",
        "",
        "**Pass bar (section 15):** useful in at least 15 of 30.",
        "",
        "## Measures",
        "",
        measures_md(m, repos, extra),
        "",
        "Notes: tokens are rendered header + stored raw description + PR title only (the commit body "
        "shown below is for the judge, a card would not carry it). A skipped call site is dropped, not "
        "replaced by the next caller. Only judged call sites count toward tokens, same-commit and "
        "description counts; blame time covers every rendered call site.",
        "",
    ]
    n = 0
    for repo in repos:
        out += [
            f"## {repo.label}",
            "",
            f"Checkout: `{repo.root}` at HEAD `{repo.head}` (blame and the lines shown are at this HEAD). "
            f"Sampling: {repo.mode}.",
            "",
        ]
        for tgt in repo.targets:
            judged = tgt.judged
            if not judged:
                continue
            out += [
                f"### Target: `{tgt.qualified}` - {tgt.path}:{tgt.start}-{tgt.end}",
                "",
                f"Signature: `{clip(tgt.signature, 200) or '(none stored)'}`  ",
                f"Callers in the index: {tgt.callers_total}; blamed here: {len(judged)} of {len(tgt.sites)} rendered.",
                "",
            ]
            for s in judged:
                n += 1
                out += [
                    f"#### Call site {n}: `{s.caller_name}` ({s.caller_path}:{s.line})",
                    "",
                    f"Call line: `{s.call_line.strip()}`",
                    "",
                    fence("\n".join(s.context)),
                    "",
                    f"- Commit: `{s.sha[:10]}` {s.date[:10]} - {s.subject}"
                    + (
                        "  **(also a line-level commit of the target)**"
                        if s.same_as_target
                        else ""
                    ),
                    f"- Body: {clip(s.body, BODY_CHARS) or '(empty)'}".replace(
                        "\n", "\n  "
                    ),
                    f"- Stored description: {clip(s.description, DESC_CHARS) if s.description else '(none stored)'}".replace(
                        "\n", "\n  "
                    ),
                    f"- PR: {'#' + str(s.pr_number) + ' ' + s.pr_title if s.pr_number else '(none in the DB)'}",
                    "",
                    "**Verdict:** useful y / n: ____   (note: ____)",
                    "",
                ]
    return "\n".join(out) + "\n"


def main() -> None:
    """Entry point."""
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--repo", action="append", required=True, help="LABEL=ROOT:WDB:CDB")
    ap.add_argument("--seed", type=int, default=62)
    ap.add_argument("--per-repo", type=int, default=15)
    ap.add_argument("--sites-per-target", type=int, default=3)
    ap.add_argument("--out", required=True)
    ap.add_argument("--json")
    ap.add_argument(
        "--widen-pool",
        type=int,
        default=0,
        help="when described-only finds fewer than --per-repo sites in "
        f"{MAX_DESCRIBED_POOL} candidates, retry with this many (0: do not widen)",
    )
    ap.add_argument(
        "--extra-json", help="measures JSON of an earlier run to show as a column"
    )
    ap.add_argument("--extra-key", default="whygraph")
    ap.add_argument("--extra-label", default="")
    args = ap.parse_args()
    repos = [open_repo(spec) for spec in args.repo]
    for repo in repos:
        sample(repo, args.seed, args.per_repo, args.sites_per_target)
        sites = [s for t in repo.targets for s in t.judged]
        repo.plain_undescribed = (sum(s.description is None for s in sites), len(sites))
        if repo.plain_undescribed[0] * 2 > repo.plain_undescribed[1]:
            # more than half lack a stored description: prefer described call-site commits
            sample(
                repo,
                args.seed,
                args.per_repo,
                args.sites_per_target,
                described_only=True,
            )
            first = sum(len(t.judged) for t in repo.targets)
            if first < args.per_repo and args.widen_pool > MAX_DESCRIBED_POOL:
                sample(
                    repo,
                    args.seed,
                    args.per_repo,
                    args.sites_per_target,
                    described_only=True,
                    pool=args.widen_pool,
                )
                repo.mode += (
                    f"; the {MAX_DESCRIBED_POOL}-candidate pool gave only {first} site(s), "
                    "so the pool was widened (deviation from the 50-candidate rule)"
                )
    m = measures(repos)
    extra = None
    if args.extra_json:
        old = json.loads(Path(args.extra_json).read_text(encoding="utf-8"))
        extra = (args.extra_label or args.extra_key, old[args.extra_key])
    Path(args.out).write_text(sheet(repos, m, args.seed, extra), encoding="utf-8")
    if args.json:
        Path(args.json).write_text(json.dumps(m, indent=2), encoding="utf-8")
    print(json.dumps(m, indent=2))


if __name__ == "__main__":
    main()
