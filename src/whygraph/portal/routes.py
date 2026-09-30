"""The portal's management endpoints: ``/api/portal/*`` and ``/api/projects/*``.

The per-project *data* routes (Explorer, Chat) are the unchanged
:mod:`whygraph.serve.routes` / :mod:`whygraph.serve.chat` routers, mounted
under ``/api/projects/{slug}`` by :func:`whygraph.portal.app.create_portal_app`.
This module holds what the portal adds on top (plan section 4.5.2).

Conventions:

* Handlers are sync ``def`` (run in the threadpool) unless they only
  await; each opens its own portal DB session.
* Errors are :class:`~whygraph.portal.deps.ApiError` -
  ``{"error": ..., "code"?: ...}``.
* Every route here except ``GET /api/portal/state`` and
  ``POST /api/portal/setup`` depends on
  :func:`~whygraph.portal.deps.current_user` (``409 setup required``).
* A handler that touches a project's DB or config runs under
  :func:`~whygraph.portal.deps.project_context` or
  :func:`~whygraph.portal.deps.project_db`.
* Every write to a config layer or secret invalidates the context cache
  (``invalidate(project_id)``, or ``invalidate(None)`` for global writes).
"""

from __future__ import annotations

import copy
import json
import os
import secrets as secrets_mod
import shutil
from dataclasses import asdict
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, func, select

from whygraph.agents import AGENTS as AGENT_TARGETS
from whygraph.agents import (
    UnknownAgentError,
    config_path_for,
    remove_entry,
    resolve_agent,
)
from whygraph.core.config import Config, ConfigError, normalize_v2
from whygraph.core.context import use_project
from whygraph.core.safe_paths import UnsafePathError, check_inside
from whygraph.db import get_session as project_session
from whygraph.db.engine import dispose_engine
from whygraph.db.models import RationaleCache
from whygraph.hooks import (
    HELPER_RELPATH,
    HooksError,
    HooksResult,
    resolve_hook_names,
    sync_hooks,
)
from whygraph.mcp.errors import WhyGraphError
from whygraph.mcp.resources import _repo_overview_resource
from whygraph.project_setup import (
    PORTAL_ENV,
    PORTAL_JSON,
    HttpMcp,
    InitializeResult,
    PortalMarker,
    initialize_project,
)
from whygraph.services.git import (
    GitError,
    InvalidRepoUrlError,
    Repository,
    git_env,
    parse_github_url,
    strip_userinfo,
)
from whygraph.services.github import GitHubError, RepoAccessError, check_repo_access

from .config_layers import ConfigPolicyError, find_secret_paths, load_layer, save_layer
from .context import build_project_context, resolve_root
from .db import get_session
from .deps import (
    ApiError,
    BoundProject,
    PortalState,
    bound_from,
    checked_db_paths,
    current_user,
    portal_state,
    principal_of,
    project_context,
    project_db,
    unsafe_path_error,
)
from .models import Project, ProjectAgent, ScanRun, Secret, User
from .paths import BACKUPS_DIR, check_project_paths
from .policy import (
    DEFAULTS_ALLOWLIST,
    PUT_ALLOWLIST,
    ImportPreview,
    filter_layer,
    preview_import,
)
from .projects import unique_slug
from .repos import (
    DISCOVERY_LIMIT,
    check_path,
    detect_existing,
    root_status,
)
from .estimate import scan_estimate as _scan_estimate
from .runner import RunNotFound, RunnerUnavailable, log_tail, stale_info
from .secrets import (
    GITHUB_TOKEN,
    LLM_API_KEY,
    LLM_KEY_PROVIDERS,
    delete_secret,
    put_secret,
    secret_status,
)
from .security import Principal

public_router = APIRouter(prefix="/api/portal")
"""``GET state`` and ``POST setup`` - the only routes usable before setup."""

portal_router = APIRouter(prefix="/api/portal", dependencies=[Depends(current_user)])
projects_router = APIRouter(
    prefix="/api/projects", dependencies=[Depends(current_user)]
)

