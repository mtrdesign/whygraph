"""Tests for :func:`whygraph.core.gitignore.ensure_gitignore_entries`."""

from __future__ import annotations

from pathlib import Path

from whygraph.core.gitignore import ensure_gitignore_entries

ENTRIES = ["whygraph.toml", ".whygraph/", ".codegraph/"]


def test_creates_gitignore_when_absent(tmp_path: Path) -> None:
    added = ensure_gitignore_entries(tmp_path, ENTRIES)

    assert added == ENTRIES
    gitignore = tmp_path / ".gitignore"
    assert gitignore.exists()
    body = gitignore.read_text(encoding="utf-8")
    assert "# WhyGraph" in body
    for entry in ENTRIES:
        assert entry in body.splitlines()


def test_appends_only_missing_entries(tmp_path: Path) -> None:
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("whygraph.toml\n", encoding="utf-8")

    added = ensure_gitignore_entries(tmp_path, ENTRIES)

    assert added == [".whygraph/", ".codegraph/"]
    lines = gitignore.read_text(encoding="utf-8").splitlines()
    # The pre-existing entry is not duplicated.
    assert lines.count("whygraph.toml") == 1


def test_idempotent_no_duplicates_on_rerun(tmp_path: Path) -> None:
    ensure_gitignore_entries(tmp_path, ENTRIES)
    first = (tmp_path / ".gitignore").read_text(encoding="utf-8")

    added = ensure_gitignore_entries(tmp_path, ENTRIES)
    second = (tmp_path / ".gitignore").read_text(encoding="utf-8")

    assert added == []
    assert first == second


def test_slash_insensitive_match(tmp_path: Path) -> None:
    """A pre-seeded ``.whygraph/`` is not re-added as ``.whygraph``."""
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text(".whygraph/\n.codegraph/\n", encoding="utf-8")

    added = ensure_gitignore_entries(tmp_path, ENTRIES)

    assert added == ["whygraph.toml"]
    body = gitignore.read_text(encoding="utf-8")
    assert body.count(".whygraph/") == 1
    assert body.count(".codegraph/") == 1


def test_preserves_existing_user_content(tmp_path: Path) -> None:
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("node_modules/\n*.log\n", encoding="utf-8")

    ensure_gitignore_entries(tmp_path, ENTRIES)

    body = gitignore.read_text(encoding="utf-8")
    assert "node_modules/" in body
    assert "*.log" in body
    # User content stays first; WhyGraph block appended after.
    assert body.index("node_modules/") < body.index("whygraph.toml")
