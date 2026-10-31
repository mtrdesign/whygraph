"""Project notes over REST (plan 40 section 5.4, tests section 10 items 15-17).

All nine ``/api/projects/{slug}/notes`` routes through a production portal
with four roles on one project (Ben the org owner, Cy an org admin, Dee and
Finn members, Eve a viewer by grant): the lifecycle, the second-confirmer
rule for a wide note (including edit-as-authorship), the role gates, the
statuses of section 6.4, the audit fields (ids and enums, never a body), the
display names and ``deps.bound_project``. The route inventories live beside
the other route tables (``test_portal_app.py``, ``test_portal_tenancy.py``,
``test_portal_linked_manage.py``).
"""

# ruff: noqa: F811 -- pytest fixtures (`team`, ...) are imported

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from starlette.requests import Request

from test_portal_app import (  # noqa: F401 -- fixtures
    at,
    env,
    github_fake,
    make_repo,
    manual_ctx,
    production_env,
)
from test_portal_hosts_isolation import _insert_project
from test_portal_identity_routes import audit_log, events  # noqa: F401 -- fixture
from test_portal_members import be, team, with_roles  # noqa: F401 -- fixture
from whygraph.core.context import use_project
from whygraph.db.engine import get_session as project_session
from whygraph.db.models.project_note import ProjectNote
from whygraph.mcp import notes
from whygraph.portal import deps
from whygraph.portal import notes_routes
from whygraph.serve.errors import WhyGraphError

BODY = "never call billing synchronously from checkout"
N = "/api/projects/api/notes"
WIDE = {"kind": "project"}
NARROW = {"kind": "path", "path": "sample.py"}


@pytest.fixture
def api(team: SimpleNamespace, production_env: SimpleNamespace) -> SimpleNamespace:
    """``acme`` with ``api``: Ben owner, Cy admin, Dee / Finn members, Eve a viewer."""
    t = team
    t.root = make_repo(production_env.tmp, "api")
    t.project_id = _insert_project(t.org_id, "api", "Acme API", t.root, t.ids["ben"])
    with_roles(t, cy="admin", dee="member", eve="member", finn="member")
    from whygraph.portal import db as portal_db
    from whygraph.portal.models import ProjectGrant

    with portal_db.get_session() as session:
        session.add(
            ProjectGrant(
                org_id=t.org_id,
                project_id=t.project_id,
                user_id=t.ids["eve"],
                role="viewer",
                granted_by=t.ids["ben"],
            )
        )
    t.client.app.state.portal.contexts.invalidate()
    return t


def call(t: SimpleNamespace, who: str, method: str, path: str, **kw: Any) -> Any:
    """``method path`` on acme's host as ``who``."""
    be(t, who)
    return t.client.request(method, at("acme") + path, **kw)


def propose(t: SimpleNamespace, who: str, anchor: dict, body: str = BODY) -> dict:
    response = call(
        t, who, "POST", N, json={"body": body, "kind": "constraint", "anchor": anchor}
    )
    assert response.status_code == 201, response.text
    return response.json()


def seed_rows(t: SimpleNamespace, *rows: dict) -> list[int]:
    """Insert raw ``project_note`` rows (agent proposals) and return their ids."""
    ids = []
    with use_project(manual_ctx(t.root, "api", "acme")), project_session() as session:
        for row in rows:
            values = {
                "kind": "gotcha",
                "body": "agent text",
                "status": "proposed",
                "anchor_kind": "project",
                "source": "mcp",
                "origin": "mcp",
                "proposed_by": t.uids["dee"],
                "proposed_at": "2026-10-30T10:00:00+00:00",
            }
            values.update(row)
            note = ProjectNote(**values)
            session.add(note)
            session.commit()
            session.refresh(note)
            ids.append(note.id)
    return ids


def audit_of(log: pytest.LogCaptureFixture, event: str) -> list[dict]:
    return [e for e in events(log) if e["event"] == event]


# ---------------------------------------------------------------------------
# The lifecycle and the read routes
# ---------------------------------------------------------------------------


