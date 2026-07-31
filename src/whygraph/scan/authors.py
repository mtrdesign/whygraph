"""AuthorResolver — collapse a repository's raw identities into people.

One human routinely writes commits under several addresses (work laptop,
personal machine, GitHub's web UI) and under more than one display name,
so the obvious ``GROUP BY author_email`` reports the same person several
times while looking authoritative. This crawler rebuilds the ``author``
table so that one human is one row, and is the producer
:mod:`whygraph.db.models.author` has always named.

Resolution is **evidence-driven, never heuristic**, and the signals are
ordered by how much authority they carry:

0. **git's mailmap** (:meth:`Repository.check_mailmap`) — a human
   explicitly asserting "these contacts are me". It is the only signal
   that is a statement of intent rather than an inference, so it runs
   first and its *name* output also decides ``primary_name``. It is an
   enhancement, not a prerequisite: no mailmap, or no usable git, simply
   degrades to the signals below.
A. **GitHub's own triples** — ``pull_request.commit_titles`` entries
   already carry ``author_login`` / ``author_name`` / ``author_email``
   together, so GitHub is asserting the link and nothing is inferred.
B. **The GitHub noreply parse** — ``12345+login@users.noreply.github.com``
   deterministically yields ``login``. Pure string work, so it still
   resolves identities on repositories scanned with ``--no-remote``.
C. **Byte-equal emails** — implicit: the same address is the same node by
   construction.

What resolution deliberately refuses to do matters as much. It never
merges on display name ("Alex Kim" is not one person globally), never
fuzzy-matches addresses, and never treats a shared or bot address as
evidence — because **a false merge is worse than a false split**.
Collapsing two people silently attributes one person's work to another
with no symptom; leaving one person as two rows is merely untidy.

Two ordering invariants:

* The crawler runs **after** :class:`~whygraph.scan.pr_origin_enricher.PROriginEnricher`.
  An address can appear *only* in the ``on_default_branch=0`` rows that
  phase writes, and running earlier would make that identity invisible.
* ``commit_count`` / ``first_seen`` / ``last_seen`` count
  ``on_default_branch=1`` commits **only**, matching every other velocity
  query — a PR-origin commit is the same work as its squash commit, so
  counting both would double-count. The flag records "on the default
  branch *as of the last scan*", so these are DB truth, not git truth.

Output is fully deterministic — clusters sorted by a stable key, every
JSON array sorted, every tie broken lexicographically — so a table
rebuilt on each scan produces no diff noise.
"""

from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from rich.progress import Progress
from sqlmodel import delete, select

from whygraph.db import get_session
from whygraph.db.models.author import Author
from whygraph.db.models.commit import Commit as CommitRow
from whygraph.db.models.issue import Issue
from whygraph.db.models.pull_request import PullRequest
from whygraph.services.git import GitError, Repository

from .crawler import Crawler

_log = logging.getLogger(__name__)

_SHARED_EMAILS = frozenset(
    {
        "noreply@github.com",  # GitHub web-UI / merge commits — SHARED across users
        "action@github.com",
        "actions@github.com",
        "githubactions@github.com",
    }
)
"""Addresses that are not one person. A literal list, never derived.

An address here is still recorded in a cluster's ``emails`` — so nothing
disappears — but is NEVER used as merge evidence. Unioning on a shared
address collapses an entire team into a single row, which is exactly the
undetectable failure this module is built to avoid. Deliberately
incomplete (GitLab, Gerrit and enterprise CI have their own): it is a
frozenset literal precisely so extending it is a one-line change.
"""

_NOREPLY_SUFFIX = "@users.noreply.github.com"

_Node = tuple[str, str]
"""A namespaced identity node — ``("email", …)`` or ``("login", …)``.

Namespacing is load-bearing: a login and a display name are routinely the
same text, and a bare-string union-find would merge them by coincidence.
Names are never nodes at all (they are cluster attributes), because
same-name is the classic false-merge vector.
"""


