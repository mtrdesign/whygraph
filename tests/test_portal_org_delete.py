"""Deleting a production org: ``DELETE /api/org`` (M2d-2 plan sections 4.1, 4.8, 5.1).

The world of ``test_portal_github_import`` (a production portal whose GitHub
App talks to :class:`~github_fake.FakeGitHub`, git over dumb HTTP on a real
socket) and the runner's fake scan child. Ben owns ``acme``, Cy is its admin
and Dee its member; Ada is the password instance admin, an owner nowhere.

Covered: owner only (admin, member and a non-owner instance admin ``403``);
the typed slug (``409 confirm_slug``); local mode ``404``; a running scan
cancelled; ``409 busy`` while a sync is still fetching, released cleanly so
the org keeps working; projects, clones, ``repos/<org>/``, run files, org
config and secrets, memberships gone; the slug retired (``409 slug_taken``,
the same answer as a live slug); members keep their accounts and sessions
while the org's host answers ``404``; another org untouched; a scan request
during the deletion refused; an import racing the deletion leaves no row and
no clone; removing a project deletes its run files.
"""

# ruff: noqa: F811 -- pytest fixtures imported from other test modules

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlmodel import col, select

from test_portal_app import (  # noqa: F401
    at,
    github_sign_in,
    log_in,
    portal_client,
)
from test_portal_github_import import (  # noqa: F401 -- fixtures
    API_REPO,
    World,
    app_env,
    connected,
    env,
    github_app_key,
    github_git_server,
    import_repo,
    production_env,
    project_row,
    world,
)
from test_portal_identity_routes import (  # noqa: F401 -- `audit_log` is a fixture
    audit_log,
    create_org,
    events,
)
from test_portal_runner import TERMINAL, scanner, wait_for  # noqa: F401
from whygraph.portal import db as portal_db
from whygraph.portal import org_routes
from whygraph.portal import routes as routes_mod
from whygraph.portal.models import (
    Membership,
    Organization,
    Project,
    ProjectConfig,
    RetiredOrgSlug,
    ScanRun,
    Secret,
    User,
    UserSession,
)
from whygraph.services.git import Repository