def test_the_lifecycle_through_all_nine_routes(
    api: SimpleNamespace, audit_log: pytest.LogCaptureFixture
) -> None:
    t = api
    made = propose(t, "dee", NARROW)
    assert made["status"] == "proposed" and made["source"] == "human"
    assert made["origin"] == "ui" and made["anchor_state"] == "current"
    nid = made["id"]
    # Names come from the portal's users table.
    assert made["proposed_by"] == t.uids["dee"] and made["proposed_by_name"] == "Dee"
    assert "confirmed_by_name" not in made
    # A duplicate proposal answers 200 with the existing note.
    again = call(
        t,
        "dee",
        "POST",
        N,
        json={"body": BODY, "kind": "constraint", "anchor": NARROW},
    )
    assert again.status_code == 200 and again.json()["id"] == nid
    assert again.json()["duplicate"] is True
    audit_log.clear()
    confirmed = call(t, "dee", "POST", f"{N}/{nid}/confirm", json={})
    assert confirmed.status_code == 200, confirmed.text
    view = confirmed.json()
    assert view["status"] == "confirmed" and view["confirmed_by_name"] == "Dee"
    assert view["resource"] == f"whygraph://note/{nid}"
    # A narrow note: its proposer may confirm it herself (self_confirmed).
    (event,) = audit_of(audit_log, "note_confirmed")
    assert event["self_confirmed"] is True and event["note_id"] == nid
    assert (event["uid"], event["org"], event["project"]) == (
        t.uids["dee"],
        "acme",
        "api",
    )
    assert (event["source"], event["anchor_kind"]) == ("human", "path")
    assert event["org_id"] == t.org_id and BODY not in str(event)
    # Reads: list, one, summary, for-target.
    listed = call(t, "eve", "GET", N)
    assert listed.status_code == 200
    assert [n["id"] for n in listed.json()] == [nid]
    assert listed.json()[0]["proposed_by_name"] == "Dee"
    one = call(t, "eve", "GET", f"{N}/{nid}").json()
    assert one["id"] == nid and one["superseded_by_ids"] == []
    assert call(t, "eve", "GET", f"{N}/summary").json() == {
        "proposed": 0,
        "proposed_by_agents": 0,
        "confirmed": 1,
    }
    target = call(
        t,
        "eve",
        "GET",
        f"{N}/for-target",
        params={"path": "sample.py", "line_start": 1, "line_end": 2},
    )
    assert target.status_code == 200 and [n["id"] for n in target.json()] == [nid]
    assert call(t, "eve", "GET", f"{N}/for-target").json()["code"] == "bad_target"
    # Reconfirm, then retire.
    audit_log.clear()
    sure = call(t, "finn", "POST", f"{N}/{nid}/reconfirm", json={})
    assert sure.status_code == 200 and sure.json()["reconfirmed_by"] == t.uids["finn"]
    (event,) = audit_of(audit_log, "note_reconfirmed")
    assert event["self_confirmed"] is False and event["uid"] == t.uids["finn"]
    audit_log.clear()
    retired = call(t, "finn", "POST", f"{N}/{nid}/retire", json={"reason": "outdated"})
    assert retired.status_code == 200, retired.text
    assert retired.json()["status"] == "retired"
    (event,) = audit_of(audit_log, "note_retired")
    assert event["reason"] == "outdated" and event["note_id"] == nid
    assert BODY not in str(audit_log.records) and "idempotent" not in str(event)
    # A revision lists on the revised note's chain.
    base = propose(t, "dee", NARROW, "base text")
    call(t, "ben", "POST", f"{N}/{base['id']}/confirm", json={})
    revision = call(
        t,
        "dee",
        "POST",
        N,
        json={
            "body": "revised text",
            "kind": "constraint",
            "anchor": NARROW,
            "supersedes_id": base["id"],
        },
    )
    assert (
        revision.status_code == 201 and revision.json()["supersedes_id"] == base["id"]
    )
    chain = call(t, "dee", "GET", f"{N}/{base['id']}").json()
    assert chain["superseded_by_ids"] == [revision.json()["id"]]


