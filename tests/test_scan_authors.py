"""Identity-merge cases for `whygraph.scan.authors.build_authors`."""

from __future__ import annotations

from pathlib import Path

import pytest

from whygraph.scan import authors as authors_module
from whygraph.scan.db import Database
from whygraph.scan.git import Commit
from whygraph.scan.github import Issue, PullRequest


def _commit(sha: str, *, name: str, email: str, when: str = "2026-04-01T00:00:00+00:00") -> Commit:
    return Commit(
        sha=sha,
        parent_shas=[],
        author_name=name,
        author_email=email,
        authored_at=when,
        committed_at=when,
        subject="x",
        body="",
        files_changed=1,
        insertions=1,
        deletions=0,
    )


def _pr(
    number: int,
    *,
    author: str | None,
    commit_titles: list[dict] | None = None,
    when: str = "2026-04-01T00:00:00+00:00",
) -> PullRequest:
    return PullRequest(
        number=number,
        title="t",
        body="b",
        state="closed",
        draft=False,
        created_at=when,
        updated_at=when,
        closed_at=None,
        merged_at=when,
        merge_commit_sha=None,
        head_sha="0" * 40,
        head_ref="feat",
        base_ref="main",
        author=author,
        html_url=f"https://github.com/o/r/pull/{number}",
        labels=[],
        commit_titles=commit_titles or [],
    )


def _issue(number: int, *, author: str, when: str = "2026-04-01T00:00:00+00:00") -> Issue:
    return Issue(
        number=number,
        title="t",
        body="b",
        state="open",
        created_at=when,
        updated_at=when,
        closed_at=None,
        author=author,
        html_url=f"https://github.com/o/r/issues/{number}",
        labels=[],
    )


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "whygraph.db")


def test_login_localpart_match_merges_commit_email_with_pr_login(db: Database) -> None:
    """Commit email ``alice@example.com`` and PR login ``alice`` resolve
    to one identity: localpart matches login."""
    db.upsert_commit(_commit("a" * 40, name="Alice", email="alice@example.com"))
    db.upsert_pull_request(_pr(1, author="alice"))
    written = authors_module.build_authors(db)
    rows = db.iter_authors()
    assert written == 1
    assert len(rows) == 1
    row = rows[0]
    assert row["primary_login"] == "alice"
    assert row["primary_email"] == "alice@example.com"
    assert row["commit_count"] == 1
    assert row["pr_count"] == 1


def test_commit_titles_link_email_to_login(db: Database) -> None:
    """A PR whose opener login differs from any commit email still links
    via ``commit_titles[].author_email``."""
    db.upsert_commit(_commit("a" * 40, name="Alice", email="alice@anthropic.com"))
    db.upsert_pull_request(
        _pr(
            1,
            author="alice-gh",  # opener login doesn't match email localpart
            commit_titles=[
                {
                    "oid": "a" * 40,
                    "headline": "x",
                    "author_login": "alice-gh",
                    "author_name": "Alice",
                    "author_email": "alice@anthropic.com",
                }
            ],
        )
    )
    authors_module.build_authors(db)
    rows = db.iter_authors()
    assert len(rows) == 1
    row = rows[0]
    assert row["primary_login"] == "alice-gh"
    assert "alice@anthropic.com" in row["emails"]
    assert row["commit_count"] == 1
    assert row["pr_count"] == 1


def test_commit_only_author_has_no_login(db: Database) -> None:
    """A pure committer (no PR, no commit_titles entry) survives as a
    nameless-from-GitHub identity."""
    db.upsert_commit(_commit("a" * 40, name="Carol", email="carol@example.com"))
    authors_module.build_authors(db)
    rows = db.iter_authors()
    assert len(rows) == 1
    row = rows[0]
    assert row["primary_login"] is None
    assert row["primary_email"] == "carol@example.com"
    assert row["commit_count"] == 1
    assert row["pr_count"] == 0