KEYED_PROVIDERS: tuple[str, ...] = ("anthropic", "openai", "openrouter", "deepseek")
"""Providers that need an API key (``claude-cli`` and ``ollama`` are key-less)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _origins(state: PortalState):
    assert state.origins is not None  # the guard refuses requests before startup
    return state.origins


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SetupBody(_Strict):
    """``POST /api/portal/setup``."""

    display_name: str = Field(min_length=1, max_length=100)


class PathBody(_Strict):
    """``POST /api/portal/check-path``."""

    path: str


class SecretsBody(_Strict):
    """Write-only secrets: a value sets, ``null`` deletes, absent leaves alone."""

    llm: dict[str, str | None] = Field(default_factory=dict)
    github_token: str | None = None


class ConfigBody(_Strict):
    """``PUT .../config`` and ``PUT /api/portal/defaults``.

    ``config`` replaces the whole stored layer when present.
    """

    config: dict[str, Any] | None = None
    secrets: SecretsBody | None = None


class AddProjectBody(_Strict):
    """``POST /api/projects``."""

    source: Literal["local", "github"]
    path: str | None = None
    url: str | None = None
    token: str | None = None
    name: str | None = Field(default=None, max_length=200)


class PatchProjectBody(_Strict):
    """``PATCH /api/projects/{slug}`` - only the display name is renamable."""

    name: str = Field(min_length=1, max_length=200)


class DeleteProjectBody(_Strict):
    """``DELETE /api/projects/{slug}``."""

    strip_agent_entries: bool = False
    confirm_tracked: list[str] = Field(default_factory=list)
    confirm_name: str | None = None


class InitBody(_Strict):
    """``POST /api/projects/{slug}/init``.

    ``agent_actions`` keys are agent names or their config file paths
    (``.vscode/mcp.json``).
    """

    agents: list[str] = Field(default_factory=list)
    force: bool = False
    dry_run: bool = False
    confirm_tracked: list[str] = Field(default_factory=list)
    agent_actions: dict[str, Literal["migrate", "remove"]] = Field(default_factory=dict)


class ScanBody(_Strict):
    """``POST /api/projects/{slug}/scans``."""

    trigger: Literal["manual", "hook", "describe"] | None = None
    analyze: bool | None = None


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _user_dict(principal: Principal | None) -> dict | None:
    if principal is None:
        return None
    return {
        "uid": principal.uid,
        "display_name": principal.display_name,
        "role": principal.role,
    }


def _secrets_view(session: Session, project_id: int | None) -> dict:
    return {
        "llm": {
            tag: secret_status(
                session, kind=LLM_API_KEY, provider=tag, project_id=project_id
            )
            for tag in LLM_KEY_PROVIDERS
        },
        "github_token": secret_status(
            session, kind=GITHUB_TOKEN, project_id=project_id
        ),
    }


def _apply_secrets(
    session: Session, secrets: SecretsBody, project_id: int | None
) -> None:
    for provider, value in secrets.llm.items():
        if provider not in LLM_KEY_PROVIDERS:
            raise ApiError(422, f"unknown key provider {provider!r}")
        _put_or_delete(session, LLM_API_KEY, provider, value, project_id)
    if "github_token" in secrets.model_fields_set:
        _put_or_delete(session, GITHUB_TOKEN, None, secrets.github_token, project_id)


def _put_or_delete(
    session: Session,
    kind: str,
    provider: str | None,
    value: str | None,
    project_id: int | None,
) -> None:
    if value is None:
        delete_secret(session, kind=kind, provider=provider, project_id=project_id)
        return
    try:
        put_secret(
            session, kind=kind, value=value, provider=provider, project_id=project_id
        )
    except ValueError as exc:
        raise ApiError(422, str(exc)) from exc


def _checked_layer(raw: dict, spec, base: Path) -> dict:
    """Apply rule 4 (no secrets) and an allowlist to a user-supplied layer."""
    secrets_found = find_secret_paths(raw)
    if secrets_found:
        raise ApiError(
            422,
            "secrets are stored separately, not in config",
            keys=secrets_found,
        )
    normalized, _ = normalize_v2(raw, base)
    kept, dropped = filter_layer(normalized, spec)
    if dropped:
        raise ApiError(422, "these keys cannot be set here", keys=dropped)
    return kept


def _missing_key(
    config: Config, tasks: tuple[str, ...] = ("analyze", "chat")
) -> str | None:
    """The provider of the resolved model of ``tasks`` when it has no key."""
    for task in tasks:
        try:
            provider = config.model_for(task).provider
        except ConfigError:
            continue
        if provider not in KEYED_PROVIDERS:
            continue
        section = config.llm.section(provider)
        if not getattr(section, "api_key", None):
            return provider
    return None


def _project_stats(state: PortalState, project: BoundProject) -> dict | None:
    """Counts for the project home, or ``None`` until initialized (blocking).

    Also ``None`` when a DB path is a symlink (nothing is opened); the data
    routes answer ``409 unsafe_path`` for such a project.
    """
    try:
        check_project_paths(project.root)
    except UnsafePathError:
        return None
    if project.initialized_at is None or not project.db_path.is_file():
        return None
    try:
        state.migrations.ensure(project.ctx)
    except UnsafePathError:
        return None
    try:
        overview = _repo_overview_resource()
        with project_session() as session:
            cards = session.exec(select(func.count()).select_from(RationaleCache)).one()
    except WhyGraphError:
        return None
    coverage = overview["llm_description_coverage"]
    return {
        "commits": overview["counts"]["commits"],
        "described": coverage["described"],
        "described_pct": round(coverage["fraction"] * 100, 1),
        "pull_requests": overview["counts"]["pull_requests"],
        "issues": overview["counts"]["issues"],
        "rationale_cards": cards,
    }


def _active_run(session: Session, project_id: int) -> dict | None:
    run = session.exec(
        select(ScanRun)
        .where(ScanRun.project_id == project_id)
        .where(col(ScanRun.status).in_(("queued", "running")))
        .order_by(col(ScanRun.id).desc())
    ).first()
    if run is None:
        return None
    return {"id": run.id, "status": run.status, "trigger": run.trigger}


FINISHED_STATUSES: tuple[str, ...] = ("ok", "failed", "interrupted", "cancelled")
"""``scan_runs.status`` values of a run that has ended."""


def _last_scan_status(session: Session, project_id: int) -> str | None:
    return session.exec(
        select(ScanRun.status)
        .where(ScanRun.project_id == project_id)
        .where(col(ScanRun.status).in_(FINISHED_STATUSES))
        .order_by(col(ScanRun.id).desc())
    ).first()


def _summary(session: Session, project: Project, root: Path) -> dict:
    status = root_status(root)
    stale = (
        stale_info(root, project.last_scanned_head)
        if status == "ok" and project.initialized_at is not None
        else None
    )
    return {
        "slug": project.slug,
        "name": project.name,
        "source": project.source,
        "root": str(root),
        "remote_url": project.remote_url,
        "initialized": project.initialized_at is not None,
        "initialized_at": project.initialized_at,
        "last_scan_at": project.last_scan_at,
        "created_at": project.created_at,
        "root_status": status,
        "running_scan": _active_run(session, project.id),  # type: ignore[arg-type]
        # The newest ended run's status, for a "scan failed" badge.
        "last_scan_status": _last_scan_status(session, project.id),  # type: ignore[arg-type]
        # HEAD vs last_scanned_head (the runner's catch-up check, section 4.6).
        "stale": stale,
    }


def _details(state: PortalState, project: BoundProject) -> dict:
    """The ``GET /api/projects/{slug}`` body (blocking; context must be bound)."""
    with get_session() as session:
        row = session.get(Project, project.id)
        if row is None:
            raise ApiError(404, f"project {project.slug!r} not found")
        body = _summary(session, row, project.root)
        body["agents"] = sorted(
            a.agent
            for a in session.exec(
                select(ProjectAgent).where(ProjectAgent.project_id == project.id)
            ).all()
        )
    body["missing_key"] = _missing_key(project.ctx.config)
    body["mcp_url"] = f"{_origins(state).base_url}/mcp/{project.slug}"
    body["detected"] = _detected(project) if body["root_status"] == "ok" else None
    body["port_change"] = _port_change_for(state, project.slug)
    body["stats"] = (
        _project_stats(state, project) if body["root_status"] == "ok" else None
    )
    return body


def _port_change_for(state: PortalState, slug: str) -> dict | None:
    """This project's entry of the start-up port reconcile, or ``None``."""
    report = state.port_change or {}
    for item in report.get("projects", ()):
        if item["slug"] == slug:
            return item
    for item in report.get("unmounted", ()):
        if item["slug"] == slug:
            return {**item, "unmounted": True, "port": report["port"]}
    return None


