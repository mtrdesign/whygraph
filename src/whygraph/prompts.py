from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from whygraph.backend import SymbolNode
from whygraph.cochange.service import MIN_COMMITS_FOR_DISPLAY
from whygraph.cochange.types import CoChangeReport, VolatilityReport
from whygraph.context import RationaleContext
from whygraph.evidence.types import EvidenceRecord

# Bump whenever SYSTEM_PROMPT, Rationale schema, or build_user_prompt
# changes in a way that should invalidate cached rationale.
#   v1 → v2: added PR + issue evidence formatting.
#   v2 → v3: inlined the JSON output schema in SYSTEM_PROMPT so the
#            claude_cli backend (no output_config) produces the right shape.
#   v3 → v4: added Callers / Callees structural-context sections; added the
#            "structural context, not evidence" guideline to SYSTEM_PROMPT.
#   v4 → v5: added Co-evolves with + Volatility sections (git-derived
#            signals); build_user_prompt now takes a RationaleContext.
PROMPT_VERSION = "v5"


class Rationale(BaseModel):
    purpose: str = Field(
        description="One sentence describing what this code does."
    )
    why: str = Field(
        description=(
            "One paragraph explaining why this code exists — the historical "
            "or contextual rationale, not the mechanism. Cite relevant "
            "commits when supporting a claim."
        )
    )
    constraints: list[str] = Field(
        description=(
            "Things that must be preserved when modifying: invariants, "
            "contracts, dependencies on caller behaviour. Empty array if "
            "none are evidenced."
        )
    )
    tradeoffs: list[str] = Field(
        description=(
            "Notable design tradeoffs visible in the history. Empty array "
            "if none are evidenced."
        )
    )
    risks: list[str] = Field(
        description=(
            "Risks of modification: regressions, breaking changes for "
            "consumers, compliance or security implications. Empty array "
            "if none are evidenced."
        )
    )


SYSTEM_PROMPT = """You are an analyst that explains why code exists, not just what it does.

Given a code symbol's location and a bundle of evidence (commits, blame data, etc.), generate a structured rationale explaining the historical and contextual reasons for this code.

Guidelines:
- Be specific. Cite evidence (commit subjects or short SHAs) when supporting a claim.
- Be honest. If evidence is thin or unclear, say "Insufficient evidence" rather than speculating. Do not invent intent that the commits do not support.
- Prefer the language of the original commits over your own paraphrasing.
- For constraints / tradeoffs / risks: only include items you can defend from the evidence. An empty array is the correct answer when there's no signal.
- Keep each list entry to one or two sentences. Keep "purpose" to one sentence and "why" to one short paragraph.
- Use the Callers / Callees sections (when present) to reason about blast radius and consumer-facing constraints — but treat them as structural context, not as evidence in their own right. Don't claim a constraint exists just because a caller exists; cite commit/PR evidence for the claim itself.
- The "Co-evolves with" section lists files historically modified in the same commits as the target. Treat as evidence of intent coupling — useful for "constraints" and "risks" — but cite the underlying commits/PRs when making specific claims, not the raw co-change percentage.
- The "Volatility" section indicates whether this code is stable or actively churning. Calibrate confidence accordingly: a single recent commit is thin history; many recent commits across multiple authors signals an active design that may not be settled.

Output a JSON object with this exact shape (and no other fields, prose, or markdown formatting):

{
  "purpose": "one sentence describing what this code does",
  "why": "one short paragraph explaining why this code exists, citing relevant commit subjects or short SHAs",
  "constraints": ["string", "string"],
  "tradeoffs": ["string", "string"],
  "risks": ["string", "string"]
}

constraints / tradeoffs / risks are arrays of short strings; an empty array is the correct answer when there is no evidence to support an entry."""


def _parse_date(value: Any) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        # datetime.fromisoformat handles "Z" suffix as of Python 3.11+.
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _iso_day(unix_seconds: int | None) -> str:
    if not unix_seconds:
        return "????-??-??"
    return datetime.fromtimestamp(unix_seconds, tz=timezone.utc).strftime("%Y-%m-%d")


def _commit_time(record: EvidenceRecord) -> int:
    payload = record.payload if isinstance(record.payload, dict) else None
    if payload is None:
        return 0
    t = payload.get("author_time")
    return t if isinstance(t, int) else 0


def _format_neighbor_block(label: str, sym: SymbolNode) -> list[str]:
    lines = [f"  {sym.qualified_name}"]
    if sym.signature:
        lines.append(f"    {sym.signature}")
    if sym.docstring:
        # Indent each docstring line; trim trailing whitespace per line.
        for doc_line in sym.docstring.splitlines():
            lines.append(f"    {doc_line.rstrip()}")
    return lines


def _neighbor_section_header(
    label: str, included: int, truncated: int, target_qname: str, relation: str
) -> str:
    if truncated > 0:
        total = included + truncated
        return f"{label} ({included} of {total} — {relation} {target_qname}):"
    return f"{label} ({included} — {relation} {target_qname}):"


def _format_cochange_section(report: CoChangeReport) -> list[str]:
    if (
        report.commits_considered < MIN_COMMITS_FOR_DISPLAY
        or not report.neighbors
    ):
        return []
    if report.truncated > 0:
        total = len(report.neighbors) + report.truncated
        header = (
            f"Co-evolves with (top {len(report.neighbors)} of {total} — "
            f"files modified in the same commits as {report.target_file}):"
        )
    else:
        header = (
            f"Co-evolves with ({len(report.neighbors)} — files modified in "
            f"the same commits as {report.target_file}):"
        )
    lines: list[str] = ["", header]
    for n in report.neighbors:
        pct = round(n.percent)
        lines.append(
            f"  {pct:3d}%  ({n.cochange_count}/{n.target_commits_total})  "
            f"{n.file_path}"
        )
    return lines