def _json_list(raw: str | None) -> list:
    """Decode a JSON-encoded list column; empty list on anything malformed.

    Mirrors ``pr_origin_enricher.py``'s helper — duplicated rather than
    imported to keep the scan layer free of an upward dependency on the
    MCP server layer.
    """
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _login_from_noreply(email: str) -> str | None:
    """Signal B — the GitHub login behind a ``users.noreply.github.com`` address.

    Handles both forms GitHub has issued: the modern
    ``<id>+<login>@users.noreply.github.com`` and the older
    ``<login>@users.noreply.github.com``.

    Parameters
    ----------
    email : str
        Any email address; non-noreply addresses simply return ``None``.

    Returns
    -------
    str or None
        The parsed login, or ``None`` when the address is not a GitHub
        noreply address or its local part yields no usable login.
    """
    local = email.strip().lower()
    if not local.endswith(_NOREPLY_SUFFIX):
        return None
    local = local[: -len(_NOREPLY_SUFFIX)]
    if "+" in local:
        local = local.partition("+")[2]
    login = local.strip()
    # Reject anything that cannot be a login rather than inventing one —
    # a wrong login is a merge key, and a merge is the expensive mistake.
    if not login or any(ch in login for ch in " \t@<>+"):
        return None
    return login


def _is_bot(email: str, login: str | None) -> bool:
    """Whether a contact is a bot rather than a human.

    Recognizes the ``[bot]`` marker GitHub appends to app logins, in
    either the login itself or the local part of its noreply address
    (``41898282+github-actions[bot]@users.noreply.github.com``).

    Bots are not dropped — their commits are real and should stay
    countable — they just never merge across the bot/human boundary, so a
    bot forms its own cluster (:func:`_resolve`).
    """
    if login and "[bot]" in login.lower():
        return True
    return "[bot]" in email.partition("@")[0].lower()


def _format_contact(name: str, email: str) -> str:
    """Render a ``Name <email>`` contact for ``git check-mailmap``.

    A blank name is replaced with a placeholder: git needs the
    ``Name <email>`` shape, and a name the mailmap does not rewrite is
    ignored by the caller anyway (only a *changed* name is treated as an
    assertion).
    """
    return f"{name.strip() or 'unknown'} <{email}>"


def _split_contact(line: str) -> tuple[str, str] | None:
    """Parse one ``git check-mailmap`` output line into ``(name, email)``.

    Returns ``None`` for a line that is not in ``Name <email>`` shape, so
    unexpected output degrades to "no mapping" rather than to a wrong one.
    """
    line = line.strip()
    if not line.endswith(">") or "<" not in line:
        return None
    name, _, rest = line.rpartition("<")
    return name.strip(), rest[:-1].strip()


def _mailmap_pairs(
    repository: Repository, identities: list[tuple[str, str]]
) -> dict[tuple[str, str], tuple[str, str]]:
    """Signal 0 — map raw ``(name, email)`` contacts to their canonical form.

    One batched :meth:`Repository.check_mailmap` call for every identity
    in the repository.

    Parameters
    ----------
    repository : Repository
        The repository whose mailmap (``./.mailmap``, ``mailmap.file`` or
        ``mailmap.blob``) is consulted.
    identities : list[tuple[str, str]]
        Distinct ``(name, email)`` pairs to canonicalize.

    Returns
    -------
    dict[tuple[str, str], tuple[str, str]]
        Raw pair → canonical pair, for the pairs git actually returned.
        Empty when git is unavailable or fails (logged at DEBUG — Signal 0
        is an enhancement, so resolution proceeds on signals A/B/C), and
        empty when the output is ragged: the result is positional, so a
        short list must not be zipped onto the inputs.
    """
    if not identities:
        return {}
    contacts = [_format_contact(name, email) for name, email in identities]
    try:
        lines = repository.check_mailmap(contacts)
    except GitError as exc:
        _log.debug("mailmap unavailable; resolving without Signal 0: %s", exc)
        return {}
    if len(lines) != len(contacts):
        _log.debug(
            "check-mailmap returned %d lines for %d contacts; ignoring Signal 0",
            len(lines),
            len(contacts),
        )
        return {}
    pairs: dict[tuple[str, str], tuple[str, str]] = {}
    for raw, line in zip(identities, lines):
        parsed = _split_contact(line)
        if parsed is not None:
            pairs[raw] = parsed
    return pairs


class _Union:
    """Minimal union–find over :data:`_Node` values.

    Deliberately hand-rolled and tiny (no dependency): the interesting
    part of this module is *what may merge*, not the merging itself.
    """

    def __init__(self) -> None:
        self._parent: dict[_Node, _Node] = {}

    def add(self, node: _Node) -> None:
        """Register ``node`` as its own set if not already known."""
        self._parent.setdefault(node, node)

    def find(self, node: _Node) -> _Node:
        """Return the representative of ``node``'s set, path-compressing."""
        self.add(node)
        root = node
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[node] != root:
            self._parent[node], node = root, self._parent[node]
        return root

    def union(self, a: _Node, b: _Node) -> None:
        """Merge the sets containing ``a`` and ``b``.

        The smaller representative wins so the result does not depend on
        insertion order (plan §3.6 determinism).
        """
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        lo, hi = (ra, rb) if ra < rb else (rb, ra)
        self._parent[hi] = lo

    def nodes(self) -> list[_Node]:
        """Every known node, in sorted order."""
        return sorted(self._parent)