def _detected(project: BoundProject) -> dict:
    """The add response's ``detected`` block, recomputed from the repo (blocking)."""
    if project.source == "local":
        return detect_existing(
            project.root, preview_import(project.root).custom_db_paths
        )
    return detect_existing(project.root)


def _hooks_dict(result: HooksResult | None) -> dict | None:
    if result is None:
        return None
    return {
        "installed": list(result.installed),
        "removed": list(result.removed),
        "actions": dict(result.actions),
    }


def _init_dict(result: InitializeResult) -> dict:
    return {
        "dry_run": result.dry_run,
        "gitignore_added": list(result.gitignore_added),
        "hooks": _hooks_dict(result.hooks),
        "hooks_error": result.hooks_error,
        "agent_files": [asdict(f) for f in result.agent_files],
        "asset_files": [asdict(f) for f in result.asset_files],
        "configured_agents": list(result.configured_agents),
        "needs_confirmation": [f.file for f in result.needs_confirmation],
        "refused": [f.file for f in result.refused],
        "marker_written": result.marker_written,
    }


def _unsafe_reason(root: Path, *paths: Path) -> str | None:
    """Why one of ``paths`` must not be touched (a symlink out of ``root``), or ``None``."""
    for path in paths:
        try:
            check_inside(root, path)
        except UnsafePathError as exc:
            return (
                f"{exc} (WhyGraph never follows a symbolic link out of the repository)"
            )
    return None


def _clone_dir_is_safe(root: Path, data_dir: Path) -> bool:
    """Whether ``root`` is a direct child of ``<data dir>/repos`` (rmtree guard)."""
    real = Path(os.path.realpath(root))
    return real.parent == Path(os.path.realpath(data_dir / "repos"))


def _probe(owner: str, name: str, token: str | None) -> None:
    try:
        check_repo_access(owner, name, token)
    except RepoAccessError as exc:
        raise ApiError(400, str(exc), code=exc.code) from exc
    except GitHubError as exc:
        message = str(exc).replace(token, "***") if token else str(exc)
        raise ApiError(502, message, code="github_error") from exc


# ---------------------------------------------------------------------------
# /api/portal
# ---------------------------------------------------------------------------


def _package_version() -> str | None:
    """The installed ``whygraph`` version (what ``whygraph version`` prints)."""
    try:
        return _pkg_version("whygraph")
    except PackageNotFoundError:
        return None


@public_router.get("/state")
def get_state(request: Request) -> dict:
    """Portal status for the first screen: mode, setup, port, shared folders, version.

    In degraded mode (the portal DB failed to migrate) the body is just
    ``{"error": ...}``, so the UI can show the failure.
    """
    state = portal_state(request)
    if state.degraded:
        return {"error": state.degraded}
    principal = request.scope.get("state", {}).get("principal")
    return {
        "mode": state.mode,
        "setup_complete": principal is not None,
        "user": _user_dict(principal),
        "port": state.port,
        "shared_folders": [str(f) for f in state.shared_folders],
        "version": _package_version(),
        # What the start-up port reconcile did (markers / agent files), or null.
        "port_change": state.port_change,
    }


