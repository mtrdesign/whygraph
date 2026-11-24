"""Tests for the scan's "Plans" phase - :mod:`whygraph.scan.plans_crawler`.

Plan 61 section 9.1 cases 5 (the HEAD-dependent half), 6 and 7. Every test
runs the real crawler against a throwaway git repository; commit rows come
from a real :class:`GitCrawler`, PR rows are inserted directly.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from rich.progress import Progress
from sqlmodel import col, select

from whygraph.analyze import project_purpose
from whygraph.db import get_session
from whygraph.db.models import Plan, PlanLink, PullRequest
from whygraph.mcp import plans as mcp_plans
from whygraph.scan import plans_crawler
from whygraph.scan.git_crawler import GitCrawler
from whygraph.scan.plans_crawler import PlansCrawler
from whygraph.services.git import Repository

FIXTURES = Path(__file__).parent / "fixtures" / "plans"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout


def _write(root: Path, files: dict[str, str | bytes | None]) -> None:
    for rel, content in files.items():
        target = root / rel
        if content is None:
            _git(root, "rm", "-q", "--", rel)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, newline="")
        _git(root, "add", "--", rel)


def _commit(root: Path, message: str, files: dict[str, str | bytes | None]) -> str:
    _write(root, files)
    _git(root, "commit", "-q", "--allow-empty", "-m", message)
    return _git(root, "rev-parse", "HEAD").strip()


@pytest.fixture
def repo(
    tmp_path: Path, whygraph_db_initialized: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """An empty repository on ``main`` as ``cwd``, with a migrated WhyGraph DB."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "tester@example.com")
    _git(root, "config", "user.name", "Test User")
    _git(root, "config", "commit.gpgsign", "false")
    monkeypatch.chdir(root)
    return root


def _crawl(
    root: Path, *, plans_dir: str = "docs/plans", **kwargs: object
) -> PlansCrawler:
    crawler = PlansCrawler(
        Progress(),
        repository=Repository(root, default_branch="main"),
        plans_dir=plans_dir,
        **kwargs,  # type: ignore[arg-type]
    )
    crawler.run()
    assert crawler.error is None, crawler.error
    assert isinstance(crawler.summary, str) and "\n" not in crawler.summary
    return crawler


def _record_commits(root: Path) -> None:
    crawler = GitCrawler(Progress(), repository=Repository(root, default_branch="main"))
    crawler.run()
    assert crawler.error is None


def _rows() -> dict[str, Plan]:
    with get_session() as session:
        return {
            row.stem: Plan.model_validate(row.model_dump())
            for row in session.exec(select(Plan)).all()
        }


def _links(kind: str | None = None) -> dict[tuple[str, str, str], PlanLink]:
    with get_session() as session:
        query = select(PlanLink)
        if kind is not None:
            query = query.where(col(PlanLink.kind) == kind)
        return {
            (row.stem, row.kind, row.ref): PlanLink.model_validate(row.model_dump())
            for row in session.exec(query).all()
        }


def _count_reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    reads: list[str] = []
    real = project_purpose._read_entry

    def spy(git, entry, max_bytes):  # noqa: ANN001
        reads.append(entry.name)
        return real(git, entry, max_bytes)

    monkeypatch.setattr(project_purpose, "_read_entry", spy)
    return reads


def _plan(title: str, body: str = "") -> str:
    return f"# Plan: {title}\n\n**Objective:** Do {title}.\n\n## Notes\n{body}\n"


# --- case 6: indexing ---------------------------------------------------------------------


