"""Core package: configuration, logging, and shell utilities.

Public API
----------
* :class:`Config`, :class:`ConfigError`, :func:`get_config` — runtime
  configuration loaded from ``whygraph.toml`` at the project root.
* :mod:`whygraph.core.context` - the project-context seam
  (:func:`~whygraph.core.context.use_project`,
  :func:`~whygraph.core.context.set_strict`) that :func:`get_config`
  and the project-root lookup consult before falling back to ``cwd``.
* :func:`configure_logging`, :func:`get_logger`, :class:`LogLevel` —
  Rich-backed logging setup and accessors.
* :class:`Shell`, :class:`ShellError` — configurable subprocess wrapper
  with structured errors and DEBUG-level tracing (invocation, returncode,
  duration, truncated stdout/stderr).
* :class:`ShellCommand` — reusable argv + parser pair that
  :meth:`Shell.run` can execute and return a typed result for. Use the
  constructor for inline one-shot commands, or subclass for commands
  with parameters or stateful parsing.
"""

from __future__ import annotations

from pathlib import Path

from whygraph.core.config import CONFIG_FILENAME, Config, ConfigError
from whygraph.core.context import active_project
from whygraph.core.logger import LogLevel, configure_logging, get_logger
from whygraph.core.shell import Shell, ShellError
from whygraph.core.shell_command import ShellCommand

_config: Config | None = None


def get_config(project_root: Path | None = None) -> Config:
    """Return the package-wide :class:`Config`, loading it lazily.

    The first call resolves the project root (via
    ``git rev-parse --show-toplevel``), looks for ``whygraph.toml`` there,
    and either parses it or falls back to :meth:`Config.defaults`. The
    result is cached for subsequent calls; use :func:`_reset_config` to
    clear the cache in tests.

    If a project is bound via :func:`whygraph.core.context.use_project`,
    its ``config`` is returned instead and the cache is neither read nor
    written.

    Parameters
    ----------
    project_root : Path, optional
        Override for the directory in which to look for ``whygraph.toml``.
        If ``None``, the git repository root is used (or the current
        working directory if not inside a repo). Ignored while a project
        context is bound.

    Returns
    -------
    Config
        The bound project's configuration, else the cached one.

    Raises
    ------
    whygraph.core.context.ProjectContextError
        If strict mode is on and no project context is bound.
    """
    ctx = active_project("get_config")
    if ctx is not None:
        return ctx.config
    global _config
    if _config is None:
        root = project_root or _resolve_root()
        candidate = root / CONFIG_FILENAME
        _config = (
            Config.from_toml(candidate) if candidate.exists() else Config.defaults()
        )
    return _config


def _reset_config() -> None:
    """Clear the cached :class:`Config` so the next call reloads.

    Intended for test isolation; not part of the public API.
    """
    global _config
    _config = None


def _resolve_root() -> Path:
    """Locate the project root by walking up to the nearest ``.git`` marker.

    No subprocess, no dependency on the ``git`` binary — just ``pathlib``.
    ``.exists()`` matches both regular repos (``.git`` directory) and
    worktrees (``.git`` is a file pointing elsewhere).

    If a project is bound via :func:`whygraph.core.context.use_project`,
    its ``root`` is returned without touching the filesystem.

    Returns
    -------
    Path
        The bound project's root, else the directory containing ``.git``
        at or above the current working directory, or ``Path.cwd()`` if
        no repository is detected.

    Raises
    ------
    whygraph.core.context.ProjectContextError
        If strict mode is on and no project context is bound.
    """
    ctx = active_project("_resolve_root")
    if ctx is not None:
        return ctx.root
    start = Path.cwd().resolve()
    for candidate in [start, *start.parents]:
        if (candidate / ".git").exists():
            return candidate
    return Path.cwd()


__all__ = [
    "Config",
    "ConfigError",
    "LogLevel",
    "Shell",
    "ShellCommand",
    "ShellError",
    "configure_logging",
    "get_config",
    "get_logger",
]