@public_router.post("/setup", status_code=201)
def post_setup(body: SetupBody, request: Request) -> dict:
    """First run in local mode: create the single user (``409`` once done).

    The ``settings`` row (the mode) was already written by the lifespan at
    first start; setup only creates the user.
    """
    state = portal_state(request)
    name = body.display_name.strip()
    if not name:
        raise ApiError(422, "display_name must not be blank")
    with state.setup_lock:
        with get_session() as session:
            if session.exec(select(User.id)).first() is not None:
                raise ApiError(409, "setup already complete")
            user = User(display_name=name)
            session.add(user)
            session.flush()
            principal = principal_of(user)
        state.set_principal(principal)
    return {"setup_complete": True, "user": _user_dict(principal)}


@portal_router.get("/repos")
def get_repos(request: Request, q: str = "") -> dict:
    """Git repositories discovered under the shared folders (cached 60 s)."""
    state = portal_state(request)
    found = state.discovery.get(state.shared_folders)
    with get_session() as session:
        registered = set(
            session.exec(select(Project.root).where(Project.source == "local")).all()
        )
    needle = q.strip().lower()
    return {
        "repos": [
            {"path": str(p), "name": p.name, "registered": str(p) in registered}
            for p in found
            if needle in str(p).lower()
        ],
        "truncated": len(found) >= DISCOVERY_LIMIT,
    }


@portal_router.post("/check-path")
def post_check_path(body: PathBody, request: Request) -> dict:
    """Whether a typed path can be added, and the fix command when it cannot."""
    state = portal_state(request)
    try:
        return check_path(body.path, state.shared_folders, state.data_dir)
    except ValueError as exc:
        raise ApiError(422, str(exc)) from exc


def _defaults_view(session: Session) -> dict:
    any_key = session.exec(select(Secret.id).where(Secret.kind == LLM_API_KEY)).first()
    return {
        "config": load_layer(session, None),
        "secrets": _secrets_view(session, None),
        "no_provider_key": any_key is None,
    }


@portal_router.get("/defaults")
def get_defaults() -> dict:
    """The global default config (rule 6) and the global secrets' status."""
    with get_session() as session:
        return _defaults_view(session)


@portal_router.put("/defaults")
def put_defaults(body: ConfigBody, request: Request) -> dict:
    """Replace the global defaults and / or set global secrets.

    Only ``[llm]`` (with connection keys), ``[analyze]``, ``[rationale]``
    and ``[chat]`` are accepted (rule 6); secrets inside ``config`` are a
    ``422`` (rule 4). A changed provider endpoint clears the global key
    and the project keys of every project that inherits the endpoint
    (rule 3); ``cleared_project_keys`` lists them as ``{slug, provider}``.
    Invalidates every project's context.
    """
    state = portal_state(request)
    cleared: list[dict] = []
    with get_session() as session:
        if body.config is not None:
            kept = _checked_layer(body.config, DEFAULTS_ALLOWLIST, state.data_dir)
            try:
                Config.from_dict(kept, state.data_dir)
                cleared_ids = save_layer(session, None, kept)
            except (ConfigError, ConfigPolicyError) as exc:
                raise ApiError(422, str(exc)) from exc
            for project_id, provider in cleared_ids:
                row = session.get(Project, project_id)
                if row is not None:
                    cleared.append({"slug": row.slug, "provider": provider})
        if body.secrets is not None:
            _apply_secrets(session, body.secrets, None)
    state.contexts.invalidate(None)
    with get_session() as session:
        view = _defaults_view(session)
    view["cleared_project_keys"] = cleared
    return view


# ---------------------------------------------------------------------------
# /api/projects
# ---------------------------------------------------------------------------


@projects_router.get("")
def list_projects() -> dict:
    """Every registered project with its status."""
    with get_session() as session:
        rows = session.exec(select(Project).order_by(Project.name)).all()
        return {"projects": [_summary(session, p, resolve_root(p)) for p in rows]}


@projects_router.post("", status_code=201)
def add_project(
    body: AddProjectBody,
    request: Request,
    principal: Principal = Depends(current_user),
) -> dict:
    """Register a local repo (under a shared folder) or clone a GitHub repo.

    Nothing inside a local repo is written. The response carries the
    project, the ``detected`` 1.x state and the import report. Error codes:
    ``bad_token``, ``no_access``, ``not_found``, ``not_shared``,
    ``not_git``, ``duplicate`` (plus ``invalid_url``, ``not_linked``,
    ``protected``, ``clone_failed``, ``github_error``).
    """
    state = portal_state(request)
    if body.source == "local":
        project_id, detected, preview = _add_local(state, body, principal)
    else:
        project_id, detected, preview = _add_github(state, body, principal)
    ctx = state.contexts.get(project_id)
    with get_session() as session:
        row = session.get(Project, project_id)
        assert row is not None
        session.expunge(row)
    project = bound_from(row, ctx)
    with use_project(ctx):
        details = _details(state, project)
    return {"project": details, "detected": detected, "import": preview.report()}


