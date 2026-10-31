"""The project-purpose card over REST (plan 41 section 5.4, tests section 10).

``GET`` / ``POST /api/projects/{slug}/purpose`` through a production portal
with four roles on one project (Ben the org owner, Cy an org admin, Dee a
member - a contributor on the project - and Eve a viewer by grant) and
through a local portal with its single user. The model is a stub
completion client (no network, no spend): every client the factory makes
answers a canned card, so the usage ledger, the run rows, the audit log
and the notes it produces are real.

Covered: the role and budget guards (zero model calls), the usage row's
shape, single-flight by project **id** (the same slug in another org does
not block), the portal-wide cap, removal during a generation, the run-row
lifecycle (``ok`` / ``failed`` / ``no_input`` / ``blocked`` write one;
``unchanged`` / ``409`` / guard refusals write none), staleness against the
producing run and its cache, the prune to 50, the audit fields (never a
body) and who may confirm. The linked refusal (``403 managed_on_platform``)
is in ``test_portal_linked_manage.py``'s ``REFUSED`` table; the route
inventories are in ``test_portal_app.py`` and ``test_portal_tenancy.py``.
"""

# ruff: noqa: F811 -- pytest fixtures (`api`, `team`, ...) are imported

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterator

import pytest
from sqlmodel import col, select

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
from test_portal_notes import api, call  # noqa: F401 -- fixture
from whygraph.core.context import use_project
from whygraph.db.engine import get_session as project_session
from whygraph.db.models.project_note import ProjectNote
from whygraph.db.models.project_purpose_run import ProjectPurposeRun
from whygraph.portal import db as portal_db
from whygraph.portal import purpose_routes
from whygraph.portal.models import Organization, Project, UsageEvent
from whygraph.portal.secrets import LLM_API_KEY, put_secret
from whygraph.services.llm import (
    CompletionRequest,
    CompletionResponse,
    LlmClient,
    LlmClientFactory,
    LlmError,
)

P = "/api/projects/api/purpose"
NOTES = "/api/projects/api/notes"

ANSWER = (
    "What it is: A billing service that settles invoices.\n"
    "Who it is for: Finance teams.\n"
    "Main parts: an API, a settlement worker and a ledger.\n"
    "Stack: Python, FastAPI, PostgreSQL"
)
README = (
    "# Billing\n\nBilling settles customer invoices every night and keeps a ledger "
    "of every payment. " * 12
)


# ---------------------------------------------------------------------------
# The stub model
# ---------------------------------------------------------------------------


class _PurposeClient(LlmClient):
    """Every client the factory makes: records the request, answers :data:`ANSWER`."""

    provider = "stub"
    calls: list[CompletionRequest] = []
    answer: str = ANSWER
    error: Exception | None = None
    during: Callable[[], None] | None = None

    def __init__(self, provider: str, model: str) -> None:
        super().__init__(model=model)
        self.provider = provider

    @classmethod
    def from_config(cls, config: Any, **overrides: Any) -> "_PurposeClient":
        raise NotImplementedError

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        type(self).calls.append(request)
        if type(self).during is not None:
            type(self).during()
        if type(self).error is not None:
            raise type(self).error
        return CompletionResponse(
            text=type(self).answer,
            model=self.model,
            provider=self.provider,
            input_tokens=6000,
            output_tokens=120,
            finish_reason="stop",
        )


@pytest.fixture
def model(monkeypatch: pytest.MonkeyPatch) -> type[_PurposeClient]:
    """Route every completion through :class:`_PurposeClient` (fresh per test)."""

    def make(self, provider: str, *, model: str | None = None, **kw: Any) -> LlmClient:
        return _PurposeClient(provider, model or "default-model")

    monkeypatch.setattr(LlmClientFactory, "make", make)
    monkeypatch.setattr(_PurposeClient, "calls", [])
    monkeypatch.setattr(_PurposeClient, "answer", ANSWER)
    monkeypatch.setattr(_PurposeClient, "error", None)
    monkeypatch.setattr(_PurposeClient, "during", None)
    # The single-flight set, the cap and the staleness cache are module
    # state: start every test with them empty.
    monkeypatch.setattr(purpose_routes, "_RUNNING", set())
    monkeypatch.setattr(purpose_routes, "_STALE", {})
    monkeypatch.setattr(
        purpose_routes,
        "_SLOTS",
        threading.BoundedSemaphore(purpose_routes.MAX_CONCURRENT),
    )
    return _PurposeClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def commit_file(root: Path, rel: str, text: str) -> None:
    """Write ``rel`` and commit it (the purpose reads ``HEAD``, never the tree)."""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    _git(root, "add", rel)
    _git(root, "-c", "commit.gpgsign=false", "commit", "-q", "-m", f"add {rel}")


