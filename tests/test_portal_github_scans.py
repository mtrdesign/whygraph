"""Scans of production GitHub projects (M2d-2 plan sections 0.2 #5, #6, #14, #20, #23, 4.6).

The world of ``test_portal_github_import`` (a production portal whose GitHub
App talks to :class:`~github_fake.FakeGitHub`, git over dumb HTTP on a real
socket) with ``acme/api`` imported, and the runner's fake scan child
(``tests/fixtures/fake_scan.py``).

Covered: "Scan now" is a sync that fetches first; the sync follows a rename
(``set-url`` from the repository id), a force-push (forced checkout, noted
``history_rewritten``) and a default-branch change, and refuses a branch
that tracks WhyGraph's state; the child's token file (``0600``, never the
environment, refreshed across an expiry, every value redacted, deleted when
the child ends however it ends); access lost on a refused mint, on a git
``401`` and in the refresher, the ``409 github_access_lost`` refusal, the
summary fields and the restore on a successful mint; the reconcile (a sync
only when the remote moved, access lost and restored by its mint, its own
loop at start that survives an exception); and no GitHub credential in the
portal DB, a run file, the clone's git config or a response after an
import, a scan and a refresh (acceptance criterion 9).
"""

# ruff: noqa: F811 -- pytest fixtures imported from other test modules

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlmodel import select