def _insert_project(
    session: Session,
    *,
    slug: str,
    name: str,
    source: str,
    root: str,
    remote_url: str | None,
    principal: Principal,
) -> int:
    project = Project(
        slug=slug,
        name=name,
        source=source,
        root=root,
        remote_url=remote_url,
        created_by=principal.user_id,
    )
    session.add(project)
    session.flush()
    assert project.id is not None
    return project.id


def _add_local(
    state: PortalState, body: AddProjectBody, principal: Principal
) -> tuple[int, dict, ImportPreview]:
    if not body.path:
        raise ApiError(422, "path is required for a local project")
    try:
        check = check_path(body.path, state.shared_folders, state.data_dir)
    except ValueError as exc:
        raise ApiError(422, str(exc)) from exc
    if check["protected"]:
        raise ApiError(
            400, "the path overlaps the portal data directory", code="protected"
        )
    if not check["shared"]:
        raise ApiError(
            400,
            "the path is not under a shared folder",
            code="not_shared",
            folder_suggestion=check["folder_suggestion"],
            command=check["command"],
        )
    if not check["is_git"]:
        raise ApiError(400, "the path is not a git repository", code="not_git")

    root = Path(check["path"])
    link = check["github"]
    token = (body.token or "").strip() or None
    if token is not None:
        if link is None:
            raise ApiError(
                400,
                "a token was given but the repository has no GitHub origin",
                code="not_linked",
            )
        owner, repo = link["slug"].split("/", 1)
        _probe(owner, repo, token)

    preview = preview_import(root)
    layer = copy.deepcopy(preview.layer)
    if link is not None:
        layer.setdefault("scan", {}).setdefault("forge", "auto")
    detected = detect_existing(root, preview.custom_db_paths)
    name = (body.name or "").strip() or root.name
    try:
        with get_session() as session:
            if session.exec(
                select(Project.id).where(Project.root == str(root))
            ).first():
                raise ApiError(
                    409, "this repository is already registered", code="duplicate"
                )
            project_id = _insert_project(
                session,
                slug=unique_slug(session, name),
                name=name,
                source="local",
                root=str(root),
                remote_url=strip_userinfo(link["remote_url"]) if link else None,
                principal=principal,
            )
            if layer:
                save_layer(session, project_id, layer)
            for tag, key in preview.llm_keys.items():
                put_secret(
                    session,
                    kind=LLM_API_KEY,
                    value=key,
                    provider=tag,
                    project_id=project_id,
                )
            github_token = token or preview.github_token
            if github_token:
                put_secret(
                    session,
                    kind=GITHUB_TOKEN,
                    value=github_token,
                    project_id=project_id,
                )
    except IntegrityError as exc:
        raise ApiError(
            409, "this repository is already registered", code="duplicate"
        ) from exc
    state.contexts.invalidate(project_id)
    return project_id, detected, preview


def _add_github(
    state: PortalState, body: AddProjectBody, principal: Principal
) -> tuple[int, dict, ImportPreview]:
    url = (body.url or "").strip()
    token = (body.token or "").strip() or None
    try:
        owner, repo = parse_github_url(url)
    except InvalidRepoUrlError as exc:
        raise ApiError(400, str(exc), code="invalid_url") from exc
    _probe(owner, repo, token)

    canonical = f"https://github.com/{owner}/{repo}"
    name = (body.name or "").strip() or repo
    # One GitHub add at a time: the duplicate check, the slug, the clone and
    # the insert happen under the lock, so a double-submit of one URL gets
    # "duplicate" instead of racing the other request's clone.
    with state.github_add_lock:
        return _clone_and_insert(state, canonical, name, token, principal)


def _clone_and_insert(
    state: PortalState,
    canonical: str,
    name: str,
    token: str | None,
    principal: Principal,
) -> tuple[int, dict, ImportPreview]:
    """Clone into a private temp dir, move it into place, insert the row."""
    with get_session() as session:
        for remote in session.exec(
            select(Project.remote_url).where(Project.source == "github")
        ).all():
            if remote and remote.removesuffix(".git").lower() == canonical.lower():
                raise ApiError(
                    409, "this repository is already registered", code="duplicate"
                )
        slug = unique_slug(session, name)

    repos_dir = state.data_dir / "repos"
    repos_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    dest = repos_dir / slug
    if dest.exists() or dest.is_symlink():
        raise ApiError(409, f"{dest} already exists", code="duplicate")
    # A dot-name is never a slug, so the temp dir cannot collide with a
    # project; only this request knows it, so only this request removes it.
    tmp = repos_dir / f".clone-{slug}-{secrets_mod.token_hex(6)}"
    try:
        Repository.clone(canonical, tmp, env=git_env(token))
    except GitError as exc:
        _remove_clone(tmp, state.data_dir)
        message = str(exc).replace(token, "***") if token else str(exc)
        raise ApiError(502, message, code="clone_failed") from exc
    try:
        os.rename(tmp, dest)  # fails on a non-empty dest; never merges into it
    except OSError as exc:
        _remove_clone(tmp, state.data_dir)
        raise ApiError(409, f"{dest} already exists", code="duplicate") from exc

    try:
        with get_session() as session:
            project_id = _insert_project(
                session,
                slug=slug,
                name=name,
                source="github",
                root=f"repos/{slug}",
                remote_url=canonical,
                principal=principal,
            )
            save_layer(session, project_id, {"scan": {"forge": "auto"}})
            if token:
                put_secret(
                    session, kind=GITHUB_TOKEN, value=token, project_id=project_id
                )
    except IntegrityError as exc:
        _remove_clone(dest, state.data_dir)  # the checkout this request moved in
        raise ApiError(
            409, "this repository is already registered", code="duplicate"
        ) from exc
    state.contexts.invalidate(project_id)
    # A fresh clone carries no WhyGraph state, and its whygraph.toml is never
    # imported (rule 5) - but report what is there, like a local add.
    return project_id, detect_existing(dest), ImportPreview()


