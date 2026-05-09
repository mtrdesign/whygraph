"""Author identity resolution.

Builds a deduplicated `authors` table from commits, pull-request
``commit_titles[].author_*`` entries, PR openers, and issue openers.
Replaces the email-localpart heuristic that ``velocity_by_author`` used
to do at query time.

Dedup rule (priority order):

1. Exact GitHub login match — wins immediately.
2. Same email — merges identities.
3. Email local-part matching a known login (e.g. ``alice@…`` ↔ login
   ``alice``) — merges identities.

Identities that share none of the above stay separate. Names are not
used for merging because collisions are common and hard to disambiguate.

The table is rebuilt fresh on every scan. Callers that want stable IDs
across runs should hold onto ``primary_login`` / ``primary_email``
instead of ``id``.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable

from whygraph.scan import db as db_module


@dataclass
class _Identity:
    logins: set[str] = field(default_factory=set)
    emails: set[str] = field(default_factory=set)
    names: set[str] = field(default_factory=set)
    first_seen: str | None = None
    last_seen: str | None = None
    commit_count: int = 0
    pr_count: int = 0
    issue_count: int = 0

    def merge(self, other: "_Identity") -> None:
        self.logins |= other.logins
        self.emails |= other.emails
        self.names |= other.names
        self.first_seen = _earliest(self.first_seen, other.first_seen)
        self.last_seen = _latest(self.last_seen, other.last_seen)
        self.commit_count += other.commit_count
        self.pr_count += other.pr_count
        self.issue_count += other.issue_count

    def see(self, when: str | None) -> None:
        self.first_seen = _earliest(self.first_seen, when)
        self.last_seen = _latest(self.last_seen, when)


def _earliest(a: str | None, b: str | None) -> str | None:
    candidates = [x for x in (a, b) if x]
    return min(candidates) if candidates else None


def _latest(a: str | None, b: str | None) -> str | None:
    candidates = [x for x in (a, b) if x]
    return max(candidates) if candidates else None


def _localpart(email: str) -> str | None:
    return email.split("@", 1)[0].lower() if "@" in email else None


class _IdentityIndex:
    """Online union-find over identities keyed by login / email / localpart."""

    def __init__(self) -> None:
        self._by_login: dict[str, _Identity] = {}
        self._by_email: dict[str, _Identity] = {}
        self._by_localpart: dict[str, _Identity] = {}

    def add(
        self,
        *,
        login: str | None,
        email: str | None,
        name: str | None,
        when: str | None,
        commit_delta: int = 0,
        pr_delta: int = 0,
        issue_delta: int = 0,
    ) -> None:
        login = login.strip() or None if login else None
        email = email.strip().lower() or None if email else None
        name = name.strip() or None if name else None
        if not (login or email or name):
            return

        candidates: list[_Identity] = []
        if login and (existing := self._by_login.get(login)):
            candidates.append(existing)
        if email and (existing := self._by_email.get(email)):
            candidates.append(existing)
        if email and (lp := _localpart(email)):
            if (existing := self._by_localpart.get(lp)) and existing not in candidates:
                candidates.append(existing)
        if login and (existing := self._by_localpart.get(login.lower())) and existing not in candidates:
            candidates.append(existing)

        # Dedupe candidate list (same identity reachable by multiple keys).
        seen_ids: set[int] = set()
        unique: list[_Identity] = []
        for c in candidates:
            if id(c) not in seen_ids:
                seen_ids.add(id(c))
                unique.append(c)

        if not unique:
            ident = _Identity()
        else:
            ident = unique[0]
            for other in unique[1:]:
                ident.merge(other)

        if login:
            ident.logins.add(login)
        if email:
            ident.emails.add(email)
        if name:
            ident.names.add(name)
        ident.see(when)
        ident.commit_count += commit_delta
        ident.pr_count += pr_delta
        ident.issue_count += issue_delta

        # Reindex everything pointing at this identity (cheap — small sets).
        for lg in ident.logins:
            self._by_login[lg] = ident
            self._by_localpart[lg.lower()] = ident
        for em in ident.emails:
            self._by_email[em] = ident
            if lp := _localpart(em):
                self._by_localpart[lp] = ident

    def identities(self) -> Iterable[_Identity]:
        seen: set[int] = set()
        for ident in (
            *self._by_login.values(),
            *self._by_email.values(),
            *self._by_localpart.values(),
        ):
            if id(ident) not in seen:
                seen.add(id(ident))
                yield ident


def _pick_primary(values: set[str]) -> str | None:
    """Pick the lexicographically smallest non-empty value (deterministic)."""
    cleaned = [v for v in values if v]
    return min(cleaned) if cleaned else None


def build_authors(db: db_module.Database) -> int:
    """Rebuild the ``authors`` table from commits + pull_requests + issues.

    Idempotent — clears the table first. Returns the number of rows
    written.
    """
    index = _IdentityIndex()
    cur = db._conn.cursor()

    # Commits: each row contributes one (name, email) identity + bumps commit_count.
    cur.execute("SELECT author_name, author_email, committed_at FROM commits")
    for name, email, when in cur.fetchall():
        index.add(
            login=None,
            email=email,
            name=name,
            when=when,
            commit_delta=1,
        )

    # PR openers + per-commit author_login/author_name/author_email entries
    # from commit_titles. The PR opener row gets pr_count=1 charged to its
    # canonical identity; commit_titles entries don't increment pr_count
    # (they're already counted via the opener), but they DO contribute
    # logins that link emails back to GitHub accounts.
    cur.execute("SELECT number, author, commit_titles, created_at FROM pull_requests")
    for number, opener_login, commit_titles_raw, created_at in cur.fetchall():
        if opener_login:
            index.add(
                login=opener_login,
                email=None,
                name=None,
                when=created_at,
                pr_delta=1,
            )
        try:
            entries = json.loads(commit_titles_raw or "[]")
        except (TypeError, json.JSONDecodeError):
            entries = []
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                index.add(
                    login=entry.get("author_login"),
                    email=entry.get("author_email"),
                    name=entry.get("author_name"),
                    when=created_at,
                )

    # Issues: opener login → issue_count.
    cur.execute("SELECT author, created_at FROM issues")
    for opener_login, created_at in cur.fetchall():
        if opener_login:
            index.add(
                login=opener_login,
                email=None,
                name=None,
                when=created_at,
                issue_delta=1,
            )

    db.clear_authors()
    written = 0
    for ident in index.identities():
        if not (ident.logins or ident.emails or ident.names):
            continue
        db.insert_author(
            primary_login=_pick_primary(ident.logins),
            primary_name=_pick_primary(ident.names),
            primary_email=_pick_primary(ident.emails),
            emails=sorted(ident.emails),
            logins=sorted(ident.logins),
            names=sorted(ident.names),
            first_seen=ident.first_seen,
            last_seen=ident.last_seen,
            commit_count=ident.commit_count,
            pr_count=ident.pr_count,
            issue_count=ident.issue_count,
        )
        written += 1
    return written


def resolve_author(db: db_module.Database, identity: str) -> dict | None:
    """Resolve an identity string to one ``authors`` row.

    Lookup order: exact login → exact email → email local-part → name
    (case-insensitive substring on stored names). Returns the row dict
    with ``emails`` / ``logins`` / ``names`` JSON-decoded, or ``None``
    if no match.
    """
    if not identity or not identity.strip():
        return None
    needle = identity.strip()
    cur = db._conn.cursor()

    # Exact login.
    cur.execute(
        "SELECT * FROM authors WHERE primary_login = ? "
        "OR logins LIKE ? "
        "ORDER BY commit_count DESC, id ASC LIMIT 1",
        (needle, f'%"{needle}"%'),
    )
    row = cur.fetchone()
    if row:
        return _hydrate(cur, row)

    # Exact email (case-insensitive).
    lowered = needle.lower()
    cur.execute(
        "SELECT * FROM authors WHERE LOWER(primary_email) = ? "
        "OR LOWER(emails) LIKE ? "
        "ORDER BY commit_count DESC, id ASC LIMIT 1",
        (lowered, f'%"{lowered}"%'),
    )
    row = cur.fetchone()
    if row:
        return _hydrate(cur, row)

    # Email local-part match: input "alice" hits "alice@example.com".
    cur.execute(
        "SELECT * FROM authors WHERE LOWER(emails) LIKE ? "
        "ORDER BY commit_count DESC, id ASC LIMIT 1",
        (f'%"{lowered}@%',),
    )
    row = cur.fetchone()
    if row:
        return _hydrate(cur, row)

    # Name (case-insensitive substring on the JSON blob).
    cur.execute(
        "SELECT * FROM authors WHERE LOWER(names) LIKE ? "
        "ORDER BY commit_count DESC, id ASC LIMIT 1",
        (f'%{lowered}%',),
    )
    row = cur.fetchone()
    if row:
        return _hydrate(cur, row)

    return None


def _hydrate(cur, row: tuple) -> dict:
    cols = [d[0] for d in cur.description]
    out = dict(zip(cols, row, strict=True))
    for key in ("emails", "logins", "names"):
        try:
            out[key] = json.loads(out[key])
        except (TypeError, json.JSONDecodeError):
            out[key] = []
    return out


def author_lookup_table(db: db_module.Database) -> dict[str, int]:
    """Return ``{key: author_id}`` keyed by every login/email known to the
    table. Useful when you want to resolve many identities in a single
    pass without firing a query per identity.

    Keys are the original login or email strings (emails lowercased).
    """
    out: dict[str, int] = {}
    cur = db._conn.cursor()
    cur.execute("SELECT id, logins, emails FROM authors")
    for author_id, logins_raw, emails_raw in cur.fetchall():
        try:
            logins = json.loads(logins_raw or "[]")
        except (TypeError, json.JSONDecodeError):
            logins = []
        try:
            emails = json.loads(emails_raw or "[]")
        except (TypeError, json.JSONDecodeError):
            emails = []
        for lg in logins:
            out[lg] = int(author_id)
        for em in emails:
            out[em.lower()] = int(author_id)
    return out