def test_list_filters_and_the_attention_query(api: SimpleNamespace) -> None:
    t = api
    narrow = propose(t, "dee", NARROW, "narrow one")
    wide = propose(t, "dee", WIDE, "wide one")
    call(t, "ben", "POST", f"{N}/{wide['id']}/confirm", json={})
    only = call(t, "dee", "GET", N, params={"status": "proposed"}).json()
    assert [n["id"] for n in only] == [narrow["id"]]
    by_kind = call(t, "dee", "GET", N, params={"anchor_kind": "project"}).json()
    assert [n["id"] for n in by_kind] == [wide["id"]]
    found = call(t, "dee", "GET", N, params={"q": "NARROW"}).json()
    assert [n["id"] for n in found] == [narrow["id"]]
    page = call(t, "dee", "GET", N, params={"limit": 1}).json()
    assert [n["id"] for n in page] == [wide["id"]]
    older = call(t, "dee", "GET", N, params={"before_id": wide["id"]}).json()
    assert [n["id"] for n in older] == [narrow["id"]]
    # Nothing is stale yet: "Needs attention" is empty.
    attention = call(t, "dee", "GET", N, params={"attention": "true"})
    assert attention.status_code == 200 and attention.json() == []


def test_summary_counts_agent_proposals(api: SimpleNamespace) -> None:
    t = api
    seed_rows(t, {}, {"source": "chat", "origin": "chat:3"}, {"source": "human"})
    done = propose(t, "dee", NARROW)
    call(t, "dee", "POST", f"{N}/{done['id']}/confirm", json={})
    assert call(t, "eve", "GET", f"{N}/summary").json() == {
        "proposed": 3,
        "proposed_by_agents": 2,
        "confirmed": 1,
    }


def test_display_names_and_the_former_member(api: SimpleNamespace) -> None:
    t = api
    ghost, mine = seed_rows(
        t,
        {"proposed_by": "no-such-uid", "origin": "ui", "source": "human"},
        {"proposed_by": t.uids["finn"]},
    )
    listed = {n["id"]: n for n in call(t, "ben", "GET", N).json()}
    assert listed[ghost]["proposed_by_name"] == "Former member"
    assert listed[mine]["proposed_by_name"] == "Finn"
    # A confirmer who is not the proposer gets her own name.
    done = call(t, "cy", "POST", f"{N}/{mine}/confirm", json={}).json()
    assert done["confirmed_by_name"] == "Cy"
    assert done["proposed_by_name"] == "Finn"


# ---------------------------------------------------------------------------
# Wide notes need a second confirmer
# ---------------------------------------------------------------------------


def test_a_wide_note_needs_a_second_confirmer(
    api: SimpleNamespace, audit_log: pytest.LogCaptureFixture
) -> None:
    t = api
    wide = propose(t, "dee", WIDE)
    nid = wide["id"]
    own = call(t, "dee", "POST", f"{N}/{nid}/confirm", json={})
    assert (
        own.status_code == 403 and own.json()["code"] == "note_needs_second_confirmer"
    )
    # Edit-as-authorship: a second contributor who edits it is refused too.
    edited = call(t, "finn", "POST", f"{N}/{nid}/confirm", json={"body": "finn's text"})
    assert edited.status_code == 403
    assert edited.json()["code"] == "note_needs_second_confirmer"
    assert call(t, "dee", "GET", f"{N}/{nid}").json()["status"] == "proposed"
    audit_log.clear()
    # Another contributor confirming it unedited is the second confirmer.
    ok = call(t, "finn", "POST", f"{N}/{nid}/confirm", json={})
    assert ok.status_code == 200, ok.text
    (event,) = audit_of(audit_log, "note_confirmed")
    assert event["self_confirmed"] is False and event["anchor_kind"] == "project"
    # The proposer cannot "still true" it either; a project admin can.
    refused = call(t, "dee", "POST", f"{N}/{nid}/reconfirm", json={})
    assert refused.status_code == 403
    assert refused.json()["code"] == "note_needs_second_confirmer"
    assert call(t, "cy", "POST", f"{N}/{nid}/reconfirm", json={}).status_code == 200


