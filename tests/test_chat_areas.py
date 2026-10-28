"""Tests for ``chat/areas.py`` and the area / window filters built on it (plan 38 §5.8, §5.9)."""

from __future__ import annotations

import dataclasses
import json
import sqlite3

import pytest

from whygraph import core
from whygraph.chat import areas
from whygraph.chat.tools import ToolRegistry
from whygraph.core.config import AreaConfig, Config
from whygraph.mcp.errors import WhyGraphError


def _set_areas(monkeypatch: pytest.MonkeyPatch, *items: AreaConfig) -> None:
    monkeypatch.setattr(
        core,
        "_config",
        dataclasses.replace(
            core._config or Config(), areas=tuple(sorted(items, key=lambda a: a.name))
        ),
    )


def _area(name="checkout", paths=("src/checkout/",), labels=(), description="d"):
    return AreaConfig(name, description, tuple(paths), tuple(labels))


def test_predicates_escape_like_metacharacters(tmp_path) -> None:
    area = _area(paths=("src/a_b", "src/c%d/"))
    assert areas.path_predicates(area) == [
        ("src/a\\_b/%", "src/a_b"),
        ("src/c\\%d/%", None),
    ]
    con = sqlite3.connect(":memory:")
    con.execute("create table t(path text)")
    con.executemany(
        "insert into t values (?)",
        [
            ("src/a_b/x.py",),
            ("src/aXb/x.py",),
            ("src/a_b",),
            ("src/c%d/y",),
            ("src/cZd/y",),
        ],
    )
    hits = set()
    for pattern, exact in areas.path_predicates(area):
        for (p,) in con.execute(
            "select path from t where path like ? escape '\\'", (pattern,)
        ):
            hits.add(p)
        if exact:
            hits.add(exact)
    assert hits == {"src/a_b/x.py", "src/a_b", "src/c%d/y"}


def test_prefix_versus_exact() -> None:
    area = _area(paths=("src/checkout/", "docs/guide.md", "lib"))
    assert areas.path_in_area(area, "src/checkout/a.py")
    assert not areas.path_in_area(area, "src/checkout")
    assert not areas.path_in_area(area, "src/checkout2/a.py")
    assert areas.path_in_area(area, "docs/guide.md")
    assert not areas.path_in_area(area, "docs/guide.md.bak")
    assert areas.path_in_area(area, "lib")
    assert areas.path_in_area(area, "lib/x.py")
    assert not areas.path_in_area(area, "library/x.py")


def test_reverse_lookups(monkeypatch) -> None:
    _set_areas(
        monkeypatch,
        _area("checkout", ("src/checkout/",), ("Area:Checkout",)),
        _area("auth", ("src/auth/",), ("area:auth",)),
    )
    assert areas.areas_for_paths(["src/auth/x.py", "README.md"]) == ["auth"]
    assert areas.areas_for_labels(["AREA:CHECKOUT", "bug"]) == ["checkout"]
    assert areas.areas_for_labels([]) == []


def test_resolve_area_message_lists_names(monkeypatch) -> None:
    _set_areas(monkeypatch, _area("checkout"), _area("auth"))
    assert areas.resolve_area(" Checkout ").name == "checkout"
    with pytest.raises(WhyGraphError) as exc:
        areas.resolve_area("nope")
    assert "auth, checkout" in str(exc.value)
    assert "overview" in str(exc.value)
    assert exc.value.code is None


def test_overview_block_budget_and_truncated(monkeypatch) -> None:
    _set_areas(monkeypatch, _area("a", ("p/", "q/")))
    block = areas.overview_block()
    assert block == {
        "items": [{"name": "a", "description": "d", "paths": ["p/", "q/"]}]
    }

    many = [
        _area(
            f"a{i:02d}",
            tuple(f"dir{j}/{'x' * 60}/" for j in range(8)),
            description="y" * 200,
        )
        for i in range(40)
    ]
    _set_areas(monkeypatch, *many)
    block = areas.overview_block()
    assert block["truncated"] is True
    assert all(set(i) == {"name"} for i in block["items"])
    assert len(json.dumps(block)) <= areas.OVERVIEW_BUDGET_CHARS

    _set_areas(monkeypatch, *many[:3])
    block = areas.overview_block()
    assert "truncated" not in block
    assert all(len(i["paths"]) <= areas.OVERVIEW_PATHS_PER_AREA for i in block["items"])


# --- tool integration ------------------------------------------------------
from test_chat_tools import (  # noqa: E402,F401 -- fixtures
    _result,
    chat_repo,
    searchable_history,
)


@pytest.mark.usefixtures("searchable_history")
def test_find_changes_area_filters_with_escaping(monkeypatch) -> None:
    _set_areas(
        monkeypatch,
        _area("ui", ("src/playground/",)),
        _area("pkg", ("src/pkg_a",)),
        _area("wild", ("src/pkg%/",)),
        _area("gone", ("src/pkg",)),
    )
    reg = ToolRegistry()
    res = _result(reg, "find_changes", area="ui")
    assert {c["sha"] for c in res["commits"]} == {"hit", "squash"}
    assert res["area"] == "ui"
    assert res["commits"][0]["matched_paths"] == ["src/playground/src/api.ts"]
    # `_` literal, exact entry also matches children.
    assert [c["sha"] for c in _result(reg, "find_changes", area="pkg")["commits"]] == [
        "under"
    ]
    assert _result(reg, "find_changes", area="wild")["commits"] == []
    assert _result(reg, "find_changes", area="gone")["commits"] == []
    # combines with query
    res = _result(reg, "find_changes", area="ui", query="dropdown")
    assert [c["sha"] for c in res["commits"]] == ["squash", "hit"]


