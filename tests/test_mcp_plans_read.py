"""Tests for the plan read side - :mod:`whygraph.mcp.plans`.

Plan 61 section 9.1 cases 8, 9, 10 and 10a. The database is built directly
(plan rows through the pure builders, links, commits and PRs inserted), and
the CodeGraph lookup is replaced by a fixed symbol list.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import event

from whygraph.db import get_engine, get_session
from whygraph.db.models import Commit, CommitFileChange, Plan, PlanLink, PullRequest
from whygraph.mcp import plans
from whygraph.mcp.errors import WhyGraphError
from whygraph.mcp.targets import Target

FIXTURES = Path(__file__).parent / "fixtures" / "plans"
HARNESS = (FIXTURES / "harness.md").read_text(encoding="utf-8")
UNTAGGED = (FIXTURES / "harness-untagged.md").read_text(encoding="utf-8")

WIDGET = """# Plan: Tune the widget

**Objective:** Tune it.
**Non-goals:** `other_thing`.

## Steps

1. **Edit the widget**
   - **Files:** src/app/widget.py:10-20, src/app/pt.py:30
   - **Boundaries:** `helper`

2. **Second**
   - **Files:** src/app/widget.py:50-60

## Resolved decisions

| # | Question | Decision |
|---|---|---|
| Q1 | Which class? | Use `Widget.render()` |

## Implementation log