def test_a_project_admin_confirms_her_own_wide_note_and_may_edit(
    api: SimpleNamespace, audit_log: pytest.LogCaptureFixture
) -> None:
    t = api
    wide = propose(t, "cy", WIDE)
    audit_log.clear()
    done = call(t, "cy", "POST", f"{N}/{wide['id']}/confirm", json={"body": "edited"})
    assert done.status_code == 200, done.text
    assert done.json()["edited_on_confirm"] is True
    (event,) = audit_of(audit_log, "note_confirmed")
    assert event["self_confirmed"] is True


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------


def test_a_viewer_reads_and_is_refused_every_write(api: SimpleNamespace) -> None:
    t = api
    made = propose(t, "dee", NARROW)
    nid = made["id"]
    for path in (N, f"{N}/summary", f"{N}/{nid}"):
        assert call(t, "eve", "GET", path).status_code == 200, path
    writes = [
        (N, {"body": "x", "kind": "gotcha", "anchor": NARROW}),
        (f"{N}/{nid}/confirm", {}),
        (f"{N}/{nid}/reconfirm", {}),
        (f"{N}/{nid}/retire", {"reason": "rejected"}),
        (f"{N}/reject-agent-proposals", {"proposed_by": "x", "origin": "mcp"}),
    ]
    for path, body in writes:
        response = call(t, "eve", "POST", path, json=body)
        assert response.status_code == 403, (path, response.text)
        assert response.json()["code"] == "forbidden", path
    assert call(t, "dee", "GET", f"{N}/{nid}").json()["status"] == "proposed"


def test_a_non_member_sees_no_project(api: SimpleNamespace) -> None:
    t = api
    t.client.cookies.clear()
    response = t.client.get(at("acme") + N)
    assert response.status_code in (401, 404)


# ---------------------------------------------------------------------------
# Statuses
# ---------------------------------------------------------------------------


def test_the_statuses_of_the_error_codes(api: SimpleNamespace) -> None:
    t = api
    missing = call(t, "dee", "GET", f"{N}/999")
    assert (missing.status_code, missing.json()["code"]) == (404, "note_not_found")
    missing = call(t, "dee", "POST", f"{N}/999/retire", json={"reason": "wrong"})
    assert (missing.status_code, missing.json()["code"]) == (404, "note_not_found")
    invalid = call(
        t, "dee", "POST", N, json={"body": "", "kind": "gotcha", "anchor": NARROW}
    )
    assert (invalid.status_code, invalid.json()["code"]) == (400, "note_invalid")
    empty = call(t, "dee", "POST", N, json={})
    assert (empty.status_code, empty.json()["code"]) == (400, "note_invalid")
    made = propose(t, "dee", NARROW)
    nid = made["id"]
    call(t, "dee", "POST", f"{N}/{nid}/confirm", json={})
    state = call(t, "dee", "POST", f"{N}/{nid}/confirm", json={})
    assert (state.status_code, state.json()["code"]) == (409, "note_state")
    wrong = call(t, "dee", "POST", f"{N}/{nid}/retire", json={"reason": "rejected"})
    assert wrong.status_code in (400, 409)
    assert wrong.json()["code"] in ("note_state", "note_invalid")
    stray = call(
        t, "dee", "POST", f"{N}/{nid}/reconfirm", json={"line_start": 1, "line_end": 2}
    )
    assert (stray.status_code, stray.json()["code"]) == (400, "note_invalid")
    bad = call(t, "dee", "GET", f"{N}/not-a-number")
    assert bad.status_code == 422


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("note_not_found", 404),
        ("note_state", 409),
        ("note_invalid", 400),
        ("note_limit", 429),
        ("note_needs_second_confirmer", 403),
        ("note_no_actor", 500),
    ],
)
def test_every_domain_code_has_its_section_6_4_status(
    api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, code: str, status: int
) -> None:
    def refuse(*_a: Any, **_k: Any) -> Any:
        raise WhyGraphError("a refusal that quotes nothing", code=code)

    monkeypatch.setattr(notes, "propose", refuse)
    monkeypatch.setattr(notes, "get_note", refuse)
    posted = call(
        api, "dee", "POST", N, json={"body": "x", "kind": "gotcha", "anchor": NARROW}
    )
    got = call(api, "dee", "GET", f"{N}/1")
    for response in (posted, got):
        assert response.status_code == status, response.text
        assert response.json()["code"] == code