def _remove_clone(path: Path, data_dir: Path) -> bool:
    if path.exists() and _clone_dir_is_safe(path, data_dir):
        shutil.rmtree(path)
        return True
    return False


@projects_router.get("/{slug}")
def get_project(
    request: Request, project: BoundProject = Depends(project_context)
) -> dict:
    """Project details; ``stats`` is ``null`` until initialized."""
    return _details(portal_state(request), project)


@projects_router.patch("/{slug}")
def patch_project(
    body: PatchProjectBody,
    request: Request,
    project: BoundProject = Depends(project_context),
) -> dict:
    """Rename the project's display ``name`` (the slug is immutable)."""
    name = body.name.strip()
    if not name:
        raise ApiError(422, "name must not be blank")
    with get_session() as session:
        row = session.get(Project, project.id)
        if row is None:
            raise ApiError(404, f"project {project.slug!r} not found")
        row.name = name
        session.add(row)
    return _details(portal_state(request), project)


@projects_router.delete("/{slug}")
def delete_project(
    request: Request,
    project: BoundProject = Depends(project_context),
    body: DeleteProjectBody | None = Body(default=None),
) -> dict:
    """Unregister a project (plan section 4.5.5).

    Refused while a scan or sync is queued or running. Strips the managed
    hooks, optionally the ``whygraph`` agent entries (tracked files only
    with ``confirm_tracked``; all-or-nothing), and the portal markers;
    never deletes a local repo or the rest of its ``.whygraph/``. A GitHub
    clone's checkout is deleted only when ``confirm_name`` equals the
    project name and the checkout sits directly under
    ``<data dir>/repos``.
    """
    state = portal_state(request)
    body = body or DeleteProjectBody()
    if state.runner.is_busy(project.id):
        raise ApiError(409, "a scan or sync is queued or running for this project")
    if project.source == "github" and body.confirm_name != project.name:
        raise ApiError(
            409,
            f"type the project name to delete the checkout at {project.root}",
            code="confirm_name",
            folder=str(project.root),
        )

    with get_session() as session:
        agents = [
            a.agent
            for a in session.exec(
                select(ProjectAgent).where(ProjectAgent.project_id == project.id)
            ).all()
        ]
    root_ok = project.root.is_dir()
    warnings: list[str] = []
    agent_files: list[dict] = []
    if body.strip_agent_entries and root_ok:
        targets = []
        for target in (AGENT_TARGETS[a] for a in agents if a in AGENT_TARGETS):
            unsafe = _unsafe_reason(
                project.root,
                config_path_for(target, project.root),
                project.root / BACKUPS_DIR,
            )
            if unsafe:
                warnings.append(f"left the {target.name} entry: {unsafe}")
            else:
                targets.append(target)
        preview = [
            remove_entry(
                t, project.root, confirm_tracked=body.confirm_tracked, dry_run=True
            )
            for t in targets
        ]
        pending = [asdict(f) for f in preview if f.status == "needs_confirmation"]
        if pending:
            raise ApiError(
                409,
                "confirm the git-tracked agent files first",
                code="needs_confirmation",
                agent_files=pending,
            )
        agent_files = [
            asdict(remove_entry(t, project.root, confirm_tracked=body.confirm_tracked))
            for t in targets
        ]

    dispose_engine(project.db_path)
    state.migrations.forget(project.db_path)
    state.contexts.invalidate(project.id)

    hooks = None
    if root_ok:
        unsafe = _unsafe_reason(project.root, project.root / HELPER_RELPATH)
        if unsafe:
            warnings.append(f"left the git hooks: {unsafe}")
        else:
            try:
                hooks = _hooks_dict(sync_hooks(project.root, []))
            except HooksError as exc:
                warnings.append(str(exc))
        for marker in (PORTAL_JSON, PORTAL_ENV):
            unsafe = _unsafe_reason(project.root, project.root / marker)
            if unsafe:
                warnings.append(f"left {marker}: {unsafe}")
            else:
                (project.root / marker).unlink(missing_ok=True)
    else:
        warnings.append(f"{project.root} is not mounted; hooks and markers were left")

    with get_session() as session:
        row = session.get(Project, project.id)
        if row is not None:
            session.delete(row)

    checkout_deleted = False
    if project.source == "github":
        checkout_deleted = _remove_clone(project.root, state.data_dir)
        if not checkout_deleted and project.root.exists():
            warnings.append(f"refused to delete {project.root}: not a portal clone")
    return {
        "removed": project.slug,
        "hooks": hooks,
        "agent_files": agent_files,
        "checkout_deleted": checkout_deleted,
        "warnings": warnings,
    }


