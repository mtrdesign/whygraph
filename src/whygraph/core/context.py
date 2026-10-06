"""Project-context seam: which project the current code path is serving.

A long-running process (the portal) serves many projects from one
interpreter, but the package's three global lookups -
:func:`whygraph.core.get_config`, :func:`whygraph.core._resolve_root` and
:func:`whygraph.db.get_engine` - were written for "one process, one
project, found by walking up from ``cwd``". This module lets a caller
bind a :class:`ProjectContext` to the current execution context with
:func:`use_project`; each of those lookups consults it first, so the code
that reads project data (MCP tools, chat, the serve routes) becomes
project-aware without threading a parameter through every call site.

The binding lives in a :class:`contextvars.ContextVar`, so it is scoped to
the current thread / asyncio task and does **not** leak into a plain
:class:`threading.Thread` or a :class:`concurrent.futures.ThreadPoolExecutor`
worker. Code that fans out must copy the context explicitly
(:func:`contextvars.copy_context`); the scan crawlers already do.

With no context bound, the lookups behave exactly as before (walk up
from ``cwd``) - unless *strict mode* is on (:func:`set_strict`), in which
case they raise :class:`ProjectContextError` instead of guessing. The
portal turns strict mode on for its lifetime so a request path that
forgets to bind a project fails loudly rather than reading the wrong
repository. Strict mode is off by default, so the CLI is unaffected.

Examples
--------
>>> ctx = ProjectContext(slug="demo", root=Path("/repos/demo"),
...                      config=Config.defaults())          # doctest: +SKIP
>>> with use_project(ctx):                                  # doctest: +SKIP
...     get_config() is ctx.config
True
"""

from __future__ import annotations

import contextlib
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .config import Config
from .remote import RemoteProject


class ProjectContextError(RuntimeError):
    """A project-scoped lookup ran with no project bound under strict mode.

    Signals a programming error (a code path that forgot
    :func:`use_project`), not a user error - it is deliberately *not* a
    :class:`whygraph.mcp.errors.WhyGraphError`, so HTTP layers surface it
    as a 500 rather than a 404 / 400.
    """


@dataclass(frozen=True)
class ProjectContext:
    """The project a code path is serving.

    Parameters
    ----------
    slug : str
        Stable project identifier (the portal's URL slug).
    root : Path
        The repository root - what :func:`whygraph.core._resolve_root`
        returns while this context is bound. The default WhyGraph DB
        (``<root>/.whygraph/whygraph.db``) is derived from it unless
        ``config.whygraph_db`` overrides it.
    config : Config
        The project's configuration - what :func:`whygraph.core.get_config`
        returns while this context is bound. ``whygraph_db`` and
        ``codegraph_db`` are read from it, exactly as from a
        ``whygraph.toml``-loaded config.
    remote : RemoteProject or None
        Set for a project *linked* to a WhyGraph platform (M2e): its
        history lives there, so the MCP tool bodies ask it instead of a
        local database and :func:`whygraph.db.get_engine` refuses to open
        one at all. ``None`` (the default) for an ordinary project.
    llm_allowed : bool
        Whether a code path serving this context may spend on the LLM
        implicitly: the lazy description backfill and rationale
        generation on a cache miss. ``True`` (the default) everywhere but
        a portal request by a project *viewer*, whose bound context is a
        ``dataclasses.replace`` copy with ``False`` (M2f-1 plan section
        4.6) - never a mutation of the cached context.
    """

    slug: str
    root: Path
    config: Config
    remote: RemoteProject | None = None
    llm_allowed: bool = True


_current: ContextVar[ProjectContext | None] = ContextVar(
    "whygraph_project", default=None
)

# Process-wide, not per-context: strict mode is a property of the process
# (the portal), not of one request.
_strict: bool = False


@contextlib.contextmanager
def use_project(ctx: ProjectContext) -> Iterator[ProjectContext]:
    """Bind ``ctx`` as the current project for the duration of a block.

    Blocks nest: leaving an inner block restores the outer binding (the
    reset uses the :class:`contextvars.Token` from the matching set, so
    the previous value comes back even if it was ``None``).

    Parameters
    ----------
    ctx : ProjectContext
        The project to serve inside the block.

    Yields
    ------
    ProjectContext
        ``ctx`` itself, for ``with use_project(...) as ctx:`` convenience.

    Notes
    -----
    The binding follows :mod:`contextvars` semantics: it is visible in the
    current thread / task and in anything that runs a copy of the current
    context (``asyncio`` tasks, Starlette's threadpool helpers,
    :class:`whygraph.scan.crawler.Crawler`), but not in a bare thread.
    """
    token = _current.set(ctx)
    try:
        yield ctx
    finally:
        _current.reset(token)


def current_project() -> ProjectContext | None:
    """Return the bound :class:`ProjectContext`, or ``None`` if there is none.

    Never raises, regardless of strict mode; use it to *inspect* the
    binding. The global lookups use :func:`active_project` instead.

    Returns
    -------
    ProjectContext or None
        The context bound by the innermost enclosing :func:`use_project`.
    """
    return _current.get()


def set_strict(flag: bool) -> None:
    """Turn strict mode on or off for the whole process.

    In strict mode a project-scoped lookup with no bound context raises
    :class:`ProjectContextError` instead of falling back to the
    repository around ``cwd``. The portal enables it for its lifetime and
    disables it on shutdown; tests that enable it must reset it.

    Parameters
    ----------
    flag : bool
        ``True`` to fail closed, ``False`` (the default) for today's
        ``cwd`` fallback.
    """
    global _strict
    _strict = flag


def is_strict() -> bool:
    """Return whether strict mode is on (see :func:`set_strict`).

    Returns
    -------
    bool
        The current process-wide strict flag.
    """
    return _strict


def active_project(lookup: str) -> ProjectContext | None:
    """Return the bound context for a global lookup, enforcing strict mode.

    This is the single early branch shared by
    :func:`whygraph.core.get_config`, :func:`whygraph.core._resolve_root`
    and (through them) :func:`whygraph.db.get_engine`.

    Parameters
    ----------
    lookup : str
        Name of the calling lookup, used in the error message.

    Returns
    -------
    ProjectContext or None
        The bound context, or ``None`` when none is bound and strict mode
        is off (the caller then takes its legacy ``cwd`` path).

    Raises
    ------
    ProjectContextError
        If no context is bound and strict mode is on.
    """
    ctx = _current.get()
    if ctx is None and _strict:
        raise ProjectContextError(
            f"{lookup}() called with no project context in strict mode; "
            "wrap the call in whygraph.core.context.use_project(...)"
        )
    return ctx


__all__ = [
    "ProjectContext",
    "ProjectContextError",
    "active_project",
    "current_project",
    "is_strict",
    "set_strict",
    "use_project",
]