def test_multiple_emails_same_login_merge(db: Database) -> None:
    """One person, two emails (work + personal), linked through
    commit_titles entries on a single PR. Should collapse to one row."""
    db.upsert_commit(_commit("a" * 40, name="Bob", email="bob@work.com"))
    db.upsert_commit(_commit("b" * 40, name="Bob", email="bob@home.com"))
    db.upsert_pull_request(
        _pr(
            1,
            author="bob-gh",
            commit_titles=[
                {
                    "oid": "a" * 40,
                    "headline": "x",
                    "author_login": "bob-gh",
                    "author_name": "Bob",
                    "author_email": "bob@work.com",
                },
                {
                    "oid": "b" * 40,
                    "headline": "y",
                    "author_login": "bob-gh",
                    "author_name": "Bob",
                    "author_email": "bob@home.com",
                },
            ],
        )
    )
    authors_module.build_authors(db)
    rows = db.iter_authors()
    assert len(rows) == 1
    row = rows[0]
    assert sorted(row["emails"]) == ["bob@home.com", "bob@work.com"]
    assert row["primary_login"] == "bob-gh"
    assert row["commit_count"] == 2
    assert row["pr_count"] == 1


def test_issue_opener_only_has_issue_count(db: Database) -> None:
    """An identity that only opened an issue (no commits, no PRs) gets
    issue_count=1 and zeros elsewhere."""
    db.upsert_issue(_issue(7, author="ghost-user"))
    authors_module.build_authors(db)
    rows = db.iter_authors()
    assert len(rows) == 1
    row = rows[0]
    assert row["primary_login"] == "ghost-user"
    assert row["issue_count"] == 1
    assert row["commit_count"] == 0
    assert row["pr_count"] == 0


def test_build_authors_is_idempotent(db: Database) -> None:
    """Running build_authors twice with the same data produces the same
    rows (counts don't accumulate, table is rebuilt fresh)."""
    db.upsert_commit(_commit("a" * 40, name="Alice", email="alice@example.com"))
    db.upsert_pull_request(_pr(1, author="alice"))
    authors_module.build_authors(db)
    first = db.iter_authors()
    authors_module.build_authors(db)
    second = db.iter_authors()
    assert len(first) == len(second) == 1
    assert first[0]["commit_count"] == second[0]["commit_count"] == 1
    assert first[0]["pr_count"] == second[0]["pr_count"] == 1


def test_resolve_author_by_login(db: Database) -> None:
    db.upsert_commit(_commit("a" * 40, name="Alice", email="alice@example.com"))
    db.upsert_pull_request(_pr(1, author="alice"))
    authors_module.build_authors(db)
    out = authors_module.resolve_author(db, "alice")
    assert out is not None
    assert out["primary_login"] == "alice"


def test_resolve_author_by_email(db: Database) -> None:
    db.upsert_commit(_commit("a" * 40, name="Alice", email="alice@example.com"))
    authors_module.build_authors(db)
    out = authors_module.resolve_author(db, "alice@example.com")
    assert out is not None
    assert out["primary_email"] == "alice@example.com"


def test_resolve_author_by_localpart(db: Database) -> None:
    """Inputting just the local-part of the email still resolves."""
    db.upsert_commit(_commit("a" * 40, name="Alice", email="alice@example.com"))
    authors_module.build_authors(db)
    out = authors_module.resolve_author(db, "alice")
    # login is None for commit-only authors; but localpart match should
    # still find the row via emails JSON.
    assert out is not None
    assert out["primary_email"] == "alice@example.com"


def test_resolve_author_unknown_returns_none(db: Database) -> None:
    db.upsert_commit(_commit("a" * 40, name="Alice", email="alice@example.com"))
    authors_module.build_authors(db)
    assert authors_module.resolve_author(db, "nobody") is None
    assert authors_module.resolve_author(db, "") is None


def test_author_lookup_table_keys_logins_and_emails(db: Database) -> None:
    db.upsert_commit(_commit("a" * 40, name="Alice", email="alice@example.com"))
    db.upsert_pull_request(_pr(1, author="alice"))
    authors_module.build_authors(db)
    table = authors_module.author_lookup_table(db)
    rows = db.iter_authors()
    expected_id = rows[0]["id"]
    assert table["alice"] == expected_id
    assert table["alice@example.com"] == expected_id