- [x] Step 1: done
- [ ] Step 2: failed
"""

NOW = "2026-01-01T00:00:00+00:00"


def _sym(qn: str, path: str, start: int, end: int, kind: str = "method"):
    return SimpleNamespace(
        kind=kind,
        name=qn.rsplit("::", 1)[-1],
        qualified_name=qn,
        file_path=path,
        start_line=start,
        end_line=end,
    )


SYMBOLS = {
    "src/app/widget.py": [
        _sym("Widget", "src/app/widget.py", 1, 80, "class"),
        _sym("Widget::render", "src/app/widget.py", 10, 25),
        _sym("Widget::other", "src/app/widget.py", 40, 70),
    ],
    "src/app/pt.py": [
        _sym("f", "src/app/pt.py", 30, 40, "function"),
    ],
}


@pytest.fixture(autouse=True)
def _db(whygraph_db_initialized: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plans, "_file_symbols", lambda path: SYMBOLS.get(path, []))


def add_plan(
    stem: str,
    body: str,
    *,
    present: int = 1,
    updated: str = NOW,
    links: dict[str, list] | None = None,
) -> None:
    spans = plans.echo_spans(body)
    outline = plans.build_outline(body, spans)
    title, objective = plans.parse_header(body)
    with get_session() as session:
        session.add(
            Plan(
                stem=stem,
                path=f"docs/plans/{stem}.md",
                blob_sha="ab" * 20,
                parser_version=1,
                title=title or stem,
                line_count=plans.line_count(body),
                objective=objective,
                digest=plans.build_digest(body, outline, spans, stem=stem),
                outline=json.dumps(outline),
                why_index=json.dumps(plans.build_why_index(body, outline, spans)),
                body=body,
                present=present,
                first_seen_at=NOW,
                updated_at=updated,
            )
        )
        session.commit()
    for path, (sections, declared) in (links or {}).items():
        link(stem, "path", path, sections=sections, declared=declared)


def link(
    stem: str, kind: str, ref: str, *, sections: str | None = None, declared: int = 0
) -> None:
    with get_session() as session:
        session.add(
            PlanLink(
                stem=stem, kind=kind, ref=ref, sections=sections, declared=declared
            )
        )
        session.commit()


def add_commit(
    sha: str,
    files: list[str] = (),  # type: ignore[assignment]
    *,
    on_default: int = 1,
    parents: str = "p",
    when: str = NOW,
) -> None:
    with get_session() as session:
        session.add(
            Commit(
                sha=sha,
                parent_shas=parents,
                author_name="a",
                author_email="a@x",
                authored_at=when,
                committed_at=when,
                subject=f"subject {sha}",
                body="",
                files_changed=len(files),
                insertions=0,
                deletions=0,
                scanned_at=when,
                on_default_branch=on_default,
            )
        )
        for path in files:
            session.add(CommitFileChange(commit_sha=sha, path=path, change_type="M"))
        session.commit()


def add_pr(
    number: int,
    *,
    state: str = "closed",
    merged: bool = False,
    merge_sha: str | None = None,
    head: str = "h",
    files: list[str] | None = None,
    changed: int | None = None,
    titles: list[str] = (),  # type: ignore[assignment]
) -> None:
    with get_session() as session:
        session.add(
            PullRequest(
                number=number,
                title=f"PR {number}",
                state=state,
                created_at=NOW,
                updated_at=NOW,
                merged_at=NOW if merged else None,
                merge_commit_sha=merge_sha,
                head_sha=head,
                base_ref="main",
                html_url="u",
                labels="[]",
                fetched_at=NOW,
                commit_titles=json.dumps([{"oid": t} for t in titles]),
                files=json.dumps(files or []),
                changed_files=changed,
            )
        )
        session.commit()


def status_of(stem: str) -> str:
    with get_session() as session:
        return plans.derive_status(session, [stem], "docs/plans")[stem].status


# --- case 8: derive_status --------------------------------------------------------------------


def test_status_table() -> None:
    for stem in ("a", "b", "c", "d", "e", "f"):
        add_plan(stem, WIDGET)
    add_commit("1" * 40, ["src/x.py"], on_default=1)
    link("a", "commit", "1" * 40)
    add_commit("2" * 40, ["src/x.py"], on_default=0)
    link("b", "commit", "2" * 40)
    add_pr(10, state="open")
    link("c", "pr", "10")
    add_pr(11, state="closed", merged=True)  # merged: state is never "merged"
    link("d", "pr", "11")
    add_pr(12, state="closed")  # closed, unmerged, no commit rows
    link("e", "pr", "12")
    link("f", "commit", "9" * 40)  # not scanned
    assert [status_of(s) for s in "abcdef"] == [
        "shipped",
        "in_flight",
        "in_flight",
        "shipped",
        "not_implemented",
        "not_implemented",
    ]


def test_publishing_links_do_not_count() -> None:
    for stem in ("a", "b", "c", "d", "e", "f", "g", "h"):
        add_plan(stem, WIDGET)
    add_commit("1" * 40, ["docs/plans/a.md"])
    link("a", "commit", "1" * 40)  # only the plan: publishing
    add_commit("2" * 40, ["docs/plans/b.md", "src/x.py"])
    link("b", "commit", "2" * 40)  # one path outside: implementing
    add_commit("3" * 40, [])
    link("c", "commit", "3" * 40)  # no file rows: implementing
    add_pr(10, merged=True, files=["docs/plans/d.md"], changed=1)
    link("d", "pr", "10")  # known files, all under: publishing
    # unknown files: [], a null changed_files, changed_files > len(files)
    add_pr(11, merged=True, files=[])
    add_pr(12, merged=True, files=["docs/plans/e.md"], changed=None)
    add_pr(13, merged=True, files=["docs/plans/e.md"], changed=5)
    link("e", "pr", "11")
    link("f", "pr", "12")
    link("g", "pr", "13")
    add_commit("4" * 40, [], parents="p q")  # a merge commit, no PR
    link("h", "commit", "4" * 40)
    assert status_of("a") == "not_implemented"
    assert status_of("b") == "shipped"
    assert status_of("c") == "shipped"
    assert status_of("d") == "not_implemented"
    assert [status_of(s) for s in "efg"] == ["shipped"] * 3
    assert status_of("h") == "not_implemented"
    with get_session() as session:
        assert plans.derive_status(session, ["a"], "docs/plans")["a"].publishing == 1


def test_a_merge_commit_follows_its_pr() -> None:
    add_plan("m", WIDGET)
    add_plan("n", WIDGET)
    add_commit("5" * 40, [], parents="p q")
    link("m", "commit", "5" * 40)
    add_pr(20, merged=True, merge_sha="5" * 40, files=["src/x.py"], changed=1)
    add_commit("6" * 40, [], parents="p q")
    link("n", "commit", "6" * 40)
    add_pr(21, merged=True, merge_sha="6" * 40, files=["docs/plans/n.md"], changed=1)
    assert status_of("m") == "shipped"
    assert status_of("n") == "not_implemented"


# --- case 9: plans_for, symbol scope -----------------------------------------------------------


def _target(qn: str | None, path: str, start: int, end: int) -> Target:
    return Target(path=path, line_start=start, line_end=end, qualified_name=qn)


def _shipped_widget(stem: str = "widget", **kw) -> None:
    add_plan(
        stem,
        WIDGET,
        links={"src/app/widget.py": ("2,3", 1), "src/app/pt.py": ("2", 1)},
        **kw,
    )
    sha = "a" * 40
    add_commit(sha, ["src/app/widget.py", "src/app/pt.py", f"docs/plans/{stem}.md"])
    link(stem, "commit", sha)


def test_symbol_scope_declared_link_and_hints() -> None:
    _shipped_widget()
    out = plans.plans_for(
        [],
        ["src/app/widget.py"],
        target=_target("Widget::render", "src/app/widget.py", 10, 25),
        scope="symbol",
    )
    assert out == [
        {
            "stem": "widget",
            "title": "Tune the widget",
            "status": "shipped",
            "link": "declared",
            "uri": "whygraph://plan/widget",
            "hints": [
                {"id": "step 1", "title": "Edit the widget", "n": 2},
                {"id": "Q1", "title": "Which class?", "n": 4},
            ],
        }
    ]
    # A location call for the same range gives the same list.
    by_range = plans.plans_for(
        [],
        ["src/app/widget.py"],
        target=_target(None, "src/app/widget.py", 10, 25),
        scope="symbol",
    )
    assert by_range == out


def test_symbol_scope_declared_needs_the_rules() -> None:
    _shipped_widget()
    widget = "src/app/widget.py"
    for target in (
        _target("Widget", widget, 1, 80),  # the class is not the innermost anchor
        _target("Widget::other", widget, 40, 70),  # step 2 is logged failed
        _target("src/app/widget.py", widget, 1, 3000),  # no anchor
        _target(None, widget, 10, 45),  # spans two symbols: the class, no step names it
    ):
        assert plans.plans_for([], [widget], target=target, scope="symbol") == [], (
            target
        )
    # A point on a symbol's first line makes no link; inside it does.
    assert (
        plans.plans_for(
            [],
            ["src/app/pt.py"],
            target=_target("f", "src/app/pt.py", 30, 40),
            scope="symbol",
        )
        == []
    )
    SYMBOLS["src/app/pt.py"] = [_sym("g", "src/app/pt.py", 20, 35, "function")]
    got = plans.plans_for(
        [],
        ["src/app/pt.py"],
        target=_target("g", "src/app/pt.py", 20, 35),
        scope="symbol",
    )
    assert [(r["stem"], r["link"]) for r in got] == [("widget", "declared")]


def test_symbol_scope_declared_only_when_shipped_present_and_built() -> None:
    add_plan("w", WIDGET, links={"src/app/widget.py": ("2", 1)})
    target = _target("Widget::render", "src/app/widget.py", 10, 25)
    args = ([], ["src/app/widget.py"])
    assert (
        plans.plans_for(*args, target=target, scope="symbol") == []
    )  # not implemented
    add_commit("b" * 40, ["src/app/widget.py"], on_default=0)
    link("w", "commit", "b" * 40)
    assert plans.plans_for(*args, target=target, scope="symbol") == []  # in flight
    with get_session() as session:
        commit = session.get(Commit, "b" * 40)
        commit.on_default_branch = 1
        session.add(commit)
        session.commit()
    assert len(plans.plans_for(*args, target=target, scope="symbol")) == 1
    # A shipped plan whose implementing commits never touched the file.
    add_plan(
        "v",
        WIDGET.replace("Widget.render()", "Widget.none()"),
        links={"src/app/widget.py": ("2", 1)},
    )
    add_commit("c" * 40, ["src/other.py"])
    link("v", "commit", "c" * 40)
    assert [
        r["stem"] for r in plans.plans_for(*args, target=target, scope="symbol")
    ] == ["w"]
    # Not present any more: the path link is gone, so only history is left.
    with get_session() as session:
        row = session.get(Plan, "w")
        row.present = 0
        session.add(row)
        session.commit()
    assert plans.plans_for(*args, target=target, scope="symbol") == []


def test_symbol_scope_commit_and_pr_links_use_the_given_shas() -> None:
    add_plan("c1", WIDGET)
    add_plan("p1", WIDGET, present=0)
    add_plan("p2", WIDGET)
    add_commit("d" * 40, [], on_default=0)
    link("c1", "commit", "d" * 40)
    add_pr(30, merged=True, merge_sha="e" * 40, titles=["f" * 40])
    link("p1", "pr", "30")
    add_pr(31, merged=True, merge_sha="0" * 40)
    link("p2", "pr", "31")
    got = plans.plans_for(["d" * 40, "f" * 40], ["x.py"], scope="symbol")
    assert [(r["stem"], r["link"], r["status"]) for r in got] == [
        ("c1", "commit", "in_flight"),
        ("p1", "pr", "shipped"),
    ]
    assert got[1]["present"] is False  # history stays, marked
    assert all("hints" not in r and "sections" not in r for r in got)
    assert plans.plans_for([], ["x.py"], scope="symbol") == []


def test_symbol_scope_ceiling() -> None:
    long = "T" * 200
    for i in range(5):
        body = WIDGET.replace("Tune the widget", f"{i} {long}")
        add_plan(
            f"w{i}",
            body,
            links={"src/app/widget.py": ("2", 1)},
            updated=f"2026-01-0{i + 1}T00:00:00+00:00",
        )
        sha = f"{i}" * 40
        add_commit(sha, ["src/app/widget.py"])
        link(f"w{i}", "commit", sha)
    got = plans.plans_for(
        [str(i) * 40 for i in range(5)],
        ["src/app/widget.py"],
        target=_target("Widget::render", "src/app/widget.py", 10, 25),
        scope="symbol",
    )
    assert len(got) <= 3
    assert all(len(r["title"]) <= 80 for r in got)
    assert all(len(r.get("hints", [])) <= 2 for r in got)
    assert len(json.dumps(got)) <= 1200
    assert [r["stem"] for r in got][0] == "w4"  # newest first within a rank


# --- case 9 / 10a: file scope, shape ----------------------------------------------------------


def test_file_scope_links_ranking_and_sections() -> None:
    path = "src/app/widget.py"
    add_plan("decl", WIDGET, links={path: ("2,3", 1)})
    add_plan(
        "mention",
        WIDGET,
        links={path: ("5,6,7,8,9,10,11", 0)},
        updated="2026-02-01T00:00:00+00:00",
    )
    add_plan("gone", WIDGET, present=0)
    for i in range(4):
        add_plan(f"f{i}", WIDGET, links={path: (None, 0)})
    link("gone", "path", path)  # a stale path link of a plan that left HEAD
    add_commit("1" * 40, [path])
    link("decl", "commit", "1" * 40)
    link("gone", "commit", "1" * 40)
    got = plans.plans_for(["1" * 40], [path], scope="file")
    by_stem = {r["stem"]: r for r in got}
    assert len(got) == 5 and sum(r["link"] == "file" for r in got) <= 3
    assert [r["link"] for r in got[:2]] == ["commit", "commit"]
    assert by_stem["decl"]["sections"] == [2, 3]
    assert "gone" in by_stem and by_stem["gone"]["present"] is False
    assert by_stem["gone"]["link"] == "commit"
    assert all("hints" not in r and "objective" not in r for r in got)
    mention = plans.plans_for([], [path], scope="file", limit=50)
    row = next(r for r in mention if r["stem"] == "mention")
    assert row["sections"] == [5, 6, 7, 8, 9] and row["sections_total"] == 7
    # Absent plans keep history links only: no declared / file link for "gone".
    assert all(r["link"] != "declared" or r["stem"] != "gone" for r in mention)


def test_file_scope_not_implemented_last_and_detail_full() -> None:
    path = "src/app/widget.py"
    add_plan("built", WIDGET, links={path: (None, 0)})
    add_plan(
        "idea", WIDGET, links={path: ("2", 1)}, updated="2026-03-01T00:00:00+00:00"
    )
    add_commit("1" * 40, [path])
    add_commit("2" * 40, [path])
    link("built", "commit", "1" * 40)
    got = plans.plans_for(["1" * 40, "2" * 40], [path], scope="file", detail="full")
    assert [r["stem"] for r in got] == ["built", "idea"]
    full = got[0]
    assert set(full) == {
        "stem",
        "title",
        "status",
        "link",
        "uri",
        "path",
        "present",
        "commits",
    }
    assert full["commits"] == ["1" * 8] and full["present"] is True
    assert full["path"] == "docs/plans/built.md"
    compact = plans.plans_for(["1" * 40], [path], scope="file")
    assert set(compact[0]) == {"stem", "title", "status", "link", "uri"}


def test_plans_for_is_local_only_and_off_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _shipped_widget()
    args = (["a" * 40], ["src/app/widget.py"])
    assert plans.plans_for(*args)
    monkeypatch.setattr(plans, "_linked", lambda: True)
    assert plans.plans_for(*args) == []
    with pytest.raises(WhyGraphError) as err:
        plans.plan_record("widget")
    assert err.value.code == "plan_not_found"
    monkeypatch.undo()
    from whygraph import core
    from whygraph.core.config import Config, PlansConfig

    monkeypatch.setattr(
        core,
        "_config",
        Config(whygraph_db=core.get_config().whygraph_db, plans=PlansConfig(dir="")),
    )
    assert plans.plans_for(*args) == []


def test_plans_for_never_selects_the_body() -> None:
    _shipped_widget()
    seen: list[str] = []

    def spy(conn, cursor, statement, *a):  # noqa: ANN001
        seen.append(statement)

    engine = get_engine()
    event.listen(engine, "before_cursor_execute", spy)
    try:
        for scope in ("symbol", "file"):
            plans.plans_for(
                ["a" * 40],
                ["src/app/widget.py"],
                target=_target("Widget::render", "src/app/widget.py", 10, 25),
                scope=scope,  # type: ignore[arg-type]
            )
    finally:
        event.remove(engine, "before_cursor_execute", spy)
    assert seen and not any("plan.body" in s or "plan.outline" in s for s in seen)


def test_card_plan_shas_drops_area_items() -> None:
    def item(sha: str, source: str):
        return SimpleNamespace(commit=SimpleNamespace(sha=sha), source=source)

    got = plans.card_plan_shas(
        [
            item("a", "blame"),
            item("b", "area"),
            item("c", "pr-origin"),
            item("a", "blame-walked"),
        ]
    )
    assert got == ["a", "c"]


# --- case 10: resources ----------------------------------------------------------------------


def _big_plan(sections: int = 120) -> str:
    parts = ["# Plan: Big\n\n**Objective:** Be big.\n"]
    for i in range(sections):
        parts.append(f"\n## Section {i}\n\n" + ("text line\n" * 100))
        for j in range(3):
            parts.append(f"\n### Part {i}.{j}\n\n" + ("more text\n" * 50))
    return "".join(parts)


def test_plan_record_digest_form_and_bounds() -> None:
    add_plan("big", _big_plan(60), links={f"src/p{i}.py": (None, 0) for i in range(30)})
    for i in range(12):
        sha = f"{i:x}" * 40
        add_commit(sha, [f"src/p{i}.py"], when=f"2026-01-{i + 1:02d}T00:00:00+00:00")
        link("big", "commit", sha)
        add_pr(100 + i, merged=True, files=["src/x.py"], changed=1)
        link("big", "pr", str(100 + i))
    record = plans.plan_record("big")
    assert record["kind"] == "plan" and record["reading"] == plans.PLAN_READING
    assert record["status"] == "shipped"
    assert "body" not in record and record["digest"].startswith("# Big")
    assert len(record["implementing_commits"]) == 5
    assert record["implementing_commits_total"] >= 12
    assert len(record["pull_requests"]) == 5 and record["pull_requests_total"] == 12
    assert len(record["paths"]) == 10 and record["paths_total"] == 30
    assert len(record["contents"]) <= 40 and record["contents_total"] == 60
    assert record["contents"][0] == {
        "n": 1,
        "title": "Section 0",
        "chars": record["contents"][0]["chars"],
        "children": 3,
    }
    assert len(json.dumps(record["contents"])) <= 3000
    assert len(json.dumps(record)) <= 8500
    assert "objective" in record and "link" not in record


def test_plan_record_unknown_stem_and_resource_content() -> None:
    for stem in ("nope", "Bad Stem", ""):
        with pytest.raises(WhyGraphError) as err:
            plans.plan_record(stem)
        assert err.value.code == "plan_not_found"
    assert plans._plan_resource("nope") == {"error": "not_found", "stem": "nope"}
    assert plans._plan_section_resource("nope", "0") == {
        "error": "not_found",
        "stem": "nope",
        "section": "0",
    }
    add_plan("p", WIDGET)
    assert plans._plan_resource("p")["stem"] == "p"
    for n in ("x", "-1", "99", "1.5", "٣"):
        assert plans._plan_section_resource("p", n)["error"] == "not_found"


def test_plan_section_text_status_children_and_copies() -> None:
    add_plan("h", HARNESS)
    head = plans.plan_section("h", 0)
    assert "PROPOSAL" not in head["text"]
    assert plans.STATUS_LINE_REPLACEMENT in head["text"]
    assert head["kind"] == "plan" and head["reading"] == plans.PLAN_READING
    assert (
        head["status"] == "not_implemented"
        and head["prev"] is None
        and head["next"] == 1
    )
    assert head["version"] == "abababab" and "copies" not in head
    steps = plans.plan_section("h", 4)
    assert steps["title"] == "Steps" and steps["level"] == 2
    assert [c["title"] for c in steps["children"]][0].startswith("Step 1")
    step4 = plans.plan_section("h", 8)
    assert step4["title"].startswith("Step 4") and step4["level"] == 3
    assert step4["copies"] is True and step4["copies_note"] == plans.COPIES_NOTE
    plain = plans.plan_section("h", 12)
    assert plain["title"].startswith("Resolved decisions") and "copies" not in plain
    # Joined, the sections are the stored body (apart from the replaced line).
    n = 0
    texts = []
    while True:
        try:
            texts.append(plans.plan_section("h", n))
        except WhyGraphError:
            break
        n += 1
    assert "".join(t["text"] for t in texts[1:]) in HARNESS
    assert texts[-1]["next"] is None


def test_plan_section_bounds_and_untagged_prior_art() -> None:
    body = _big_plan(40) + "\n## Long\n\n" + ("x" * 20000) + "\n"
    add_plan("big", body)
    with get_session() as session:
        outline = json.loads(session.get(Plan, "big").outline)
    for entry in outline:
        answer = plans.plan_section("big", entry["n"])
        assert len(answer["text"]) <= plans.SECTION_MAX_CHARS
        assert len(json.dumps(answer)) <= 11000
    cut = [e for e in outline if e["cut"]]
    assert cut and plans.plan_section("big", cut[0]["n"])["text"].endswith(
        plans.LINE_CUT_NOTICE
    )
    many = plans.plan_section("big", 1)
    assert len(many["children"]) == 3
    add_plan("u", UNTAGGED)
    with get_session() as session:
        outline = json.loads(session.get(Plan, "u").outline)
    prior = next(e for e in outline if e["title"].lower().startswith("prior art"))
    assert plans.plan_section("u", prior["n"])["copies"] is True
    with pytest.raises(WhyGraphError):
        plans.plan_section("u", True)  # a bool is not a section number


def test_children_are_capped_at_twenty() -> None:
    body = "# T\n\n## Top\n\n" + "".join(f"### Kid {i}\n\ntext\n\n" for i in range(30))
    add_plan("kids", body)
    top = plans.plan_section("kids", 1)
    assert len(top["children"]) == 20 and top["children_total"] == 30
