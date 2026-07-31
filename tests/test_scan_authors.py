"""Tests for :mod:`whygraph.scan.authors` (author identity resolution).

Covers the pure helpers, then resolution against seeded fixtures. The
highest-value cases here are the *negative* ones — same display name stays
two rows, a shared address never merges — because a false merge silently
attributes one person's work to another and has no symptom, while a false
split is merely untidy.

``Repository.check_mailmap`` is stubbed throughout rather than writing a
real mailmap into a temp repo: the stub keeps these tests hermetic and
lets the git-unavailable degradation path be exercised by raising.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from rich.progress import Progress
from sqlmodel import func, select

from whygraph.db import get_session
from whygraph.db.models import Author, Commit, Issue, PullRequest
from whygraph.scan.authors import (
    AuthorResolver,
    _is_bot,
    _json_list,
    _login_from_noreply,
    _Union,
)
from whygraph.services.git import GitError

_MTR = "tsvetoslav.nikolov@mtr-design.com"
_PFIZER = "tsvetoslav.nikolov@pfizer.com"
_NOREPLY = "85567502+cvetty@users.noreply.github.com"
_SHARED = "noreply@github.com"


# --- fixtures ----------------------------------------------------------------


def _commit(
    sha: str,
    *,
    name: str,
    email: str,
    on_default_branch: int = 1,
    authored_at: str = "2026-01-01T00:00:00+00:00",
) -> Commit:
    return Commit(
        sha=sha,
        parent_shas="",
        author_name=name,
        author_email=email,
        authored_at=authored_at,
        committed_at=authored_at,
        subject=sha,
        body="",
        files_changed=1,
        insertions=1,
        deletions=0,
        scanned_at="2026-01-02T00:00:00+00:00",
        on_default_branch=on_default_branch,
    )


def _pr(
    number: int,
    *,
    author: str | None,
    triples: list[tuple[str, str, str]] | None = None,
    commit_titles: str | None = None,
) -> PullRequest:
    if commit_titles is None:
        commit_titles = json.dumps(
            [
                {
                    "oid": f"oid{number}{i}",
                    "headline": "work",
                    "author_login": login,
                    "author_name": name,
                    "author_email": email,
                }
                for i, (login, name, email) in enumerate(triples or [])
            ]
        )
    return PullRequest(
        number=number,
        title=f"PR {number}",
        state="MERGED",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        merged_at="2026-01-02T00:00:00+00:00",
        merge_commit_sha=None,
        head_sha=f"head{number}",
        base_ref="main",
        html_url=f"https://example.test/pr/{number}",
        labels="[]",
        author=author,
        fetched_at="2026-01-02T00:00:00+00:00",
        commit_titles=commit_titles,
    )


def _issue(number: int, *, author: str | None) -> Issue:
    return Issue(
        number=number,
        title=f"Issue {number}",
        body="",
        state="OPEN",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        author=author,
        html_url=f"https://example.test/issue/{number}",
        labels="[]",
        fetched_at="2026-01-02T00:00:00+00:00",
    )


class _StubRepo:
    """Duck-typed :class:`Repository` — only ``check_mailmap`` is called.

    The default instance is an *identity* mailmap (every contact echoed
    back), which is what a repository with no mailmap actually produces.
    """

    def __init__(
        self,
        mapping: dict[str, str] | None = None,
        *,
        raises: bool = False,
        ragged: bool = False,
    ) -> None:
        self._mapping = mapping or {}
        self._raises = raises
        self._ragged = ragged
        self.calls = 0

    def check_mailmap(self, contacts) -> tuple[str, ...]:
        self.calls += 1
        if self._raises:
            raise GitError("git unavailable")
        out = [self._mapping.get(_email_of(c), c) for c in contacts]
        return tuple(out[:-1]) if self._ragged else tuple(out)


def _email_of(contact: str) -> str:
    return contact.rpartition("<")[2].rstrip(">")


def _resolve(repo: _StubRepo | None = None) -> tuple[AuthorResolver, list[dict]]:
    """Run the crawler and return it plus the ``author`` rows it wrote."""
    resolver = AuthorResolver(Progress(), repository=repo or _StubRepo())
    resolver.run()
    with get_session() as session:
        rows = [a.model_dump() for a in session.exec(select(Author)).all()]
    return resolver, rows


# --- pure helpers ------------------------------------------------------------


@pytest.mark.parametrize(
    ("email", "expected"),
    [
        (_NOREPLY, "cvetty"),
        ("cvetty@users.noreply.github.com", "cvetty"),  # the older form
        (_MTR, None),
        ("weird+@users.noreply.github.com", None),  # empty local part
        (_SHARED, None),  # different domain entirely
    ],
)
def test_login_from_noreply(email: str, expected: str | None) -> None:
    assert _login_from_noreply(email) == expected


@pytest.mark.parametrize(
    ("email", "login", "expected"),
    [
        ("x@example.com", "github-actions[bot]", True),
        ("41898282+github-actions[bot]@users.noreply.github.com", None, True),
        (_MTR, "cvetty", False),
    ],
)
def test_is_bot(email: str, login: str | None, expected: bool) -> None:
    assert _is_bot(email, login) is expected


@pytest.mark.parametrize("raw", [None, "", "{}", "not json", '"a string"'])
def test_json_list_never_raises(raw: str | None) -> None:
    assert _json_list(raw) == []


def test_union_is_transitive_idempotent_and_namespaced() -> None:
    u = _Union()
    u.union(("email", "a"), ("login", "x"))
    u.union(("login", "x"), ("email", "b"))
    u.union(("email", "a"), ("login", "x"))  # idempotent re-union

    assert u.find(("email", "a")) == u.find(("email", "b"))
    # A login and an identical display-name/email string are distinct nodes.
    u.add(("email", "x"))
    assert u.find(("email", "x")) != u.find(("login", "x"))


# --- the headline case: this repo's real identities --------------------------


def _seed_real_identities(session) -> None:
    """§0's four real identities plus §3.3's three real GitHub triples."""
    session.add(
        _commit(
            "d1", name="cvetty", email=_MTR, authored_at="2026-01-01T00:00:00+00:00"
        )
    )
    session.add(
        _commit(
            "d2", name="cvetty", email=_MTR, authored_at="2026-02-01T00:00:00+00:00"
        )
    )
    session.add(
        _commit(
            "d3", name="cvetty", email=_MTR, authored_at="2026-03-01T00:00:00+00:00"
        )
    )
    session.add(_commit("n1", name="Tsvetoslav Nikolov", email=_NOREPLY))
    session.add(_commit("n2", name="Tsvetoslav Nikolov", email=_NOREPLY))
    # The @pfizer.com address is visible ONLY in PR-origin rows.
    session.add(_commit("o1", name="cvetty", email=_MTR, on_default_branch=0))
    session.add(_commit("o2", name="cvetty", email=_PFIZER, on_default_branch=0))
    session.add(
        _pr(
            1,
            author="cvetty",
            triples=[
                ("cvetty", "cvetty", _MTR),
                ("cvetty", "cvetty", _PFIZER),
                ("cvetty", "Tsvetoslav Nikolov", _NOREPLY),
            ],
        )
    )
    session.commit()


def test_real_identities_resolve_to_one_row(whygraph_db_initialized: Path) -> None:
    """Four raw identities across three emails and two names — one human."""
    with get_session() as session:
        _seed_real_identities(session)

    resolver, rows = _resolve()

    assert resolver.error is None
    assert len(rows) == 1
    row = rows[0]
    assert row["primary_login"] == "cvetty"
    assert row["primary_email"] == _MTR  # 3 default-branch commits vs 2 vs 0
    assert row["primary_name"] == "cvetty"
    assert json.loads(row["emails"]) == sorted([_MTR, _PFIZER, _NOREPLY])
    assert json.loads(row["logins"]) == ["cvetty"]
    assert json.loads(row["names"]) == ["Tsvetoslav Nikolov", "cvetty"]
    assert row["pr_count"] == 1
    assert row["issue_count"] == 0


def test_pr_origin_only_email_lands_in_emails(whygraph_db_initialized: Path) -> None:
    """The @pfizer.com address appears only in on_default_branch=0 rows, so
    this pins the ordering invariant: resolution must run after Phase 2."""
    with get_session() as session:
        _seed_real_identities(session)

    _, rows = _resolve()

    assert _PFIZER in json.loads(rows[0]["emails"])


def test_commit_count_is_the_db_default_branch_count(
    whygraph_db_initialized: Path,
) -> None:
    """``commit_count`` equals the DB's own default-branch count for the
    cluster's emails — asserted as an identity, never against a constant,
    because ``on_default_branch`` records scan-time state."""
    with get_session() as session:
        _seed_real_identities(session)

    _, rows = _resolve()
    emails = json.loads(rows[0]["emails"])

    with get_session() as session:
        expected = session.exec(
            select(func.count())
            .select_from(Commit)
            .where(Commit.on_default_branch == 1)
            .where(Commit.author_email.in_(emails))
        ).one()
        all_rows = session.exec(
            select(func.count())
            .select_from(Commit)
            .where(Commit.author_email.in_(emails))
        ).one()

    assert rows[0]["commit_count"] == expected
    # PR-origin rows are the same work as their squash commit — excluded.
    assert rows[0]["commit_count"] < all_rows


# --- the false-merge guards --------------------------------------------------


def test_same_display_name_stays_two_rows(whygraph_db_initialized: Path) -> None:
    """The most important test here: name is never merge evidence, because
    "Alex Kim" is not one person globally."""
    with get_session() as session:
        session.add(_commit("a1", name="Alex Kim", email="alex@one.example"))
        session.add(_commit("a2", name="Alex Kim", email="alex@two.example"))
        session.commit()

    _, rows = _resolve()

    assert len(rows) == 2
    assert {r["primary_email"] for r in rows} == {
        "alex@one.example",
        "alex@two.example",
    }


def test_shared_email_does_not_merge_two_logins(whygraph_db_initialized: Path) -> None:
    """``noreply@github.com`` is shared across users — unioning on it would
    collapse the whole team. It is still recorded in both rows."""
    with get_session() as session:
        session.add(_commit("s1", name="Alice", email="alice@example.com"))
        session.add(_commit("s2", name="Bob", email="bob@example.com"))
        session.add(_commit("s3", name="Web Flow", email=_SHARED))
        session.add(
            _pr(1, author="alice", triples=[("alice", "Alice", "alice@example.com")])
        )
        session.add(_pr(2, author="bob", triples=[("bob", "Bob", "bob@example.com")]))
        # GitHub reports the shared address for both logins.
        session.add(_pr(3, author="alice", triples=[("alice", "Alice", _SHARED)]))
        session.add(_pr(4, author="bob", triples=[("bob", "Bob", _SHARED)]))
        session.commit()

    _, rows = _resolve()

    assert len(rows) == 2
    by_login = {r["primary_login"]: r for r in rows}
    assert set(by_login) == {"alice", "bob"}
    for row in rows:
        assert _SHARED in json.loads(row["emails"])
        # …and it never becomes anyone's primary.
        assert row["primary_email"] != _SHARED


def test_unclaimed_shared_email_keeps_its_own_row(
    whygraph_db_initialized: Path,
) -> None:
    """A shared address no login claimed still forms a row — it is never
    used as merge evidence, but it does not disappear either."""
    with get_session() as session:
        session.add(_commit("u1", name="Web Flow", email=_SHARED))
        session.commit()

    _, rows = _resolve()

    assert [json.loads(r["emails"]) for r in rows] == [[_SHARED]]


def test_bot_forms_its_own_cluster(whygraph_db_initialized: Path) -> None:
    with get_session() as session:
        session.add(_commit("h1", name="Alice", email="alice@example.com"))
        session.add(
            _commit(
                "b1",
                name="github-actions[bot]",
                email="41898282+github-actions[bot]@users.noreply.github.com",
            )
        )
        session.commit()

    _, rows = _resolve()

    assert len(rows) == 2
    bot = [r for r in rows if r["primary_login"] == "github-actions[bot]"]
    human = [r for r in rows if r["primary_email"] == "alice@example.com"]
    assert len(bot) == 1 and len(human) == 1
    assert "alice@example.com" not in json.loads(bot[0]["emails"])


# --- offline / partial GitHub data -------------------------------------------


def test_noreply_parse_works_with_no_pr_rows(whygraph_db_initialized: Path) -> None:
    """Signal B is pure string work, so a --no-remote scan still recovers
    the login behind a GitHub noreply address."""
    with get_session() as session:
        session.add(_commit("nb1", name="Tsvetoslav Nikolov", email=_NOREPLY))
        session.commit()

    _, rows = _resolve()

    assert len(rows) == 1
    assert rows[0]["primary_login"] == "cvetty"
    assert json.loads(rows[0]["logins"]) == ["cvetty"]


def test_pr_author_who_never_committed_gets_a_row(
    whygraph_db_initialized: Path,
) -> None:
    with get_session() as session:
        session.add(_pr(1, author="reviewer", triples=[]))
        session.commit()

    _, rows = _resolve()

    assert len(rows) == 1
    assert rows[0]["primary_login"] == "reviewer"
    assert rows[0]["commit_count"] == 0
    assert rows[0]["pr_count"] == 1
    assert rows[0]["first_seen"] is None and rows[0]["last_seen"] is None


def test_null_pr_author_produces_no_phantom_row(
    whygraph_db_initialized: Path,
) -> None:
    """A deleted GitHub account leaves ``author IS NULL`` — no row for None."""
    with get_session() as session:
        session.add(_commit("g1", name="Alice", email="alice@example.com"))
        session.add(_pr(1, author=None, triples=[]))
        session.commit()

    resolver, rows = _resolve()

    assert resolver.error is None
    assert len(rows) == 1
    assert rows[0]["primary_email"] == "alice@example.com"


def test_issue_counts_are_attributed_to_the_login(
    whygraph_db_initialized: Path,
) -> None:
    """Synthetic by necessity — this repo has zero issues scanned."""
    with get_session() as session:
        session.add(_commit("i1", name="Alice", email="alice@example.com"))
        session.add(
            _pr(1, author="alice", triples=[("alice", "Alice", "alice@example.com")])
        )
        session.add(_issue(10, author="alice"))
        session.add(_issue(11, author="alice"))
        session.add(_issue(12, author="bob"))
        session.commit()

    _, rows = _resolve()

    by_login = {r["primary_login"]: r for r in rows}
    assert by_login["alice"]["issue_count"] == 2
    assert by_login["bob"]["issue_count"] == 1
    assert by_login["bob"]["commit_count"] == 0


def test_malformed_commit_titles_are_skipped(whygraph_db_initialized: Path) -> None:
    with get_session() as session:
        session.add(_commit("m1", name="Alice", email="alice@example.com"))
        session.add(_pr(1, author="alice", commit_titles="}{ not json"))
        session.add(_pr(2, author="alice", commit_titles=json.dumps(["a string", 7])))
        session.add(
            _pr(3, author="alice", triples=[("alice", "Alice", "alice@example.com")])
        )
        session.commit()

    resolver, rows = _resolve()

    assert resolver.error is None
    assert len(rows) == 1
    assert rows[0]["primary_login"] == "alice"
    assert rows[0]["pr_count"] == 3


# --- determinism -------------------------------------------------------------


def test_two_consecutive_runs_are_byte_identical(
    whygraph_db_initialized: Path,
) -> None:
    with get_session() as session:
        _seed_real_identities(session)
        session.add(_commit("z1", name="Alice", email="alice@example.com"))
        session.add(_commit("z2", name="Bob", email="bob@example.com"))
        session.commit()

    _, first = _resolve()
    _, second = _resolve()

    assert first == second
    assert [r["id"] for r in second] == list(range(1, len(second) + 1))


# --- Signal 0 · the mailmap --------------------------------------------------


def test_mailmap_merges_emails_nothing_else_could(
    whygraph_db_initialized: Path,
) -> None:
    """Two ordinary addresses with no GitHub data and no noreply form —
    the case only a human-curated mailmap can fix."""
    with get_session() as session:
        session.add(_commit("mm1", name="Real Person", email="real@example.com"))
        session.add(_commit("mm2", name="rp", email="alias@example.com"))
        session.commit()

    repo = _StubRepo({"alias@example.com": "Real Person <real@example.com>"})
    _, rows = _resolve(repo)

    assert repo.calls == 1  # ONE subprocess for every identity
    assert len(rows) == 1
    assert json.loads(rows[0]["emails"]) == ["alias@example.com", "real@example.com"]


def test_mailmap_name_outranks_the_frequency_winner(
    whygraph_db_initialized: Path,
) -> None:
    """``primary_name`` is what a contributors list displays, so a curated
    name beats a tally — even when the tally says otherwise."""
    with get_session() as session:
        session.add(_commit("f1", name="cvetty", email=_MTR))
        session.add(_commit("f2", name="cvetty", email=_MTR))
        session.add(_commit("f3", name="cvetty", email=_MTR))
        session.commit()

    repo = _StubRepo({_MTR: f"Tsvetoslav Nikolov <{_MTR}>"})
    _, rows = _resolve(repo)

    assert rows[0]["primary_name"] == "Tsvetoslav Nikolov"
    assert "cvetty" in json.loads(rows[0]["names"])


def test_mailmap_cannot_defeat_the_shared_address_denylist(
    whygraph_db_initialized: Path,
) -> None:
    """Even a careless mailmap mapping a shared address must not merge the
    identities behind it."""
    with get_session() as session:
        session.add(_commit("d1", name="Alice", email="alice@example.com"))
        session.add(_commit("d2", name="Web Flow", email=_SHARED))
        session.commit()

    # The mailmap claims the shared address belongs to Alice.
    repo = _StubRepo({_SHARED: "Alice <alice@example.com>"})
    _, rows = _resolve(repo)

    assert len(rows) == 2
    alice = [r for r in rows if r["primary_email"] == "alice@example.com"][0]
    assert _SHARED not in json.loads(alice["emails"])


def test_mailmap_failure_degrades_to_signals_abc(
    whygraph_db_initialized: Path,
) -> None:
    """git absent / not a work tree: resolution completes on A/B/C, sets a
    summary, and leaves ``error`` None — a scan never fails because
    identity resolution did."""
    with get_session() as session:
        session.add(_commit("gf1", name="Tsvetoslav Nikolov", email=_NOREPLY))
        session.commit()

    resolver, rows = _resolve(_StubRepo(raises=True))

    assert resolver.error is None
    assert resolver.summary and "identit" in resolver.summary
    assert len(rows) == 1
    assert rows[0]["primary_login"] == "cvetty"  # Signal B still fired


def test_ragged_mailmap_output_is_a_noop(whygraph_db_initialized: Path) -> None:
    """``check-mailmap`` output is positional, so a short list must be
    discarded rather than mis-zipped onto the inputs."""
    with get_session() as session:
        session.add(_commit("r1", name="Real Person", email="real@example.com"))
        session.add(_commit("r2", name="rp", email="alias@example.com"))
        session.commit()

    repo = _StubRepo(
        {"alias@example.com": "Real Person <real@example.com>"}, ragged=True
    )
    _, rows = _resolve(repo)

    # No merge — the mapping was thrown away rather than applied to the
    # wrong contact.
    assert len(rows) == 2
