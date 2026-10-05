"""Building a :class:`~whygraph.core.context.ProjectContext` from portal rows.

The context a request (or MCP call) runs under is assembled from the
portal database:

1. Each config layer - the project's org defaults row, then its own row -
   goes through :func:`whygraph.core.config.normalize_v2`, then the two
   are deep-merged with :func:`~whygraph.core.config.merge_v2` (the
   project wins).
2. The DB paths are **forced** to ``<root>/.whygraph/whygraph.db`` and
   ``<root>/.codegraph/codegraph.db``, whatever the rows say (plan rule
   4.2.1 #2): a config pointing at another project's database, or at the
   portal's own, is never opened. A *symlink* at those paths is refused
   where the DB is opened (:mod:`whygraph.portal.paths`).
3. Decrypted secrets are injected **in memory only**: the project's key
   for a provider, else its org's default (never another org's) - except
   that an org-default key is never injected into a project that
   overrides that provider's endpoint (rule 3), and a secret that no
   longer decrypts is skipped (never falling back to a different scope's
   key).
4. :meth:`whygraph.core.config.Config.from_dict` builds the frozen
   ``Config``. Its ``api_key`` / ``scan_token`` fields are excluded from
   ``repr``, so a traceback never prints a live key.
5. A ``platform`` project (one *linked* to a WhyGraph platform, M2e) gets
   a :class:`~whygraph.portal.linked.LinkedProject` as its ``remote``
   instead of any secrets - its history lives on the platform, and the
   presence of a ``remote`` is what makes :func:`whygraph.db.get_engine`
   refuse to open a local database for it.

:class:`ContextCache` keeps one built context per project; a cache miss
does its database and Fernet work on a worker thread
(:meth:`ContextCache.aget`), never on the event loop.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Callable

import anyio.to_thread
from cryptography.fernet import InvalidToken
from sqlalchemy import or_
from sqlmodel import Session, select

from whygraph.core.config import Config, merge_v2, normalize_v2
from whygraph.core.context import ProjectContext

from .config_layers import endpoint_of, load_layer
from .db import data_dir, get_session
from .linked import LinkedProject
from .models import PlatformLink, Project, Secret
from .paths import db_paths
from .platform_client import PlatformHttp
from .secrets import (
    CLAUDE_OAUTH_TOKEN,
    GITHUB_TOKEN,
    LLM_API_KEY,
    LLM_KEY_PROVIDERS,
    decrypt,
)

_log = logging.getLogger(__name__)


class ProjectNotFound(LookupError):
    """No project row exists for the requested id."""


def resolve_root(project: Project) -> Path:
    """Return a project's absolute repository root.

    Parameters
    ----------
    project : Project
        A project row. A relative ``root`` (a GitHub clone, stored as
        ``repos/<org slug>/<slug>``) resolves against the data directory.

    Returns
    -------
    Path
        The absolute root.
    """
    root = Path(project.root)
    return root if root.is_absolute() else data_dir() / root


def build_project_context(
    session: Session, project: Project, *, transport: Any = None
) -> ProjectContext:
    """Assemble a project's :class:`ProjectContext` from the portal DB.

    Parameters
    ----------
    session : Session
        A portal DB session.
    project : Project
        The project row (``project.id`` must be set).
    transport : httpx.BaseTransport, optional
        Replaces the network of a linked project's platform client (tests
        plug a fake platform in); ``None`` in a real run.

    Returns
    -------
    ProjectContext
        ``slug``, the absolute ``root`` and a validated ``Config`` with
        forced DB paths and decrypted secrets injected (see the module
        docstring). A ``platform`` project also carries its ``remote``.

    Raises
    ------
    whygraph.core.config.ConfigError
        If the merged config fails validation.
    ValueError
        If a linked project's stored origins are no longer acceptable (an
        ``http`` platform without the development switch) - loudly, because
        a linked project without a ``remote`` would read a local database.
    """
    root = resolve_root(project)
    project_layer, merged = _merged_layer(session, project, root)
    remote = None
    if project.source == "platform":  # a linked project reads no secrets
        remote = _linked_remote(session, project, transport)
    else:
        _inject_secrets(session, project, project_layer, merged)
    return ProjectContext(
        slug=project.slug,
        root=root,
        config=Config.from_dict(merged, root),
        remote=remote,
    )


def _linked_remote(
    session: Session, project: Project, transport: Any
) -> LinkedProject | None:
    """The platform a ``platform`` project's reads go to (M2e plan section 4.10).

    The connection token is decrypted into the client and never written
    anywhere else. A token that no longer decrypts is left out rather than
    guessed at: the platform then answers ``401 invalid_token`` and the
    link reads as revoked, which is the truth the user has to act on.
    """
    row = session.get(PlatformLink, project.id)
    if row is None:  # a platform project always has one; a bare row is inert
        _log.warning("project %s is linked but has no link row", project.slug)
        return None
    try:
        token: str | None = decrypt(row.token_ciphertext)
    except InvalidToken:
        _log.warning(
            "the connection token of project %s is unreadable; reconnect it",
            project.slug,
        )
        token = None
    return LinkedProject(
        PlatformHttp(
            platform_origin=row.platform_origin,
            api_origin=row.api_origin,
            token=token,
            transport=transport,
        ),
        slug=row.remote_slug,
        org=row.org_slug,
        platform_origin=row.platform_origin,
        status=row.status,
        reason=row.status_reason,
    )


def resolved_layer(session: Session, project: Project) -> dict:
    """The project's merged config v2 dict with forced DB paths and **no** secrets.

    This is what a child ``whygraph scan`` receives as
    ``WHYGRAPH_CONFIG_JSON``; secrets reach it only as environment
    variables (plan section 4.6).

    Parameters
    ----------
    session : Session
        A portal DB session.
    project : Project
        The project row.

    Returns
    -------
    dict
        The global layer deep-merged with the project layer (each
        normalized), with ``whygraph_db`` / ``codegraph_db`` forced to the
        root defaults.
    """
    _, merged = _merged_layer(session, project, resolve_root(project))
    return merged


def _merged_layer(session: Session, project: Project, root: Path) -> tuple[dict, dict]:
    """Return ``(project_layer, merged)`` - normalized, merged, DB paths forced.

    A linked (``platform``) project is built from the ``Config`` defaults and
    the project layer's ``[scan]`` table only (hooks); its org layer is
    never read (M2e plan section 4.9).
    """
    if project.source == "platform":
        raw = load_layer(session, project.id, org_id=project.org_id)
        scan = raw.get("scan")
        org_layer: dict = {}
        project_raw = {"scan": scan} if isinstance(scan, dict) else {}
    else:
        org_layer = load_layer(session, None, org_id=project.org_id)
        project_raw = load_layer(session, project.id, org_id=project.org_id)
    global_layer, _ = normalize_v2(org_layer, root)
    project_layer, _ = normalize_v2(project_raw, root)
    merged = merge_v2(global_layer, project_layer)

    # Rule 4.2.1 #2: DB paths are the root's defaults, never a row's value.
    # They are the paths .paths.check_project_paths examines before a DB
    # is opened (the initialized gate, migrations, the runner).
    whygraph_db, codegraph_db = db_paths(root)
    merged["whygraph_db"] = str(whygraph_db)
    merged["codegraph_db"] = str(codegraph_db)
    return project_layer, merged


def _inject_secrets(
    session: Session, project: Project, project_layer: dict, merged: dict
) -> None:
    """Write decrypted keys and tokens into ``merged`` (in memory).

    Only the project's own org's secrets are read: its org defaults
    (the "global" scope below) and the project's. The Claude subscription
    token, like the GitHub token, is a project secret when the project
    has one, else the org default.
    """
    rows = session.exec(
        select(Secret).where(
            Secret.org_id == project.org_id,
            or_(Secret.project_id.is_(None), Secret.project_id == project.id),  # type: ignore[union-attr]
        )
    ).all()
    by_scope: dict[tuple[bool, str, str | None], Secret] = {
        (row.project_id is None, row.kind, row.provider): row for row in rows
    }

    def value(is_global: bool, kind: str, provider: str | None) -> str | None:
        row = by_scope.get((is_global, kind, provider))
        if row is None:
            return None
        try:
            return decrypt(row.ciphertext)
        except InvalidToken:
            _log.warning(
                "secret %s/%s (%s scope) is unreadable; re-enter it",
                kind,
                provider,
                "global" if is_global else "project",
            )
            return None

    llm = merged.setdefault("llm", {})
    for tag in LLM_KEY_PROVIDERS:
        attr = tag.replace("-", "_")
        if (False, LLM_API_KEY, tag) in by_scope:
            key = value(False, LLM_API_KEY, tag)
        elif endpoint_of(project_layer, attr) is not None:
            key = None  # rule 3: the project overrides the endpoint
        else:
            key = value(True, LLM_API_KEY, tag)
        if key is not None:
            llm.setdefault(attr, {})["api_key"] = key

    if (False, GITHUB_TOKEN, None) in by_scope:
        token = value(False, GITHUB_TOKEN, None)
    else:
        token = value(True, GITHUB_TOKEN, None)
    if token is not None:
        merged.setdefault("scan", {})["token"] = token

    if (False, CLAUDE_OAUTH_TOKEN, None) in by_scope:
        claude = value(False, CLAUDE_OAUTH_TOKEN, None)
    else:
        claude = value(True, CLAUDE_OAUTH_TOKEN, None)
    if claude is not None:
        llm.setdefault("claude_cli", {})["oauth_token"] = claude


class ContextCache:
    """A per-project cache of built contexts.

    Writes to a project's config or secrets (and to the global defaults or
    global secrets, which affect every project) must be followed by
    :meth:`invalidate`; the write helpers in
    :mod:`whygraph.portal.config_layers` and
    :mod:`whygraph.portal.secrets` do not know about the cache.

    Parameters
    ----------
    transport : callable, optional
        Returns the HTTP transport a linked project's platform client
        should use, read at build time (tests swap in a fake platform
        after start-up). ``None`` means the network.
    """

    def __init__(self, transport: Callable[[], Any] | None = None) -> None:
        self._contexts: dict[int, ProjectContext] = {}
        self._generation = 0
        self._lock = threading.Lock()
        self._transport = transport

    def get(self, project_id: int) -> ProjectContext:
        """Return the project's context, building it on a miss (blocking).

        Parameters
        ----------
        project_id : int
            ``projects.id``.

        Returns
        -------
        ProjectContext
            The cached or freshly built context.

        Raises
        ------
        ProjectNotFound
            If there is no such project.
        """
        with self._lock:
            ctx = self._contexts.get(project_id)
            generation = self._generation
        if ctx is not None:
            return ctx
        with get_session() as session:
            project = session.get(Project, project_id)
            if project is None:
                raise ProjectNotFound(project_id)
            ctx = build_project_context(
                session,
                project,
                transport=None if self._transport is None else self._transport(),
            )
        with self._lock:
            # An invalidate() that ran while we built makes this result stale.
            if generation == self._generation:
                self._contexts[project_id] = ctx
        return ctx

    async def aget(self, project_id: int) -> ProjectContext:
        """Async :meth:`get`: a miss runs on a worker thread, not the event loop."""
        with self._lock:
            ctx = self._contexts.get(project_id)
        if ctx is not None:
            return ctx
        return await anyio.to_thread.run_sync(self.get, project_id)

    def invalidate(self, project_id: int | None = None) -> None:
        """Forget one project's context, or every context when ``None``."""
        with self._lock:
            self._generation += 1
            if project_id is None:
                self._contexts.clear()
            else:
                self._contexts.pop(project_id, None)


__all__ = [
    "ContextCache",
    "ProjectNotFound",
    "build_project_context",
    "resolve_root",
    "resolved_layer",
]