@projects_router.get("/{slug}/config")
def get_project_config(project: BoundProject = Depends(project_context)) -> dict:
    """The stored config layer, the secrets' status and the import report.

    The import report is recomputed from the repo's ``whygraph.toml``
    (which the portal never modifies), so dropped keys and custom DB
    paths keep showing while the file still has them.
    """
    with get_session() as session:
        body = {
            "config": load_layer(session, project.id),
            "secrets": _secrets_view(session, project.id),
        }
    preview = (
        preview_import(project.root) if project.source == "local" else ImportPreview()
    )
    body["import"] = preview.report()
    return body


@projects_router.put("/{slug}/config")
def put_project_config(
    body: ConfigBody,
    request: Request,
    project: BoundProject = Depends(project_context),
) -> dict:
    """Replace the project's config layer and / or set its secrets.

    Rule 1b allowlist; ``api_key`` / ``token`` in ``config`` is a ``422``
    that never echoes the value; a changed ``base_url`` / ``host`` clears
    that project's key for the provider (rule 3). A change to
    ``[scan].hooks`` on an initialized local project re-runs
    :func:`whygraph.hooks.sync_hooks`.
    """
    state = portal_state(request)
    old_hooks = project.ctx.config.scan_hooks
    with get_session() as session:
        row = session.get(Project, project.id)
        if row is None:
            raise ApiError(404, f"project {project.slug!r} not found")
        if body.config is not None:
            kept = _checked_layer(body.config, PUT_ALLOWLIST, project.root)
            try:
                save_layer(session, project.id, kept)
            except ConfigPolicyError as exc:
                raise ApiError(422, str(exc)) from exc
        if body.secrets is not None:
            _apply_secrets(session, body.secrets, project.id)
        try:
            new_ctx = build_project_context(session, row)
        except ConfigError as exc:
            raise ApiError(422, str(exc)) from exc
    state.contexts.invalidate(project.id)

    hooks: dict | None = None
    hooks_error: str | None = None
    new_hooks = new_ctx.config.scan_hooks
    if (
        new_hooks != old_hooks
        and project.initialized_at is not None
        and project.source == "local"
        and project.root.is_dir()
    ):
        unsafe = _unsafe_reason(project.root, project.root / HELPER_RELPATH)
        try:
            if unsafe:
                raise HooksError(f"hooks not changed: {unsafe}")
            hooks = _hooks_dict(sync_hooks(project.root, resolve_hook_names(new_hooks)))
        except HooksError as exc:
            hooks_error = str(exc)

    with get_session() as session:
        view = {
            "config": load_layer(session, project.id),
            "secrets": _secrets_view(session, project.id),
        }
    view["hooks"] = hooks
    view["hooks_error"] = hooks_error
    return view


def _agent_actions(raw: dict[str, str]) -> dict[str, str]:
    """Map ``agent_actions`` keys (agent names or config files) to agent names."""
    by_file = {
        "/".join(t.relative_path): name
        for name, t in AGENT_TARGETS.items()
        if t.scope == "project"
    }
    out: dict[str, str] = {}
    for key, action in raw.items():
        name = by_file.get(key.strip("/"))
        if name is None:
            name = resolve_agent(key).name
        out[name] = action
    return out


@projects_router.post("/{slug}/init")
def init_project(
    body: InitBody,
    request: Request,
    project: BoundProject = Depends(project_context),
) -> dict:
    """Initialize the repo: gitignore, hooks, agent MCP entries + assets, markers.

    Runs :func:`whygraph.project_setup.initialize_project` with the
    portal's :class:`~whygraph.agents.HttpMcp` and
    :class:`~whygraph.project_setup.PortalMarker`; hooks come from
    ``[scan].hooks`` (none for a GitHub clone). The project DB is created
    or migrated (with a backup) first, under the project context.
    ``initialized_at`` is set only once the markers are written, i.e. no
    agent file awaits confirmation. ``dry_run`` returns the per-file plan
    and writes nothing; ``force`` is "Update agent files".
    """
    state = portal_state(request)
    origins = _origins(state)
    if root_status(project.root) != "ok":
        raise ApiError(409, f"{project.root} is not available", code="root_missing")
    try:
        for name in body.agents:
            resolve_agent(name)
        actions = _agent_actions(body.agent_actions)
    except UnknownAgentError as exc:
        raise ApiError(422, str(exc)) from exc
    hooks: bool | tuple[str, ...] = (
        False if project.source == "github" else project.ctx.config.scan_hooks
    )
    checked_db_paths(project)

    try:
        if not body.dry_run:
            state.migrations.ensure(project.ctx)
        result = initialize_project(
            project.root,
            agents=body.agents,
            hooks=hooks,
            mcp=HttpMcp(slug=project.slug, port=origins.port, host=origins.agent_host),
            marker=PortalMarker(slug=project.slug, port=origins.port),
            force=body.force,
            confirm_tracked=body.confirm_tracked,
            agent_actions=actions,
            dry_run=body.dry_run,
            contained=True,
        )
    except UnsafePathError as exc:
        raise unsafe_path_error(exc) from exc
    except OSError as exc:
        raise ApiError(500, f"initialize failed: {exc}") from exc

    initialized = project.initialized_at is not None
    if not result.dry_run:
        removed = {
            f.agent
            for f in result.agent_files
            if f.agent
            and actions.get(f.agent) == "remove"
            and f.status in ("write", "overwrite", "skip")
        }
        with get_session() as session:
            existing = {
                a.agent: a
                for a in session.exec(
                    select(ProjectAgent).where(ProjectAgent.project_id == project.id)
                ).all()
            }
            for agent in result.configured_agents:
                row = existing.get(agent)
                if row is None:
                    session.add(ProjectAgent(project_id=project.id, agent=agent))
                else:
                    row.configured_at = _now()
                    session.add(row)
            for agent in removed - set(result.configured_agents):
                if agent in existing:
                    session.delete(existing[agent])
            if result.marker_written:
                row = session.get(Project, project.id)
                if row is not None and row.initialized_at is None:
                    row.initialized_at = _now()
                    session.add(row)
                initialized = True

    response = _init_dict(result)
    response["initialized"] = initialized
    preview = (
        preview_import(project.root) if project.source == "local" else ImportPreview()
    )
    response["custom_db_paths"] = [p for p in preview.custom_db_paths if p["exists"]]
    return response