def _format_volatility_section(report: VolatilityReport) -> list[str]:
    if report.commits_total == 0:
        return []
    if report.days_since_last_change is None:
        last = "unknown"
    elif report.days_since_last_change == 0:
        last = "today"
    elif report.days_since_last_change == 1:
        last = "1 day ago"
    else:
        last = f"{report.days_since_last_change} days ago"
    author_word = "author" if report.distinct_authors == 1 else "authors"
    return [
        "",
        "Volatility (this file, all-time):",
        (
            f"  Last changed: {last} — {report.commits_total} "
            f"commit(s) total — {report.distinct_authors} distinct {author_word}"
        ),
        (
            f"  Recent: {report.commits_90d} in last 90d, "
            f"{report.commits_180d} in last 180d, "
            f"{report.commits_365d} in last 365d"
        ),
    ]


def build_user_prompt(
    node: SymbolNode,
    evidence: list[EvidenceRecord],
    context: RationaleContext,
) -> str:
    neighbors = context.neighbors
    lines: list[str] = []
    lines.append(f"Symbol: {node.qualified_name}")
    lines.append(f"Kind: {node.kind}")
    lines.append(
        f"Location: {node.file_path}:{node.start_line}-{node.end_line}"
    )
    lines.append(f"Language: {node.language}")
    if node.signature:
        lines.append(f"Signature: {node.signature}")
    if node.docstring:
        lines.append("")
        lines.append("Docstring:")
        lines.append(node.docstring)

    prs = [e for e in evidence if e.source == "pr"]
    issues = [e for e in evidence if e.source == "issue"]
    commits = sorted(
        [e for e in evidence if e.source == "git_commit"],
        key=_commit_time,
        reverse=True,
    )
    blames = [e for e in evidence if e.source == "git_blame"]

    lines.append("")
    lines.append(
        f"Evidence: {len(evidence)} item(s) — {len(prs)} PR(s), "
        f"{len(issues)} issue(s), {len(commits)} commit(s), "
        f"{len(blames)} blame entr(ies)."
    )

    if prs:
        lines.append("")
        lines.append("Pull requests (highest-signal narrative — read first):")
        for pr in prs:
            p = pr.payload if isinstance(pr.payload, dict) else {}
            num = pr.ref or "?"
            title = p.get("title") or ""
            author = p.get("author") or "unknown"
            merged = _iso_day(_parse_date(p.get("merged_at")))
            closes = p.get("closes_issues") or []
            body = (p.get("body") or "").strip()
            lines.append("")
            lines.append(f"  PR #{num}  merged {merged}  by {author}")
            lines.append(f"    {title}")
            if closes:
                lines.append(
                    f"    Closes: {', '.join(f'#{n}' for n in closes)}"
                )
            if body:
                for body_line in body.split("\n"):
                    lines.append(f"    {body_line}")

    if issues:
        lines.append("")
        lines.append("Linked issues (motivation / problem statement):")
        for issue in issues:
            p = issue.payload if isinstance(issue.payload, dict) else {}
            num = issue.ref or "?"
            title = p.get("title") or ""
            labels = p.get("labels") or []
            body = (p.get("body") or "").strip()
            lines.append("")
            label_part = (
                "  [" + ", ".join(labels) + "]" if labels else ""
            )
            lines.append(f"  ISSUE #{num}{label_part}")
            lines.append(f"    {title}")
            if body:
                for body_line in body.split("\n"):
                    lines.append(f"    {body_line}")

    if commits:
        lines.append("")
        lines.append("Commits (newest first):")
        for c in commits:
            p = c.payload if isinstance(c.payload, dict) else {}
            sha = (c.ref or "")[:8]
            t = p.get("author_time")
            date = _iso_day(t if isinstance(t, int) else None)
            author = p.get("author") or "unknown"
            subject = p.get("subject") or ""
            body = (p.get("body") or "").strip()
            lines.append("")
            lines.append(f"  COMMIT {sha}  {date}  by {author}")
            lines.append(f"    {subject}")
            if body:
                for body_line in body.split("\n"):
                    lines.append(f"    {body_line}")

    if blames:
        lines.append("")
        lines.append("Blame (line attribution within the symbol's range):")
        for b in blames:
            p = b.payload if isinstance(b.payload, dict) else {}
            sha = (b.ref or "")[:8]
            line_count = p.get("line_count") or 0
            line_total = p.get("line_total") or 0
            summary = p.get("summary") or ""
            lines.append(f"  {sha}  {line_count}/{line_total} lines  — {summary}")

    if neighbors.callers:
        lines.append("")
        lines.append(
            _neighbor_section_header(
                "Callers",
                len(neighbors.callers),
                neighbors.truncated_callers,
                node.qualified_name,
                "symbols that call",
            )
        )
        for caller in neighbors.callers:
            lines.append("")
            lines.extend(_format_neighbor_block("caller", caller))

    if neighbors.callees:
        lines.append("")
        lines.append(
            _neighbor_section_header(
                "Callees",
                len(neighbors.callees),
                neighbors.truncated_callees,
                node.qualified_name,
                "symbols called by",
            )
        )
        for callee in neighbors.callees:
            lines.append("")
            lines.extend(_format_neighbor_block("callee", callee))

    lines.extend(_format_cochange_section(context.cochange))
    lines.extend(_format_volatility_section(context.volatility))

    return "\n".join(lines)