def org_key(org_id: int, state: Any) -> None:
    """An org-level Anthropic key (the default ``[analyze]`` provider)."""
    with portal_db.get_session() as session:
        put_secret(
            session,
            kind=LLM_API_KEY,
            value="sk-test-key",
            provider="anthropic",
            org_id=org_id,
        )
    state.contexts.invalidate()


def runs(root: Path, slug: str = "api", org: str = "acme") -> list[ProjectPurposeRun]:
    with use_project(manual_ctx(root, slug, org)), project_session() as session:
        rows = session.exec(
            select(ProjectPurposeRun).order_by(col(ProjectPurposeRun.id))
        ).all()
        for row in rows:
            session.expunge(row)
        return list(rows)


def add_runs(root: Path, count: int, *, org: str = "acme", **values: Any) -> None:
    with use_project(manual_ctx(root, "api", org)), project_session() as session:
        for _ in range(count):
            row = {
                "requested_by": "someone",
                "started_at": "2026-10-31T00:00:00+00:00",
                "finished_at": "2026-10-31T00:00:01+00:00",
                "status": "failed",
                "error_code": "provider_error",
            }
            row.update(values)
            session.add(ProjectPurposeRun(**row))
        session.commit()


def purpose_notes_rows(root: Path, org: str = "acme") -> list[ProjectNote]:
    with use_project(manual_ctx(root, "api", org)), project_session() as session:
        rows = session.exec(
            select(ProjectNote)
            .where(ProjectNote.kind == "purpose")
            .order_by(col(ProjectNote.id))
        ).all()
        for row in rows:
            session.expunge(row)
        return list(rows)


def ledger() -> list[UsageEvent]:
    with portal_db.get_session() as session:
        rows = session.exec(select(UsageEvent).order_by(UsageEvent.id)).all()
        for row in rows:
            session.expunge(row)
        return list(rows)


def audits(log: pytest.LogCaptureFixture, event: str) -> list[dict]:
    return [e for e in events(log) if e["event"] == event]


@pytest.fixture
def acme(api: SimpleNamespace, model: type[_PurposeClient]) -> SimpleNamespace:
    """``api`` with a README committed and an org key: ready to generate."""
    t = api
    commit_file(t.root, "README.md", README)
    t.state = t.client.app.state.portal
    org_key(t.org_id, t.state)
    t.model = model
    return t


@pytest.fixture
def local(
    env: SimpleNamespace, model: type[_PurposeClient]
) -> Iterator[SimpleNamespace]:
    """A local portal with ``api``: a README, an org key, the single user."""
    root = make_repo(env.shared, "api")
    commit_file(root, "README.md", README)
    with portal_client() as client:
        _setup_project(client, root)
        state = client.app.state.portal
        with portal_db.get_session() as session:
            project = session.exec(select(Project).where(Project.slug == "api")).one()
            ids = (project.id, project.org_id)
        org_key(ids[1], state)
        yield SimpleNamespace(
            client=client,
            root=root,
            state=state,
            project_id=ids[0],
            org_id=ids[1],
            model=model,
        )


def generate(t: SimpleNamespace, who: str = "ben") -> Any:
    return call(t, who, "POST", P)


# ---------------------------------------------------------------------------
# Guards: role, budget, key
# ---------------------------------------------------------------------------


def test_a_viewer_and_a_contributor_cannot_generate(acme: SimpleNamespace) -> None:
    t = acme
    for who in ("eve", "dee"):  # a viewer by grant; a member is a contributor
        refused = generate(t, who)
        assert refused.status_code == 403, refused.text
        assert refused.json()["code"] == "forbidden"
        state = call(t, who, "GET", P)
        assert state.status_code == 200, state.text
        assert state.json()["can_generate"] is False
    assert call(t, "ben", "GET", P).json()["can_generate"] is True
    assert t.model.calls == [] and runs(t.root) == []


def test_the_estimate_hides_its_cost_without_project_usage(
    acme: SimpleNamespace,
) -> None:
    t = acme
    admin = call(t, "cy", "GET", P).json()["estimate"]
    assert admin["tokens"]["input"] == 8000 and admin["tokens"]["output"] == 400
    assert admin["missing_key"] is None and "cost_hidden" not in admin
    assert admin["model"]["provider"] == "anthropic"
    viewer = call(t, "eve", "GET", P).json()["estimate"]
    assert viewer["cost"] is None and viewer["tokens"] is None
    assert viewer["cost_hidden"] is True and viewer["model"] == admin["model"]