# ---- scans (portal/runner.py) ---------------------------------------------


@projects_router.post("/{slug}/scans", status_code=202)
async def post_scan(
    request: Request,
    project: BoundProject = Depends(project_db),
    principal: Principal = Depends(current_user),
    body: ScanBody | None = Body(default=None),
) -> dict:
    """Queue (or coalesce into) a scan; returns the pending run's id."""
    body = body or ScanBody()
    try:
        run_id = await portal_state(request).runner.request_scan(
            project, trigger=body.trigger, analyze=body.analyze, principal=principal
        )
    except RunnerUnavailable as exc:
        raise ApiError(501, str(exc)) from exc
    return {"run_id": run_id}


@projects_router.get("/{slug}/scans")
def list_scans(project: BoundProject = Depends(project_db)) -> dict:
    """The project's recent scan / sync runs, newest first."""
    with get_session() as session:
        runs = session.exec(
            select(ScanRun)
            .where(ScanRun.project_id == project.id)
            .order_by(col(ScanRun.id).desc())
            .limit(50)
        ).all()
        return {
            "runs": [
                {
                    "id": r.id,
                    "kind": r.kind,
                    "trigger": r.trigger,
                    "analyze": r.analyze,
                    "status": r.status,
                    "requested_by": r.requested_by,
                    "started_at": r.started_at,
                    "finished_at": r.finished_at,
                    "summary": json.loads(r.summary) if r.summary else None,
                }
                for r in runs
            ]
        }


@projects_router.get("/{slug}/scans/{run_id}/events")
async def scan_events(
    run_id: int, request: Request, project: BoundProject = Depends(project_db)
) -> Any:
    """SSE: replay a run's events file, then follow it until the run ends.

    Each frame's ``id:`` is the byte offset after it; a reconnect sends it
    back as ``Last-Event-ID`` and the replay resumes there.
    """
    state = portal_state(request)
    assert state.shutdown_event is not None
    last = request.headers.get("last-event-id", "").strip()
    offset = int(last) if last.isdigit() else 0
    try:
        return await state.runner.events(
            project, run_id, shutdown=state.shutdown_event, offset=offset
        )
    except RunNotFound as exc:
        raise ApiError(404, f"run {run_id} not found") from exc
    except RunnerUnavailable as exc:
        raise ApiError(501, str(exc)) from exc


@projects_router.get("/{slug}/scans/{run_id}/log")
def scan_log(run_id: int, project: BoundProject = Depends(project_db)) -> dict:
    """The tail of a run's log: ``{run_id, text, size, truncated}``.

    At most the last 64 KiB (:data:`~whygraph.portal.runner.LOG_TAIL_BYTES`),
    starting at a line boundary when cut; already redacted at write time.
    ``404`` for an unknown run or another project's.
    """
    try:
        return log_tail(project.id, run_id)
    except RunNotFound as exc:
        raise ApiError(404, f"run {run_id} not found") from exc


@projects_router.post("/{slug}/sync", status_code=202)
async def post_sync(
    request: Request,
    project: BoundProject = Depends(project_db),
    principal: Principal = Depends(current_user),
) -> dict:
    """GitHub clones: fetch + fast-forward, then rescan when HEAD moved."""
    if project.source != "github":
        raise ApiError(400, "only GitHub clones can be synced", code="not_github")
    try:
        run_id = await portal_state(request).runner.request_sync(
            project, principal=principal
        )
    except RunnerUnavailable as exc:
        raise ApiError(501, str(exc)) from exc
    return {"run_id": run_id}


@projects_router.get("/{slug}/scan-estimate")
def get_scan_estimate(project: BoundProject = Depends(project_db)) -> dict:
    """First-scan cost guard: what describing the waiting commits would cost.

    ``commits`` is an upper bound; ``missing_key`` names the analyze
    provider when it has no key (*Describe now* is then disabled).
    """
    body = _scan_estimate(project.ctx.config)
    body["missing_key"] = _missing_key(project.ctx.config, ("analyze",))
    return body


__all__ = ["portal_router", "projects_router", "public_router"]
