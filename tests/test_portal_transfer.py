"""Project transfer over REST (plan 42 section 5.8, tests section 10).

``/api/projects/{slug}/transfer`` through a production portal (``acme``:
Ben the owner, Cy an org admin, Dee a member - a contributor on the project
- and Eve a viewer by grant) and through a local portal with its single
user. The source is ``api``; the target is ``web``, another project over a
copy of the same repository, so the repository gate passes.

Covered: export -> dry run -> apply -> re-import (idempotent), the result
shape, the "imported" names, ``GET`` and ``DELETE /rationale``; the role
guard; the per-org and portal-wide slots, the runner claim and ``is_busy``;
the throttles; the upload cap, the content type and a broken archive; the
wrong-repository and needs-scan refusals; audit records without content;
temp files gone after every request; the start-up sweep. The linked refusal
(``403 managed_on_platform``) is in ``test_portal_linked_manage.py``'s
``REFUSED`` table; the route inventories are in ``test_portal_app.py`` and
``test_portal_tenancy.py``.
"""

# ruff: noqa: F811 -- pytest fixtures (`api`, `team`, ...) are imported

from __future__ import annotations

import io
import json
import os
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlmodel import select

from test_portal_app import (  # noqa: F401 -- fixtures
    _git,
    at,
    env,
    github_fake,
    make_repo,
    manual_ctx,
    portal_client,
    production_env,
)
from test_portal_hosts_isolation import _insert_project
from test_portal_identity_routes import audit_log, create_org, events  # noqa: F401
from test_portal_mcp import _setup_project
from test_portal_members import be, team, with_roles  # noqa: F401 -- fixture
from test_portal_notes import BODY, NARROW, api, call  # noqa: F401 -- fixture
from test_portal_runner import wait_idle
from test_transfer_import import _bundle, _dump_hash, _members
from whygraph.core.context import use_project
from whygraph.db.engine import get_session as project_session
from whygraph.db.models import ChatSession, Commit, RationaleCache
from whygraph.portal import db as portal_db
from whygraph.portal import transfer_bundle as tb
from whygraph.portal import transfer_routes
from whygraph.portal.models import Organization, Project, User
from whygraph.portal.transfer_export import sweep_transfer_tmp

T = "/api/projects/{slug}/transfer"
WIDE_BODY = "every service must stay stateless between requests"
T0 = "2026-09-01T10:00:00+00:00"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def seed_scan(root: Path, slug: str, org: str, project_id: int, *, described: bool):
    """The repo's commits as scanned rows, and ``last_scanned_head`` at ``HEAD``."""
    lines = _git(root, "log", "--format=%H|%P|%cI").splitlines()
    with use_project(manual_ctx(root, slug, org)), project_session() as session:
        for line in reversed(lines):
            sha, parents, committed = line.split("|")
            session.add(
                Commit(
                    sha=sha,
                    parent_shas=parents,
                    author_name="Test User",
                    author_email="tester@example.com",
                    authored_at=committed,
                    committed_at=committed,
                    subject=f"commit {sha[:7]}",
                    body="",
                    files_changed=1,
                    insertions=1,
                    deletions=0,
                    scanned_at=T0,
                    llm_description=f"Summary of {sha[:7]}." if described else None,
                    llm_description_model="openai:gpt-x" if described else None,
                )
            )
    with portal_db.get_session() as session:
        row = session.get(Project, project_id)
        row.last_scanned_head = lines[0].split("|")[0]
        session.add(row)


def seed_card(root: Path, slug: str, org: str) -> None:
    """One native rationale card on ``sample.py``."""
    with use_project(manual_ctx(root, slug, org)), project_session() as session:
        session.add(
            RationaleCache(
                path="sample.py",
                line_start=1,
                line_end=2,
                provider="openai",
                model="default",
                evidence_fingerprint="c" * 64,
                cached_at=T0,
                purpose="Adds numbers.",
                why="Because.",
                constraints="[]",
                tradeoffs="[]",
                risks="[]",
            )
        )


def seed_chat(root: Path, slug: str, org: str, owner: str | None, title: str) -> None:
    """One empty chat session owned by ``owner``."""
    with use_project(manual_ctx(root, slug, org)), project_session() as session:
        session.add(
            ChatSession(
                title=title,
                provider="anthropic",
                model="claude-x",
                created_at=T0,
                updated_at=T0,
                owner_uid=owner,
            )
        )