def test_no_key_is_409_no_llm_key_and_writes_nothing(
    api: SimpleNamespace, model: type[_PurposeClient]
) -> None:
    t = api
    commit_file(t.root, "README.md", README)
    assert call(t, "ben", "GET", P).json()["estimate"]["missing_key"] == "anthropic"
    refused = generate(t)
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "no_llm_key"
    assert "(anthropic)" in refused.json()["error"]
    assert model.calls == [] and runs(t.root) == []


@pytest.mark.parametrize("where", ["context", "sink"])
def test_a_blocked_budget_refuses_with_no_call_and_a_blocked_row(
    acme: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    t = acme
    if where == "context":  # the hard stop the bind sees
        monkeypatch.setattr(t.state.budgets, "blocked_scope", lambda *a, **k: "project")
    else:  # a budget exhausted after the bind (the bound sink knows)
        monkeypatch.setattr(purpose_routes, "usage_blocked", lambda: "project")
    refused = generate(t)
    assert refused.status_code == 403, refused.text
    assert refused.json()["code"] == "budget_exceeded"
    assert refused.json()["scope"] == "project"
    assert t.model.calls == []
    (row,) = runs(t.root)
    assert (row.status, row.error_code) == ("blocked", "budget_exceeded")
    assert row.requested_by == t.uids["ben"] and row.input_hash is None
    assert ledger() == []


def test_the_throttle_counts_per_member(
    acme: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    t = acme
    monkeypatch.setattr(purpose_routes, "USER_LIMIT", 2)
    assert generate(t).status_code == 200
    assert generate(t).json()["unchanged"] is True
    refused = generate(t)
    assert refused.status_code == 429 and refused.json()["code"] == "throttled"
    assert len(t.model.calls) == 1


# ---------------------------------------------------------------------------
# A generation: the note, the run row, the usage row, the audit
# ---------------------------------------------------------------------------


def test_a_generation_makes_a_proposed_purpose_a_run_and_a_usage_row(
    acme: SimpleNamespace, audit_log: pytest.LogCaptureFixture
) -> None:
    t = acme
    made = generate(t)
    assert made.status_code == 200, made.text
    body = made.json()
    assert body["unchanged"] is False and body["running"] is False
    assert body["confirmed"] is None and body["stale"] is False
    note = body["proposed"]
    assert note["kind"] == "purpose" and note["status"] == "proposed"
    assert note["anchor"] == {"kind": "project"}
    assert (note["source"], note["origin"]) == ("analysis", "ui")
    assert note["proposed_by"] == t.uids["ben"] and note["proposed_by_name"] == "Ben"
    assert (
        note["body"].splitlines()[0]
        == "What it is: A billing service that settles invoices."
    )
    assert len(note["body"]) <= 600
    # One call, never retried.
    (request,) = t.model.calls
    assert request.max_retries == 0
    # The run row: paths read, the hash, the model served, tokens; no body.
    (row,) = runs(t.root)
    assert (row.status, row.note_id, row.error_code) == ("ok", note["id"], None)
    assert json.loads(row.sources) == ["README.md"]
    assert row.input_hash and row.input_bytes and row.input_bytes >= 500
    assert (row.provider, row.tokens_in, row.tokens_out) == ("anthropic", 6000, 120)
    assert row.model and row.requested_by == t.uids["ben"]
    last = body["last_run"]
    assert last["status"] == "ok" and last["sources"] == ["README.md"]
    assert last["tokens_in"] == 6000 and last["error_code"] is None
    # The usage ledger: one analyze call, charged to Ben from the Explorer.
    assert t.state.usage_writer.flush()
    (usage,) = ledger()
    assert (usage.task, usage.subject, usage.source) == (
        "analyze",
        "project-purpose",
        "explorer",
    )
    assert (usage.project_id, usage.user_id) == (t.project_id, t.ids["ben"])
    assert (usage.input_tokens, usage.output_tokens) == (6000, 120)
    assert (usage.provider, usage.model_requested) == ("anthropic", row.model)
    # The audit: counts and ids only.
    (event,) = audits(audit_log, "purpose_generated")
    assert event["run_id"] == row.id and event["status"] == "ok"
    assert (event["tokens_in"], event["tokens_out"]) == (6000, 120)
    assert event["model"] == row.model and event["project"] == "api"
    dumped = json.dumps(events(audit_log))
    assert "billing" not in dumped.lower() and "What it is" not in dumped


def test_unchanged_inputs_return_the_note_with_no_call_row_or_usage(
    acme: SimpleNamespace,
) -> None:
    t = acme
    first = generate(t).json()
    assert t.state.usage_writer.flush()
    again = generate(t)
    assert again.status_code == 200, again.text
    assert again.json()["unchanged"] is True
    assert again.json()["proposed"]["id"] == first["proposed"]["id"]
    assert len(t.model.calls) == 1 and len(runs(t.root)) == 1
    assert t.state.usage_writer.flush()
    assert len(ledger()) == 1


def test_too_little_input_is_422_with_no_call_and_a_no_input_row(
    api: SimpleNamespace, model: type[_PurposeClient]
) -> None:
    t = api  # sample.py only: no README, no manifest
    org_key(t.org_id, t.client.app.state.portal)
    refused = generate(t)
    assert refused.status_code == 422, refused.text
    assert refused.json()["code"] == "purpose_no_input"
    assert model.calls == []
    (row,) = runs(t.root)
    assert (row.status, row.error_code, row.note_id) == (
        "no_input",
        "purpose_no_input",
        None,
    )
    assert row.input_bytes < purpose_routes.MIN_INPUT_BYTES and row.input_hash
    assert purpose_notes_rows(t.root) == []


def test_an_invalid_answer_is_502_purpose_invalid_and_a_failed_row(
    acme: SimpleNamespace, audit_log: pytest.LogCaptureFixture
) -> None:
    t = acme
    t.model.answer = "What it is: see https://example.com\nStack: Python"
    refused = generate(t)
    assert refused.status_code == 502, refused.text
    assert refused.json()["code"] == "purpose_invalid"
    assert len(t.model.calls) == 1
    (row,) = runs(t.root)
    assert (row.status, row.error_code, row.note_id) == (
        "failed",
        "purpose_invalid",
        None,
    )
    assert purpose_notes_rows(t.root) == []
    # The call was made and metered, so it is audited (without the answer).
    (event,) = audits(audit_log, "purpose_generated")
    assert (event["run_id"], event["status"]) == (row.id, "failed")
    assert "example.com" not in json.dumps(events(audit_log))
    assert t.state.usage_writer.flush()
    assert [u.subject for u in ledger()] == ["project-purpose"]
    assert (
        call(t, "ben", "GET", P).json()["last_run"]["error_code"] == "purpose_invalid"
    )


def test_a_provider_failure_is_502_provider_error_and_a_failed_row(
    acme: SimpleNamespace,
) -> None:
    t = acme
    t.model.error = LlmError("upstream said no")
    refused = generate(t)
    assert refused.status_code == 502, refused.text
    assert refused.json()["code"] == "provider_error"
    assert "upstream said no" not in refused.text
    (row,) = runs(t.root)
    assert (row.status, row.error_code) == ("failed", "provider_error")
    assert row.provider == "anthropic" and row.tokens_in is None


# ---------------------------------------------------------------------------
# Single-flight, the cap, removal
# ---------------------------------------------------------------------------


def test_a_second_request_while_one_runs_is_409_and_removal_is_refused(
    acme: SimpleNamespace,
) -> None:
    t = acme
    seen: dict[str, Any] = {}

    def during() -> None:
        # Inside the model call: the same session asks again, reads the
        # card, and tries to remove the project.
        seen["again"] = t.client.post(at("acme") + P)
        seen["get"] = t.client.get(at("acme") + P)
        seen["remove"] = t.client.request(
            "DELETE",
            at("acme") + "/api/projects/api",
            json={"confirm_name": "Acme API"},
        )

    be(t, "ben")
    t.model.during = during
    made = t.client.post(at("acme") + P)
    assert made.status_code == 200, made.text
    assert seen["again"].status_code == 409
    assert seen["again"].json()["code"] == "purpose_in_progress"
    assert seen["get"].json()["running"] is True
    assert seen["remove"].status_code == 409, seen["remove"].text
    assert len(t.model.calls) == 1 and len(runs(t.root)) == 1
    with portal_db.get_session() as session:
        assert session.get(Project, t.project_id) is not None
    # Released afterwards: not running, and the removal claim is gone.
    assert call(t, "ben", "GET", P).json()["running"] is False
    assert not t.state.runner._requesting.get(t.project_id)


def test_single_flight_is_by_project_id_not_slug(acme: SimpleNamespace) -> None:
    t = acme
    be(t, "ben")
    assert create_org(t.client, "globex", "Globex").status_code == 201
    with portal_db.get_session() as session:
        beta_id = session.exec(
            select(Organization.id).where(Organization.slug == "globex")
        ).one()
    beta_root = make_repo(t.root.parent / "beta", "api")
    _insert_project(beta_id, "api", "Beta API", beta_root, t.ids["ben"])
    org_key(beta_id, t.state)
    purpose_routes._RUNNING.add(t.project_id)  # acme's api is generating
    busy = call(t, "ben", "POST", P)
    assert busy.status_code == 409 and busy.json()["code"] == "purpose_in_progress"
    # beta's api (same slug) passes the single-flight check: it reaches the
    # input check (no README there).
    be(t, "ben")
    other = t.client.post(at("globex") + P)
    assert other.status_code == 422, other.text
    assert other.json()["code"] == "purpose_no_input"
    assert runs(t.root) == []  # the refused acme request wrote nothing
    assert [r.status for r in runs(beta_root, org="globex")] == ["no_input"]


def test_the_portal_wide_cap_refuses_after_a_short_wait(
    acme: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    t = acme
    slots = threading.BoundedSemaphore(1)
    slots.acquire()  # another project's generation holds the only slot
    monkeypatch.setattr(purpose_routes, "_SLOTS", slots)
    monkeypatch.setattr(purpose_routes, "SLOT_WAIT_SEC", 0.01)
    refused = generate(t)
    assert refused.status_code == 409 and refused.json()["code"] == "busy"
    assert t.model.calls == [] and runs(t.root) == []
    assert purpose_routes.is_running(t.project_id) is False


# ---------------------------------------------------------------------------
# Staleness, revisions and the prune
# ---------------------------------------------------------------------------


def test_staleness_follows_the_producing_run_and_is_cached_per_head(
    acme: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    t = acme
    assert call(t, "ben", "GET", P).json()["stale"] is None  # no live note yet
    made = generate(t).json()
    nid = made["proposed"]["id"]
    gathered: list[int] = []
    real = purpose_routes.gather_inputs

    def counting(session, git):  # noqa: ANN001, ANN202
        gathered.append(1)
        return real(session, git)

    monkeypatch.setattr(purpose_routes, "gather_inputs", counting)
    monkeypatch.setattr(purpose_routes, "_STALE", {})
    assert call(t, "ben", "GET", P).json()["stale"] is False
    assert call(t, "ben", "GET", P).json()["stale"] is False
    assert len(gathered) == 1  # cached for (project, HEAD, run)
    # A later failed run with another hash does not decide staleness.
    add_runs(t.root, 1, input_hash="f" * 64)
    assert call(t, "ben", "GET", P).json()["stale"] is False
    # A commit that does not change the inputs moves HEAD: re-read, not stale.
    commit_file(t.root, "sample.py", "changed\n")
    assert call(t, "ben", "GET", P).json()["stale"] is False
    assert len(gathered) == 2
    # The README changes: stale, and a confirmed purpose gets a revision.
    _ok(call(t, "ben", "POST", f"{NOTES}/{nid}/confirm", json={}))
    commit_file(t.root, "README.md", README + "\nIt also sends dunning letters.\n")
    state = call(t, "ben", "GET", P).json()
    assert state["stale"] is True and state["confirmed"]["id"] == nid
    t.model.answer = ANSWER.replace("Finance teams.", "Finance and collections teams.")
    revised = generate(t).json()
    assert revised["unchanged"] is False
    assert revised["proposed"]["supersedes_id"] == nid
    assert revised["confirmed"]["id"] == nid
    # The live note is now the revision; its producing run read today's inputs.
    assert revised["stale"] is False
    assert len(t.model.calls) == 2


def test_an_identical_answer_marks_the_live_note_current(
    acme: SimpleNamespace,
) -> None:
    """A changed input with the same answer: no new note, the run re-anchors it."""
    t = acme
    nid = generate(t).json()["proposed"]["id"]
    commit_file(t.root, "README.md", README + "\nSame product, longer README.\n")
    assert call(t, "ben", "GET", P).json()["stale"] is True
    again = generate(t).json()
    assert again["unchanged"] is False and again["proposed"]["id"] == nid
    assert again["stale"] is False
    assert [r.note_id for r in runs(t.root)] == [nid, nid]
    assert len(purpose_notes_rows(t.root)) == 1


def test_a_new_generation_retires_the_older_proposal_and_audits_it(
    acme: SimpleNamespace, audit_log: pytest.LogCaptureFixture
) -> None:
    t = acme
    first = generate(t).json()["proposed"]["id"]
    commit_file(t.root, "README.md", README + "\nNow with refunds.\n")
    t.model.answer = ANSWER.replace("Finance teams.", "Finance and support teams.")
    second = generate(t).json()["proposed"]
    assert second["id"] != first and "supersedes_id" not in second
    statuses = {n.id: (n.status, n.retire_reason) for n in purpose_notes_rows(t.root)}
    assert statuses[first] == ("retired", "superseded")
    (retired,) = audits(audit_log, "note_retired")
    assert retired["note_id"] == first and retired["reason"] == "superseded"
    assert (retired["source"], retired["anchor_kind"]) == ("analysis", "project")
    assert retired["uid"] == t.uids["ben"]
    assert "Finance" not in json.dumps(events(audit_log))


def test_run_rows_are_pruned_to_fifty_keeping_live_producing_runs(
    acme: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    t = acme
    first = generate(t).json()["proposed"]["id"]
    add_runs(t.root, 55)
    commit_file(t.root, "README.md", README + "\nAnd a second region.\n")
    t.model.answer = ANSWER.replace("Finance teams.", "Finance and audit teams.")
    second = generate(t).json()["proposed"]["id"]
    kept = runs(t.root)
    # The first note was retired, so its run went with the oldest ones.
    assert len(kept) == purpose_routes.RUNS_KEPT
    assert all(r.note_id != first for r in kept)
    producing = next(r for r in kept if r.note_id == second)
    _ok(call(t, "ben", "POST", f"{NOTES}/{second}/confirm", json={}))
    add_runs(t.root, 60)
    # Any written row prunes: a blocked one here.
    monkeypatch.setattr(purpose_routes, "usage_blocked", lambda: "org")
    assert generate(t).status_code == 403
    kept = runs(t.root)
    assert len(kept) == purpose_routes.RUNS_KEPT + 1
    assert producing.id in {r.id for r in kept}
    assert call(t, "ben", "GET", P).json()["stale"] is False


# ---------------------------------------------------------------------------
# Who confirms (AC 8)
# ---------------------------------------------------------------------------


def _ok(response: Any, status: int = 200) -> dict:
    assert response.status_code == status, response.text
    return response.json()


def test_the_generating_admin_confirms_alone(acme: SimpleNamespace) -> None:
    t = acme
    nid = generate(t).json()["proposed"]["id"]
    confirmed = _ok(call(t, "ben", "POST", f"{NOTES}/{nid}/confirm", json={}))
    assert (
        confirmed["status"] == "confirmed"
        and confirmed["confirmed_by"] == t.uids["ben"]
    )
    state = call(t, "dee", "GET", P).json()
    assert state["confirmed"]["id"] == nid and state["proposed"] is None


def test_a_contributor_confirms_an_unedited_proposal_but_not_an_edit(
    acme: SimpleNamespace,
) -> None:
    t = acme
    nid = generate(t).json()["proposed"]["id"]
    edited = call(
        t,
        "dee",
        "POST",
        f"{NOTES}/{nid}/confirm",
        json={"body": ANSWER.replace("Finance teams.", "Everyone.")},
    )
    assert edited.status_code == 403, edited.text
    assert edited.json()["code"] == "note_needs_second_confirmer"
    confirmed = _ok(call(t, "dee", "POST", f"{NOTES}/{nid}/confirm", json={}))
    assert (
        confirmed["status"] == "confirmed"
        and confirmed["confirmed_by"] == t.uids["dee"]
    )


def test_local_mode_the_single_user_generates_and_confirms(
    local: SimpleNamespace,
) -> None:
    w = local
    made = w.client.post(P)
    assert made.status_code == 200, made.text
    note = made.json()["proposed"]
    assert note["kind"] == "purpose" and note["source"] == "analysis"
    confirmed = w.client.post(f"{NOTES}/{note['id']}/confirm", json={})
    assert confirmed.status_code == 200, confirmed.text
    state = w.client.get(P).json()
    assert state["confirmed"]["id"] == note["id"] and state["can_generate"] is True
    assert state["last_run"]["cost_usd"] is not None  # local: the user sees cost
    assert w.state.usage_writer.flush()
    (usage,) = ledger()
    assert (usage.task, usage.subject, usage.source) == (
        "analyze",
        "project-purpose",
        "explorer",
    )
    assert len(runs(w.root, org="local")) == 1