@dataclass
class _Cluster:
    """One resolved identity: its raw values plus its aggregates.

    Attributes
    ----------
    emails, logins, names : set[str]
        Every raw value the cluster absorbed. ``emails`` may include a
        shared address that was recorded but never merged on.
    primary_login, primary_name, primary_email : str or None
        The display values, chosen deterministically (see
        :func:`_resolve`).
    first_seen, last_seen : str or None
        ISO-8601 bounds over the cluster's default-branch commits.
    commit_count, pr_count, issue_count : int
        Aggregates; ``commit_count`` counts default-branch commits only.
    """

    emails: set[str] = field(default_factory=set)
    logins: set[str] = field(default_factory=set)
    names: set[str] = field(default_factory=set)
    primary_login: str | None = None
    primary_name: str | None = None
    primary_email: str | None = None
    first_seen: str | None = None
    last_seen: str | None = None
    commit_count: int = 0
    pr_count: int = 0
    issue_count: int = 0


def _resolve(session, repository: Repository) -> list[_Cluster]:
    """Resolve every raw identity in the DB into a sorted list of clusters.

    Reads ``commit``, ``pull_request`` and ``issue``, applies signals
    0/A/B/C (module docstring), then computes each cluster's display
    values and aggregates. Purely read-only — the caller owns the rewrite.

    Parameters
    ----------
    session : sqlmodel.Session
        Open session to read through.
    repository : Repository
        Used only for Signal 0. A repository whose ``check_mailmap``
        raises is tolerated (:func:`_mailmap_pairs`).

    Returns
    -------
    list[_Cluster]
        Fully populated clusters, ordered by
        ``(primary_login, primary_email, min(emails))`` so a rebuild is
        byte-identical given identical input.
    """
    commit_rows = session.exec(
        select(
            CommitRow.author_name,
            CommitRow.author_email,
            CommitRow.authored_at,
            CommitRow.on_default_branch,
        )
    ).all()
    pr_rows = session.exec(select(PullRequest.author, PullRequest.commit_titles)).all()
    issue_authors = session.exec(select(Issue.author)).all()

    union = _Union()
    names: dict[_Node, set[str]] = defaultdict(set)
    # Shared addresses recorded onto the identity that used them without
    # ever merging on them (§3.4).
    extra_emails: dict[_Node, set[str]] = defaultdict(set)
    commits_by_email: dict[str, list[tuple[str, str, int]]] = defaultdict(list)
    pr_counts: Counter[str] = Counter()
    issue_counts: Counter[str] = Counter()

    def _add_email(value: str | None) -> str | None:
        email = (value or "").strip()
        if not email:
            return None
        union.add(("email", email))
        return email

    def _add_login(value: str | None) -> str | None:
        login = (value or "").strip()
        if not login:
            return None
        union.add(("login", login))
        return login

    # --- raw material -----------------------------------------------------
    identities: set[tuple[str, str]] = set()
    for author_name, author_email, authored_at, on_default_branch in commit_rows:
        email = _add_email(author_email)
        if email is None:
            continue
        name = (author_name or "").strip()
        identities.add((name, email))
        if name:
            names[("email", email)].add(name)
        commits_by_email[email].append(
            (name, authored_at or "", int(on_default_branch or 0))
        )

    triples: list[tuple[str, str, str]] = []
    for pr_author, commit_titles in pr_rows:
        login = _add_login(pr_author)
        if login is not None:
            # A reviewer who opened PRs but never committed still gets a
            # row — hence a standalone login node.
            pr_counts[login] += 1
        for entry in _json_list(commit_titles):
            if not isinstance(entry, dict):
                continue  # a malformed payload skips its entry, not the PR
            triple = (
                (entry.get("author_login") or "").strip(),
                (entry.get("author_name") or "").strip(),
                (entry.get("author_email") or "").strip(),
            )
            triples.append(triple)
            _add_login(triple[0])
            email = _add_email(triple[2])
            if email is not None and triple[1]:
                identities.add((triple[1], email))

    for issue_author in issue_authors:
        login = _add_login(issue_author)
        if login is not None:
            issue_counts[login] += 1

    # --- Signal 0 · the mailmap ------------------------------------------
    mailmap = _mailmap_pairs(repository, sorted(identities))
    # email → the proper name the mailmap asserted for it (§3.6).
    asserted_names: dict[str, str] = {}
    for (raw_name, raw_email), (canon_name, canon_email) in sorted(mailmap.items()):
        if canon_email and canon_email != raw_email:
            union.add(("email", canon_email))
            mergeable = (
                raw_email not in _SHARED_EMAILS
                and canon_email not in _SHARED_EMAILS
                and _is_bot(raw_email, None) == _is_bot(canon_email, None)
            )
            if mergeable:
                union.union(("email", raw_email), ("email", canon_email))
        if canon_name and canon_name != raw_name:
            asserted_names.setdefault(canon_email or raw_email, canon_name)
            asserted_names.setdefault(raw_email, canon_name)

    # --- Signal A · GitHub's login ↔ email ↔ name triples -----------------
    for login, name, email in triples:
        if email and email not in _SHARED_EMAILS and name:
            names[("email", email)].add(name)
        elif login and name:
            names[("login", login)].add(name)
        if not (login and email):
            continue
        if email in _SHARED_EMAILS:
            # Recorded so it does not disappear, but never merge evidence.
            extra_emails[("login", login)].add(email)
            continue
        union.union(("login", login), ("email", email))

    # --- Signal B · the noreply parse ------------------------------------
    for kind, value in union.nodes():
        if kind != "email" or value in _SHARED_EMAILS:
            continue
        login = _login_from_noreply(value)
        if login is None:
            continue
        union.add(("login", login))
        union.union(("email", value), ("login", login))

    # --- Signal C is implicit: byte-equal emails are the same node. -------

    groups: dict[_Node, list[_Node]] = defaultdict(list)
    for node in union.nodes():
        groups[union.find(node)].append(node)

    clusters: list[_Cluster] = []
    for members in groups.values():
        cluster = _Cluster()
        for node in members:
            kind, value = node
            if kind == "email":
                cluster.emails.add(value)
            else:
                cluster.logins.add(value)
                cluster.names |= names.get(node, set())
            cluster.emails |= extra_emails.get(node, set())
        for email in cluster.emails:
            # Names behind a shared address belong to whoever used it, so
            # they are NOT attributed here (that would be a false merge in
            # all but name).
            if email not in _SHARED_EMAILS:
                cluster.names |= names.get(("email", email), set())
        clusters.append(cluster)

    # A shared address is an attribute of the identity that used it, so
    # the standalone node it also forms is dropped once some cluster has
    # claimed it. An *unclaimed* shared address keeps its own row rather
    # than disappearing (§3.4).
    claimed: set[str] = set().union(*extra_emails.values()) if extra_emails else set()
    clusters = [
        c
        for c in clusters
        if c.logins or not (c.emails <= _SHARED_EMAILS and c.emails <= claimed)
    ]

    for cluster in clusters:
        _populate(cluster, commits_by_email, pr_counts, issue_counts, asserted_names)

    clusters.sort(
        key=lambda c: (
            c.primary_login or "",
            c.primary_email or "",
            min(c.emails) if c.emails else "",
        )
    )
    return clusters


