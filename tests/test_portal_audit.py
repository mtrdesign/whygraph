"""The persisted audit log (M2f-1 plan sections 4.9, 6.1 #5-#6, 6.2 #6).

Unit tests drive :class:`~whygraph.portal.audit_store.AuditWriter` against a
fresh portal database: batching, the overflow drop, token redaction, the
FK-safe insert of a gone org / actor, the row-by-row retry, the
``reader_request`` dedupe, the shutdown flush, pruning and the CSV escaping.
Route tests use :func:`test_portal_members.team` (a production portal whose
lifespan registers a writer): events land with their org, ``GET
/api/org/audit`` filters and pages, the CSV export, owner only, and ``GET
/api/admin/audit`` for instance admins.
"""

# ruff: noqa: F811 -- pytest fixtures are imported from other test modules

from __future__ import annotations

import csv
import io
import logging
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Iterator

import pytest
from sqlmodel import select

from test_portal_app import (  # noqa: F401 -- fixtures
    at,
    env,
    github_fake,
    production_env,
)
from test_portal_identity_routes import audit_log, events  # noqa: F401
from test_portal_members import be, team  # noqa: F401
from whygraph.portal import audit as audit_mod
from whygraph.portal import audit_store
from whygraph.portal import db as portal_db
from whygraph.portal.audit import audit
from whygraph.portal.audit_store import AuditWriter, csv_cell, iter_csv
from whygraph.portal.models import AuditEvent, Organization, User
from whygraph.portal.orgs import add_member, create_org

TOKEN = "ghp_" + "A1b2C3d4" * 5
CONNECTION_TOKEN = "wgc_" + "Z9y8X7w6" * 4


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def writer(portal_database: str) -> Iterator[AuditWriter]:
    """A started writer on a fresh database (batches wait 30 s unless flushed)."""
    w = AuditWriter(batch_wait=30.0)
    w.start()
    try:
        yield w
    finally:
        w.stop()


def submit(w: AuditWriter, event: str = "test_event", **values) -> bool:
    values.setdefault("created_at", _iso(datetime.now(timezone.utc)))
    return w.submit(event=event, **values)


def rows() -> list[AuditEvent]:
    with portal_db.get_session() as session:
        found = session.exec(select(AuditEvent).order_by(AuditEvent.id)).all()
        for row in found:
            session.expunge(row)
        return list(found)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def make_user(login: str | None = None, email: str | None = None) -> tuple[int, str]:
    with portal_db.get_session() as session:
        user = User(
            display_name=login or email or "x",
            github_login=login,
            github_id=None if login is None else 1000 + len(login),
            email=email,
        )
        session.add(user)
        session.flush()
        assert user.id is not None
        return user.id, user.uid


def make_org(slug: str) -> int:
    with portal_db.get_session() as session:
        org = create_org(session, slug=slug, name=slug.title())
        assert org.id is not None
        return org.id


def flush() -> None:
    writer = audit_mod.current_writer()
    assert writer is not None, "the production lifespan registers a writer"
    assert writer.flush()


# ---------------------------------------------------------------------------
# The writer (section 6.1 #5)
# ---------------------------------------------------------------------------


def test_the_writer_batches_by_size(
    writer: AuditWriter, monkeypatch: pytest.MonkeyPatch
) -> None:
    sizes: list[int] = []
    original = AuditWriter._write

    def spy(self, batch):  # noqa: ANN001, ANN202
        if batch:
            sizes.append(len(batch))
        return original(self, batch)

    monkeypatch.setattr(AuditWriter, "_write", spy)
    writer._batch_size = 2
    for n in range(5):
        assert submit(writer, f"e{n}")
    assert writer.flush()
    assert sizes == [2, 2, 1]
    assert [r.event for r in rows()] == ["e0", "e1", "e2", "e3", "e4"]


def test_the_writer_writes_a_partial_batch_after_the_wait(portal_database: str) -> None:
    w = AuditWriter(batch_wait=0.1)
    w.start()
    try:
        submit(w, "soon")
        deadline = time.monotonic() + 10
        while not rows() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert [r.event for r in rows()] == ["soon"]
    finally:
        w.stop()


