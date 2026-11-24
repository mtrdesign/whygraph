"""PlansCrawler - index the plans a team committed under ``[plans].dir``.

The scan's "Plans" phase (plan 61 section 4.4). It reads plan files from
``HEAD``'s git objects only, never the working tree, through the project
purpose reader's own checks (safe path, deny list, regular blob mode, size
cap, decode; :mod:`whygraph.analyze.project_purpose`), redacts them, and
stores per plan the body plus what :mod:`whygraph.mcp.plans` derives from
it: the outline, the decision digest and the why index. It then links
plans to code:

* ``path`` links - repo files a plan names outside echo and negative
  text, recomputed from the stored bodies whenever ``HEAD`` moved and the
  tracked-path set changed;
* ``commit`` links - ``WhyGraph-Plan: <stem>`` lines in stored commit
  messages, insert-only (commits are immutable);
* ``pr`` links - the same lines in PR descriptions, rebuilt every run
  (PR bodies are editable).

Rows are never deleted: a plan that leaves ``HEAD`` keeps its row with
``present = 0`` and loses only its ``path`` links.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone

from rich.progress import Progress
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Session, col, delete, func, select

from whygraph.analyze import project_purpose
from whygraph.db import get_session
from whygraph.db.models import Commit, Plan, PlanLink, PullRequest
from whygraph.db.models.plan import PLAN_MAX_BYTES
from whygraph.mcp.plans import (
    PLAN_STEM_RE,
    PLANS_PARSER_VERSION,
    build_digest,
    build_outline,
    build_why_index,
    echo_spans,
    line_count,
    normalise_newlines,
    parse_header,
    path_mentions,
    plan_trailers,
)
from whygraph.services.git import GitError, Repository
from whygraph.services.git.commands import TreeEntry

from .crawler import Crawler

_log = logging.getLogger(__name__)

PLANS_MAX = 500
"""The most plan files indexed per run; the rest are counted as ``skipped_cap``."""

PATH_LINKS_MAX = 200
"""The most ``path`` links per plan."""

LINK_SECTIONS_MAX = 12
"""The most outline numbers stored in one ``path`` link's ``sections``."""

_TRAILER_LIKE = "%whygraph-plan:%"
_INSERT_BATCH = 200


def _now() -> str:
    """The current UTC time, ISO 8601 to the second."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _drop_path_links(session: Session, stem: str) -> None:
    """Delete one plan's ``path`` links."""
    session.exec(
        delete(PlanLink).where(col(PlanLink.stem) == stem, col(PlanLink.kind) == "path")
    )


def _retire(session: Session, row: Plan) -> None:
    """Mark ``row`` gone from ``HEAD``: ``present = 0``, no path links, no path state."""
    row.present = 0
    row.paths_head = None
    row.paths_hash = None
    session.add(row)
    _drop_path_links(session, row.stem)


def _insert_links(session: Session, values: list[dict]) -> None:
    """Insert ``plan_link`` rows, ignoring ones that already exist."""
    conn = session.connection()
    for i in range(0, len(values), _INSERT_BATCH):
        conn.execute(
            sqlite_insert(PlanLink).on_conflict_do_nothing(),
            values[i : i + _INSERT_BATCH],
        )