from github_fake import _git as fixture_git
from test_portal_app import (  # noqa: F401
    CLIENT_HEADER,
    PROD_BASE,
    at,
    claim_instance,
    github_sign_in,
    prod_portal,
)
from test_portal_github_import import (  # noqa: F401 -- fixtures
    API_REPO,
    INSTALLATION,
    TOKEN_SHAPE,
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
from test_portal_runner import TERMINAL, FakeClock, scanner, wait_for  # noqa: F401
from whygraph.portal import db as portal_db
from whygraph.portal import runner as runner_mod
from whygraph.portal.app import create_portal_app
from whygraph.portal.models import ScanRun, UsageEvent
from whygraph.portal.runner import ScanRunner
from whygraph.portal.secrets import hint_for
from whygraph.services.git.credentials import TOKEN_ENV_VAR, TOKEN_FILE_ENV

ORG = "acme"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def imported(w: World) -> Path:
    """Connect GitHub and import ``acme/api``; the clone's root."""
    connected(w)
    response = import_repo(w, API_REPO)
    assert response.status_code == 201, response.text
    return w.repos / ORG / "api"


def project_id() -> int:
    row = project_row(ORG, "api")
    assert row is not None and row.id is not None
    return row.id


def request_scan(w: World, **body: Any):  # noqa: ANN201 -- a Response
    return w.client.post(at(ORG) + "/api/projects/api/scans", json=body or None)


def scan(w: World, **body: Any) -> int:
    response = request_scan(w, **body)
    assert response.status_code == 202, response.text
    return response.json()["run_id"]


def runs(w: World) -> list[dict]:
    response = w.client.get(at(ORG) + "/api/projects/api/scans")
    assert response.status_code == 200, response.text
    return response.json()["runs"]


def wait_run(w: World, run_id: int, timeout: float = 30.0) -> dict:
    def done() -> dict | None:
        run = next(r for r in runs(w) if r["id"] == run_id)
        return run if run["status"] in TERMINAL else None

    return wait_for(done, timeout)


def run_log(w: World, run_id: int) -> str:
    response = w.client.get(at(ORG) + f"/api/projects/api/scans/{run_id}/log")
    assert response.status_code == 200, response.text
    return response.json()["text"]


def details(w: World) -> dict:
    response = w.client.get(at(ORG) + "/api/projects/api")
    assert response.status_code == 200, response.text
    return response.json()


def head(root: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True
    ).stdout.strip()


def git_out(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True
    ).stdout.strip()


def bare(w: World, full_name: str = "acme/api") -> Path:
    repo = next(r for r in w.fake.repos.values() if r.full_name == full_name)
    assert repo.path is not None
    return repo.path


def force_push(w: World, files: dict[str, str], branch: str = "main") -> str:
    """Replace a branch of ``acme/api`` with a new, unrelated history; its SHA."""
    path = bare(w)
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "work"
        fixture_git("init", "-q", f"--initial-branch={branch}", str(work))
        for name, content in files.items():
            (work / name).parent.mkdir(parents=True, exist_ok=True)
            (work / name).write_text(content)
        fixture_git("add", "-A", cwd=work)
        fixture_git("commit", "-q", "-m", "Rewritten", cwd=work)
        fixture_git(
            "push", "-q", "-f", str(path), f"HEAD:refs/heads/{branch}", cwd=work
        )
        sha = fixture_git("rev-parse", "HEAD", cwd=work)
    fixture_git("--git-dir", str(path), "update-server-info")
    return sha


def token_files(w: World) -> list[Path]:
    runs_dir = w.env.data / "runs"
    return sorted(runs_dir.glob("*.token*")) if runs_dir.is_dir() else []


def recorded_env(scanner: SimpleNamespace) -> dict[str, str]:
    (call,) = scanner.calls()
    return call["env"]


# ---------------------------------------------------------------------------
# Scan now is a sync (plan section 0.2 #23) with a token file (#5)
# ---------------------------------------------------------------------------


def test_scan_now_fetches_first_and_hands_the_child_a_token_file(
    world: World, scanner: SimpleNamespace
) -> None:
    w = world
    root = imported(w)
    pushed = w.server.commit("acme/api", message="Pushed after the import")
    scanner.hold.touch()
    run_id = scan(w)
    wait_for(scanner.calls)

    # The tree moved before the child started, and the child runs on it.
    assert head(root) == pushed
    env_ = recorded_env(scanner)
    assert "GH_TOKEN" not in env_ and TOKEN_ENV_VAR not in env_
    token_file = Path(env_[TOKEN_FILE_ENV])
    assert token_file.parent == w.env.data / "runs"
    assert token_file.name == f"{run_id}.token"
    assert token_file.stat().st_mode & 0o777 == 0o600
    token = token_file.read_text()
    assert token in w.fake.installation_tokens
    assert token_files(w) == [token_file]  # no temporary file left behind

    scanner.hold.unlink()
    run = wait_run(w, run_id)
    assert run["status"] == "ok", run
    assert (run["kind"], run["trigger"]) == ("sync", "initial")
    assert run["summary"]["moved"] is True
    assert "history_rewritten" not in run["summary"]
    assert not token_file.exists() and token_files(w) == []
    row = project_row(ORG, "api")
    assert row is not None and row.last_scanned_head == pushed

    # A second Scan now with nothing new still fetches, then scans.
    run = wait_run(w, scan(w))
    assert (run["status"], run["kind"], run["trigger"]) == ("ok", "sync", "manual")
    assert run["summary"]["moved"] is False
    assert len(scanner.calls()) == 2


def test_the_token_file_is_refreshed_across_an_expiry(
    world: World, scanner: SimpleNamespace
) -> None:
    """A child outliving its first token keeps working (acceptance criterion 5).

    Tokens live 4 seconds and the file is rewritten 3 seconds before
    expiry; the child checks the token at once and again 5 seconds later,
    against the fake's git server, which refuses an expired token.
    """
    w = world
    w.fake.installation_token_ttl = 4
    imported(w)
    w.state.runner.token_refresh_margin = 3
    seen = w.env.tmp / "tokens.txt"
    scanner.configure(
        token_check=f"{w.server.url}/acme/api.git/info/refs",
        token_wait=5,
        token_record=seen,
    )
    run_id = scan(w)
    run = wait_run(w, run_id)
    assert run["status"] == "ok", (run, run_log(w, run_id))

    first, second = seen.read_text().split()
    assert first != second
    assert first in w.fake.installation_tokens and second in w.fake.installation_tokens
    # The runner learned each value it wrote: the log shows their hints,
    # never a token (and not just the pattern's "ghs_***").
    text = run_log(w, run_id)
    assert first not in text and second not in text
    assert f"token value={hint_for(first)}" in text
    assert f"token value={hint_for(second)}" in text
    assert token_files(w) == []


def test_the_token_file_goes_when_the_child_fails_or_is_cancelled(
    world: World, scanner: SimpleNamespace
) -> None:
    w = world
    imported(w)
    scanner.configure(exit=1)
    run = wait_run(w, scan(w))
    assert run["status"] == "failed" and token_files(w) == []

    scanner.configure()
    scanner.hold.touch()
    run_id = scan(w)
    wait_for(lambda: len(scanner.calls()) == 2)
    assert len(token_files(w)) == 1
    response = w.client.post(at(ORG) + f"/api/projects/api/scans/{run_id}/cancel")
    assert response.status_code == 202, response.text
    assert wait_run(w, run_id)["status"] == "cancelled"
    assert token_files(w) == []


# ---------------------------------------------------------------------------
# The production sync (plan section 0.2 #1, #6, #20)
# ---------------------------------------------------------------------------


def test_a_renamed_repo_is_fetched_from_its_new_name(
    world: World, scanner: SimpleNamespace
) -> None:
    w = world
    root = imported(w)
    pushed = w.server.commit("acme/api")
    # GitHub renames the repository; the old name no longer serves it.
    w.fake.repos[API_REPO].full_name = "acme/renamed"

    run = wait_run(w, scan(w))
    assert run["status"] == "ok", run
    assert head(root) == pushed
    assert git_out(root, "remote", "get-url", "origin") == (
        f"{w.server.url}/acme/renamed.git"
    )
    row = project_row(ORG, "api")
    assert row is not None and row.remote_url == f"{w.server.url}/acme/renamed"


def test_a_force_push_is_checked_out_and_noted(
    world: World, scanner: SimpleNamespace
) -> None:
    w = world
    root = imported(w)
    old = head(root)
    rewritten = force_push(w, {"README.md": "rewritten\n"})

    run = wait_run(w, scan(w))
    assert run["status"] == "ok", run
    assert head(root) == rewritten != old
    assert git_out(root, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert (root / "README.md").read_text() == "rewritten\n"
    assert run["summary"]["moved"] is True
    assert run["summary"]["history_rewritten"] is True
    # The portal's own untracked state survived the checkout.
    assert (root / ".whygraph" / "whygraph.db").is_file()


def test_a_new_default_branch_is_followed(
    world: World, scanner: SimpleNamespace
) -> None:
    w = world
    root = imported(w)
    trunk = w.server.commit("acme/api", branch="trunk", message="On trunk")
    w.fake.repos[API_REPO].default_branch = "trunk"

    run = wait_run(w, scan(w))
    assert run["status"] == "ok", run
    assert head(root) == trunk
    assert git_out(root, "rev-parse", "--abbrev-ref", "HEAD") == "trunk"
    assert git_out(root, "symbolic-ref", "refs/remotes/origin/HEAD") == (
        "refs/remotes/origin/trunk"
    )
    assert run["summary"]["default_branch"] == "trunk"
    row = project_row(ORG, "api")
    assert row is not None and row.default_branch == "trunk"


def test_a_branch_tracking_whygraph_state_is_refused_at_sync(
    world: World, scanner: SimpleNamespace, audit_log: pytest.LogCaptureFixture
) -> None:
    w = world
    root = imported(w)
    before = head(root)
    w.server.commit("acme/api", files={".codegraph/codegraph.db": "poison\n"})

    run = wait_run(w, scan(w))
    assert run["status"] == "failed"
    assert "tracks WhyGraph's own state" in run["summary"]["error"]
    assert head(root) == before and not (root / ".codegraph" / "codegraph.db").exists()
    assert scanner.calls() == []
    body = details(w)
    assert (body["access_lost"], body["access_lost_reason"]) == (
        True,
        "tracked_whygraph_state",
    )
    assert [
        e["reason"] for e in events(audit_log) if e["event"] == "project_access_lost"
    ] == ["tracked_whygraph_state"]

    # Not a refusal: once the repository is fixed, the next sync clears it.
    clean = force_push(w, {"README.md": "clean\n"})
    run = wait_run(w, scan(w))
    assert run["status"] == "ok", run
    assert head(root) == clean
    assert details(w)["access_lost"] is False


# ---------------------------------------------------------------------------
# Access lost (plan section 0.2 #14)
# ---------------------------------------------------------------------------


def test_a_refused_mint_marks_access_lost_and_a_good_one_restores_it(
    world: World, scanner: SimpleNamespace, audit_log: pytest.LogCaptureFixture
) -> None:
    w = world
    imported(w)
    w.fake.uninstall(INSTALLATION)

    run = wait_run(w, scan(w))
    assert run["status"] == "failed" and scanner.calls() == []
    body = details(w)
    assert (body["access_lost"], body["access_lost_reason"]) == (True, "no_access")
    listed = w.client.get(at(ORG) + "/api/projects").json()
    (summary,) = [p for p in listed["projects"] if p["slug"] == "api"]
    assert summary["access_lost"] is True
    (lost,) = [e for e in events(audit_log) if e["event"] == "project_access_lost"]
    assert (lost["project"], lost["reason"]) == ("api", "no_access")

    response = request_scan(w)
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "github_access_lost"
    assert len(runs(w)) == 1  # nothing queued

    # Reinstalled: a successful mint clears it, and scans work again.
    w.fake.add_installation(INSTALLATION, "acme", account_type="Organization")
    w.fake.repos[API_REPO].installation = INSTALLATION
    assert w.state.runner.check_access(project_id()) is True
    assert details(w)["access_lost"] is False
    assert [e["event"] for e in events(audit_log)].count("project_access_restored") == 1
    assert wait_run(w, scan(w))["status"] == "ok"


def test_a_git_401_marks_access_lost(world: World, scanner: SimpleNamespace) -> None:
    w = world
    root = imported(w)
    before = head(root)
    w.server.commit("acme/api")
    w.fake.force("git", status=401, times=None)

    run = wait_run(w, scan(w))
    assert run["status"] == "failed" and scanner.calls() == []
    assert head(root) == before
    assert details(w)["access_lost_reason"] == "git_access_denied"
    assert request_scan(w).json()["code"] == "github_access_lost"

    w.fake.clear_forced()
    assert w.state.runner.check_access(project_id()) is True
    assert details(w)["access_lost"] is False


def test_a_refused_refresh_marks_access_lost_and_stops_refreshing(
    world: World, scanner: SimpleNamespace
) -> None:
    w = world
    w.fake.installation_token_ttl = 4
    imported(w)
    w.state.runner.token_refresh_margin = 3
    scanner.hold.touch()
    run_id = scan(w)
    wait_for(scanner.calls)
    token_file = Path(recorded_env(scanner)[TOKEN_FILE_ENV])
    first = token_file.read_text()
    wait_for(lambda: token_file.read_text() != first)  # a refresh happened

    w.fake.uninstall(INSTALLATION)
    wait_for(lambda: details(w)["access_lost"], timeout=10)
    assert details(w)["access_lost_reason"] == "no_access"
    mints = len(w.fake.calls("POST", "/api/v3/app/installations/"))
    time.sleep(2)  # the refresher would have tried again by now
    assert len(w.fake.calls("POST", "/api/v3/app/installations/")) == mints
    scanner.hold.unlink()
    assert wait_run(w, run_id)["status"] == "ok"
    assert token_files(w) == []
    assert request_scan(w).json()["code"] == "github_access_lost"


def test_a_queued_run_of_an_access_lost_project_fails_without_syncing(
    world: World, scanner: SimpleNamespace
) -> None:
    w = world
    imported(w)
    scanner.hold.touch()
    held = scan(w)
    wait_for(scanner.calls)
    queued = scan(w)  # waits behind the held run
    assert queued != held
    from whygraph.portal import runner as runner_mod

    runner_mod._mark_access_lost(project_id(), runner_mod.REASON_NO_ACCESS)
    scanner.hold.unlink()
    assert wait_run(w, held)["status"] == "ok"
    run = wait_run(w, queued)
    assert run["status"] == "failed" and "reconnect" in run["summary"]["error"]
    assert len(scanner.calls()) == 1


# ---------------------------------------------------------------------------
# The reconcile (plan section 4.7, acceptance criteria 4 and 6)
# ---------------------------------------------------------------------------


def reconcile(w: World) -> None:
    w.client.portal.call(w.state.runner.reconcile)


def test_the_reconcile_syncs_only_when_the_remote_moved(
    world: World, scanner: SimpleNamespace
) -> None:
    w = world
    imported(w)
    reconcile(w)  # never scanned: left to the first, explicit scan
    assert runs(w) == []
    wait_run(w, scan(w))

    reconcile(w)  # nothing moved
    assert len(runs(w)) == 1
    pushed = w.server.commit("acme/api", message="A push whose webhook was missed")
    reconcile(w)
    (run, _first) = runs(w)
    run = wait_run(w, run["id"])
    assert (run["status"], run["kind"], run["trigger"]) == ("ok", "sync", "reconcile")
    # A system run that spends nothing writes no usage (M2f-2 plan section 4.5).
    assert run["summary"]["usage"]["calls"] == 0
    assert w.state.usage_writer.flush()
    with portal_db.get_session() as session:
        assert session.exec(select(UsageEvent)).first() is None
    row = project_row(ORG, "api")
    assert row is not None and row.last_scanned_head == pushed
    reconcile(w)
    assert len(runs(w)) == 2


def test_the_reconcile_marks_a_refused_mint_and_clears_it_once_access_returns(
    world: World, scanner: SimpleNamespace, audit_log: pytest.LogCaptureFixture
) -> None:
    w = world
    imported(w)
    wait_run(w, scan(w))
    w.fake.uninstall(INSTALLATION)
    w.server.commit("acme/api")
    reconcile(w)
    assert details(w)["access_lost_reason"] == "no_access"
    assert len(runs(w)) == 1

    reconcile(w)  # still uninstalled: still lost, nothing queued
    assert details(w)["access_lost"] is True and len(runs(w)) == 1

    w.fake.add_installation(INSTALLATION, "acme", account_type="Organization")
    w.fake.repos[API_REPO].installation = INSTALLATION
    reconcile(w)  # one mint: access is back, without a webhook
    assert details(w)["access_lost"] is False
    assert [e["event"] for e in events(audit_log)].count("project_access_restored") == 1
    assert len(runs(w)) == 1
    reconcile(w)  # and the missed push is synced on the next pass
    assert wait_run(w, runs(w)[0]["id"])["trigger"] == "reconcile"


def test_the_reconcile_runs_at_start_on_its_own_loop_and_survives_an_exception(
    app_env: object,
    production_env: SimpleNamespace,
    scanner: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A restart that missed a push syncs it (acceptance criterion 4)."""
    with prod_portal() as client:
        w = World(client, app_env, production_env)  # type: ignore[arg-type]
        claim_instance(client)
        client.cookies.clear()
        assert github_sign_in(client, "ben").status_code == 200
        assert create_org(client, "acme", "Acme").status_code == 201
        imported(w)
        wait_run(w, scan(w))
    pushed = w.server.commit("acme/api", message="Pushed while the portal was down")

    release, entered = threading.Event(), threading.Event()
    real = runner_mod._reconcile_targets
    passes: list[int] = []

    def targets():  # noqa: ANN202
        passes.append(1)
        if len(passes) == 1:
            entered.set()
            assert release.wait(20)
            raise RuntimeError("the first pass fails")
        return real()

    monkeypatch.setattr(runner_mod, "_reconcile_targets", targets)

    def reconcile_runs() -> list[str]:
        """The statuses of the reconcile runs."""
        with portal_db.get_session() as session:
            return list(
                session.exec(
                    select(ScanRun.status).where(ScanRun.trigger == "reconcile")
                ).all()
            )

    clock = FakeClock()
    app = create_portal_app(runner=ScanRunner(sleep=clock.sleep))
    with TestClient(app, base_url=PROD_BASE, headers=CLIENT_HEADER) as client:
        # The start did not wait for the (blocked) first pass.
        assert entered.wait(10)
        assert client.get(PROD_BASE + "/api/portal/state").status_code == 200
        release.set()
        wait_for(lambda: clock.requested == [3600])
        assert reconcile_runs() == []  # the failed pass queued nothing

        clock.advance()  # the loop survived: the next pass catches up
        wait_for(lambda: reconcile_runs() == ["ok"])
        assert len(passes) == 2
    row = project_row(ORG, "api")
    assert row is not None and row.last_scanned_head == pushed


def test_no_github_credential_lands_in_the_db_a_run_file_or_a_response(
    world: World, scanner: SimpleNamespace
) -> None:
    """Acceptance criterion 9, after an import, a scan and a token refresh.

    Nothing shaped like a GitHub token (``gh[opsur]_...``: the user's
    ``ghu_`` / ``ghr_``, the installation's ``ghs_``) is in any column of
    any portal table, in the run's log and events files, in the clone's git
    config or in the body of a route a member uses. The pattern-redacted
    ``ghs_***`` is not one.
    """
    w = world
    w.fake.installation_token_ttl = 4
    root = imported(w)
    w.state.runner.token_refresh_margin = 3
    seen = w.env.tmp / "tokens.txt"
    scanner.configure(
        token_check=f"{w.server.url}/acme/api.git/info/refs",
        token_wait=5,
        token_record=seen,
    )
    run_id = scan(w)
    run = wait_run(w, run_id)
    assert run["status"] == "ok", (run, run_log(w, run_id))
    first, second = seen.read_text().split()
    assert first != second  # the child outlived its first token

    with portal_db.get_session() as session:
        tables = session.exec(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'"
            )
        ).all()
        assert len(tables) > 10
        for (table,) in tables:
            rows = session.exec(text(f'SELECT * FROM "{table}"')).all()
            assert not TOKEN_SHAPE.search(repr(rows)), table
    run_files = sorted((w.env.data / "runs").iterdir())
    assert {p.suffix for p in run_files} >= {".log", ".jsonl"}, run_files
    for path in run_files:
        assert not TOKEN_SHAPE.search(path.read_text()), path
    assert not TOKEN_SHAPE.search((root / ".git" / "config").read_text())

    project = at(ORG) + "/api/projects/api"
    for url in (
        at(ORG) + "/api/portal/state",
        at(ORG) + "/api/portal/defaults",
        at(ORG) + "/api/projects",
        at(ORG) + "/api/github/installations",
        at(ORG) + f"/api/github/installations/{INSTALLATION}/repos",
        project,
        project + "/config",
        project + "/scans",
        project + f"/scans/{run_id}/log",
        project + f"/scans/{run_id}/events",
    ):
        response = w.client.get(url)
        assert response.status_code == 200, (url, response.text)
        assert not TOKEN_SHAPE.search(response.text), url


def test_the_production_log_is_path_free_and_starts_with_the_flags(
    world: World, scanner: SimpleNamespace
) -> None:
    """MODE-2: no data dir or clone path in the log; the argv line is the flags."""
    w = world
    root = imported(w)
    run_id = scan(w)
    run = wait_run(w, run_id)
    assert run["status"] == "ok", run
    text = run_log(w, run_id)
    assert str(w.env.data) not in text and str(root) not in text
    assert "$ whygraph scan --skip-analyze" in text
    assert "--managed-by-portal" not in text and "-m whygraph" not in text


def test_a_missing_clone_is_reported_without_its_path(
    world: World, scanner: SimpleNamespace
) -> None:
    w = world
    root = imported(w)
    shutil.rmtree(root / ".git")
    run_id = scan(w)
    run = wait_run(w, run_id)
    assert run["status"] == "failed", run
    assert str(w.env.data) not in json.dumps(run)
    assert "server copy is missing" in run["summary"]["error"]
    assert "server copy is missing" in run_log(w, run_id)