@pytest.mark.usefixtures("searchable_history")
def test_find_changes_area_errors_and_guards(monkeypatch) -> None:
    _set_areas(monkeypatch, _area("ui", ("src/playground/",)))
    reg = ToolRegistry()
    err = _result(reg, "find_changes", area="nope")
    assert "ui" in json.dumps(err)
    both = _result(reg, "find_changes", area="ui", path="src")
    assert "area" in both["error"] and "path" in both["error"]
    assert "error" in _result(reg, "find_changes", since_days=30)


@pytest.mark.usefixtures("searchable_history")
def test_find_changes_since_days(monkeypatch) -> None:
    reg = ToolRegistry()
    res = _result(reg, "find_changes", query="dropdown", since_days=1)
    assert res["commits"] == [] and res["since_days"] == 1
    res = _result(reg, "find_changes", query="dropdown", since_days=99999)
    assert res["since_days"] == 3650
    assert [c["sha"] for c in res["commits"]] == ["squash", "hit"]


@pytest.mark.usefixtures("searchable_history")
def test_list_recent_activity_since_days() -> None:
    reg = ToolRegistry()
    res = _result(reg, "list_recent_activity", since_days=1)
    assert (
        res["commits"] == [] and res["pull_requests"] == [] and res["since_days"] == 1
    )
    res = _result(reg, "list_recent_activity", since_days=3650)
    assert res["commits"] and res["pull_requests"]
    assert "since_days" not in _result(reg, "list_recent_activity")


@pytest.mark.usefixtures("searchable_history")
def test_overview_lists_areas_only_in_chat(monkeypatch) -> None:
    from whygraph.mcp.resources import _repo_overview_resource

    reg = ToolRegistry()
    assert "areas" not in _result(reg, "get_repo_overview")
    _set_areas(monkeypatch, _area("ui", ("src/playground/",)))
    assert _result(reg, "get_repo_overview")["areas"]["items"][0]["name"] == "ui"
    assert "areas" not in _repo_overview_resource()


# --- suggest_areas -----------------------------------------------------


def _suggest_session(files_by_commit):
    """An in-memory project DB with one commit per ``(sha, [paths])``, authored now."""
    from datetime import datetime, timezone

    from sqlmodel import Session, SQLModel, create_engine

    from whygraph.db.models import Commit, CommitFileChange

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    session = Session(engine)
    now = datetime.now(timezone.utc).isoformat()
    for sha, paths in files_by_commit:
        session.add(
            Commit(
                sha=sha,
                parent_shas="",
                author_name="a",
                author_email="a@example.com",
                authored_at=now,
                committed_at=now,
                subject="s",
                body="",
                files_changed=len(paths),
                insertions=1,
                deletions=0,
                scanned_at=now,
            )
        )
        session.flush()
        for path in paths:
            session.add(CommitFileChange(commit_sha=sha, path=path, change_type="M"))
    session.commit()
    return session


def _check_valid(suggestions):
    from whygraph.core.config import area_errors, areas_table_errors

    assert len(suggestions) <= 12
    for entry in suggestions:
        body = {k: v for k, v in entry.items() if k != "name"}
        assert area_errors(entry["name"], body) == []
    assert areas_table_errors({e["name"]: e for e in suggestions}) == []


def test_suggest_descends_into_src_package():
    from whygraph.chat.areas import suggest_areas

    commits = [(f"c{i}", [f"src/pkg/{d}/x.py"]) for i, d in enumerate("aaabbbcccd")]
    commits.append(("t", ["docs/readme.md", "src/pkg/a/y.py"]))
    suggestions = suggest_areas(_suggest_session(commits))
    _check_valid(suggestions)
    paths = [e["paths"][0] for e in suggestions]
    assert paths[0] == "src/pkg/a/"
    assert "src/pkg/b/" in paths and "src/pkg/c/" in paths


def test_suggest_packages_layout_and_denylist():
    from whygraph.chat.areas import suggest_areas

    commits = [
        ("1", ["packages/web/a.ts"]),
        ("2", ["packages/web/b.ts"]),
        ("3", ["packages/api/a.ts"]),
        ("4", ["packages/node_modules/x/a.js"]),
        ("5", ["packages/api/dist/out.js"]),
    ]
    suggestions = suggest_areas(_suggest_session(commits))
    _check_valid(suggestions)
    assert [e["name"] for e in suggestions] == ["web", "api"]


def test_suggest_caps_at_twelve_and_ignores_top_level_files():
    from whygraph.chat.areas import suggest_areas

    commits = [(f"c{i}", [f"d{i:02d}/f.py"]) for i in range(20)]
    commits.append(("top", ["README.md"]))
    suggestions = suggest_areas(_suggest_session(commits))
    _check_valid(suggestions)
    assert len(suggestions) == 12