# ---------------------------------------------------------------------------
# Bulk reject of agent proposals
# ---------------------------------------------------------------------------


def test_bulk_reject_is_scoped_and_audited(
    api: SimpleNamespace, audit_log: pytest.LogCaptureFixture
) -> None:
    t = api
    mine1, mine2, other_origin, other_person, human = seed_rows(
        t,
        {},
        {},
        {"origin": "chat:12", "source": "chat"},
        {"proposed_by": t.uids["finn"]},
        {"source": "human", "origin": "ui"},
    )
    audit_log.clear()
    done = call(
        t,
        "cy",
        "POST",
        f"{N}/reject-agent-proposals",
        json={"proposed_by": t.uids["dee"], "origin": "mcp"},
    )
    assert done.status_code == 200 and done.json() == {"rejected": 2}
    (event,) = audit_of(audit_log, "note_bulk_rejected")
    assert event["target"] == t.uids["dee"] and event["uid"] == t.uids["cy"]
    assert (event["origin"], event["count"]) == ("mcp", 2)
    assert (event["org"], event["project"]) == ("acme", "api")
    assert "agent text" not in str(event)
    states = {n["id"]: n["status"] for n in call(t, "cy", "GET", N).json()}
    assert states[mine1] == states[mine2] == "retired"
    assert {states[i] for i in (other_origin, other_person, human)} == {"proposed"}
    chat = call(
        t,
        "cy",
        "POST",
        f"{N}/reject-agent-proposals",
        json={"proposed_by": t.uids["dee"], "origin": "chat:12"},
    )
    assert chat.json() == {"rejected": 1}


@pytest.mark.parametrize(
    "body",
    [
        {"proposed_by": "u", "origin": "claude"},
        {"proposed_by": "u", "origin": "chat:abc"},
        {"proposed_by": "u", "origin": "chat:1 "},
        {"proposed_by": "u", "origin": "ui\n"},
        {"proposed_by": "u", "origin": 5},
        {"proposed_by": "u"},
        {"origin": "mcp"},
        {"proposed_by": "", "origin": "mcp"},
        {},
    ],
)
def test_bulk_reject_validates_its_body(
    api: SimpleNamespace, audit_log: pytest.LogCaptureFixture, body: dict
) -> None:
    audit_log.clear()
    response = call(api, "cy", "POST", f"{N}/reject-agent-proposals", json=body)
    assert (response.status_code, response.json()["code"]) == (400, "note_invalid")
    assert audit_of(audit_log, "note_bulk_rejected") == []


# ---------------------------------------------------------------------------
# deps.bound_project
# ---------------------------------------------------------------------------


def test_bound_project_raises_when_unset_and_returns_what_was_set() -> None:
    request = Request({"type": "http", "headers": [], "state": {}})
    with pytest.raises(RuntimeError):
        deps.bound_project(request)
    marker = object()
    request.state.bound_project = marker
    assert deps.bound_project(request) is marker


def test_bound_project_is_set_on_every_notes_request(
    api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each route's request reaches its handler with the project bound."""
    seen: list[str] = []
    real = deps.bound_project

    def spy(request: Request) -> Any:
        project = real(request)
        seen.append(project.slug)
        return project

    monkeypatch.setattr(notes_routes, "bound_project", spy)
    made = propose(api, "dee", NARROW)
    nid = made["id"]
    call(api, "dee", "POST", f"{N}/{nid}/confirm", json={})
    call(api, "dee", "POST", f"{N}/{nid}/reconfirm", json={})
    call(api, "dee", "POST", f"{N}/{nid}/retire", json={"reason": "wrong"})
    call(
        api,
        "dee",
        "POST",
        f"{N}/reject-agent-proposals",
        json={"proposed_by": "x", "origin": "mcp"},
    )
    # confirm and reconfirm read it twice (can_configure, audit), retire and
    # reject once (audit); a missing binding would raise RuntimeError instead.
    assert seen == ["api"] * 6