def tmp_files(state: Any) -> list[str]:
    """Transfer temp files left in ``<data>/tmp``."""
    tmp = Path(state.data_dir) / "tmp"
    return sorted(p.name for p in tmp.glob("transfer-*")) if tmp.is_dir() else []


def members_of(data: bytes) -> dict[str, list[dict]]:
    """Each JSONL member of a bundle, parsed."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return {
            name: [json.loads(line) for line in zf.read(name).splitlines() if line]
            for name in zf.namelist()
            if name.endswith(".jsonl")
        }


def counts_only(result: dict) -> dict:
    return {k: v for k, v in result.items() if k not in ("applied", "backup")}


def audits(log: pytest.LogCaptureFixture, event: str) -> list[dict]:
    return [e for e in events(log) if e["event"] == event]


def export(t: SimpleNamespace, who: str = "ben", slug: str = "api", **params: Any):
    be(t, who)
    return t.client.get(at("acme") + T.format(slug=slug) + "/export", params=params)


def send(
    t: SimpleNamespace,
    data: Any,
    *,
    who: str = "ben",
    slug: str = "web",
    org: str = "acme",
    content_type: str = "application/zip",
    **params: Any,
):
    be(t, who)
    return t.client.post(
        at(org) + T.format(slug=slug) + "/import",
        params={k: int(v) for k, v in params.items()},
        content=data,
        headers={"Content-Type": content_type},
    )


def relabel(data: bytes, tmp_path: Path, mode: str) -> bytes:
    """``data`` re-written as if exported by a ``mode`` portal.

    A local and a production portal cannot run in one test (the mode, the
    portal database and the context seam are process-wide), so each mode's
    test imports a real export of its own mode and one that carries the
    *other* mode's manifest source. The manifest's ``source`` is the only
    part of a bundle the exporter's mode changes besides which chats it
    holds, and the importer reads nothing else from it.
    """
    src, dst = tmp_path / "relabel-in.zip", tmp_path / "relabel-out.zip"
    src.write_bytes(data)
    with tb.read_bundle(src) as bundle:
        manifest = bundle.manifest.model_copy(
            update={
                "source": tb.ManifestSource(
                    mode=mode, label=f"{mode}/relabelled", name="relabelled"
                )
            }
        )
        members: dict[str, Any] = {
            name: [rec for _, rec in bundle.iter_records(name)]
            for name in (*tb.CORE_MEMBERS, *tb.CHAT_MEMBERS)
            if name in bundle.manifest.members
        }
        members[tb.CONFIG] = bundle.config or {}
    tb.write_bundle(dst, manifest, members)
    return dst.read_bytes()


@pytest.fixture
def world(api: SimpleNamespace, audit_log: pytest.LogCaptureFixture) -> SimpleNamespace:
    """``api`` scanned with two confirmed notes, a card and two chats; ``web`` scanned."""
    t = api
    t.state = t.client.app.state.portal
    t.log = audit_log
    seed_scan(t.root, "api", "acme", t.project_id, described=True)
    t.web_root = make_repo(t.root.parent, "web")
    t.web_id = _insert_project(t.org_id, "web", "Acme Web", t.web_root, t.ids["ben"])
    seed_scan(t.web_root, "web", "acme", t.web_id, described=False)
    t.web_db = manual_ctx(t.web_root, "web", "acme").config.whygraph_db
    N = "/api/projects/api/notes"
    for anchor, body in ((NARROW, BODY), ({"kind": "project"}, WIDE_BODY)):
        made = call(
            t,
            "dee",
            "POST",
            N,
            json={"body": body, "kind": "constraint", "anchor": anchor},
        )
        assert made.status_code == 201, made.text
        done = call(t, "ben", "POST", f"{N}/{made.json()['id']}/confirm", json={})
        assert done.status_code == 200, done.text
    seed_card(t.root, "api", "acme")
    seed_chat(t.root, "api", "acme", t.uids["ben"], "Ben's chat")
    seed_chat(t.root, "api", "acme", t.uids["cy"], "Cy's chat")
    return t


# ---------------------------------------------------------------------------
# The round trip
# ---------------------------------------------------------------------------


def test_export_preview_apply_reimport_and_remove_cards(world: SimpleNamespace) -> None:
    t = world
    # Export with chats: only Ben's own session travels.
    got = export(t, chats=1)
    assert got.status_code == 200, got.text
    assert got.headers["content-type"] == "application/zip"
    assert got.headers["content-disposition"].startswith(
        'attachment; filename="whygraph-api-'
    )
    assert int(got.headers["content-length"]) == len(got.content)
    assert tmp_files(t.state) == []
    rows = members_of(got.content)
    assert [s["title"] for s in rows[tb.CHAT_SESSIONS]] == ["Ben's chat"]
    assert sorted(n["body"] for n in rows[tb.NOTES]) == sorted([BODY, WIDE_BODY])
    assert len(rows[tb.RATIONALE]) == 1 and len(rows[tb.COMMIT_DESCRIPTIONS]) == 2
    data = got.content
    (exported,) = audits(t.log, "project_bundle_exported")
    assert exported["include_chats"] is True and exported["notes"] == 2
    assert exported["chat_sessions"] == 1 and exported["project"] == "api"
    assert exported["uid"] == t.uids["ben"] and len(exported["bundle_sha256"]) == 64
    assert BODY not in json.dumps(exported) and WIDE_BODY not in json.dumps(exported)

    # The dry run changes nothing and is not audited.
    before = _dump_hash(t.web_db)
    dry = send(t, data, dry_run=True)
    assert dry.status_code == 200, dry.text
    preview = dry.json()
    assert preview["applied"] is False
    assert _dump_hash(t.web_db) == before
    assert audits(t.log, "project_bundle_imported") == []
    assert preview["source"]["label"] == "acme/api"
    assert preview["notes"]["inserted"]["confirmed"] == 2
    assert [w["body"] for w in preview["wide_confirmed"]] == [WIDE_BODY]
    assert preview["rationale"]["inserted"] == 1
    assert preview["descriptions"]["filled"] == 2
    assert preview["chats"]["sessions"] == 1
    assert preview["backup"] == {
        "written": False,
        "skipped_low_disk": False,
        "path": None,
    }
    assert tmp_files(t.state) == []

    # The apply matches the preview, and is audited without content.
    applied = send(t, data)
    assert applied.status_code == 200, applied.text
    result = applied.json()
    assert result["applied"] is True
    assert counts_only(result) == counts_only(preview)
    assert result["backup"]["written"] is True and result["backup"]["path"] is None
    (imported,) = audits(t.log, "project_bundle_imported")
    assert imported["bundle_sha256"] == exported["bundle_sha256"]
    assert imported["source_mode"] == "production" and imported["notes"] == 2
    assert imported["rationale"] == 1 and imported["config_applied"] is False
    assert imported["partial"] is False
    assert BODY not in json.dumps(imported)
    assert tmp_files(t.state) == []

    # The imported notes stay confirmed, named from the bundle, badged.
    notes = call(t, "ben", "GET", "/api/projects/web/notes").json()
    assert {n["status"] for n in notes} == {"confirmed"}
    assert all(n["confirmed_by_imported"] and n["proposed_by_imported"] for n in notes)
    assert {n["confirmed_by_name"] for n in notes} == {"Ben"}
    assert all(n["confirmed_by"] == "imported:" + t.uids["ben"] for n in notes)

    # Re-importing adds nothing.
    again = send(t, data).json()
    assert sum(again["notes"]["inserted"].values()) == 0
    assert again["notes"]["skipped_duplicate"] == 2
    assert again["rationale"] == {**again["rationale"], "inserted": 0, "existing": 1}
    assert again["descriptions"]["filled"] == 0
    assert (again["chats"]["sessions"], again["chats"]["matched_existing"]) == (0, 1)

    # GET: two applies logged, newest first; one imported card.
    state = call(t, "ben", "GET", T.format(slug="web")).json()
    assert state["scanned"] is True and state["imported_rationale"] == 1
    assert state["max_bundle_bytes"] == tb.MAX_BUNDLE_BYTES
    assert [i["counts"]["rationale"]["inserted"] for i in state["imports"]] == [0, 1]
    first = state["imports"][1]
    assert (first["source_label"], first["source_mode"]) == ("acme/api", "production")
    assert (first["imported_by"], first["imported_by_name"]) == (t.uids["ben"], "Ben")

    # DELETE /rationale removes exactly the imported card.
    removed = call(t, "ben", "DELETE", T.format(slug="web") + "/rationale")
    assert removed.status_code == 200 and removed.json() == {"removed": 1}
    (event,) = audits(t.log, "imported_rationale_removed")
    assert event["count"] == 1 and event["project"] == "web"
    assert call(t, "ben", "GET", T.format(slug="web")).json()["imported_rationale"] == 0
    native = call(t, "ben", "DELETE", T.format(slug="api") + "/rationale").json()
    assert native == {"removed": 0}
    # Every slot and claim was given back.
    assert t.state.transfer_org._held == {} and t.state.transfer_portal._held == {}
    assert not t.state.runner._requesting.get(t.web_id)


def test_production_directions_round_trip(
    world: SimpleNamespace, tmp_path: Path
) -> None:
    """production -> production and local -> production (plan 42 AC 11, section 10).

    The other two directions are in
    ``test_local_directions_round_trip``; see :func:`relabel` for why a
    local-mode bundle is a relabelled export here.
    """
    t = world
    mirror_root = make_repo(t.root.parent, "mirror")
    mirror_id = _insert_project(
        t.org_id, "mirror", "Acme Mirror", mirror_root, t.ids["ben"]
    )
    seed_scan(mirror_root, "mirror", "acme", mirror_id, described=False)
    original = export(t, chats=1).content

    for source_mode, data, target in (
        ("production", original, "web"),
        ("local", relabel(original, tmp_path, "local"), "mirror"),
    ):
        dry = send(t, data, slug=target, dry_run=True)
        assert dry.status_code == 200, dry.text
        assert dry.json()["source"]["mode"] == source_mode
        applied = send(t, data, slug=target)
        assert applied.status_code == 200, applied.text
        result = applied.json()
        assert counts_only(result) == counts_only(dry.json())
        assert result["backup"]["path"] is None
        assert result["notes"]["inserted"]["confirmed"] == 2
        assert result["rationale"]["inserted"] == 1
        assert result["descriptions"]["filled"] == 2
        assert result["chats"]["sessions"] == 1
        notes = call(t, "ben", "GET", f"/api/projects/{target}/notes").json()
        assert {n["status"] for n in notes} == {"confirmed"}
        assert all(n["confirmed_by_imported"] for n in notes)
        state = call(t, "ben", "GET", T.format(slug=target)).json()
        assert state["imported_rationale"] == 1
        assert state["imports"][0]["source_mode"] == source_mode

        # The reverse trip adds nothing: the copy goes back to the source.
        back = export(t, slug=target, chats=1).content
        reverse = send(t, back, slug="api", dry_run=True).json()
        assert sum(reverse["notes"]["inserted"].values()) == 0
        assert reverse["notes"]["skipped_duplicate"] == 2
        assert reverse["rationale"]["inserted"] == 0
        assert reverse["descriptions"]["filled"] == 0
        assert reverse["chats"]["sessions"] == 0
    assert tmp_files(t.state) == []


def test_only_project_admins_may_transfer(world: SimpleNamespace) -> None:
    t = world
    data = export(t).content
    routes = [
        ("GET", T.format(slug="web"), {}),
        ("GET", T.format(slug="web") + "/export", {}),
        (
            "POST",
            T.format(slug="web") + "/import",
            {"content": data, "headers": {"Content-Type": "application/zip"}},
        ),
        ("DELETE", T.format(slug="web") + "/rationale", {}),
    ]
    for who in ("dee", "eve"):  # a contributor and a viewer
        for method, path, kw in routes:
            response = call(t, who, method, path, **kw)
            assert response.status_code == 403, (who, method, path, response.text)
            assert response.json()["code"] == "forbidden"
            assert response.json()["action"] == "project.configure"
    assert tmp_files(t.state) == []
    # An org admin is a project admin.
    assert call(t, "cy", "GET", T.format(slug="web")).status_code == 200
    assert send(t, data, who="cy", dry_run=True).status_code == 200


# ---------------------------------------------------------------------------
# Slots, claims, busy, throttles
# ---------------------------------------------------------------------------


def test_slots_claim_and_busy_refuse_with_409(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    t = world
    data = export(t).content
    org_slot, portal_slot = t.state.transfer_org, t.state.transfer_portal

    # Another transfer in the org holds its only slot.
    assert org_slot.try_acquire(t.org_id)
    for response in (export(t), send(t, data, dry_run=True)):
        assert response.status_code == 409 and response.json()["code"] == "busy"
    # ... while another org's project still imports.
    be(t, "ben")
    assert create_org(t.client, "globex", "Globex").status_code == 201
    with portal_db.get_session() as session:
        globex = session.exec(
            select(Organization.id).where(Organization.slug == "globex")
        ).one()
    g_root = make_repo(t.root.parent / "globex", "web")
    g_id = _insert_project(globex, "web", "Globex Web", g_root, t.ids["ben"])
    seed_scan(g_root, "web", "globex", g_id, described=False)
    other = send(t, data, org="globex", dry_run=True)
    assert other.status_code == 200, other.text
    org_slot.release(t.org_id)

    # Two transfers elsewhere hold the portal's two slots.
    assert portal_slot.try_acquire("portal") and portal_slot.try_acquire("portal")
    busy = send(t, data, dry_run=True)
    assert busy.status_code == 409 and busy.json()["code"] == "busy"
    portal_slot.release("portal")
    portal_slot.release("portal")

    # A scan queued or running refuses an import, never an export.
    with monkeypatch.context() as patch:
        patch.setattr(t.state.runner, "is_busy", lambda project_id: True)
        scanning = send(t, data, dry_run=True)
        assert scanning.status_code == 409 and scanning.json()["code"] == "busy"
        assert export(t).status_code == 200

    # A removal in progress refuses all three writers.
    t.state.runner._removing.add(t.web_id)
    try:
        for response in (
            export(t, slug="web"),
            send(t, data, dry_run=True),
            call(t, "ben", "DELETE", T.format(slug="web") + "/rationale"),
        ):
            assert response.status_code == 409, response.text
            assert response.json()["code"] == "busy"
    finally:
        t.state.runner._removing.discard(t.web_id)
    assert send(t, data, dry_run=True).status_code == 200
    assert org_slot._held == {} and portal_slot._held == {}
    assert tmp_files(t.state) == []


def test_throttles_count_exports_and_imports(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    t = world
    monkeypatch.setattr(transfer_routes, "EXPORT_LIMIT", 1)
    monkeypatch.setattr(transfer_routes, "IMPORT_PROJECT_LIMIT", 1)
    data = export(t).content
    refused = export(t)
    assert refused.status_code == 429, refused.text
    assert refused.json()["code"] == "throttled"
    assert refused.json()["retry_after"] > 0 and "retry-after" in refused.headers
    assert send(t, data, dry_run=True).status_code == 200  # a dry run counts
    again = send(t, data, who="cy")  # another member, the same project
    assert again.status_code == 429 and again.json()["code"] == "throttled"


# ---------------------------------------------------------------------------
# The upload
# ---------------------------------------------------------------------------


def test_the_upload_is_typed_capped_and_never_kept(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    t = world
    wrong = send(t, b"{}", content_type="application/json")
    assert wrong.status_code == 415 and wrong.json()["code"] == "bundle_invalid"
    broken = send(t, b"not a zip at all", dry_run=True)
    assert broken.status_code == 400 and broken.json()["code"] == "bundle_invalid"
    assert tmp_files(t.state) == []

    monkeypatch.setattr(tb, "MAX_BUNDLE_BYTES", 64)
    declared = send(t, b"x" * 65)
    assert declared.status_code == 413 and declared.json()["code"] == "body_too_large"

    def chunks():  # no Content-Length: the streamed count trips the cap
        for _ in range(5):
            yield b"x" * 20

    streamed = send(t, chunks())
    assert streamed.status_code == 413 and streamed.json()["code"] == "body_too_large"
    assert tmp_files(t.state) == []


def test_wrong_repository_and_unscanned_projects_are_refused(
    world: SimpleNamespace, tmp_path: Path
) -> None:
    t = world
    stranger = _bundle(tmp_path / "other.zip", _members(), roots=("f" * 40,))
    before = _dump_hash(t.web_db)
    wrong = send(t, stranger.read_bytes())
    assert wrong.status_code == 409 and wrong.json()["code"] == "bundle_wrong_repo"
    assert _dump_hash(t.web_db) == before
    # A project with no first scan: nothing to export, nowhere to import.
    new_root = make_repo(t.root.parent, "new")
    _insert_project(t.org_id, "new", "Acme New", new_root, t.ids["ben"])
    state = call(t, "ben", "GET", T.format(slug="new")).json()
    assert state == {
        "scanned": False,
        "imported_rationale": 0,
        "max_bundle_bytes": tb.MAX_BUNDLE_BYTES,
        "imports": [],
    }
    unscanned = export(t, slug="new")
    assert unscanned.status_code == 409
    assert unscanned.json()["code"] == "transfer_needs_scan"
    data = export(t).content
    target = send(t, data, slug="new")
    assert target.status_code == 409 and target.json()["code"] == "transfer_needs_scan"
    assert tmp_files(t.state) == []
    assert audits(t.log, "project_bundle_imported") == []


# ---------------------------------------------------------------------------
# Local mode: owner-less chats, settings, the backup path, the sweep
# ---------------------------------------------------------------------------


def test_local_round_trip_with_settings_and_the_startup_sweep(
    env: SimpleNamespace,
) -> None:
    data_dir = Path(os.environ["WHYGRAPH_DATA"])
    tmp = data_dir / "tmp"
    tmp.mkdir(parents=True)
    old, fresh, other = (
        tmp / "transfer-old.zip",
        tmp / "transfer-in-fresh.zip",
        tmp / "other.zip",
    )
    for path in (old, fresh, other):
        path.write_bytes(b"x")
    hours_ago = time.time() - 2 * 3600
    for path in (old, other):
        os.utime(path, (hours_ago, hours_ago))

    demo, copy = make_repo(env.shared, "demo"), make_repo(env.shared, "copy")
    with portal_client() as client:
        # The start-up sweep removed only the stale transfer file.
        assert (old.exists(), fresh.exists(), other.exists()) == (False, True, True)
        fresh.unlink()
        _setup_project(client, demo)
        added = client.post(
            "/api/projects", json={"source": "local", "path": str(copy)}
        )
        assert added.status_code == 201, added.text
        init = client.post("/api/projects/copy/init", json={"agents": []})
        assert init.status_code == 200, init.text
        wait_idle(client, "demo")
        wait_idle(client, "copy")
        ids = {}
        with portal_db.get_session() as session:
            for row in session.exec(select(Project)).all():
                ids[row.slug] = row.id
        seed_scan(demo, "demo", "local", ids["demo"], described=True)
        seed_scan(copy, "copy", "local", ids["copy"], described=False)
        with portal_db.get_session() as session:
            me = session.exec(select(User.uid)).one()
        seed_chat(demo, "demo", "local", None, "Before accounts")
        seed_chat(demo, "demo", "local", me, "Mine")
        saved = client.put(
            "/api/projects/demo/config",
            json={"config": {"rationale": {"model": "gpt-x"}}},
        )
        assert saved.status_code == 200, saved.text

        got = client.get("/api/projects/demo/transfer/export", params={"chats": 1})
        assert got.status_code == 200, got.text
        titles = sorted(s["title"] for s in members_of(got.content)[tb.CHAT_SESSIONS])
        assert titles == ["Before accounts", "Mine"]
        result = client.post(
            "/api/projects/copy/transfer/import",
            params={"apply_config": 1},
            content=got.content,
            headers={"Content-Type": "application/zip"},
        )
        assert result.status_code == 200, result.text
        body = result.json()
        assert body["applied"] is True and body["chats"]["sessions"] == 2
        assert body["config"]["applied"] is True
        assert body["config"]["changes"] == [
            {"key": "rationale.model", "from": None, "to": "gpt-x"}
        ]
        # Local mode names the backup; it is 0600.
        backup = Path(body["backup"]["path"])
        assert backup.is_file() and (backup.stat().st_mode & 0o777) == 0o600
        config = client.get("/api/projects/copy/config").json()
        assert json.dumps(config).count("gpt-x") >= 1
        state = client.app.state.portal
        assert tmp_files(state) == []


def test_local_directions_round_trip(env: SimpleNamespace, tmp_path: Path) -> None:
    """local -> local and production -> local (plan 42 AC 11, section 10).

    The other two directions are in ``test_production_directions_round_trip``.
    """
    demo, copy = make_repo(env.shared, "demo"), make_repo(env.shared, "copy")
    twin = make_repo(env.shared, "twin")
    with portal_client() as client:
        _setup_project(client, demo)
        for path in (copy, twin):
            added = client.post(
                "/api/projects", json={"source": "local", "path": str(path)}
            )
            assert added.status_code == 201, added.text
            init = client.post(f"/api/projects/{path.name}/init", json={"agents": []})
            assert init.status_code == 200, init.text
        for slug in ("demo", "copy", "twin"):
            wait_idle(client, slug)
        ids = {}
        with portal_db.get_session() as session:
            for row in session.exec(select(Project)).all():
                ids[row.slug] = row.id
        seed_scan(demo, "demo", "local", ids["demo"], described=True)
        seed_scan(copy, "copy", "local", ids["copy"], described=False)
        seed_scan(twin, "twin", "local", ids["twin"], described=False)
        seed_card(demo, "demo", "local")
        seed_chat(demo, "demo", "local", None, "Before accounts")
        for anchor, body in ((NARROW, BODY), ({"kind": "project"}, WIDE_BODY)):
            made = client.post(
                "/api/projects/demo/notes",
                json={"body": body, "kind": "constraint", "anchor": anchor},
            )
            assert made.status_code == 201, made.text
            done = client.post(
                f"/api/projects/demo/notes/{made.json()['id']}/confirm", json={}
            )
            assert done.status_code == 200, done.text

        got = client.get("/api/projects/demo/transfer/export", params={"chats": 1})
        assert got.status_code == 200, got.text
        original = got.content
        for source_mode, data, target in (
            ("local", original, "copy"),
            ("production", relabel(original, tmp_path, "production"), "twin"),
        ):
            post = {"content": data, "headers": {"Content-Type": "application/zip"}}
            dry = client.post(
                f"/api/projects/{target}/transfer/import",
                params={"dry_run": 1},
                **post,
            )
            applied = client.post(
                f"/api/projects/{target}/transfer/import",
                params={"dry_run": 0},
                **post,
            )
            assert dry.status_code == 200 and applied.status_code == 200, applied.text
            result = applied.json()
            assert result["source"]["mode"] == source_mode
            assert result["notes"]["inserted"]["confirmed"] == 2
            assert result["rationale"]["inserted"] == 1
            assert result["descriptions"]["filled"] == 2
            assert result["chats"]["sessions"] == 1
            assert Path(result["backup"]["path"]).is_file()
            notes = client.get(f"/api/projects/{target}/notes").json()
            assert {n["status"] for n in notes} == {"confirmed"}
            assert all(n["confirmed_by_imported"] for n in notes)

            back = client.get(
                f"/api/projects/{target}/transfer/export", params={"chats": 1}
            ).content
            reverse = client.post(
                "/api/projects/demo/transfer/import",
                params={"dry_run": 1},
                content=back,
                headers={"Content-Type": "application/zip"},
            ).json()
            assert sum(reverse["notes"]["inserted"].values()) == 0
            assert reverse["rationale"]["inserted"] == 0
            assert reverse["descriptions"]["filled"] == 0
            assert reverse["chats"]["sessions"] == 0


# ---------------------------------------------------------------------------
# The sweep on its own
# ---------------------------------------------------------------------------


def test_the_sweep_refuses_symlinks(tmp_path: Path) -> None:
    data = tmp_path / "data"
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "transfer-victim.zip"
    victim.write_bytes(b"x")
    stale = time.time() - 7200
    os.utime(victim, (stale, stale))
    data.mkdir()
    (data / "tmp").symlink_to(outside)
    assert sweep_transfer_tmp(data) == 0 and victim.exists()
    (data / "tmp").unlink()
    (data / "tmp").mkdir()
    link = data / "tmp" / "transfer-link.zip"
    link.symlink_to(victim)
    os.utime(link, (stale, stale), follow_symlinks=False)
    real = data / "tmp" / "transfer-real.zip"
    real.write_bytes(b"x")
    os.utime(real, (stale, stale))
    assert sweep_transfer_tmp(data) == 1
    assert not real.exists() and link.is_symlink() and victim.exists()
    assert sweep_transfer_tmp(tmp_path / "missing") == 0