def test_a_full_queue_drops_and_warns_once_a_minute(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(logging.getLogger("whygraph"), "propagate", True)
    caplog.set_level(logging.WARNING, logger="whygraph.portal.audit_store")
    w = AuditWriter(maxsize=2)  # never started: nothing drains the queue
    assert submit(w, "a") and submit(w, "b")
    assert submit(w, "c") is False
    assert submit(w, "d") is False
    warnings = [r for r in caplog.records if "queue full" in r.getMessage()]
    assert len(warnings) == 1
    assert "dropped 1 event" in warnings[0].getMessage()


def test_every_string_is_redacted(writer: AuditWriter) -> None:
    submit(
        writer,
        target=f"token {TOKEN}",
        org="acme",
        fields={
            "note": f"leaked {TOKEN}",
            "nested": {"list": [CONNECTION_TOKEN, 3, None, True]},
            "path": None,
        },
    )
    assert writer.flush()
    (row,) = rows()
    assert TOKEN not in (row.target or "")
    assert row.target == "token ghp_***"
    assert row.fields == {
        "note": "leaked ghp_***",
        "nested": {"list": ["wgc_***", 3, None, True]},
        "path": None,
    }


def test_a_gone_org_or_actor_becomes_null_and_the_snapshots_stay(
    writer: AuditWriter,
) -> None:
    org_id = make_org("gone")
    user_id, uid = make_user(login="ben")
    with portal_db.get_session() as session:
        session.delete(session.get(Organization, org_id))
    submit(writer, "org_deleted", org_id=org_id, org="gone", uid=uid)
    submit(writer, "nobody", org_id=987654, org="never", uid="no-such-uid")
    assert writer.flush()
    deleted, nobody = rows()
    assert (deleted.org_id, deleted.org_slug) == (None, "gone")
    assert (deleted.actor_id, deleted.actor_label) == (user_id, "@ben")
    assert (nobody.org_id, nobody.org_slug) == (None, "never")
    assert (nobody.actor_id, nobody.actor_label) == (None, None)


def test_a_slug_only_caller_still_gets_its_org(writer: AuditWriter) -> None:
    org_id = make_org("acme")
    _, uid = make_user(email="ada@example.com")
    submit(writer, "member_added", org="acme", uid=uid)
    assert writer.flush()
    (row,) = rows()
    assert (row.org_id, row.org_slug, row.actor_label) == (
        org_id,
        "acme",
        "ada@example.com",
    )


def test_a_bad_row_is_dropped_alone(writer: AuditWriter) -> None:
    submit(writer, "first")
    submit(writer, None)  # NOT NULL event: the batch fails, then row by row
    submit(writer, "third")
    assert writer.flush()
    assert [r.event for r in rows()] == ["first", "third"]


def test_reader_gets_are_kept_once_per_actor_and_org_per_hour(
    writer: AuditWriter,
) -> None:
    for _ in range(3):
        submit(writer, "reader_request", uid="ada", org_id=1, fields={"method": "GET"})
    submit(writer, "reader_request", uid="ada", org_id=2, fields={"method": "GET"})
    submit(writer, "reader_request", uid="ada", org_id=1, fields={"method": "POST"})
    submit(writer, "reader_request", uid="ada", org_id=1, fields={"method": "POST"})
    assert writer.flush()
    assert [r.fields["method"] for r in rows()] == ["GET", "GET", "POST", "POST"]


def test_stop_writes_what_is_queued(portal_database: str) -> None:
    w = AuditWriter(batch_wait=60.0)
    w.start()
    for n in range(3):
        submit(w, f"e{n}")
    w.stop()
    assert [r.event for r in rows()] == ["e0", "e1", "e2"]
    w.stop()  # idempotent


def test_audit_hands_the_registered_writer_a_record(
    writer: AuditWriter, audit_log: pytest.LogCaptureFixture
) -> None:
    org_id = make_org("acme")
    _, uid = make_user(login="ben")
    audit_mod.set_writer(writer)
    try:
        request = {"client": ("203.0.113.9", 1), "headers": []}
        audit("org_renamed", request, uid=uid, org_id=org_id, org="acme", name="New")
    finally:
        audit_mod.clear_writer(writer)
    assert audit_mod.current_writer() is None
    audit("unpersisted", {"client": None, "headers": []})
    assert writer.flush()
    (row,) = rows()
    assert (row.event, row.org_id, row.org_slug, row.ip) == (
        "org_renamed",
        org_id,
        "acme",
        "203.0.113.9",
    )
    assert row.fields == {"name": "New"}
    (record, _) = events(audit_log)
    assert record == {
        "event": "org_renamed",
        "uid": uid,
        "target": None,
        "ip": "203.0.113.9",
        "host": "",
        "org_id": org_id,
        "org": "acme",
        "name": "New",
    }


def test_clear_writer_leaves_another_writer_registered(portal_database: str) -> None:
    first, second = AuditWriter(), AuditWriter()
    audit_mod.set_writer(second)
    try:
        audit_mod.clear_writer(first)
        assert audit_mod.current_writer() is second
    finally:
        audit_mod.set_writer(None)


def test_prune_deletes_rows_past_the_retention(writer: AuditWriter) -> None:
    now = datetime.now(timezone.utc)
    submit(writer, "old", created_at=_iso(now - timedelta(days=401)))
    submit(writer, "kept", created_at=_iso(now - timedelta(days=399)))
    assert writer.flush()
    assert audit_store.prune() == 1
    assert [r.event for r in rows()] == ["kept"]


# ---------------------------------------------------------------------------
# CSV (section 6.1 #6)
# ---------------------------------------------------------------------------


def test_formula_like_cells_are_escaped() -> None:
    for value in ("=1+1", "+1", "-1", "@SUM(A1)", "\tx", "\rx"):
        assert csv_cell(value) == "'" + value
    assert csv_cell("plain") == "plain"
    assert csv_cell(None) == ""
    assert csv_cell(-3) == "-3"  # numbers are not text a sheet evaluates


def test_the_csv_escapes_a_login_and_a_slug(writer: AuditWriter) -> None:
    with portal_db.get_session() as session:
        session.add(
            AuditEvent(
                event="member_added",
                org_slug="=HYPERLINK(1)",
                actor_label="@=evil",
                target="-2",
                fields={"github_login": "=cmd"},
            )
        )
    lines = list(iter_csv(audit_store.make_filter()))
    header, row = list(csv.reader(io.StringIO("".join(lines))))
    assert header == list(audit_store.CSV_COLUMNS)
    cells = dict(zip(header, row))
    assert cells["org"] == "'=HYPERLINK(1)"
    assert cells["actor"] == "'@=evil"
    assert cells["target"] == "'-2"
    assert cells["fields"] == '{"github_login": "=cmd"}'  # one JSON cell


def test_the_csv_stops_at_its_cap(writer: AuditWriter) -> None:
    for n in range(5):
        submit(writer, f"e{n}")
    assert writer.flush()
    lines = list(iter_csv(audit_store.make_filter(), max_rows=3, chunk=2))
    assert len(lines) == 4  # header + 3


# ---------------------------------------------------------------------------
# Routes (section 6.2 #6)
# ---------------------------------------------------------------------------


def join(t: SimpleNamespace, name: str, role: str) -> None:
    """Make ``name`` a ``role`` of ``acme`` directly (no members route)."""
    with portal_db.get_session() as session:
        add_member(session, org_id=t.org_id, user_id=t.ids[name], role=role)


def rename(t: SimpleNamespace, name: str) -> None:
    response = t.client.patch(at("acme") + "/api/org", json={"name": name})
    assert response.status_code == 200, response.text


def org_audit(t: SimpleNamespace, **params) -> dict:
    response = t.client.get(at("acme") + "/api/org/audit", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_events_land_with_their_org_and_actor(team: SimpleNamespace) -> None:
    t = team
    be(t, "ben")
    rename(t, "Acme Two")
    flush()
    body = org_audit(t)
    renamed = [e for e in body["events"] if e["event"] == "org_renamed"]
    assert len(renamed) == 1
    (event,) = renamed
    assert event["org"] == "acme"
    assert event["actor"] == {"uid": t.uids["ben"], "label": "@ben"}
    assert event["fields"] == {"name": "Acme Two", "previous": "Acme"}
    assert event["ip"]
    with portal_db.get_session() as session:
        row = session.get(AuditEvent, event["id"])
        assert (row.org_id, row.actor_id) == (t.org_id, t.ids["ben"])
    # The org's sign-ins are org-less: not on the org's page.
    assert all(e["event"] != "github_signin" for e in body["events"])


def test_the_org_log_filters_and_pages(team: SimpleNamespace) -> None:
    t = team
    be(t, "ben")
    for n in range(3):
        rename(t, f"Acme {n}")
    flush()
    page = org_audit(t, event="org_renamed", limit=2)
    assert [e["fields"]["name"] for e in page["events"]] == ["Acme 2", "Acme 1"]
    assert page["next"] == page["events"][-1]["id"]
    rest = org_audit(t, event="org_renamed", limit=2, before=page["next"])
    assert [e["fields"]["name"] for e in rest["events"]] == ["Acme 0"]
    assert rest["next"] is None
    for actor in ("@ben", "ben", "BEN", t.uids["ben"]):
        assert len(org_audit(t, event="org_renamed", actor=actor)["events"]) == 3
    assert org_audit(t, actor="@cy")["events"] == []
    today = datetime.now(timezone.utc).date()
    assert len(org_audit(t, event="org_renamed", to=today.isoformat())["events"]) == 3
    tomorrow = (today + timedelta(days=1)).isoformat()
    assert org_audit(t, **{"from": tomorrow})["events"] == []
    yesterday = (today - timedelta(days=1)).isoformat()
    assert org_audit(t, to=yesterday)["events"] == []
    bad = t.client.get(at("acme") + "/api/org/audit", params={"from": "soon"})
    assert bad.status_code == 422
    assert bad.json()["code"] == "bad_date"
    too_many = t.client.get(at("acme") + "/api/org/audit", params={"limit": 201})
    assert too_many.status_code == 422


def test_the_csv_export(team: SimpleNamespace) -> None:
    t = team
    be(t, "ben")
    rename(t, "=Acme")
    flush()
    response = t.client.get(
        at("acme") + "/api/org/audit.csv", params={"event": "org_renamed"}
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    assert (
        'filename="whygraph-audit-acme.csv"' in response.headers["content-disposition"]
    )
    header, row = list(csv.reader(io.StringIO(response.text)))
    cells = dict(zip(header, row))
    # Every "@login" label is escaped too: "@" starts a formula in a sheet.
    assert (cells["event"], cells["org"], cells["actor"]) == (
        "org_renamed",
        "acme",
        "'@ben",
    )
    assert '"name": "=Acme"' in cells["fields"]
    bad = t.client.get(at("acme") + "/api/org/audit.csv", params={"to": "x"})
    assert bad.status_code == 422


def test_the_org_log_is_the_owners(team: SimpleNamespace) -> None:
    t = team
    join(t, "cy", "admin")
    join(t, "dee", "member")
    for name in ("cy", "dee", "ada"):  # ada: an instance admin reading acme
        be(t, name)
        for path in ("/api/org/audit", "/api/org/audit.csv"):
            response = t.client.get(at("acme") + path)
            assert response.status_code == 403, (name, path, response.text)
            assert response.json()["action"] == "org.audit"
    be(t, "eve")  # not a member
    assert t.client.get(at("acme") + "/api/org/audit").status_code == 404
    be(t, "ben")
    assert t.client.get(at() + "/api/org/audit").status_code == 404  # base host


def test_the_admin_log_shows_orgless_and_deleted_orgs_events(
    team: SimpleNamespace,
) -> None:
    t = team
    be(t, "ben")
    rename(t, "Acme Two")
    assert (
        t.client.post(at() + "/api/orgs", json={"slug": "tmp", "name": "T"}).status_code
        == 201
    )
    deleted = t.client.request(
        "DELETE", at("tmp") + "/api/org", json={"confirm_slug": "tmp"}
    )
    assert deleted.status_code == 200, deleted.text
    flush()
    refused = t.client.get(at() + "/api/admin/audit")
    assert refused.status_code == 403
    be(t, "ada")
    assert t.client.get(at("acme") + "/api/admin/audit").status_code == 404
    response = t.client.get(at() + "/api/admin/audit", params={"limit": 200})
    assert response.status_code == 200, response.text
    found = response.json()["events"]
    names = {e["event"] for e in found}
    assert "github_signin" in names  # org-less
    assert "org_deleted" in names  # the org is gone, the slug kept
    assert "org_renamed" not in names  # acme's own page shows that
    tmp = t.client.get(at() + "/api/admin/audit", params={"org": "tmp"}).json()
    assert {e["org"] for e in tmp["events"]} == {"tmp"}
    assert {"org_created", "org_deleted"} <= {e["event"] for e in tmp["events"]}