class PlansCrawler(Crawler):
    """Index committed plans and their commit, PR and path links.

    Best-effort about the repository (no ``HEAD``, a failing ``ls-tree``,
    an unreadable or unparsable plan never fail the scan), strict about
    the database (a DB error is captured in :attr:`Crawler.error`).

    Parameters
    ----------
    progress : rich.progress.Progress
        Shared Progress instance owned by the orchestrator.
    repository : Repository
        The project's repository; only its git object store is read.
    plans_dir : str
        ``[plans].dir``, already validated by ``check_plans_dir``; ``""``
        turns indexing off (every row becomes ``present = 0``).
    max_plans : int, optional
        The most plan files indexed per run (default :data:`PLANS_MAX`).
    """

    def __init__(
        self,
        progress: Progress,
        *,
        repository: Repository,
        plans_dir: str,
        max_plans: int = PLANS_MAX,
    ) -> None:
        # "plans" is the THREAD name and the key the results panel and the
        # playground's scan progress look this crawler up by.
        super().__init__("plans", progress, total=None)
        self._repository = repository
        self._plans_dir = plans_dir.rstrip("/")
        self._max_plans = max_plans
        self._skipped = {
            "skipped_name": 0,
            "skipped_duplicate": 0,
            "skipped_unreadable": 0,
            "skipped_cap": 0,
        }

    # --- the phase ---------------------------------------------------------------

    def work(self) -> None:
        """Index the plans at ``HEAD``, then refresh the path, commit and PR links."""
        if not self._plans_dir:
            self._disable()
            self.summary = "disabled"
            return
        head = self._repository.head_commit()
        if head is None:
            self.summary = "no HEAD"
            return
        try:
            entries = self._repository.tree_entries(
                head, self._plans_dir, recursive=True
            )
        except GitError as exc:
            _log.info("plans: cannot list %s at HEAD: %s", self._plans_dir, exc)
            self.summary = "no HEAD"
            return
        kept, left_alone = self._select(entries)
        self.set_total(len(kept))
        changed, removed = self._index(head, kept, left_alone)
        self._path_links(head, changed)
        self._commit_links()
        self._pr_links()
        self.summary = self._summary(len(kept), len(changed), removed)

    def _disable(self) -> None:
        """Indexing is off: every row leaves ``HEAD``'s index; rows are kept."""
        with get_session() as session:
            for row in session.exec(select(Plan).where(col(Plan.present) == 1)).all():
                _retire(session, row)
            session.exec(delete(PlanLink).where(col(PlanLink.kind) == "path"))

    def _select(
        self, entries: tuple[TreeEntry, ...]
    ) -> tuple[list[tuple[TreeEntry, str]], set[str]]:
        """The plan files to index, and the stems the cap leaves untouched."""
        prefix = f"{self._plans_dir}/"
        by_stem: dict[str, TreeEntry] = {}
        for entry in sorted(entries, key=lambda e: e.name):
            if (
                entry.type != "blob"
                or entry.mode not in project_purpose._READABLE_MODES
                or not entry.name.startswith(prefix)
                or not entry.name.endswith(".md")
            ):
                continue
            stem = entry.name.rsplit("/", 1)[-1][: -len(".md")]
            if not PLAN_STEM_RE.fullmatch(stem):
                self._skipped["skipped_name"] += 1
                continue
            if stem in by_stem:
                self._skipped["skipped_duplicate"] += 1
                continue
            by_stem[stem] = entry
        ordered = [(entry, stem) for stem, entry in by_stem.items()]
        kept = ordered[: self._max_plans]
        over = ordered[self._max_plans :]
        self._skipped["skipped_cap"] += len(over)
        return kept, {stem for _, stem in over}

    def _index(
        self, head: str, kept: list[tuple[TreeEntry, str]], left_alone: set[str]
    ) -> tuple[set[str], int]:
        """Upsert one row per kept plan; retire rows no longer at ``HEAD``."""
        now = _now()
        changed: set[str] = set()
        removed = 0
        with get_session() as session:
            rows = {row.stem: row for row in session.exec(select(Plan)).all()}
            for entry, stem in kept:
                row = rows.get(stem)
                if row is not None:
                    row.path = entry.name
                    row.present = 1
                stale = (
                    row is None
                    or row.blob_sha != entry.sha
                    or row.parser_version != PLANS_PARSER_VERSION
                )
                if stale:
                    derived = self._derive(head, entry, stem)
                    if derived is None:
                        self._skipped["skipped_unreadable"] += 1
                    else:
                        if row is None:
                            row = Plan(
                                stem=stem, first_seen_at=now, updated_at=now, **derived
                            )
                        else:
                            if row.blob_sha != entry.sha:
                                row.updated_at = now
                            for key, value in derived.items():
                                setattr(row, key, value)
                        changed.add(stem)
                if row is not None:
                    session.add(row)
                self.advance(1)
            seen = {stem for _, stem in kept}
            for stem, row in rows.items():
                if stem in seen or stem in left_alone or not row.present:
                    continue
                _retire(session, row)
                removed += 1
        return changed, removed

    def _derive(self, head: str, entry: TreeEntry, stem: str) -> dict | None:
        """Read one plan blob and derive its stored columns; ``None`` if unreadable."""
        found = project_purpose._lookup(self._repository, entry.name, head)
        if found is None:
            return None
        raw = project_purpose._read_entry(self._repository, found, PLAN_MAX_BYTES)
        if raw is None:
            return None
        raw = normalise_newlines(raw)
        body = project_purpose.redact_purpose_text(raw)
        try:
            title, objective = parse_header(body)
            spans = echo_spans(body)
            outline = build_outline(body, spans)
            digest = build_digest(body, outline, spans, stem=stem)
            why_index = build_why_index(body, outline, spans)
        except Exception as exc:  # noqa: BLE001 - one hostile plan must not fail a scan
            _log.warning("plans: cannot parse %s: %s", entry.name, exc)
            return None
        return {
            "path": entry.name,
            "blob_sha": entry.sha,
            "parser_version": PLANS_PARSER_VERSION,
            "title": title or stem,
            "line_count": line_count(body),
            "objective": objective,
            "digest": digest,
            "outline": json.dumps(outline, separators=(",", ":")),
            "why_index": json.dumps(why_index, separators=(",", ":")),
            "body": body,
            "redacted_lines": max(0, raw.count("\n") - body.count("\n")),
            "truncated": int(entry.size is not None and entry.size > PLAN_MAX_BYTES),
            "present": 1,
        }

    # --- links -------------------------------------------------------------------------

    def _path_links(self, head: str, changed: set[str]) -> None:
        """Recompute ``path`` links of present plans whose blob or tracked set changed."""
        with get_session() as session:
            rows = session.exec(select(Plan).where(col(Plan.present) == 1)).all()
            need = [
                row for row in rows if row.paths_head != head or row.stem in changed
            ]
            if not need:
                return
            try:
                tracked = set(self._repository.tracked_paths(head))
            except GitError as exc:
                _log.warning("plans: cannot list the files HEAD tracks: %s", exc)
                return
            paths_hash = hashlib.sha256(
                "\n".join(sorted(tracked)).encode("utf-8", "surrogateescape")
            ).hexdigest()
            for row in need:
                if row.stem in changed or row.paths_hash != paths_hash:
                    _drop_path_links(session, row.stem)
                    session.flush()
                    links = self._mentions(row, tracked)
                    if links:
                        _insert_links(session, links)
                row.paths_head = head
                row.paths_hash = paths_hash
                session.add(row)

    @staticmethod
    def _mentions(row: Plan, tracked: set[str]) -> list[dict]:
        """The ``path`` link rows of one plan, from its stored body and outline."""
        try:
            outline = json.loads(row.outline)
            mentions = path_mentions(row.body, outline, echo_spans(row.body))
        except Exception as exc:  # noqa: BLE001 - a bad stored row loses its links only
            _log.warning("plans: cannot read the mentions of %s: %s", row.stem, exc)
            return []
        links = []
        for mention in mentions:
            if mention.path not in tracked:
                continue
            sections = mention.sections[:LINK_SECTIONS_MAX]
            links.append(
                {
                    "stem": row.stem,
                    "kind": "path",
                    "ref": mention.path,
                    "sections": ",".join(map(str, sections)) if sections else None,
                    "declared": int(mention.declared),
                }
            )
            if len(links) >= PATH_LINKS_MAX:
                break
        return links

    def _commit_links(self) -> None:
        """Insert ``commit`` links from stored commit messages (insert-only)."""
        with get_session() as session:
            found = session.exec(
                select(Commit.sha, Commit.body).where(
                    col(Commit.body).like(_TRAILER_LIKE)
                )
            ).all()
            values = [
                {
                    "stem": stem,
                    "kind": "commit",
                    "ref": sha,
                    "sections": None,
                    "declared": 0,
                }
                for sha, body in found
                for stem in plan_trailers(body)
            ]
            if values:
                _insert_links(session, values)

    def _pr_links(self) -> None:
        """Rebuild every ``pr`` link from the stored PR descriptions."""
        with get_session() as session:
            session.exec(delete(PlanLink).where(col(PlanLink.kind) == "pr"))
            found = session.exec(
                select(PullRequest.number, PullRequest.body).where(
                    col(PullRequest.body).like(_TRAILER_LIKE)
                )
            ).all()
            values = [
                {
                    "stem": stem,
                    "kind": "pr",
                    "ref": str(number),
                    "sections": None,
                    "declared": 0,
                }
                for number, body in found
                for stem in plan_trailers(body)
            ]
            if values:
                _insert_links(session, values)

    # --- summary -------------------------------------------------------------------------

    def _summary(self, plans: int, changed: int, removed: int) -> str:
        """One line: plans indexed, changed and removed; link totals; skips."""
        with get_session() as session:
            totals = dict(
                session.exec(
                    select(PlanLink.kind, func.count()).group_by(col(PlanLink.kind))
                ).all()
            )
        noun = "plan" if plans == 1 else "plans"
        line = (
            f"{plans} {noun} ({changed} changed, {removed} removed); links "
            f"{totals.get('commit', 0)} commit, {totals.get('pr', 0)} pr, "
            f"{totals.get('path', 0)} path"
        )
        skips = [f"{key} {value}" for key, value in self._skipped.items() if value]
        return f"{line}; {', '.join(skips)}" if skips else line
