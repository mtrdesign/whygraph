"""Tests for the ``whygraph_area_history`` MCP tool and its underlying
rename-chain alias resolver.

Each test seeds an isolated WhyGraph DB with ``commit_file_change`` rows
(and matching ``commit`` rows) so the tool's path → alias → commit join
runs end to end without needing a real working tree.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from whygraph.db import get_session
from whygraph.db.models import Commit, CommitFileChange
from whygraph.mcp.area_history import whygraph_area_history
from whygraph.mcp.errors import WhyGraphError
from whygraph.mcp.path_history import resolve_path_aliases


def _commit(
    sha: str,
    *,
    subject: str,
    committed_at: str,
) -> Commit:
    return Commit(
        sha=sha,
        parent_shas="",
        author_name="Test User",
        author_email="tester@example.com",
        authored_at=committed_at,
        committed_at=committed_at,
        subject=subject,
        body="",
        files_changed=1,
        insertions=1,
        deletions=0,
        scanned_at="2026-05-26T00:00:00+00:00",
        llm_description="diff summary",
    )


def _change(
    *,
    commit_sha: str,
    path: str,
    change_type: str = "M",
    renamed_from: str | None = None,
    similarity: int | None = None,
) -> CommitFileChange:
    return CommitFileChange(
        commit_sha=commit_sha,
        path=path,
        change_type=change_type,
        renamed_from=renamed_from,
        similarity=similarity,
        lines_added=1,
        lines_deleted=0,
    )


def _seed_rename_history(session) -> tuple[str, str, str, str]:
    """Seed a 4-commit history: legacy.py → mid.py → final.py.

    Commits, oldest to newest:
        c_old        — A legacy.py
        c_mid        — M legacy.py     (an edit while still named legacy.py)
        c_rename1    — R legacy.py → mid.py
        c_rename2    — R mid.py → final.py
        c_recent     — M final.py
    """
    session.add(_commit("sha_old", subject="add legacy", committed_at="2025-01-01T00:00:00+00:00"))
    session.add(_commit("sha_mid", subject="tweak legacy", committed_at="2025-02-01T00:00:00+00:00"))
    session.add(_commit("sha_rename1", subject="rename to mid", committed_at="2025-03-01T00:00:00+00:00"))
    session.add(_commit("sha_rename2", subject="rename to final", committed_at="2025-04-01T00:00:00+00:00"))
    session.add(_commit("sha_recent", subject="edit final", committed_at="2025-05-01T00:00:00+00:00"))

    session.add(_change(commit_sha="sha_old", path="legacy.py", change_type="A"))
    session.add(_change(commit_sha="sha_mid", path="legacy.py", change_type="M"))
    session.add(
        _change(
            commit_sha="sha_rename1",
            path="mid.py",
            change_type="R",
            renamed_from="legacy.py",
            similarity=100,
        )
    )
    session.add(
        _change(
            commit_sha="sha_rename2",
            path="final.py",
            change_type="R",
            renamed_from="mid.py",
            similarity=100,
        )
    )
    session.add(_change(commit_sha="sha_recent", path="final.py", change_type="M"))

    return "sha_old", "sha_mid", "sha_rename1", "sha_recent"


def test_resolve_path_aliases_walks_rename_chain(
    whygraph_db_initialized: Path,
) -> None:
    with get_session() as session:
        _seed_rename_history(session)
        session.commit()
        aliases = resolve_path_aliases(session, "final.py")

    assert aliases == {"final.py", "mid.py", "legacy.py"}


def test_resolve_path_aliases_returns_only_seed_when_no_renames(
    whygraph_db_initialized: Path,
) -> None:
    with get_session() as session:
        session.add(_commit("sha", subject="add", committed_at="2025-01-01T00:00:00+00:00"))
        session.add(_change(commit_sha="sha", path="foo.py", change_type="A"))
        session.commit()
        aliases = resolve_path_aliases(session, "foo.py")

    assert aliases == {"foo.py"}


def test_area_history_tool_traverses_renames(
    whygraph_db_initialized: Path,
) -> None:
    """Asking for area-history of the current path surfaces commits that
    only ever touched the predecessor paths."""
    with get_session() as session:
        _seed_rename_history(session)
        session.commit()

    result = whygraph_area_history("final.py")

    shas = [item["commit"]["sha"] for item in result["evidence"]]
    # Newest first.
    assert shas == ["sha_recent", "sha_rename2", "sha_rename1", "sha_mid", "sha_old"]
    assert result["path"] == "final.py"
    assert result["include_renames"] is True


def test_area_history_tool_can_skip_rename_chain(
    whygraph_db_initialized: Path,
) -> None:
    with get_session() as session:
        _seed_rename_history(session)
        session.commit()

    result = whygraph_area_history("final.py", include_renames=False)

    shas = [item["commit"]["sha"] for item in result["evidence"]]
    # Only commits that literally touched ``final.py`` — predecessors omitted.
    assert shas == ["sha_recent", "sha_rename2"]


def test_area_history_tool_respects_limit(
    whygraph_db_initialized: Path,
) -> None:
    with get_session() as session:
        _seed_rename_history(session)
        session.commit()

    result = whygraph_area_history("final.py", limit=2)

    assert len(result["evidence"]) == 2
    assert result["evidence"][0]["commit"]["sha"] == "sha_recent"


def test_area_history_tool_rejects_invalid_inputs() -> None:
    with pytest.raises(WhyGraphError, match="path is required"):
        whygraph_area_history("")
    with pytest.raises(WhyGraphError, match="limit must be >= 1"):
        whygraph_area_history("foo.py", limit=0)