def _populate(
    cluster: _Cluster,
    commits_by_email: dict[str, list[tuple[str, str, int]]],
    pr_counts: Counter[str],
    issue_counts: Counter[str],
    asserted_names: dict[str, str],
) -> None:
    """Fill in one cluster's aggregates and display values, deterministically.

    ``commit_count`` / ``first_seen`` / ``last_seen`` cover
    ``on_default_branch = 1`` commits only. Every "most frequent" choice
    breaks ties lexicographically, and ``primary_name`` prefers the
    mailmap's proper name over any frequency count — it is the string a
    human-facing contributors list displays, so a curated name outranks a
    tally.
    """
    email_commits: Counter[str] = Counter()
    name_commits: Counter[str] = Counter()
    timestamps: list[str] = []
    for email in cluster.emails:
        for name, authored_at, on_default_branch in commits_by_email.get(email, ()):
            if on_default_branch != 1:
                continue
            email_commits[email] += 1
            if name:
                name_commits[name] += 1
            if authored_at:
                timestamps.append(authored_at)

    cluster.commit_count = sum(email_commits.values())
    cluster.first_seen = min(timestamps) if timestamps else None
    cluster.last_seen = max(timestamps) if timestamps else None
    cluster.pr_count = sum(pr_counts[login] for login in cluster.logins)
    cluster.issue_count = sum(issue_counts[login] for login in cluster.logins)

    # A shared address never becomes anyone's primary — it is not theirs.
    candidates = {e for e in cluster.emails if e not in _SHARED_EMAILS}
    candidates = candidates or cluster.emails
    if candidates:
        cluster.primary_email = min(candidates, key=lambda e: (-email_commits[e], e))

    if len(cluster.logins) == 1:
        cluster.primary_login = next(iter(cluster.logins))
    elif cluster.logins:
        cluster.primary_login = min(
            cluster.logins, key=lambda login: (-pr_counts[login], login)
        )

    mailmap_name = asserted_names.get(cluster.primary_email or "")
    if mailmap_name is None:
        for email in sorted(cluster.emails):
            if email in asserted_names:
                mailmap_name = asserted_names[email]
                break
    if mailmap_name is not None:
        cluster.primary_name = mailmap_name
        cluster.names.add(mailmap_name)
    elif name_commits:
        cluster.primary_name = min(name_commits, key=lambda n: (-name_commits[n], n))
    elif cluster.names:
        cluster.primary_name = min(cluster.names)