def test_first_index_stores_the_derived_columns(repo: Path) -> None:
    harness = (FIXTURES / "harness.md").read_text(encoding="utf-8")
    _commit(
        repo,
        "add plans",
        {
            "docs/plans/add-notes-kind-filter.md": harness,
            "src/playground/src/pages/NotesPage.tsx": "export {}\n",
            "src/playground/src/components/NotesTab.tsx": "export {}\n",
        },
    )
    crawler = _crawl(repo)
    row = _rows()["add-notes-kind-filter"]
    assert row.path == "docs/plans/add-notes-kind-filter.md"
    assert row.title == "Add a filter dropdown to the notes list"
    assert row.objective is not None and row.objective.startswith("A member narrows")
    assert row.parser_version == mcp_plans.PLANS_PARSER_VERSION
    assert row.line_count == 125
    assert row.body == harness
    assert row.present == 1 and row.truncated == 0 and row.redacted_lines == 0
    outline = json.loads(row.outline)
    assert outline[8]["title"] == "Step 4: Notes page: add the Kind dropdown"
    why = json.loads(row.why_index)
    assert why["steps"][3]["n"] == 8
    assert row.digest.startswith("# Add a filter dropdown to the notes list")
    assert "whygraph://plan/add-notes-kind-filter/section/<n>" in row.digest
    links = _links("path")
    page = links[
        ("add-notes-kind-filter", "path", "src/playground/src/pages/NotesPage.tsx")
    ]
    assert page.declared == 1 and page.sections == "8"
    # NotesTab.tsx is a Non-goals / Boundaries mention: no link.
    assert not any(ref.endswith("NotesTab.tsx") for _, _, ref in links)
    assert row.paths_head == _git(repo, "rev-parse", "HEAD").strip()
    assert crawler.summary.startswith(
        "1 plan (1 changed, 0 removed); links 0 commit, 0 pr, 1 path"
    )