NOT_FOUND = {"error": "not found"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def delete_org(
    w: World, org: str = "acme", confirm: str | None = None
) -> httpx.Response:
    """``DELETE /api/org`` on ``org``'s host, confirming with ``confirm`` (default: ``org``)."""
    return w.client.request(
        "DELETE",
        at(org) + "/api/org",
        json={"confirm_slug": org if confirm is None else confirm},
    )


def sign_in(w: World, login: str) -> None:
    w.client.cookies.clear()
    assert github_sign_in(w.client, login).status_code == 200


def team(w: World) -> None:
    """Cy and Dee sign in once; Ben makes Cy an admin and Dee a member of ``acme``."""
    sign_in(w, "cy")
    sign_in(w, "dee")
    sign_in(w, "ben")
    for login, role in (("cy", "admin"), ("dee", "member")):
        response = w.client.post(
            at("acme") + "/api/org/members", json={"github_login": login, "role": role}
        )
        assert response.status_code == 201, response.text


def imported(w: World, org: str = "acme") -> int:
    """Import ``acme/api`` into ``org`` (GitHub already connected); its project id."""
    response = import_repo(w, API_REPO, org=org)
    assert response.status_code == 201, response.text
    row = project_row(org, "api")
    assert row is not None and row.id is not None
    return row.id


def scan(w: World, org: str = "acme") -> int:
    response = w.client.post(at(org) + "/api/projects/api/scans", json={})
    assert response.status_code == 202, response.text
    return response.json()["run_id"]


def wait_run(w: World, run_id: int, org: str = "acme") -> dict:
    def done() -> dict | None:
        response = w.client.get(at(org) + "/api/projects/api/scans")
        run = next(r for r in response.json()["runs"] if r["id"] == run_id)
        return run if run["status"] in TERMINAL else None

    return wait_for(done, 30)


def configure(w: World, org: str) -> None:
    """Org defaults and an org key, plus a project key, in ``org``."""
    response = w.client.put(
        at(org) + "/api/portal/defaults",
        json={
            "config": {"llm": {"model": f"anthropic/{org}-model"}},
            "secrets": {"llm": {"anthropic": f"sk-ant-{org}-org-key-0123456789"}},
        },
    )
    assert response.status_code == 200, response.text
    response = w.client.put(
        at(org) + "/api/projects/api/config",
        json={"secrets": {"llm": {"openai": f"sk-{org}-project-key-0123456789"}}},
    )
    assert response.status_code == 200, response.text


def run_files(w: World, run_id: int) -> list[Path]:
    runs = w.env.data / "runs"
    return [runs / f"{run_id}{ext}" for ext in (".jsonl", ".log")]


def org_id(slug: str) -> int | None:
    with portal_db.get_session() as session:
        return session.exec(
            select(Organization.id).where(Organization.slug == slug)
        ).first()


def rows_of(oid: int) -> dict[str, int]:
    """How many rows the org still owns, per table."""
    with portal_db.get_session() as session:
        project_ids = list(
            session.exec(select(Project.id).where(Project.org_id == oid)).all()
        )
        return {
            "projects": len(project_ids),
            "memberships": len(
                session.exec(select(Membership).where(Membership.org_id == oid)).all()
            ),
            "config": len(
                session.exec(
                    select(ProjectConfig).where(ProjectConfig.org_id == oid)
                ).all()
            ),
            "secrets": len(
                session.exec(select(Secret).where(Secret.org_id == oid)).all()
            ),
            "runs": len(
                session.exec(
                    select(ScanRun).where(col(ScanRun.project_id).in_(project_ids))
                ).all()
            )
            if project_ids
            else 0,
        }


def runner_released(w: World) -> bool:
    runner = w.state.runner
    with runner._claims:
        return not runner._deleting_orgs and not runner._removing


# ---------------------------------------------------------------------------
# Who may delete, and how it is confirmed
# ---------------------------------------------------------------------------


def test_only_an_owner_deletes_an_org(world: World) -> None:
    w = world
    team(w)
    for login, role in (("cy", "admin"), ("dee", "member")):
        sign_in(w, login)
        response = delete_org(w)
        assert response.status_code == 403, (login, response.text)
        assert response.json() == {
            "error": f"your role ({role}) cannot org.own",
            "code": "forbidden",
            "action": "org.own",
        }
    # An instance admin reads every org and writes to none.
    w.client.cookies.clear()
    assert log_in(w.client, "ada@example.com").status_code == 200
    response = delete_org(w)
    assert (response.status_code, response.json()["code"]) == (403, "forbidden")
    assert org_id("acme") is not None
    assert runner_released(w)


def test_the_slug_must_be_typed(world: World) -> None:
    w = world
    for confirm in ("", "Acme", "acme ", "bravo"):
        response = delete_org(w, confirm=confirm)
        assert response.status_code == 409, (confirm, response.text)
        assert response.json() == {
            "error": "type the organization's slug (acme) to delete it",
            "code": "confirm_slug",
        }
    no_body = w.client.request("DELETE", at("acme") + "/api/org")
    assert (no_body.status_code, no_body.json()["code"]) == (409, "confirm_slug")
    assert org_id("acme") is not None


def test_local_mode_has_no_org_deletion(env: SimpleNamespace) -> None:
    with portal_client() as client:
        setup = client.post("/api/portal/setup", json={"display_name": "Tess"})
        assert setup.status_code == 201
        response = client.request("DELETE", "/api/org", json={"confirm_slug": "local"})
        assert (response.status_code, response.json()) == (404, NOT_FOUND)
        assert org_id("local") is not None


# ---------------------------------------------------------------------------
# What a deletion removes, and what it keeps
# ---------------------------------------------------------------------------


def test_deleting_an_org_removes_everything_it_owns_and_nothing_else(
    world: World, scanner: SimpleNamespace, audit_log: pytest.LogCaptureFixture
) -> None:
    w = world
    team(w)
    assert create_org(w.client, "bravo", "Bravo").status_code == 201
    connected(w)
    imported(w, "acme")
    imported(w, "bravo")  # the same repository, in another org
    configure(w, "acme")
    configure(w, "bravo")
    acme_run, bravo_run = scan(w, "acme"), scan(w, "bravo")
    assert wait_run(w, acme_run)["status"] == "ok"
    assert wait_run(w, bravo_run, "bravo")["status"] == "ok"
    (w.env.data / "runs" / f"{acme_run}.token").write_text("left over")
    in_flight = w.repos / "acme" / ".clone-api-0123456789ab"
    in_flight.mkdir()
    acme, bravo = org_id("acme"), org_id("bravo")
    assert acme is not None and bravo is not None
    before = rows_of(acme)
    assert all(before.values()), before
    bravo_before = rows_of(bravo)
    with portal_db.get_session() as session:
        users_before = len(session.exec(select(User)).all())
        cy_id = session.exec(select(User.id).where(User.github_login == "cy")).one()
        cy_sessions = len(
            session.exec(select(UserSession).where(UserSession.user_id == cy_id)).all()
        )
    assert cy_sessions >= 1

    response = delete_org(w)

    assert response.status_code == 200, response.text
    assert response.json() == {"deleted": "acme", "projects": 1}
    assert org_id("acme") is None
    assert rows_of(acme) == dict.fromkeys(before, 0)
    assert not (w.repos / "acme").exists()
    assert not any(p.exists() for p in run_files(w, acme_run))
    assert not (w.env.data / "runs" / f"{acme_run}.token").exists()
    with portal_db.get_session() as session:
        assert session.get(RetiredOrgSlug, "acme") is not None
        # Members keep their accounts and their sessions.
        assert len(session.exec(select(User)).all()) == users_before
        assert (
            len(
                session.exec(
                    select(UserSession).where(UserSession.user_id == cy_id)
                ).all()
            )
            == cy_sessions
        )
    # The org's host is gone; the account and the other org still answer.
    gone = w.client.get(at("acme") + "/api/projects")
    assert (gone.status_code, gone.json()) == (404, NOT_FOUND)
    assert w.client.get(at() + "/api/account").status_code == 200
    orgs = w.client.get(at() + "/api/account/orgs")
    assert [o["slug"] for o in orgs.json()] == ["bravo"], orgs.text
    # Another org is untouched: rows, clone, run files, config and keys.
    assert rows_of(bravo) == bravo_before
    assert (w.repos / "bravo" / "api" / ".git").is_dir()
    assert all(p.is_file() for p in run_files(w, bravo_run))
    defaults = w.client.get(at("bravo") + "/api/portal/defaults")
    assert "bravo-model" in defaults.text
    assert wait_run(w, scan(w, "bravo"), "bravo")["status"] == "ok"
    (event,) = [e for e in events(audit_log) if e["event"] == "org_deleted"]
    assert (event["org"], event["projects"]) == ("acme", 1)
    assert event["uid"]
    assert runner_released(w)


def test_a_deleted_slug_is_retired(world: World) -> None:
    w = world
    assert delete_org(w).status_code == 200
    again = create_org(w.client, "acme", "Acme again")
    live = create_org(w.client, "bravo", "Bravo")
    assert live.status_code == 201
    taken = create_org(w.client, "bravo", "Bravo again")
    # One answer for a retired slug and a live one: a deletion is not disclosed.
    assert again.status_code == taken.status_code == 409, again.text
    assert again.json() == {
        "error": "the organization 'acme' already exists",
        "code": "slug_taken",
    }
    assert taken.json()["code"] == "slug_taken"
    assert org_id("acme") is None


def test_a_running_scan_is_cancelled(world: World, scanner: SimpleNamespace) -> None:
    w = world
    connected(w)
    project_id = imported(w)
    scanner.hold.touch()
    scan(w)
    wait_for(scanner.calls)
    job = wait_for(lambda: w.state.runner._running.get(project_id))
    assert job.alive()

    response = delete_org(w)

    assert response.status_code == 200, response.text
    assert job.cancelled and job.done.is_set() and not job.alive()
    assert org_id("acme") is None and not (w.repos / "acme").exists()
    assert runner_released(w)


# ---------------------------------------------------------------------------
# Concurrency (plan section 4.8 step 1, 4.5 step 7)
# ---------------------------------------------------------------------------


def test_a_sync_still_fetching_is_busy_and_releases_everything(
    world: World, scanner: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world
    connected(w)
    imported(w)
    fetching, finish = threading.Event(), threading.Event()
    real_fetch = Repository.fetch_default

    def slow_fetch(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        fetching.set()
        finish.wait(30)
        return real_fetch(self, *args, **kwargs)

    monkeypatch.setattr(Repository, "fetch_default", slow_fetch)
    w.state.runner.org_delete_wait = 0.5
    run_id = scan(w)
    assert fetching.wait(10)

    response = delete_org(w)

    assert response.status_code == 409, response.text
    assert response.json() == {
        "error": "a sync is finishing; try again in a minute",
        "code": "busy",
    }
    assert runner_released(w)
    assert org_id("acme") is not None and (w.repos / "acme" / "api").is_dir()
    finish.set()
    # The cancel reached the sync: it stops after the fetch, without a scan.
    assert wait_run(w, run_id)["status"] == "cancelled"
    assert scanner.calls() == []
    # The org keeps working, and a second attempt goes through.
    assert wait_run(w, scan(w))["status"] == "ok"
    assert delete_org(w).status_code == 200
    assert org_id("acme") is None


def test_a_scan_request_during_the_deletion_is_refused(
    world: World, scanner: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world
    connected(w)
    imported(w)
    entered, release = threading.Event(), threading.Event()
    real = org_routes._delete_rows

    def slow_delete_rows(*args):  # noqa: ANN002, ANN202
        entered.set()
        release.wait(30)
        return real(*args)

    monkeypatch.setattr(org_routes, "_delete_rows", slow_delete_rows)
    deleted: list[httpx.Response] = []
    deleter = threading.Thread(target=lambda: deleted.append(delete_org(w)))
    deleter.start()
    assert entered.wait(10)
    refused = w.client.post(at("acme") + "/api/projects/api/scans", json={})
    second = delete_org(w)
    release.set()
    deleter.join(30)
    assert refused.status_code == 409, refused.text
    assert (second.status_code, second.json()["code"]) == (409, "busy")
    assert deleted[0].status_code == 200, deleted[0].text
    assert scanner.calls() == []
    assert runner_released(w)


def test_an_import_racing_the_deletion_leaves_no_row_and_no_clone(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The import has cloned and is about to insert when the org goes."""
    w = world
    connected(w)
    entered, release = threading.Event(), threading.Event()
    real = routes_mod._insert_imported

    def paused_insert(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        entered.set()
        release.wait(30)
        return real(*args, **kwargs)

    monkeypatch.setattr(routes_mod, "_insert_imported", paused_insert)
    answers: list[httpx.Response] = []
    importer = threading.Thread(target=lambda: answers.append(import_repo(w, API_REPO)))
    importer.start()
    assert entered.wait(30)
    assert (w.repos / "acme" / "api" / ".git").is_dir()  # cloned, not inserted

    response = delete_org(w)
    release.set()
    importer.join(30)

    assert response.status_code == 200, response.text
    assert response.json()["projects"] == 0
    assert (answers[0].status_code, answers[0].json()) == (404, NOT_FOUND)
    assert not (w.repos / "acme").exists()
    with portal_db.get_session() as session:
        assert session.exec(select(Project)).all() == []


# ---------------------------------------------------------------------------
# Removing one project (plan section 0.2 #17)
# ---------------------------------------------------------------------------


def test_removing_a_production_project_deletes_its_run_files(
    world: World, scanner: SimpleNamespace
) -> None:
    w = world
    connected(w)
    imported(w)
    run_id = scan(w)
    assert wait_run(w, run_id)["status"] == "ok"
    files = run_files(w, run_id)
    assert all(p.is_file() for p in files)

    response = w.client.request(
        "DELETE", at("acme") + "/api/projects/api", json={"confirm_name": "api"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["checkout_deleted"] is True
    assert not any(p.exists() for p in files)