def _to_author_row(cluster: _Cluster, author_id: int) -> Author:
    """Build one ``author`` row from a populated cluster.

    ``emails`` / ``logins`` / ``names`` are ``TEXT`` columns holding
    JSON-encoded arrays, not list columns — they are serialized here with
    ``json.dumps(sorted(...))``, which is also what makes a rebuilt table
    byte-identical.

    ``author_id`` is assigned from the cluster's sorted position rather
    than left to the database. It keeps a rebuild deterministic by
    construction instead of by relying on SQLite reusing rowids after the
    ``DELETE``, and it keeps the multi-row insert off SQLAlchemy's
    "sentinel column" path, which rejects a batch of rows whose nullable
    autoincrement primary key is unset. The value stays ephemeral — see
    :mod:`whygraph.db.models.author`; nothing may reference it across
    scans.
    """
    return Author(
        id=author_id,
        primary_login=cluster.primary_login,
        primary_name=cluster.primary_name,
        primary_email=cluster.primary_email,
        emails=json.dumps(sorted(cluster.emails)),
        logins=json.dumps(sorted(cluster.logins)),
        names=json.dumps(sorted(cluster.names)),
        first_seen=cluster.first_seen,
        last_seen=cluster.last_seen,
        commit_count=cluster.commit_count,
        pr_count=cluster.pr_count,
        issue_count=cluster.issue_count,
    )


class AuthorResolver(Crawler):
    """Rebuild the ``author`` table from commits + GitHub authorship.

    Runs after PR-origin recovery (see the module docstring for why the
    ordering is forced by data rather than taste) and rewrites the whole
    table in one transaction — ``author.id`` is documented as ephemeral,
    so a rebuild is the intended shape.

    Best-effort by design: a failure here is logged and reported through
    :attr:`Crawler.summary`, leaving :attr:`Crawler.error` ``None`` so a
    scan never fails because identity resolution did. The existing table
    is left untouched in that case.

    Parameters
    ----------
    progress : rich.progress.Progress
        Shared Progress instance owned by the orchestrator.
    repository : Repository
        Used only for Signal 0 (the mailmap). Resolution still works
        without a usable git repository.
    """

    def __init__(self, progress: Progress, *, repository: Repository) -> None:
        # "authors" is the THREAD name and the key the closing results
        # panel looks this crawler up by — a mismatch renders "— skipped".
        super().__init__("authors", progress, total=None)
        self._repository = repository

    def work(self) -> None:
        try:
            with get_session() as session:
                clusters = _resolve(session, self._repository)
                self.set_total(len(clusters))
                session.exec(delete(Author))
                for index, cluster in enumerate(clusters, start=1):
                    session.add(_to_author_row(cluster, index))
                    self.advance(1)
        except Exception as exc:  # noqa: BLE001 — best-effort phase
            _log.warning("author resolution failed; table left as-is: %s", exc)
            self.summary = "resolution skipped"
            return

        raw = sum(len(c.emails) + len(c.logins) for c in clusters)
        noun = "identity" if len(clusters) == 1 else "identities"
        self.summary = f"{len(clusters)} {noun} from {raw} raw"