def test_unchanged_blob_is_not_reread(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _commit(repo, "plan", {"docs/plans/alpha.md": _plan("Alpha")})
    _crawl(repo)
    reads = _count_reads(monkeypatch)
    _commit(repo, "other", {"README.md": "hi\n"})
    crawler = _crawl(repo)
    assert reads == []
    assert "(0 changed, 0 removed)" in crawler.summary


def test_parser_version_bump_rereads_unchanged_blobs(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _commit(repo, "plan", {"docs/plans/alpha.md": _plan("Alpha")})
    _crawl(repo)
    first = _rows()["alpha"]
    reads = _count_reads(monkeypatch)
    monkeypatch.setattr(plans_crawler, "PLANS_PARSER_VERSION", 99)
    _crawl(repo)
    assert reads == ["docs/plans/alpha.md"]
    row = _rows()["alpha"]
    assert row.parser_version == 99
    assert row.updated_at == first.updated_at  # the blob did not change


def test_deleted_plan_keeps_its_row_and_comes_back_without_a_reread(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = _plan("Alpha", "Touches src/a.py.")
    _commit(repo, "plan", {"docs/plans/alpha.md": content, "src/a.py": "x = 1\n"})
    _crawl(repo)
    assert ("alpha", "path", "src/a.py") in _links()
    _commit(repo, "drop", {"docs/plans/alpha.md": None})
    crawler = _crawl(repo)
    row = _rows()["alpha"]
    assert row.present == 0 and row.paths_head is None and row.paths_hash is None
    assert _links("path") == {}
    assert "(0 changed, 1 removed)" in crawler.summary
    reads = _count_reads(monkeypatch)
    _commit(repo, "restore", {"docs/plans/alpha.md": content})
    _crawl(repo)
    assert reads == []
    assert _rows()["alpha"].present == 1
    assert ("alpha", "path", "src/a.py") in _links()


def test_moved_plan_updates_its_path_without_a_reread(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = _plan("Alpha")
    _commit(repo, "plan", {"docs/plans/alpha.md": content})
    _crawl(repo)
    reads = _count_reads(monkeypatch)
    (repo / "docs/plans/2026").mkdir()
    _git(repo, "mv", "docs/plans/alpha.md", "docs/plans/2026/alpha.md")
    _git(repo, "commit", "-q", "-m", "move")
    _crawl(repo)
    assert reads == []
    assert _rows()["alpha"].path == "docs/plans/2026/alpha.md"


def test_duplicate_stems_bad_names_and_symlinks_are_skipped(repo: Path) -> None:
    _commit(
        repo,
        "plans",
        {
            "docs/plans/a/same.md": _plan("First"),
            "docs/plans/b/same.md": _plan("Second"),
            "docs/plans/Bad_Name.md": _plan("Bad"),
            "docs/plans/notes.txt": "not a plan\n",
        },
    )
    (repo / "docs/plans/link.md").symlink_to("a/same.md")
    _git(repo, "add", "docs/plans/link.md")
    _git(repo, "commit", "-q", "-m", "link")
    crawler = _crawl(repo)
    rows = _rows()
    assert set(rows) == {"same"}
    assert rows["same"].path == "docs/plans/a/same.md"
    assert rows["same"].title == "First"
    assert "skipped_name 1" in crawler.summary
    assert "skipped_duplicate 1" in crawler.summary


def test_binary_plan_keeps_the_previous_row(repo: Path) -> None:
    _commit(repo, "plan", {"docs/plans/alpha.md": _plan("Alpha")})
    _crawl(repo)
    before = _rows()["alpha"]
    _commit(repo, "binary", {"docs/plans/alpha.md": b"\x00\x01binary"})
    crawler = _crawl(repo)
    row = _rows()["alpha"]
    assert row.present == 1
    assert row.blob_sha == before.blob_sha and row.body == before.body
    assert "skipped_unreadable 1" in crawler.summary


def test_truncated_flag_follows_the_blob_size(repo: Path) -> None:
    big = "# Plan: Big\n" + ("b" * 99 + "\n") * (513 * 1024 // 100 + 1)
    fits = "# Plan: Fits\n" + ("f" * 99 + "\n") * 2850  # about 285 KB
    _commit(repo, "plans", {"docs/plans/big.md": big, "docs/plans/fits.md": fits})
    _crawl(repo)
    rows = _rows()
    assert rows["big"].truncated == 1
    assert len(rows["big"].body.encode()) <= 512 * 1024
    assert rows["fits"].truncated == 0 and rows["fits"].body == fits


def test_branch_switch_retires_and_restores_a_plan(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _commit(repo, "base", {"src/a.py": "x = 1\n"})
    _git(repo, "checkout", "-q", "-b", "feature")
    _commit(
        repo,
        "plan\n\nWhyGraph-Plan: feat",
        {
            "docs/plans/feat.md": _plan("Feat", "Changes src/a.py."),
            "src/a.py": "x = 2\n",
        },
    )
    _record_commits(repo)
    _crawl(repo)
    assert _rows()["feat"].present == 1
    assert ("feat", "path", "src/a.py") in _links()
    commit_links = _links("commit")
    assert len(commit_links) == 1

    _git(repo, "checkout", "-q", "main")
    _crawl(repo)
    assert _rows()["feat"].present == 0
    assert _links("path") == {}
    assert _links("commit") == commit_links  # history stays

    reads = _count_reads(monkeypatch)
    _git(repo, "checkout", "-q", "feature")
    _crawl(repo)
    assert reads == []
    assert _rows()["feat"].present == 1
    assert ("feat", "path", "src/a.py") in _links()


def test_head_move_without_file_changes_reparses_nothing(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _commit(repo, "plan", {"docs/plans/alpha.md": _plan("Alpha", "src/a.py")})
    _crawl(repo)
    before = _rows()["alpha"]
    calls: list[str] = []
    real = plans_crawler.path_mentions

    def spy(body, outline, spans):  # noqa: ANN001
        calls.append(body[:10])
        return real(body, outline, spans)

    monkeypatch.setattr(plans_crawler, "path_mentions", spy)
    head = _commit(repo, "empty", {})
    _crawl(repo)
    after = _rows()["alpha"]
    assert calls == []
    assert after.paths_hash == before.paths_hash
    assert after.paths_head == head != before.paths_head


def test_head_moved_links_follow_the_tracked_files(repo: Path) -> None:
    _commit(
        repo, "plan", {"docs/plans/alpha.md": _plan("Alpha", "Will add src/late.py.")}
    )
    _crawl(repo)
    assert _links("path") == {}
    _commit(repo, "code lands", {"src/late.py": "y = 1\n"})
    _crawl(repo)
    assert ("alpha", "path", "src/late.py") in _links()
    _commit(repo, "code goes", {"src/late.py": None})
    _crawl(repo)
    assert _links("path") == {}


def test_path_link_sections_are_capped_at_twelve(repo: Path) -> None:
    sections = "".join(f"## S{i}\nsee src/a.py\n" for i in range(20))
    body = "# Plan: Many\n\n**Read first:** src/a.py\n\n" + sections
    _commit(repo, "plan", {"docs/plans/many.md": body, "src/a.py": "x\n"})
    _crawl(repo)
    link = _links("path")[("many", "path", "src/a.py")]
    assert link.sections == ",".join(str(n) for n in range(1, 13))
    assert link.declared == 0


def test_path_mention_cap(repo: Path) -> None:
    files = {f"src/m{i}.py": "x\n" for i in range(210)}
    body = "# Plan: Cap\n\n## Notes\n" + "".join(f"src/m{i}.py\n" for i in range(210))
    _commit(repo, "plan", {"docs/plans/cap.md": body, **files})
    _crawl(repo)
    assert len(_links("path")) == plans_crawler.PATH_LINKS_MAX


def test_over_the_cap_plans_are_left_alone(repo: Path) -> None:
    files = {f"docs/plans/p{i}.md": _plan(f"P{i}") for i in range(5)}
    _commit(repo, "plans", files)
    _crawl(repo)
    assert {row.present for row in _rows().values()} == {1}
    crawler = _crawl(repo, max_plans=3)
    assert "skipped_cap 2" in crawler.summary
    rows = _rows()
    assert len(rows) == 5
    assert {stem for stem, row in rows.items() if row.present} == set(rows)

    _commit(repo, "more", {f"docs/plans/q{i}.md": _plan(f"Q{i}") for i in range(2)})
    crawler = _crawl(repo, max_plans=3)
    rows = _rows()
    assert {"p0", "p1", "p2"} <= set(rows) and "q0" not in rows


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_secret_lines_are_redacted_and_counted(repo: Path, newline: str) -> None:
    text = newline.join(
        ["# Plan: S", "", "**Objective:** O.", "api_key: abc123", "after", ""]
    )
    _commit(repo, "plan", {"docs/plans/s.md": text})
    _crawl(repo)
    row = _rows()["s"]
    assert row.redacted_lines == 1
    assert "abc123" not in row.body and "\r" not in row.body
    assert "abc123" not in row.digest


def test_disabled_retires_every_row(repo: Path) -> None:
    _commit(
        repo,
        "plan",
        {"docs/plans/alpha.md": _plan("Alpha", "src/a.py"), "src/a.py": "x\n"},
    )
    _crawl(repo)
    assert _links("path")
    crawler = _crawl(repo, plans_dir="")
    assert crawler.summary == "disabled"
    assert {row.present for row in _rows().values()} == {0}
    assert _links("path") == {}


def test_unborn_head_finishes_without_error(repo: Path) -> None:
    crawler = _crawl(repo)
    assert crawler.summary == "no HEAD"
    assert _rows() == {}


def test_missing_plans_dir_indexes_nothing(repo: Path) -> None:
    _commit(repo, "code", {"src/a.py": "x\n"})
    crawler = _crawl(repo)
    assert (
        crawler.summary
        == "0 plans (0 changed, 0 removed); links 0 commit, 0 pr, 0 path"
    )


# --- case 7: commit and PR links ------------------------------------------------------------


def _pr(number: int, body: str | None) -> PullRequest:
    return PullRequest(
        number=number,
        title=f"PR {number}",
        body=body,
        state="closed",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        merged_at="2026-01-02T00:00:00+00:00",
        head_sha=f"head{number}",
        base_ref="main",
        html_url=f"https://example.test/pr/{number}",
        labels="[]",
        fetched_at="2026-01-02T00:00:00+00:00",
    )


def test_commit_links_are_idempotent_across_scans(repo: Path) -> None:
    first = _commit(
        repo,
        "one\n\nWhyGraph-Plan: alpha\nwhygraph-plan: docs/plans/beta.md",
        {"a": "1\n"},
    )
    second = _commit(repo, "two\r\n\r\nWhyGraph-Plan: alpha\r\n", {"b": "1\n"})
    _commit(repo, "three\n\nmentions WhyGraph-Plan: gamma mid-line", {"c": "1\n"})
    _record_commits(repo)
    _crawl(repo)
    expected = {
        ("alpha", "commit", first),
        ("beta", "commit", first),
        ("alpha", "commit", second),
    }
    assert set(_links("commit")) == expected
    _crawl(repo)
    assert set(_links("commit")) == expected


def test_pr_links_rebuild_after_a_body_edit(repo: Path) -> None:
    _commit(repo, "base", {"a": "1\n"})
    with get_session() as session:
        session.add(_pr(7, "Implements it.\r\n\r\nWhyGraph-Plan: alpha\r\n"))
        session.add(_pr(8, None))
    _crawl(repo)
    assert set(_links("pr")) == {("alpha", "pr", "7")}
    with get_session() as session:
        pr = session.get(PullRequest, 7)
        assert pr is not None
        pr.body = "Edited: no plan any more."
        session.add(pr)
    _crawl(repo)
    assert _links("pr") == {}
